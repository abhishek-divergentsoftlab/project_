import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { rfqs as rfqApi } from "@/api/endpoints";
import { useAuth } from "@/context/useAuth";
import type { ProductDetails, RFQ, RFQCreatePayload, RFQRole } from "@/types";

interface Attribute {
  key: string;
  value: string;
  mustMatch: boolean;
}

/** Suffix used in product_details to flag an attribute for heavier matching. */
const MUST_MATCH_SUFFIX = "__must_match";

/** Product attributes back into editable rows. `name` has its own field. */
function toAttributeRows(details: ProductDetails): Attribute[] {
  // Collect which keys have a __must_match sibling set to true.
  const mustMatchKeys = new Set<string>();
  for (const [key, value] of Object.entries(details)) {
    if (key.endsWith(MUST_MATCH_SUFFIX) && value === true) {
      mustMatchKeys.add(key.slice(0, -MUST_MATCH_SUFFIX.length));
    }
  }

  const rows = Object.entries(details)
    .filter(([key]) => key !== "name" && !key.endsWith(MUST_MATCH_SUFFIX))
    .map(([key, value]) => ({
      key,
      // Nested specs -- {"value": 20000, "unit": "mAh"} -- are shown as JSON
      // rather than as "[object Object]", so an edit does not destroy them.
      value:
        value !== null && typeof value === "object"
          ? JSON.stringify(value)
          : String(value),
      mustMatch: mustMatchKeys.has(key),
    }));
  return rows.length > 0 ? rows : [{ key: "", value: "", mustMatch: false }];
}

/** Turn the typed attribute rows into the JSONB payload. */
function buildProductDetails(name: string, attributes: Attribute[]): ProductDetails {
  const details: ProductDetails = {};
  if (name.trim()) details.name = name.trim();

  for (const { key, value, mustMatch } of attributes) {
    const trimmedKey = key.trim();
    const trimmedValue = value.trim();
    if (!trimmedKey || !trimmedValue) continue;

    // Let obvious booleans and numbers through as real JSON types so that
    // numeric matching has something to work with later.
    const lowered = trimmedValue.toLowerCase();
    if (lowered === "true" || lowered === "yes") details[trimmedKey] = true;
    else if (lowered === "false" || lowered === "no") details[trimmedKey] = false;
    else if (trimmedValue !== "" && !Number.isNaN(Number(trimmedValue))) {
      details[trimmedKey] = Number(trimmedValue);
    } else if (trimmedValue.startsWith("{") || trimmedValue.startsWith("[")) {
      // A structured spec round-tripping through the edit form.
      try {
        details[trimmedKey] = JSON.parse(trimmedValue);
      } catch {
        details[trimmedKey] = trimmedValue;
      }
    } else details[trimmedKey] = trimmedValue;

    // Write the must-match flag as a sibling key if the checkbox is checked.
    if (mustMatch) {
      details[`${trimmedKey}${MUST_MATCH_SUFFIX}`] = true;
    }
  }
  return details;
}

export function RFQNew() {
  const { user } = useAuth();
  const navigate = useNavigate();
  // Present only on /rfqs/:rfqId/edit.
  const { rfqId } = useParams<{ rfqId: string }>();
  const editing = Boolean(rfqId);

  // A "both" account has to choose a side per RFQ; a single-sided account
  // cannot post on the other side, so the field is fixed for them.
  const canChooseRole = user?.role === "both";
  const defaultRole: RFQRole = user?.role === "seller" ? "seller" : "buyer";

  const [form, setForm] = useState({
    role: defaultRole,
    category: "",
    title: "",
    description: "",
    productName: "",
    quantityValue: "",
    quantityUnit: "",
    minOrderValue: "",
    minOrderUnit: "",
    priceAmount: "",
    priceCurrency: "",
    pricePerUnit: "",
    city: "",
    state: "",
    // Left blank: a recognised city fills in the state and country server side,
    // and defaulting to India put every Hamburg listing in the wrong country.
    country: "",
    deadlineDays: "",
    publishNow: true,
  });
  const [attributes, setAttributes] = useState<Attribute[]>([{ key: "", value: "", mustMatch: false }]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(editing);
  const [existing, setExisting] = useState<RFQ | null>(null);

  const prefill = useCallback((rfq: RFQ) => {
    setForm({
      role: rfq.role,
      category: rfq.category,
      title: rfq.title,
      description: rfq.description ?? "",
      productName: typeof rfq.product_details.name === "string" ? rfq.product_details.name : "",
      quantityValue: rfq.quantity ? String(rfq.quantity.value) : "",
      quantityUnit: rfq.quantity?.unit ?? "",
      minOrderValue: rfq.minimum_order ? String(rfq.minimum_order.value) : "",
      minOrderUnit: rfq.minimum_order?.unit ?? "",
      priceAmount: rfq.price_target ? String(rfq.price_target.amount) : "",
      priceCurrency: rfq.price_target?.currency ?? "",
      pricePerUnit: rfq.price_target?.per_unit ?? "",
      city: rfq.location?.city ?? "",
      state: rfq.location?.state ?? "",
      country: rfq.location?.country ?? "",
      // An edit must not silently republish a draft, nor unpublish a live one.
      deadlineDays: "",
      publishNow: rfq.status === "active",
    });
    setAttributes(toAttributeRows(rfq.product_details));
    setExisting(rfq);
  }, []);

  useEffect(() => {
    if (!rfqId) return;
    let cancelled = false;
    setLoading(true);
    rfqApi
      .get(rfqId)
      .then((rfq) => {
        if (!cancelled) prefill(rfq);
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err, "Could not load that RFQ"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [rfqId, prefill]);

  function update<K extends keyof typeof form>(key: K, value: (typeof form)[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function updateAttribute(index: number, patch: Partial<Attribute>) {
    setAttributes((current) =>
      current.map((attribute, i) => (i === index ? { ...attribute, ...patch } : attribute)),
    );
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    const payload: RFQCreatePayload = {
      role: form.role,
      category: form.category,
      title: form.title,
      status: form.publishNow ? "active" : "draft",
      product_details: buildProductDetails(form.productName, attributes),
    };

    // A closed or expired listing keeps its status through an edit -- the
    // checkbox only chooses between draft and active.
    if (editing && existing && !["draft", "active"].includes(existing.status)) {
      payload.status = existing.status;
    }

    // Notes can be cleared, so an empty box is an explicit null when editing
    // and simply omitted when creating.
    const description = form.description.trim();
    if (description) payload.description = description;
    else if (editing) payload.description = null;

    if (form.quantityValue) {
      payload.quantity = { value: Number(form.quantityValue), unit: form.quantityUnit };
    }
    if (form.role === "seller" && form.minOrderValue) {
      payload.minimum_order = { value: Number(form.minOrderValue), unit: form.minOrderUnit };
    }
    if (form.priceAmount) {
      payload.price_target = {
        amount: Number(form.priceAmount),
        currency: form.priceCurrency,
        per_unit: form.pricePerUnit || form.quantityUnit || null,
      };
    }
    if (form.city || form.state || form.country) {
      payload.location = {
        city: form.city || null,
        state: form.state || null,
        country: form.country || null,
      };
    }
    if (form.deadlineDays) {
      payload.deadline = {
        in_days: Number(form.deadlineDays),
        raw: `within ${form.deadlineDays} days`,
      };
    }

    try {
      if (editing && rfqId) {
        // role is absent from RFQUpdate on purpose: flipping a live listing
        // from buy-side to sell-side would invalidate everyone's matches.
        const { role: _role, ...patch } = payload;
        void _role;
        await rfqApi.update(rfqId, patch);
      } else {
        await rfqApi.create(payload);
      }
      navigate("/rfqs", { replace: true });
    } catch (err) {
      setError(errorMessage(err, `Could not ${editing ? "save" : "create"} the RFQ`));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <p className="muted">Loading&hellip;</p>;

  return (
    <section>
      <p className="muted">
        <Link to="/rfqs">&larr; My RFQs</Link>
      </p>

      <h1>{editing ? "Edit RFQ" : "New RFQ"}</h1>
      <p className="muted">
        Structured fields drive matching. Anything product-specific goes in attributes.
        {editing ? " Saving re-indexes the listing." : ""}
      </p>

      <form className="card wide" onSubmit={handleSubmit}>
        {error && <p className="error">{error}</p>}

        <label htmlFor="role">I am</label>
        <select
          id="role"
          value={form.role}
          disabled={!canChooseRole || editing}
          onChange={(e) => update("role", e.target.value as RFQRole)}
        >
          <option value="buyer">Buying</option>
          <option value="seller">Selling</option>
        </select>
        {editing ? (
          <small className="muted">
            A listing cannot change sides &mdash; create a new one instead.
          </small>
        ) : (
          !canChooseRole && (
            <small className="muted">Fixed by your account role ({user?.role}).</small>
          )
        )}

        <label htmlFor="title">Title</label>
        <input
          id="title"
          required
          placeholder="Need 6000 red Type-C cables"
          value={form.title}
          onChange={(e) => update("title", e.target.value)}
        />

        <label htmlFor="category">Category</label>
        <input
          id="category"
          required
          placeholder="Electronics"
          value={form.category}
          onChange={(e) => update("category", e.target.value)}
        />

        <label htmlFor="productName">Product name</label>
        <input
          id="productName"
          placeholder="USB cable"
          value={form.productName}
          onChange={(e) => update("productName", e.target.value)}
        />

        <div className="row">
          <div>
            <label htmlFor="quantityValue">Quantity</label>
            <input
              id="quantityValue"
              type="number"
              min="0"
              step="any"
              value={form.quantityValue}
              onChange={(e) => update("quantityValue", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="quantityUnit">Unit</label>
            <input
              id="quantityUnit"
              placeholder="pcs, kg, tonnes"
              value={form.quantityUnit}
              onChange={(e) => update("quantityUnit", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="priceAmount">Target price</label>
            <input
              id="priceAmount"
              type="number"
              min="0"
              step="any"
              value={form.priceAmount}
              onChange={(e) => update("priceAmount", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="priceCurrency">Currency</label>
            <input
              id="priceCurrency"
              maxLength={3}
              value={form.priceCurrency}
              onChange={(e) => update("priceCurrency", e.target.value.toUpperCase())}
            />
          </div>
        </div>

        {form.role === "seller" && (
          <div className="row">
            <div>
              <label htmlFor="minOrderValue">Minimum Order</label>
              <input
                id="minOrderValue"
                type="number"
                min="0"
                step="any"
                value={form.minOrderValue}
                onChange={(e) => update("minOrderValue", e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="minOrderUnit">Minimum Order Unit</label>
              <input
                id="minOrderUnit"
                placeholder="pcs, kg, tonnes"
                value={form.minOrderUnit}
                onChange={(e) => update("minOrderUnit", e.target.value)}
              />
            </div>
          </div>
        )}

        <div className="row">
          <div>
            <label htmlFor="city">City</label>
            <input
              id="city"
              placeholder="Indore"
              value={form.city}
              onChange={(e) => update("city", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="state">State</label>
            <input id="state" value={form.state} onChange={(e) => update("state", e.target.value)} />
          </div>
          <div>
            <label htmlFor="country">Country</label>
            <input id="country" value={form.country} onChange={(e) => update("country", e.target.value)} />
          </div>
          <div>
            <label htmlFor="deadlineDays">Deadline (days)</label>
            <input
              id="deadlineDays"
              type="number"
              min="0"
              placeholder={
                editing && existing?.deadline?.date
                  ? new Date(existing.deadline.date).toLocaleDateString()
                  : undefined
              }
              value={form.deadlineDays}
              onChange={(e) => update("deadlineDays", e.target.value)}
            />
            {editing && (
              <small className="muted">Leave blank to keep the current deadline.</small>
            )}
          </div>
        </div>

        <small className="muted">
          A recognised city fills in the state, country and coordinates, so matches can be
          ranked by real distance.
        </small>

        <label htmlFor="description">Notes</label>
        <textarea
          id="description"
          rows={3}
          value={form.description}
          onChange={(e) => update("description", e.target.value)}
        />

        <fieldset className="attributes">
          <legend>Product attributes</legend>
          <p className="muted">
            Whatever matters for this product &mdash; ply, colour, material, amperage. Stored as
            JSON, so no two products need the same fields.
          </p>

          {attributes.map((attribute, index) => (
            // Index keys are safe here: rows are only appended and removed,
            // never reordered.
            <div className="attribute-row" key={index}>
              <input
                aria-label={`Attribute ${index + 1} name`}
                placeholder="colour"
                value={attribute.key}
                onChange={(e) => updateAttribute(index, { key: e.target.value })}
              />
              <input
                aria-label={`Attribute ${index + 1} value`}
                placeholder="red"
                value={attribute.value}
                onChange={(e) => updateAttribute(index, { value: e.target.value })}
              />
              <label
                className={`must-match-label${attribute.mustMatch ? " checked" : ""}`}
                title="When checked, matching strongly favours exact or nearest values for this attribute"
              >
                <input
                  type="checkbox"
                  checked={attribute.mustMatch}
                  onChange={(e) => updateAttribute(index, { mustMatch: e.target.checked })}
                />
                Must match
              </label>
              <button
                type="button"
                className="secondary"
                aria-label={`Remove attribute ${index + 1}`}
                onClick={() => setAttributes((current) => current.filter((_, i) => i !== index))}
              >
                &times;
              </button>
            </div>
          ))}

          <button
            type="button"
            className="secondary"
            onClick={() => setAttributes((current) => [...current, { key: "", value: "", mustMatch: false }])}
          >
            Add attribute
          </button>
        </fieldset>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.publishNow}
            onChange={(e) => update("publishNow", e.target.checked)}
          />
          Publish immediately (otherwise saved as a draft)
        </label>

        <button type="submit" disabled={submitting}>
          {submitting
            ? editing
              ? "Saving\u2026"
              : "Creating\u2026"
            : editing
              ? "Save changes"
              : "Create RFQ"}
        </button>
      </form>
    </section>
  );
}

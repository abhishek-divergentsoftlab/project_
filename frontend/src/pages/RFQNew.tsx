import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { moderation as moderationApi, rfqs as rfqApi } from "@/api/endpoints";
import { IconAlert, IconChevronLeft, IconClose, IconPlus } from "@/components/icons";
import { useAuth } from "@/context/useAuth";
import { useFeedback } from "@/context/useFeedback";
import type { ProductDetails, RFQ, RFQCreatePayload, RFQRole } from "@/types";
import { normalizeCurrency, CURRENCY_NAMES } from "@/utils/currency";

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
  const { toast } = useFeedback();
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

  const [moderationWarning, setModerationWarning] = useState<string | null>(null);

  // Debounced real-time safety pre-check
  useEffect(() => {
    if (!form.title.trim() && !form.description.trim()) {
      setModerationWarning(null);
      return;
    }
    const timer = setTimeout(() => {
      void moderationApi
        .check(form.title, form.description, form.category)
        .then((res) => {
          if (!res.is_safe && res.reason) {
            setModerationWarning(res.reason);
          } else {
            setModerationWarning(null);
          }
        })
        .catch(() => { });
    }, 400);
    return () => clearTimeout(timer);
  }, [form.title, form.description, form.category]);

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

    if (moderationWarning) {
      setError(
        "Listing violates safety policies regarding prohibited items. Please remove prohibited items to proceed.",
      );
      return;
    }

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
        currency: normalizeCurrency(form.priceCurrency) || form.priceCurrency || "INR",
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
        toast("Changes saved.");
      } else {
        await rfqApi.create(payload);
        toast(payload.status === "active" ? "RFQ published." : "Draft saved.");
      }
      navigate("/rfqs", { replace: true });
    } catch (err) {
      setError(errorMessage(err, `Could not ${editing ? "save" : "create"} the RFQ`));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <div className="loading-state">
        <span className="spinner" />
        Loading RFQ…
      </div>
    );
  }

  const currencyName = CURRENCY_NAMES[form.priceCurrency];

  return (
    <section className="page page-narrow">
      <header>
        <Link to="/rfqs" className="back-link">
          <IconChevronLeft size={16} />
          My RFQs
        </Link>
        <h1>{editing ? "Edit RFQ" : "New RFQ"}</h1>
        <p className="page-subtitle">
          {editing
            ? "Saving updates the listing and refreshes its matches."
            : "Structured details get you better matches. Only the title and category are required."}
        </p>
      </header>

      <form className="panel form-panel" onSubmit={handleSubmit}>
        {error && <p className="error">{error}</p>}

        <div className="form-section">
          <div className="form-section-head">
            <h2>What you need</h2>
          </div>

          <div className="field">
            <span className="field-label" id="role-label">
              I am
            </span>
            <div className="tabs segmented" role="radiogroup" aria-labelledby="role-label">
              {(["buyer", "seller"] as RFQRole[]).map((value) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={form.role === value}
                  className={form.role === value ? "tab active" : "tab"}
                  disabled={(!canChooseRole || editing) && form.role !== value}
                  onClick={() => update("role", value)}
                >
                  {value === "buyer" ? "Buying" : "Selling"}
                </button>
              ))}
            </div>
            {editing ? (
              <small>A listing can't change sides — create a new one instead.</small>
            ) : (
              !canChooseRole && <small>Set by your account type ({user?.role}).</small>
            )}
          </div>

          <div className="field">
            <label htmlFor="title">Title</label>
            <input
              id="title"
              required
              placeholder="e.g. Need 6,000 red USB-C cables"
              value={form.title}
              onChange={(e) => update("title", e.target.value)}
            />
          </div>

          {moderationWarning && (
            <div className="alert alert-warning" role="alert">
              <IconAlert size={16} />
              <div>
                <strong>This listing may break marketplace rules</strong>
                <p>{moderationWarning}</p>
              </div>
            </div>
          )}

          <div className="field-grid">
            <div className="field">
              <label htmlFor="category">Category</label>
              <input
                id="category"
                required
                placeholder="e.g. Electronics"
                value={form.category}
                onChange={(e) => update("category", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="productName">
                Product name <span className="optional">Optional</span>
              </label>
              <input
                id="productName"
                placeholder="e.g. USB cable"
                value={form.productName}
                onChange={(e) => update("productName", e.target.value)}
              />
            </div>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section-head">
            <h2>Quantity &amp; price</h2>
          </div>

          <div className="field-grid field-grid-4">
            <div className="field">
              <label htmlFor="quantityValue">Quantity</label>
              <input
                id="quantityValue"
                type="number"
                min="0"
                step="any"
                inputMode="decimal"
                value={form.quantityValue}
                onChange={(e) => update("quantityValue", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="quantityUnit">Unit</label>
              <input
                id="quantityUnit"
                placeholder="pcs, kg, tonnes"
                value={form.quantityUnit}
                onChange={(e) => update("quantityUnit", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="priceAmount">Target price</label>
              <input
                id="priceAmount"
                type="number"
                min="0"
                step="any"
                inputMode="decimal"
                placeholder="Per unit"
                value={form.priceAmount}
                onChange={(e) => update("priceAmount", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="priceCurrency">Currency</label>
              <input
                id="priceCurrency"
                placeholder="INR, USD…"
                value={form.priceCurrency}
                aria-describedby={currencyName ? "currency-hint" : undefined}
                onChange={(e) => {
                  const val = e.target.value;
                  const norm = normalizeCurrency(val);
                  if (norm && norm !== val.toUpperCase() && val.length > 2) {
                    update("priceCurrency", norm);
                  } else {
                    update("priceCurrency", val.toUpperCase());
                  }
                }}
                onBlur={() => {
                  if (form.priceCurrency) {
                    update("priceCurrency", normalizeCurrency(form.priceCurrency));
                  }
                }}
              />
              {currencyName && <small id="currency-hint">{currencyName}</small>}
            </div>
          </div>

          {form.role === "seller" && (
            <div className="field-grid field-grid-4">
              <div className="field">
                <label htmlFor="minOrderValue">Minimum order</label>
                <input
                  id="minOrderValue"
                  type="number"
                  min="0"
                  step="any"
                  inputMode="decimal"
                  value={form.minOrderValue}
                  onChange={(e) => update("minOrderValue", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="minOrderUnit">Unit</label>
                <input
                  id="minOrderUnit"
                  placeholder="pcs, kg, tonnes"
                  value={form.minOrderUnit}
                  onChange={(e) => update("minOrderUnit", e.target.value)}
                />
              </div>
            </div>
          )}
        </div>

        <div className="form-section">
          <div className="form-section-head">
            <h2>Delivery</h2>
            <p>A recognised city fills in the state and country, so matches can be ranked by real distance.</p>
          </div>

          <div className="field-grid">
            <div className="field">
              <label htmlFor="city">City</label>
              <input
                id="city"
                placeholder="e.g. Indore"
                value={form.city}
                onChange={(e) => update("city", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="state">State</label>
              <input id="state" value={form.state} onChange={(e) => update("state", e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="country">Country</label>
              <input id="country" value={form.country} onChange={(e) => update("country", e.target.value)} />
            </div>
          </div>

          <div className="field field-narrow">
            <label htmlFor="deadlineDays">Dispatch within (days)</label>
            <input
              id="deadlineDays"
              type="number"
              min="0"
              inputMode="numeric"
              placeholder={
                editing && existing?.deadline?.date
                  ? `Currently ${new Date(existing.deadline.date).toLocaleDateString()}`
                  : undefined
              }
              value={form.deadlineDays}
              onChange={(e) => update("deadlineDays", e.target.value)}
            />
            <small>
              Time to have the goods ready, not including transport.
              {editing ? " Leave blank to keep the current deadline." : ""}
            </small>
          </div>
        </div>

        <div className="form-section">
          <div className="form-section-head">
            <h2>Details</h2>
            <p>Specs that matter for this product — material, colour, grade, amperage.</p>
          </div>

          <div className="field">
            <label htmlFor="description">
              Notes <span className="optional">Optional</span>
            </label>
            <textarea
              id="description"
              rows={3}
              value={form.description}
              onChange={(e) => update("description", e.target.value)}
            />
          </div>

          <fieldset className="attributes">
            <legend>Product attributes</legend>
            <div className="attribute-list">
              {attributes.map((attribute, index) => (
                // Index keys are safe here: rows are only appended and removed,
                // never reordered.
                <div className="attribute-row" key={index}>
                  <input
                    aria-label={`Attribute ${index + 1} name`}
                    placeholder="Name, e.g. colour"
                    value={attribute.key}
                    onChange={(e) => updateAttribute(index, { key: e.target.value })}
                  />
                  <input
                    aria-label={`Attribute ${index + 1} value`}
                    placeholder="Value, e.g. red"
                    value={attribute.value}
                    onChange={(e) => updateAttribute(index, { value: e.target.value })}
                  />
                  <label
                    className={`must-match-label${attribute.mustMatch ? " checked" : ""}`}
                    title="Matching strongly favours exact or nearest values for this attribute"
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
                    className="icon-btn"
                    aria-label={`Remove attribute ${index + 1}`}
                    title="Remove"
                    onClick={() => setAttributes((current) => current.filter((_, i) => i !== index))}
                  >
                    <IconClose size={16} />
                  </button>
                </div>
              ))}
            </div>
            <button
              type="button"
              className="ghost small-btn add-attribute-btn"
              onClick={() => setAttributes((current) => [...current, { key: "", value: "", mustMatch: false }])}
            >
              <IconPlus size={14} />
              Add attribute
            </button>
          </fieldset>
        </div>

        <div className="form-footer">
          <label className="checkbox form-footer-start">
            <input
              type="checkbox"
              checked={form.publishNow}
              onChange={(e) => update("publishNow", e.target.checked)}
            />
            Publish now
            <span className="muted">(otherwise saved as a draft)</span>
          </label>
          <Link to="/rfqs" className="button secondary">
            Cancel
          </Link>
          <button type="submit" disabled={submitting}>
            {submitting
              ? editing
                ? "Saving\u2026"
                : "Creating\u2026"
              : editing
                ? "Save changes"
                : form.publishNow
                  ? "Publish RFQ"
                  : "Save draft"}
          </button>
        </div>
      </form>
    </section>
  );
}

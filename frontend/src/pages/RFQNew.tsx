import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { moderation as moderationApi, rfqs as rfqApi, hsnApi, type HsnSuggestion } from "@/api/endpoints";
import { IconAlert, IconCheck, IconChevronLeft, IconClose, IconMapPin, IconPlus, IconSparkles } from "@/components/icons";
import { HsnTreeSelector, type HsnSelection } from "@/components/HsnTreeSelector";
import { LocationMapPicker } from "@/components/map/lazy";
import { useAuth } from "@/context/useAuth";
import { useFeedback } from "@/context/useFeedback";
import type { ProductDetails, RFQ, RFQCreatePayload, RFQRole, SuggestedAttribute } from "@/types";
import { normalizeCurrency } from "@/utils/currency";
import {
  MAJOR_CURRENCIES,
  findSiUnit,
  calculateOrderEstimate,
} from "@/utils/siUnits";

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
function buildProductDetails(name: string, attributes: Attribute[], hsnCode?: string): ProductDetails {
  const details: ProductDetails = {};
  if (name.trim()) details.name = name.trim();
  if (hsnCode?.trim()) {
    details.hsn_code = hsnCode.trim();
    details.hsn_source = "manual";
  }

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
    hsnCode: "",
    category: "",
    title: "",
    description: "",
    productName: "",
    quantityValue: "",
    quantityUnit: "tonnes",
    minOrderValue: "",
    minOrderUnit: "tonnes",
    priceAmount: "",
    priceCurrency: "INR",
    pricePerUnit: "kg",
    city: "",
    state: "",
    country: "",
    latitude: null as number | null,
    longitude: null as number | null,
    deadlineDays: "",
    publishNow: true,
  });
  const [showMapPicker, setShowMapPicker] = useState(false);
  const [attributes, setAttributes] = useState<Attribute[]>([{ key: "", value: "", mustMatch: false }]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(editing);
  const [existing, setExisting] = useState<RFQ | null>(null);

  const prefill = useCallback((rfq: RFQ) => {
    const rawHsn = rfq.product_details.hsn_code;
    const hsn = typeof rawHsn === "string" ? rawHsn : typeof rawHsn === "number" ? String(rawHsn) : "";
    const qUnit = rfq.quantity?.unit ?? "tonnes";
    setForm({
      role: rfq.role,
      hsnCode: hsn,
      category: rfq.category,
      title: rfq.title,
      description: rfq.description ?? "",
      productName: typeof rfq.product_details.name === "string" ? rfq.product_details.name : "",
      quantityValue: rfq.quantity ? String(rfq.quantity.value) : "",
      quantityUnit: qUnit,
      minOrderValue: rfq.minimum_order ? String(rfq.minimum_order.value) : "",
      minOrderUnit: rfq.minimum_order?.unit ?? qUnit,
      priceAmount: rfq.price_target ? String(rfq.price_target.amount) : "",
      priceCurrency: rfq.price_target?.currency ?? "INR",
      pricePerUnit: rfq.price_target?.per_unit ?? (qUnit === "tonnes" ? "kg" : qUnit),
      city: rfq.location?.city ?? "",
      state: rfq.location?.state ?? "",
      country: rfq.location?.country ?? "",
      latitude: rfq.location?.latitude ?? null,
      longitude: rfq.location?.longitude ?? null,
      // An edit must not silently republish a draft, nor unpublish a live one.
      deadlineDays: "",
      publishNow: rfq.status === "active",
    });
    if (rfq.location?.latitude != null && rfq.location?.longitude != null) {
      setShowMapPicker(true);
    }
    setAttributes(toAttributeRows(rfq.product_details));
    setExisting(rfq);
  }, []);

  function handleHsnSelect(selection: HsnSelection) {
    setForm((current) => ({
      ...current,
      hsnCode: selection.hsnCode,
      category: selection.category,
      // Do not auto-fill or overwrite productName: user can freely type it
    }));
  }

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

  const [aiSuggestion, setAiSuggestion] = useState<HsnSuggestion | null>(null);
  const [suggestionDismissed, setSuggestionDismissed] = useState(false);

  // Real-time AI classification recommendation for typed Product Name (e.g. "Appy Fizz" -> HSN 2202 Beverages)
  useEffect(() => {
    const query = form.productName.trim();
    if (query.length < 2) {
      setAiSuggestion(null);
      return;
    }

    setSuggestionDismissed(false);

    const timer = setTimeout(() => {
      void hsnApi
        .suggest(query)
        .then((sug) => {
          if (sug && sug.code) {
            setAiSuggestion(sug);
          } else {
            setAiSuggestion(null);
          }
        })
        .catch(() => setAiSuggestion(null));
    }, 400);

    return () => clearTimeout(timer);
  }, [form.productName]);

  function handleApplyAiSuggestion(sug: HsnSuggestion) {
    setForm((current) => ({
      ...current,
      hsnCode: sug.code,
      category: sug.category,
    }));
    setSuggestionDismissed(true);
  }

  const orderEstimate = useMemo(() => {
    return calculateOrderEstimate(
      form.quantityValue,
      form.quantityUnit,
      form.priceAmount,
      form.pricePerUnit,
      form.priceCurrency
    );
  }, [form.quantityValue, form.quantityUnit, form.priceAmount, form.pricePerUnit, form.priceCurrency]);

  const [aiSuggestedSpecs, setAiSuggestedSpecs] = useState<SuggestedAttribute[]>([]);
  const [isAiSpecsLoading, setIsAiSpecsLoading] = useState(false);
  const [aiSpecsSource, setAiSpecsSource] = useState<string>("");

  useEffect(() => {
    const trimmed = form.productName.trim();
    if (trimmed.length < 2) {
      setAiSuggestedSpecs([]);
      setAiSpecsSource("");
      return;
    }

    setIsAiSpecsLoading(true);
    const timer = setTimeout(() => {
      rfqApi
        .suggestAttributes({
          product_name: trimmed,
          category: form.category || undefined,
          role: form.role,
        })
        .then((res) => {
          if (res && res.suggested_attributes && res.suggested_attributes.length > 0) {
            setAiSuggestedSpecs(res.suggested_attributes);
            setAiSpecsSource(res.source);
          } else {
            setAiSuggestedSpecs([]);
            setAiSpecsSource("");
          }
        })
        .catch(() => {
          setAiSuggestedSpecs([]);
          setAiSpecsSource("");
        })
        .finally(() => {
          setIsAiSpecsLoading(false);
        });
    }, 450);

    return () => clearTimeout(timer);
  }, [form.productName, form.category, form.role]);

  // Active specs directly and purely from real-time AI generation
  const activeSpecs: SuggestedAttribute[] = aiSuggestedSpecs;

  const handleAddOrFocusSpec = useCallback((spec: SuggestedAttribute, presetValue?: string) => {
    setAttributes((current) => {
      const existingIndex = current.findIndex(
        (attr) =>
          attr.key.trim().toLowerCase() === spec.key.toLowerCase() ||
          attr.key.trim().toLowerCase() === spec.label.toLowerCase()
      );
      if (existingIndex >= 0) {
        if (presetValue) {
          return current.map((attr, idx) =>
            idx === existingIndex ? { ...attr, value: presetValue } : attr
          );
        }
        return current;
      }
      const initialVal = presetValue || "";
      const mustMatch = Boolean(spec.must_match);
      if (current.length === 1 && !current[0].key.trim() && !current[0].value.trim()) {
        return [{ key: spec.label, value: initialVal, mustMatch }];
      }
      return [...current, { key: spec.label, value: initialVal, mustMatch }];
    });
  }, []);

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

    if (!form.productName.trim() && !form.title.trim()) {
      setError("Please specify the Product Name.");
      return;
    }

    if (!form.hsnCode && !form.category) {
      setError("Please select a product classification or HSN code.");
      return;
    }

    setSubmitting(true);

    const finalTitle = form.productName.trim() || form.title.trim() || "Product Listing";

    const payload: RFQCreatePayload = {
      role: form.role,
      category: form.category || "General Goods",
      title: finalTitle,
      status: form.publishNow ? "active" : "draft",
      product_details: buildProductDetails(form.productName.trim(), attributes, form.hsnCode),
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
    if (form.city || form.state || form.country || form.latitude != null) {
      payload.location = {
        city: form.city || null,
        state: form.state || null,
        country: form.country || null,
        latitude: form.latitude != null ? form.latitude : null,
        longitude: form.longitude != null ? form.longitude : null,
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

          {/* Tree-Like HSN Code & Commodity Selector */}
          <div className="field">
            <span className="field-label" id="hsn-tree-label">
              Product Classification &amp; HSN Code
            </span>
            <HsnTreeSelector
              selectedHsnCode={form.hsnCode}
              onSelect={handleHsnSelect}
              role={form.role}
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

          {/* Product Name (Direct User Input) */}
          <div className="field" style={{ marginTop: "1rem" }}>
            <label htmlFor="productName">
              Product Name <span className="required" style={{ color: "var(--accent)" }}>*</span>
            </label>
            <input
              id="productName"
              required
              placeholder="e.g. Milk, Pure Cow Milk, Salted Butter, Curd..."
              value={form.productName}
              onChange={(e) => {
                const val = e.target.value;
                update("productName", val);
                update("title", val);
              }}
            />
            <small>
              Specify what product you want to {form.role === "seller" ? "sell" : "buy"} (e.g. Milk, Butter, Curd).
            </small>
          </div>

          {/* AI Category & Classification Recommendation Card */}
          {aiSuggestion && !suggestionDismissed && form.hsnCode !== aiSuggestion.code && (
            <div className="hsn-ai-suggestion-card">
              <div className="hsn-ai-suggestion-header">
                <div className="hsn-ai-badge">
                  <IconSparkles size={14} className="hsn-sparkle-icon" />
                  <span>AI Classification Recommendation</span>
                </div>
                <button
                  type="button"
                  className="hsn-ai-dismiss"
                  onClick={() => setSuggestionDismissed(true)}
                  title="Dismiss recommendation"
                >
                  <IconClose size={13} />
                </button>
              </div>

              <div className="hsn-ai-suggestion-body">
                <div className="hsn-ai-target">
                  <strong>HSN {aiSuggestion.code}</strong> &bull; {aiSuggestion.formatted_title}
                </div>
                <div className="hsn-ai-meta">
                  Suggested Category: <span className="hsn-cat-pill">{aiSuggestion.category}</span> &bull; Chapter {aiSuggestion.chapter} ({aiSuggestion.chapter_name})
                </div>
                {aiSuggestion.reason && (
                  <p className="hsn-ai-reason">💡 {aiSuggestion.reason}</p>
                )}
              </div>

              <div className="hsn-ai-suggestion-footer">
                <button
                  type="button"
                  className="hsn-apply-ai-btn"
                  onClick={() => handleApplyAiSuggestion(aiSuggestion)}
                >
                  ✓ Apply HSN {aiSuggestion.code} ({aiSuggestion.category})
                </button>
              </div>
            </div>
          )}

          {form.hsnCode && aiSuggestion && form.hsnCode === aiSuggestion.code && (
            <div className="hsn-ai-applied-tag">
              <IconCheck size={14} className="hsn-check-icon" />
              <span>
                Classified under <strong>HSN {aiSuggestion.code}</strong> ({aiSuggestion.formatted_title}) &mdash; {aiSuggestion.chapter_name}
              </span>
            </div>
          )}
        </div>

        {/* Section 1: Quantity Specifications (SI Standardized) */}
        <div className="form-section">
          <div className="form-section-head">
            <h2>1. Quantity &amp; Volume (SI Verified)</h2>
            <p>Specify the required order volume using world-standard, SI-verified units.</p>
          </div>

          <div className="field-grid field-grid-2">
            <div className="field">
              <label htmlFor="quantityValue">
                {form.role === "seller" ? "Total Available Quantity" : "Required Quantity"}
              </label>
              <input
                id="quantityValue"
                type="number"
                min="0"
                step="any"
                inputMode="decimal"
                placeholder="e.g. 10"
                value={form.quantityValue}
                onChange={(e) => update("quantityValue", e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="quantityUnit">Quantity Unit (SI Standard)</label>
              <select
                id="quantityUnit"
                value={form.quantityUnit}
                onChange={(e) => {
                  const newUnit = e.target.value;
                  update("quantityUnit", newUnit);
                  // Intelligently suggest matching price unit if not yet diverged
                  if (!form.pricePerUnit || form.pricePerUnit === form.quantityUnit) {
                    const si = findSiUnit(newUnit);
                    if (si?.category === "mass" && newUnit === "tonnes") {
                      update("pricePerUnit", "kg"); // standard Indian & global trade habit: tons in kg rate
                    } else {
                      update("pricePerUnit", newUnit);
                    }
                  }
                }}
              >
                <optgroup label="Mass / Weight (SI Standard)">
                  <option value="tonnes">Metric Tonne (t / MT - 1,000 kg)</option>
                  <option value="quintal">Quintal (q - 100 kg)</option>
                  <option value="kg">Kilogram (kg - SI Base)</option>
                  <option value="g">Gram (g - 0.001 kg)</option>
                </optgroup>
                <optgroup label="Volume / Liquids (SI Metric)">
                  <option value="litres">Litre (L - SI Metric)</option>
                  <option value="ml">Millilitre (mL - 0.001 L)</option>
                  <option value="kl">Kilolitre (kL - 1,000 L)</option>
                  <option value="m3">Cubic Metre (m³ - 1,000 L)</option>
                </optgroup>
                <optgroup label="Length / Distance (SI)">
                  <option value="metres">Metre (m - SI Base)</option>
                  <option value="km">Kilometre (km - 1,000 m)</option>
                  <option value="cm">Centimetre (cm - 0.01 m)</option>
                  <option value="mm">Millimetre (mm - 0.001 m)</option>
                </optgroup>
                <optgroup label="Count & Commercial Packaging">
                  <option value="pieces">Pieces / Units (pcs)</option>
                  <option value="dozen">Dozen (12 pcs)</option>
                  <option value="boxes">Boxes</option>
                  <option value="cartons">Cartons (ctn)</option>
                  <option value="bags">Bags (e.g. 50 kg sacks)</option>
                  <option value="drums">Drums / Barrels (200 L)</option>
                  <option value="pallets">Pallets</option>
                </optgroup>
              </select>
            </div>
          </div>

          {/* SI Verification Badge for Quantity */}
          {form.quantityUnit && (
            <div className="si-verification-badge">
              <IconCheck size={14} className="si-check-icon" />
              <span>
                <strong>SI Standard:</strong> {findSiUnit(form.quantityUnit)?.siFormula || `${form.quantityUnit} verified`}
              </span>
            </div>
          )}

          {/* Seller Minimum Order Quantity (MOQ) */}
          {form.role === "seller" && (
            <div className="field-grid field-grid-2" style={{ marginTop: "0.75rem" }}>
              <div className="field">
                <label htmlFor="minOrderValue">Minimum Order Quantity (MOQ)</label>
                <input
                  id="minOrderValue"
                  type="number"
                  min="0"
                  step="any"
                  inputMode="decimal"
                  placeholder="e.g. 1"
                  value={form.minOrderValue}
                  onChange={(e) => update("minOrderValue", e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="minOrderUnit">MOQ Unit</label>
                <select
                  id="minOrderUnit"
                  value={form.minOrderUnit || form.quantityUnit}
                  onChange={(e) => update("minOrderUnit", e.target.value)}
                >
                  <optgroup label="Mass / Weight (SI)">
                    <option value="tonnes">Metric Tonne (t / MT)</option>
                    <option value="quintal">Quintal (q)</option>
                    <option value="kg">Kilogram (kg)</option>
                    <option value="g">Gram (g)</option>
                  </optgroup>
                  <optgroup label="Volume / Liquids (SI/Metric)">
                    <option value="litres">Litre (L)</option>
                    <option value="ml">Millilitre (mL)</option>
                    <option value="kl">Kilolitre (kL)</option>
                    <option value="m3">Cubic Metre (m³)</option>
                  </optgroup>
                  <optgroup label="Length / Distance (SI)">
                    <option value="metres">Metre (m)</option>
                    <option value="km">Kilometre (km)</option>
                    <option value="cm">Centimetre (cm)</option>
                    <option value="mm">Millimetre (mm)</option>
                  </optgroup>
                  <optgroup label="Count & Commercial Packaging">
                    <option value="pieces">Pieces (pcs)</option>
                    <option value="dozen">Dozen</option>
                    <option value="boxes">Boxes</option>
                    <option value="cartons">Cartons</option>
                    <option value="bags">Bags</option>
                    <option value="drums">Drums</option>
                    <option value="pallets">Pallets</option>
                  </optgroup>
                </select>
              </div>
            </div>
          )}
        </div>

        {/* Section 2: Pricing & Commercial Terms (Different & Separated) */}
        <div className="form-section">
          <div className="form-section-head">
            <h2>2. Target Price &amp; Commercial Terms</h2>
            <p>Enter the unit price and select from major global trade currencies.</p>
          </div>

          <div className="field-grid field-grid-3">
            <div className="field">
              <label htmlFor="priceAmount">
                {form.role === "seller" ? "Offer Price" : "Target Price"}
              </label>
              <input
                id="priceAmount"
                type="number"
                min="0"
                step="any"
                inputMode="decimal"
                placeholder="e.g. 70"
                value={form.priceAmount}
                onChange={(e) => update("priceAmount", e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="priceCurrency">Trade Currency</label>
              <select
                id="priceCurrency"
                value={form.priceCurrency}
                onChange={(e) => update("priceCurrency", e.target.value)}
              >
                {MAJOR_CURRENCIES.map((curr) => (
                  <option key={curr.code} value={curr.code}>
                    {curr.symbol} — {curr.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="pricePerUnit">Quoted Per Unit</label>
              <select
                id="pricePerUnit"
                value={form.pricePerUnit}
                onChange={(e) => update("pricePerUnit", e.target.value)}
              >
                <optgroup label="Mass / Weight (SI)">
                  <option value="kg">per Kilogram (kg)</option>
                  <option value="tonnes">per Metric Tonne (t / MT)</option>
                  <option value="quintal">per Quintal (q)</option>
                  <option value="g">per Gram (g)</option>
                </optgroup>
                <optgroup label="Volume / Liquids (SI/Metric)">
                  <option value="litres">per Litre (L)</option>
                  <option value="ml">per Millilitre (mL)</option>
                  <option value="kl">per Kilolitre (kL)</option>
                  <option value="m3">per Cubic Metre (m³)</option>
                </optgroup>
                <optgroup label="Length / Distance (SI)">
                  <option value="metres">per Metre (m)</option>
                  <option value="km">per Kilometre (km)</option>
                  <option value="cm">per Centimetre (cm)</option>
                  <option value="mm">per Millimetre (mm)</option>
                </optgroup>
                <optgroup label="Count & Commercial Packaging">
                  <option value="pieces">per Piece / Unit (pcs)</option>
                  <option value="dozen">per Dozen (12 pcs)</option>
                  <option value="boxes">per Box</option>
                  <option value="cartons">per Carton</option>
                  <option value="bags">per Bag (50kg)</option>
                  <option value="drums">per Drum (200L)</option>
                  <option value="pallets">per Pallet</option>
                </optgroup>
              </select>
            </div>
          </div>

          {/* Real-time SI Order Value Calculation Card */}
          {orderEstimate.isValid && (
            <div className="si-calculator-card">
              <div className="si-calc-header">
                <span className="si-calc-title">Estimated Total Order Value</span>
                <span className="si-calc-tag">SI-Verified Standard</span>
              </div>
              <div className="si-calc-body">
                <span className="si-calc-total">{orderEstimate.formattedTotal}</span>
                <span className="si-calc-formula">{orderEstimate.siSummary}</span>
              </div>
              {orderEstimate.conversionFormula && (
                <div className="si-calc-footer">
                  <span>✓ {orderEstimate.conversionFormula}</span>
                </div>
              )}
              {orderEstimate.warning && (
                <div className="si-calc-footer" style={{ color: "var(--warning)" }}>
                  <span>⚠ {orderEstimate.warning}</span>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="form-section">
          <div className="form-section-head">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%", flexWrap: "wrap", gap: "8px" }}>
              <div>
                <h2>Delivery & Sourcing Location</h2>
                <p>Pinpoint your dispatch hub or delivery point on the interactive map for real distance ranking.</p>
              </div>
              <button
                type="button"
                className="secondary small-btn"
                style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                onClick={() => setShowMapPicker(!showMapPicker)}
              >
                <IconMapPin size={14} />
                {showMapPicker ? "Hide Map" : "🗺️ Select on Map"}
              </button>
            </div>
          </div>

          {showMapPicker && (
            <div style={{ marginBottom: "16px" }}>
              <LocationMapPicker
                initialLat={form.latitude}
                initialLng={form.longitude}
                initialCity={form.city}
                initialState={form.state}
                initialCountry={form.country}
                onLocationSelect={(loc) => {
                  setForm((current) => ({
                    ...current,
                    city: loc.city,
                    state: loc.state,
                    country: loc.country,
                    latitude: loc.latitude,
                    longitude: loc.longitude,
                  }));
                }}
                height="280px"
                showPresets={true}
              />
            </div>
          )}

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
            <h2>Details &amp; Specifications</h2>
            <p>Specs that matter for this product — click recommended standard specs to guarantee exact match with trading partners.</p>
          </div>
          <fieldset className="attributes">
            <legend>Product attributes</legend>

            {(isAiSpecsLoading || activeSpecs.length > 0) && (
              <div className="recommended-specs-container">
                <div className="recommended-specs-header">
                  <span className="recommended-specs-title">
                    <IconSparkles size={13} className="sparkle-icon" />
                    <strong>AI Specifications for "{form.productName}":</strong>
                    {isAiSpecsLoading && <span className="ai-loading-pulse">Discovering specs live with AI…</span>}
                    {!isAiSpecsLoading && aiSpecsSource && (
                      <span className="ai-source-badge">{aiSpecsSource === "cache" ? "Fast AI Cache" : "Live AI"}</span>
                    )}
                  </span>
                  <span className="recommended-specs-subtitle">
                    {isAiSpecsLoading
                      ? `Analyzing ${form.productName} characteristics and commercial grading requirements...`
                      : `Key technical specifications discovered live by AI for ${form.productName} (click to add).`}
                  </span>
                </div>
                {activeSpecs.length > 0 && (
                  <div className="recommended-specs-chips" role="group" aria-label="Recommended attributes">
                  {activeSpecs.map((spec) => {
                    const isPresent = attributes.some(
                      (attr) =>
                        attr.key.trim().toLowerCase() === spec.key.toLowerCase() ||
                        attr.key.trim().toLowerCase() === spec.label.toLowerCase()
                    );
                    const titleText = spec.explanation
                      ? `${spec.explanation}${spec.placeholder ? ` (${spec.placeholder})` : ""}`
                      : spec.placeholder || undefined;

                    return (
                      <button
                        key={spec.key}
                        type="button"
                        className={`spec-chip${isPresent ? " is-active" : ""}`}
                        onClick={() => handleAddOrFocusSpec(spec)}
                        title={titleText}
                      >
                        <span className="chip-plus">{isPresent ? "✓" : "+"}</span>
                        <span className="chip-label">{spec.label}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}

            <div className="attribute-list">
              {attributes.map((attribute, index) => {
                const matchingSpec = activeSpecs.find(
                  (s) =>
                    s.key.toLowerCase() === attribute.key.trim().toLowerCase() ||
                    s.label.toLowerCase() === attribute.key.trim().toLowerCase()
                );
                const isNumeric = matchingSpec?.input_type === "numeric";
                const placeholder = matchingSpec?.placeholder || (isNumeric ? "e.g. 6.0" : "Value, e.g. red");
                const hasOptions = matchingSpec?.options && matchingSpec.options.length > 0;

                return (
                  <div className="attribute-row-container" key={index}>
                    <div className="attribute-row">
                      <input
                        aria-label={`Attribute ${index + 1} name`}
                        placeholder="Name, e.g. colour"
                        value={attribute.key}
                        onChange={(e) => updateAttribute(index, { key: e.target.value })}
                      />
                      <input
                        aria-label={`Attribute ${index + 1} value`}
                        placeholder={placeholder}
                        value={attribute.value}
                        inputMode={isNumeric ? "decimal" : undefined}
                        onKeyDown={(e) => {
                          if (!isNumeric) return;
                          // Allow standard navigation and edit controls
                          if (
                            e.key === "Backspace" ||
                            e.key === "Tab" ||
                            e.key === "Enter" ||
                            e.key === "Escape" ||
                            e.key === "Delete" ||
                            e.key === "ArrowLeft" ||
                            e.key === "ArrowRight" ||
                            e.key === "ArrowUp" ||
                            e.key === "ArrowDown" ||
                            e.key === "Home" ||
                            e.key === "End"
                          ) {
                            return;
                          }
                          // Allow Ctrl/Cmd shortcuts
                          if (e.ctrlKey || e.metaKey) {
                            return;
                          }
                          // Allow digits 0-9
                          if (/^[0-9]$/.test(e.key)) {
                            return;
                          }
                          // Allow single decimal dot if not already present
                          if (e.key === "." && !attribute.value.includes(".")) {
                            return;
                          }
                          // Block all other keys (letters, %, symbols, spaces)
                          e.preventDefault();
                        }}
                        onChange={(e) => {
                          let val = e.target.value;
                          if (isNumeric) {
                            val = val.replace(/[^0-9.]/g, "");
                            const parts = val.split(".");
                            if (parts.length > 2) {
                              val = parts[0] + "." + parts.slice(1).join("");
                            }
                          }
                          updateAttribute(index, { value: val });
                        }}
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

                    {/* Quick Preset Value Options from LLM / Expert Spec */}
                    {hasOptions && (
                      <div className="attr-option-pills" role="group" aria-label="Suggested values">
                        <span className="attr-option-pill-label">Suggested:</span>
                        {matchingSpec.options!.map((opt) => {
                          const cleanVal = isNumeric
                            ? (opt.match(/[-+]?\d+(?:\.\d+)?/)?.[0] || opt.replace(/[^0-9.]/g, ""))
                            : opt;
                          return (
                            <button
                              key={opt}
                              type="button"
                              className={`attr-option-pill${attribute.value === cleanVal ? " is-selected" : ""}`}
                              onClick={() => updateAttribute(index, { value: cleanVal })}
                              title={`Choose ${cleanVal}`}
                            >
                              {cleanVal}
                            </button>
                          );
                        })}
                      </div>
                    )}
                    {matchingSpec?.explanation && (
                      <div className="attr-explanation-hint">
                        💡 {matchingSpec.explanation}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <button
              type="button"
              className="ghost small-btn add-attribute-btn"
              onClick={() => setAttributes((current) => [...current, { key: "", value: "", mustMatch: false }])}
            >
              <IconPlus size={14} />
              Add custom attribute
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

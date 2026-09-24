import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { connections as connApi, marketplace as marketApi } from "@/api/endpoints";
import {
  IconClose,
  IconFilter,
  IconMapPin,
  IconSearch,
  IconShield,
  IconSparkles,
} from "@/components/icons";
import { Modal } from "@/components/ui/Modal";
import { useFeedback } from "@/context/useFeedback";
import { formatShortDate, formatDate } from "@/utils/format";
import type {
  CatalogFilterParams,
  CatalogItem,
  CategoryCount,
  RFQRole,
} from "@/types";

const PAGE_SIZE = 18;

const SORT_OPTIONS = [
  { value: "newest", label: "Newest" },
  { value: "price_asc", label: "Price: low to high" },
  { value: "price_desc", label: "Price: high to low" },
  { value: "trust_desc", label: "Most trusted" },
] as const;

const SIDES: { value: RFQRole | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "seller", label: "Selling" },
  { value: "buyer", label: "Buying" },
];

function formatLocation(item: CatalogItem): string {
  return [item.location?.city, item.location?.country].filter(Boolean).join(", ") || "Location not set";
}

function formatSpecValue(value: unknown): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function formatPrice(item: CatalogItem): string {
  if (!item.price_target) return "Negotiable";
  const { amount, currency, per_unit } = item.price_target;
  const sym = currency === "USD" ? "$" : currency === "EUR" ? "€" : currency === "GBP" ? "£" : `${currency} `;
  const formatted = amount.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return `${sym}${formatted}${per_unit ? ` / ${per_unit}` : ""}`;
}

function formatQuantity(item: CatalogItem): string {
  if (!item.quantity) return "Flexible";
  return `${item.quantity.value.toLocaleString()} ${item.quantity.unit}`;
}

function formatMoq(item: CatalogItem): string | null {
  if (!item.minimum_order) return null;
  return `Min. order ${item.minimum_order.value.toLocaleString()} ${item.minimum_order.unit}`;
}

export function Marketplace() {
  const navigate = useNavigate();
  const { toast } = useFeedback();
  const [searchParams, setSearchParams] = useSearchParams();


  // Search & Filters state
  const queryParam = searchParams.get("q") || "";
  const roleParam = (searchParams.get("role") as RFQRole) || undefined;
  const categoryParam = searchParams.get("category") || "";
  const sortParam = (searchParams.get("sort") as "newest" | "price_asc" | "price_desc" | "trust_desc") || "newest";
  const verifiedOnlyParam = searchParams.get("verified") === "true";

  const [searchQuery, setSearchQuery] = useState(queryParam);
  const [activeRole, setActiveRole] = useState<RFQRole | "all">(roleParam || "all");
  const [selectedCategory, setSelectedCategory] = useState<string>(categoryParam);
  const [sortBy, setSortBy] = useState<"newest" | "price_asc" | "price_desc" | "trust_desc">(sortParam);
  const [verifiedOnly, setVerifiedOnly] = useState(verifiedOnlyParam);

  // Price & location filters
  const [minPrice, setMinPrice] = useState<string>("");
  const [maxPrice, setMaxPrice] = useState<string>("");
  const [currency, setCurrency] = useState<string>("");
  const [locationQuery, setLocationQuery] = useState<string>("");

  // Data state
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [categories, setCategories] = useState<CategoryCount[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Quick View Modal
  const [activeModalItem, setActiveModalItem] = useState<CatalogItem | null>(null);

  // Connecting state tracking
  const [connectingId, setConnectingId] = useState<string | null>(null);

  // Sidebar filter toggle for mobile
  const [showFiltersMobile, setShowFiltersMobile] = useState(false);

  // Load categories summary on mount
  useEffect(() => {
    let cancelled = false;
    async function loadCats() {
      try {
        const data = await marketApi.getCategories();
        if (!cancelled) setCategories(data);
      } catch (err) {
        console.error("Failed to load categories", err);
      }
    }
    void loadCats();
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch catalog items
  const loadCatalog = useCallback(
    async (offset = 0, append = false) => {
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);

      const params: CatalogFilterParams = {
        q: searchQuery.trim() || undefined,
        role: activeRole === "all" ? undefined : activeRole,
        category: selectedCategory || undefined,
        city: locationQuery.trim() || undefined,
        min_price: minPrice ? Number(minPrice) : undefined,
        max_price: maxPrice ? Number(maxPrice) : undefined,
        currency: currency || undefined,
        verified_only: verifiedOnly ? true : undefined,
        sort_by: sortBy,
        limit: PAGE_SIZE,
        offset,
      };

      try {
        const res = await marketApi.getCatalog(params);
        if (append) {
          setItems((prev) => [...prev, ...res.items]);
        } else {
          setItems(res.items);
        }
        setTotal(res.total);
      } catch (err) {
        setError(errorMessage(err, "Could not load marketplace catalog"));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [
      searchQuery,
      activeRole,
      selectedCategory,
      locationQuery,
      minPrice,
      maxPrice,
      currency,
      verifiedOnly,
      sortBy,
    ],
  );

  // Trigger search on filter changes with small debounce for text
  useEffect(() => {
    const timer = setTimeout(() => {
      void loadCatalog(0, false);
    }, 250);
    return () => clearTimeout(timer);
  }, [loadCatalog]);

  // Sync state to URL params
  useEffect(() => {
    const nextParams = new URLSearchParams();
    if (searchQuery.trim()) nextParams.set("q", searchQuery.trim());
    if (activeRole !== "all") nextParams.set("role", activeRole);
    if (selectedCategory) nextParams.set("category", selectedCategory);
    if (sortBy !== "newest") nextParams.set("sort", sortBy);
    if (verifiedOnly) nextParams.set("verified", "true");
    setSearchParams(nextParams, { replace: true });
  }, [searchQuery, activeRole, selectedCategory, sortBy, verifiedOnly, setSearchParams]);

  function handleResetFilters() {
    setSearchQuery("");
    setActiveRole("all");
    setSelectedCategory("");
    setSortBy("newest");
    setVerifiedOnly(false);
    setMinPrice("");
    setMaxPrice("");
    setCurrency("");
    setLocationQuery("");
  }

  async function handleConnect(item: CatalogItem) {
    if (item.counterparty.connection_id && item.counterparty.connection_status === "accepted") {
      navigate(`/messages?connection=${encodeURIComponent(item.counterparty.connection_id)}`);
      return;
    }
    if (item.counterparty.connection_status === "pending") {
      navigate(`/messages`);
      return;
    }

    setConnectingId(item.id);
    setError(null);
    try {
      const conn = await connApi.create(item.id);
      // Update local card state in place
      setItems((prev) =>
        prev.map((c) =>
          c.id === item.id
            ? {
                ...c,
                counterparty: {
                  ...c.counterparty,
                  connection_id: conn.id,
                  connection_status: "pending",
                  connection_direction: "sent",
                },
              }
            : c,
        ),
      );
      toast(`Request sent to ${item.counterparty.company_name || "the company"}. You'll see it under Messages.`);
    } catch (err) {
      setError(errorMessage(err, "Could not send connection request"));
    } finally {
      setConnectingId(null);
    }
  }

  function handleAskAI(item: CatalogItem) {
    const roleTerm = item.role === "seller" ? "supplier" : "buyer";
    const prompt = `Analyze this ${roleTerm} listing for "${item.title}" in category "${item.category}". Price is ${formatPrice(item)}, quantity ${formatQuantity(item)}. How does this compare with typical market conditions?`;
    navigate(`/ai-chat?prompt=${encodeURIComponent(prompt)}`);
  }

  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (activeRole !== "all") count++;
    if (selectedCategory) count++;
    if (verifiedOnly) count++;
    if (minPrice || maxPrice) count++;
    if (locationQuery.trim()) count++;
    return count;
  }, [activeRole, selectedCategory, verifiedOnly, minPrice, maxPrice, locationQuery]);

  const priceActive = Boolean(minPrice || maxPrice);

  function connectLabel(item: CatalogItem): string {
    const cp = item.counterparty;
    if (cp.connection_status === "accepted") return "Open deal room";
    if (cp.connection_status === "pending") return "Requested";
    if (connectingId === item.id) return "Sending…";
    return "Connect";
  }

  const filters = (
    <>
      <div className="filter-group">
        <label className="checkbox filter-check">
          <input
            type="checkbox"
            checked={verifiedOnly}
            onChange={(e) => setVerifiedOnly(e.target.checked)}
          />
          Verified businesses only
        </label>
      </div>

      <div className="filter-group">
        <h3 className="filter-group-title">Category</h3>
        <div className="category-filter-list">
          <button
            type="button"
            className={`cat-pill-btn ${!selectedCategory ? "selected" : ""}`}
            onClick={() => setSelectedCategory("")}
          >
            <span className="cat-name">All categories</span>
          </button>
          {categories.map((c) => {
            const count =
              activeRole === "seller"
                ? c.seller_count
                : activeRole === "buyer"
                  ? c.buyer_count
                  : c.total_count;
            if (count === 0 && activeRole !== "all") return null;

            const isSelected = selectedCategory.toLowerCase() === c.category.toLowerCase();
            return (
              <button
                key={c.category}
                type="button"
                aria-pressed={isSelected}
                className={`cat-pill-btn ${isSelected ? "selected" : ""}`}
                onClick={() => setSelectedCategory(isSelected ? "" : c.category)}
              >
                <span className="cat-name">{c.category}</span>
                <span className="cat-count">{count.toLocaleString()}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="filter-group">
        <label className="filter-group-title" htmlFor="filter-city">
          City
        </label>
        <input
          id="filter-city"
          type="text"
          placeholder="e.g. Indore"
          value={locationQuery}
          onChange={(e) => setLocationQuery(e.target.value)}
        />
      </div>

      <div className="filter-group">
        <span className="filter-group-title" id="filter-price">
          Price per unit
        </span>
        <div className="price-inputs-row" role="group" aria-labelledby="filter-price">
          <input
            type="number"
            min="0"
            inputMode="decimal"
            placeholder="Min"
            aria-label="Minimum price"
            value={minPrice}
            onChange={(e) => setMinPrice(e.target.value)}
          />
          <span className="price-dash" aria-hidden="true">
            –
          </span>
          <input
            type="number"
            min="0"
            inputMode="decimal"
            placeholder="Max"
            aria-label="Maximum price"
            value={maxPrice}
            onChange={(e) => setMaxPrice(e.target.value)}
          />
        </div>
        <select
          value={currency}
          onChange={(e) => setCurrency(e.target.value)}
          aria-label="Currency"
        >
          <option value="">Any currency</option>
          <option value="INR">INR (₹)</option>
          <option value="USD">USD ($)</option>
          <option value="EUR">EUR (€)</option>
          <option value="GBP">GBP (£)</option>
          <option value="AED">AED</option>
          <option value="SGD">SGD</option>
        </select>
      </div>
    </>
  );

  return (
    <section className="page marketplace-page">
      <header className="page-header">
        <div>
          <h1>Marketplace</h1>
          <p>Browse live listings from suppliers and buyers, and connect with the ones that fit.</p>
        </div>
      </header>

      <div className="market-toolbar">
        <div className="market-search">
          <IconSearch size={17} className="market-search-icon" />
          <input
            type="search"
            placeholder="Search products, materials or specs"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="Search marketplace"
          />
          {searchQuery && (
            <button
              type="button"
              className="market-search-clear"
              onClick={() => setSearchQuery("")}
              aria-label="Clear search"
            >
              <IconClose size={14} />
            </button>
          )}
        </div>

        <div className="tabs" role="tablist" aria-label="Listing side">
          {SIDES.map((side) => (
            <button
              key={side.value}
              type="button"
              role="tab"
              aria-selected={activeRole === side.value}
              className={activeRole === side.value ? "tab active" : "tab"}
              onClick={() => setActiveRole(side.value)}
              title={
                side.value === "seller"
                  ? "Suppliers offering goods"
                  : side.value === "buyer"
                    ? "Buyers looking for goods"
                    : undefined
              }
            >
              {side.label}
            </button>
          ))}
        </div>

        <select
          id="catalog-sort-select"
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
          className="market-sort-select"
          aria-label="Sort listings"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="secondary mobile-filter-toggle"
          onClick={() => setShowFiltersMobile(true)}
        >
          <IconFilter size={15} />
          Filters
          {activeFilterCount > 0 && <span className="filter-count">{activeFilterCount}</span>}
        </button>
      </div>

      {error && (
        <div className="error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="alert-dismiss"
            onClick={() => setError(null)}
            aria-label="Dismiss"
          >
            <IconClose size={14} />
          </button>
        </div>
      )}

      <div className="market-layout-grid">
        <aside
          className={`market-sidebar-panel ${showFiltersMobile ? "open-mobile" : ""}`}
          aria-label="Filters"
        >
          <div className="sidebar-filter-header">
            <h2>Filters</h2>
            {activeFilterCount > 0 && (
              <button type="button" className="link-button" onClick={handleResetFilters}>
                Clear all
              </button>
            )}
            <button
              type="button"
              className="icon-btn mobile-sidebar-close"
              onClick={() => setShowFiltersMobile(false)}
              aria-label="Close filters"
            >
              <IconClose size={18} />
            </button>
          </div>
          <div className="sidebar-filter-body">{filters}</div>
          <div className="sidebar-filter-footer">
            <button
              type="button"
              className="primary btn-block"
              onClick={() => setShowFiltersMobile(false)}
            >
              Show {total.toLocaleString()} {total === 1 ? "result" : "results"}
            </button>
          </div>
        </aside>

        <div className="market-cards-container">
          <div className="market-controls-bar">
            <span className="result-meta">
              {loading && items.length === 0
                ? "Searching…"
                : `${total.toLocaleString()} ${total === 1 ? "listing" : "listings"}`}
            </span>
            {selectedCategory && (
              <button
                type="button"
                className="active-tag-chip"
                onClick={() => setSelectedCategory("")}
                aria-label={`Remove filter ${selectedCategory}`}
              >
                {selectedCategory}
                <IconClose size={12} />
              </button>
            )}
            {verifiedOnly && (
              <button
                type="button"
                className="active-tag-chip"
                onClick={() => setVerifiedOnly(false)}
                aria-label="Remove verified filter"
              >
                Verified only
                <IconClose size={12} />
              </button>
            )}
            {locationQuery.trim() && (
              <button
                type="button"
                className="active-tag-chip"
                onClick={() => setLocationQuery("")}
                aria-label="Remove city filter"
              >
                {locationQuery.trim()}
                <IconClose size={12} />
              </button>
            )}
            {priceActive && (
              <button
                type="button"
                className="active-tag-chip"
                onClick={() => {
                  setMinPrice("");
                  setMaxPrice("");
                }}
                aria-label="Remove price filter"
              >
                {minPrice || "0"} – {maxPrice || "any"} {currency}
                <IconClose size={12} />
              </button>
            )}
          </div>

          {loading && items.length === 0 ? (
            <div className="market-cards-grid" aria-busy="true">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="market-card-skeleton skeleton" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <div className="panel">
              <div className="empty-state">
                <span className="empty-state-icon">
                  <IconSearch size={18} />
                </span>
                <strong>No listings match</strong>
                <p>Try a broader search term, or clear some filters.</p>
                {(activeFilterCount > 0 || searchQuery) && (
                  <button type="button" className="secondary" onClick={handleResetFilters}>
                    Clear search and filters
                  </button>
                )}
              </div>
            </div>
          ) : (
            <div className={`market-cards-grid ${loading ? "is-refreshing" : ""}`}>
              {items.map((item) => {
                const isSeller = item.role === "seller";
                const cp = item.counterparty;
                const moq = formatMoq(item);
                return (
                  <article key={item.id} className="market-item-card">
                    <div className="card-top-row">
                      <span className={`badge ${isSeller ? "badge-seller" : "badge-buyer"}`}>
                        {isSeller ? "Selling" : "Buying"}
                      </span>
                      <span className="card-category-tag">{item.category}</span>
                    </div>

                    <button
                      type="button"
                      className="card-product-title"
                      onClick={() => setActiveModalItem(item)}
                      title={item.title}
                    >
                      {item.title}
                    </button>
                    {item.description && (
                      <p className="card-description-clamp">{item.description}</p>
                    )}

                    <dl className="card-commercial-box">
                      <div>
                        <dt>{isSeller ? "Price" : "Target price"}</dt>
                        <dd className="box-price-value">{formatPrice(item)}</dd>
                      </div>
                      <div>
                        <dt>Quantity</dt>
                        <dd>{formatQuantity(item)}</dd>
                        {moq && <dd className="box-moq-text">{moq}</dd>}
                      </div>
                    </dl>

                    <div className="card-company">
                      <span className="company-avatar-small" aria-hidden="true">
                        {(cp.company_name || "C").slice(0, 2).toUpperCase()}
                      </span>
                      <div className="company-text-col">
                        <span className="company-name" title={cp.company_name || ""}>
                          {cp.company_name || "Unnamed company"}
                          {cp.kyc_status === "verified" && (
                            <span className="verified-mark" title="Verified business">
                              <IconShield size={13} />
                            </span>
                          )}
                        </span>
                        <span className="company-meta-row">
                          <span title={`Trust score ${cp.trust_score}/100`}>
                            Trust {cp.trust_score}
                          </span>
                          <span>
                            {item.location?.city || item.location?.country || "—"}
                            {item.distance_km !== null && ` · ${Math.round(item.distance_km).toLocaleString()} km`}
                          </span>
                          {item.deadline?.date && (
                            <span>By {formatShortDate(item.deadline.date)}</span>
                          )}
                        </span>
                      </div>
                    </div>

                    <div className="card-actions-footer">
                      <button
                        type="button"
                        className="ghost small-btn"
                        onClick={() => setActiveModalItem(item)}
                      >
                        View details
                      </button>
                      <button
                        type="button"
                        className={`small-btn ${cp.connection_status === "accepted" ? "primary" : "secondary"}`}
                        disabled={connectingId === item.id || cp.connection_status === "pending"}
                        onClick={() => handleConnect(item)}
                      >
                        {connectLabel(item)}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}

          {!loading && items.length < total && (
            <div className="load-more-row">
              <button
                type="button"
                className="secondary"
                disabled={loadingMore}
                onClick={() => loadCatalog(items.length, true)}
              >
                {loadingMore
                  ? "Loading…"
                  : `Show more (${(total - items.length).toLocaleString()} remaining)`}
              </button>
            </div>
          )}
        </div>
      </div>

      {showFiltersMobile && (
        <button
          type="button"
          className="market-filter-scrim"
          aria-label="Close filters"
          onClick={() => setShowFiltersMobile(false)}
        />
      )}

      <Modal
        open={activeModalItem !== null}
        onClose={() => setActiveModalItem(null)}
        size="lg"
        title={activeModalItem?.title ?? ""}
        description={
          activeModalItem && (
            <>
              {activeModalItem.role === "seller" ? "Selling" : "Buying"} · {activeModalItem.category}
            </>
          )
        }
        footer={
          activeModalItem && (
            <>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  const it = activeModalItem;
                  setActiveModalItem(null);
                  handleAskAI(it);
                }}
              >
                <IconSparkles size={15} />
                Ask AI about this
              </button>
              <button
                type="button"
                className="primary"
                disabled={
                  connectingId === activeModalItem.id ||
                  activeModalItem.counterparty.connection_status === "pending"
                }
                onClick={() => {
                  const it = activeModalItem;
                  setActiveModalItem(null);
                  void handleConnect(it);
                }}
              >
                {connectLabel(activeModalItem)}
              </button>
            </>
          )
        }
      >
        {activeModalItem && (
          <div className="listing-detail">
            {activeModalItem.description && (
              <p className="listing-detail-desc">{activeModalItem.description}</p>
            )}

            <dl className="meta-list listing-detail-terms">
              <div>
                <dt>{activeModalItem.role === "seller" ? "Price" : "Target price"}</dt>
                <dd>{formatPrice(activeModalItem)}</dd>
              </div>
              <div>
                <dt>Quantity</dt>
                <dd>{formatQuantity(activeModalItem)}</dd>
              </div>
              {activeModalItem.minimum_order && (
                <div>
                  <dt>Minimum order</dt>
                  <dd>
                    {activeModalItem.minimum_order.value.toLocaleString()}{" "}
                    {activeModalItem.minimum_order.unit}
                  </dd>
                </div>
              )}
              {activeModalItem.deadline?.date && (
                <div>
                  <dt>Needed by</dt>
                  <dd>{formatDate(activeModalItem.deadline.date)}</dd>
                </div>
              )}
            </dl>

            <div className="listing-detail-company">
              <span className="company-avatar-small" aria-hidden="true">
                {(activeModalItem.counterparty.company_name || "C").slice(0, 2).toUpperCase()}
              </span>
              <div className="company-text-col">
                <span className="company-name">
                  {activeModalItem.counterparty.company_name || "Unnamed company"}
                </span>
                <span className="company-meta-row">
                  <span>
                    <IconMapPin size={12} /> {formatLocation(activeModalItem)}
                    {activeModalItem.distance_km !== null &&
                      ` · ${Math.round(activeModalItem.distance_km).toLocaleString()} km away`}
                  </span>
                </span>
              </div>
              <div className="listing-detail-trust">
                <span>Trust {activeModalItem.counterparty.trust_score}/100</span>
                {activeModalItem.counterparty.kyc_status === "verified" ? (
                  <span className="verified-text">
                    <IconShield size={13} /> Verified
                  </span>
                ) : (
                  <span className="muted">Not verified</span>
                )}
              </div>
            </div>

            {Object.keys(activeModalItem.product_details || {}).filter((k) => !k.endsWith("__must_match")).length > 0 && (
              <div className="listing-detail-section">
                <h3>Specifications</h3>
                <dl className="specs-table">
                  {Object.entries(activeModalItem.product_details)
                    .filter(([k]) => !k.endsWith("__must_match"))
                    .map(([key, value]) => (
                      <div key={key} className="specs-table-row">
                        <dt>{key.replace(/_/g, " ")}</dt>
                        <dd>{formatSpecValue(value)}</dd>
                      </div>
                    ))}
                </dl>
              </div>
            )}

            {activeModalItem.counterparty.verified_certs.length > 0 && (
              <div className="listing-detail-section">
                <h3>Verified certifications</h3>
                <div className="tags">
                  {activeModalItem.counterparty.verified_certs.map((c) => (
                    <span key={c} className="tag">
                      {c}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Modal>
    </section>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { rfqs as rfqApi } from "@/api/endpoints";
import type { RFQ, RFQStatus } from "@/types";

const TABS: { label: string; value: RFQStatus | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Active", value: "active" },
  { label: "Draft", value: "draft" },
  { label: "Expired", value: "expired" },
  { label: "Closed", value: "closed" },
];

function formatQuantity(rfq: RFQ) {
  if (!rfq.quantity) return "—";
  return `${rfq.quantity.value.toLocaleString()} ${rfq.quantity.unit}`;
}

function formatPrice(rfq: RFQ) {
  if (!rfq.price_target) return "—";
  const { amount, currency, per_unit } = rfq.price_target;
  return `${amount.toLocaleString()} ${currency}${per_unit ? `/${per_unit}` : ""}`;
}

function formatDeadline(rfq: RFQ) {
  if (!rfq.deadline?.date) return "—";
  return new Date(rfq.deadline.date).toLocaleDateString();
}

// The API caps a page at 100 and defaults to 20. The default silently hid the
// last five listings of every seeded account, with no control to reach them.
const PAGE_SIZE = 24;

export function RFQList() {
  const [items, setItems] = useState<RFQ[]>([]);
  const [total, setTotal] = useState(0);
  const [tab, setTab] = useState<RFQStatus | "all">("all");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await rfqApi.list({
        ...(tab === "all" ? {} : { status: tab }),
        limit: PAGE_SIZE,
        offset: 0,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch (err) {
      setError(errorMessage(err, "Could not load your RFQs"));
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    setLoadingMore(true);
    setError(null);
    try {
      const data = await rfqApi.list({
        ...(tab === "all" ? {} : { status: tab }),
        limit: PAGE_SIZE,
        offset: items.length,
      });
      setItems((current) => [...current, ...data.items]);
      setTotal(data.total);
    } catch (err) {
      setError(errorMessage(err, "Could not load more RFQs"));
    } finally {
      setLoadingMore(false);
    }
  }

  async function act(id: string, action: "publish" | "close") {
    setBusyId(id);
    setError(null);
    try {
      const updated = action === "publish" ? await rfqApi.publish(id) : await rfqApi.close(id);
      // Refetch when a filter is active: the row may no longer belong here.
      if (tab === "all") {
        setItems((current) => current.map((rfq) => (rfq.id === id ? updated : rfq)));
      } else {
        await load();
      }
      // Publishing or closing changes how many pending requests the badge
      // should show, so the counts come back with the refreshed row.
    } catch (err) {
      setError(errorMessage(err, `Could not ${action} the RFQ`));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section>
      <div className="section-head">
        <h1>My RFQs</h1>
        <Link className="button" to="/rfqs/new">
          New RFQ
        </Link>
      </div>

      <div className="tabs">
        {TABS.map((item) => (
          <button
            key={item.value}
            type="button"
            className={tab === item.value ? "tab active" : "tab"}
            onClick={() => setTab(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {error && <p className="error">{error}</p>}
      {loading && <p className="muted">Loading&hellip;</p>}

      {!loading && items.length === 0 && (
        <p className="muted">
          Nothing here yet. <Link to="/rfqs/new">Create your first RFQ</Link>.
        </p>
      )}

      {!loading && items.length > 0 && (
        <p className="muted">
          {total} {total === 1 ? "listing" : "listings"} &middot; showing {items.length}
        </p>
      )}

      <div className="rfq-grid">
        {items.map((rfq) => (
          <article key={rfq.id} className="rfq-card">
            <div className="rfq-card-head">
              <div className="badge-row">
                <span className={`badge badge-${rfq.role}`}>{rfq.role}</span>
                <span className={`badge badge-${rfq.status}`}>{rfq.status}</span>
              </div>
              {rfq.pending_connections > 0 && (
                // --color-error was never defined, so this rendered white text
                // on no background at all.
                <Link to="/messages" className="badge badge-requests">
                  {rfq.pending_connections} pending
                </Link>
              )}
            </div>

            <h2>{rfq.title}</h2>
            <p className="muted">{rfq.category}</p>

            <dl>
              <div>
                <dt>Quantity</dt>
                <dd>{formatQuantity(rfq)}</dd>
              </div>
              <div>
                <dt>Target</dt>
                <dd>{formatPrice(rfq)}</dd>
              </div>
              <div>
                <dt>Location</dt>
                <dd>{rfq.location?.city ?? "—"}</dd>
              </div>
              <div>
                <dt>Deadline</dt>
                <dd>{formatDeadline(rfq)}</dd>
              </div>
            </dl>

            {rfq.search_tags.length > 0 && (
              <div className="tags">
                {rfq.search_tags.slice(0, 6).map((tag) => (
                  <span key={tag} className="tag">
                    {tag}
                  </span>
                ))}
              </div>
            )}

            <div className="rfq-actions">
              <Link className="button secondary" to={`/rfqs/${rfq.id}/edit`}>
                Edit
              </Link>
              {rfq.status === "draft" && (
                <button type="button" disabled={busyId === rfq.id} onClick={() => act(rfq.id, "publish")}>
                  Publish
                </button>
              )}
              {rfq.status === "active" && (
                <button
                  type="button"
                  className="secondary"
                  disabled={busyId === rfq.id}
                  onClick={() => act(rfq.id, "close")}
                >
                  Close
                </button>
              )}
              {/* Only published RFQs are matchable, so a draft links nowhere. */}
              {rfq.status === "draft" ? (
                <button type="button" className="secondary" disabled title="Publish this RFQ first">
                  Find {rfq.role === "buyer" ? "sellers" : "buyers"}
                </button>
              ) : (
                <Link className="button secondary" to={`/rfqs/${rfq.id}/matches`}>
                  Find {rfq.role === "buyer" ? "sellers" : "buyers"}
                </Link>
              )}
            </div>
          </article>
        ))}
      </div>

      {items.length < total && (
        <button type="button" className="secondary" disabled={loadingMore} onClick={loadMore}>
          {loadingMore
            ? "Loading\u2026"
            : `Show ${Math.min(PAGE_SIZE, total - items.length)} more`}
        </button>
      )}
    </section>
  );
}

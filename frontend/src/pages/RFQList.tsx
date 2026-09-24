import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { rfqs as rfqApi } from "@/api/endpoints";
import { IconPlus, IconRFQ } from "@/components/icons";
import { Menu } from "@/components/ui/Menu";
import { useFeedback } from "@/context/useFeedback";
import type { RFQ, RFQStatus } from "@/types";
import { formatDate } from "@/utils/format";

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
  return formatDate(rfq.deadline?.date);
}

const STATUS_LABEL: Record<RFQStatus, string> = {
  active: "Live",
  draft: "Draft",
  expired: "Expired",
  closed: "Closed",
};

// The API caps a page at 100 and defaults to 20. The default silently hid the
// last five listings of every seeded account, with no control to reach them.
const PAGE_SIZE = 24;

export function RFQList() {
  const navigate = useNavigate();
  const { toast, confirm } = useFeedback();
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
    if (action === "close") {
      const ok = await confirm({
        title: "Close this RFQ?",
        message:
          "It stops appearing in matches and the marketplace. Existing conversations stay open.",
        confirmLabel: "Close RFQ",
        tone: "danger",
      });
      if (!ok) return;
    }
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
      toast(action === "publish" ? "RFQ published — it's now visible to matches." : "RFQ closed.");
    } catch (err) {
      setError(errorMessage(err, `Could not ${action} the RFQ`));
    } finally {
      setBusyId(null);
    }
  }

  const counterpart = (rfq: RFQ) => (rfq.role === "buyer" ? "sellers" : "buyers");

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h1>My RFQs</h1>
          <p>Requests you've posted to buy or sell. Open one to see who matches.</p>
        </div>
        <Link className="button" to="/rfqs/new">
          <IconPlus size={16} />
          New RFQ
        </Link>
      </header>

      <div className="list-toolbar">
        <div className="tabs" role="tablist" aria-label="Filter by status">
          {TABS.map((item) => (
            <button
              key={item.value}
              type="button"
              role="tab"
              aria-selected={tab === item.value}
              className={tab === item.value ? "tab active" : "tab"}
              onClick={() => setTab(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        {!loading && items.length > 0 && (
          <span className="result-meta">
            {items.length < total ? `${items.length} of ${total}` : total}{" "}
            {total === 1 ? "listing" : "listings"}
          </span>
        )}
      </div>

      {error && <p className="error">{error}</p>}

      {loading ? (
        <div className="panel">
          <div className="loading-state">
            <span className="spinner" />
            Loading your RFQs…
          </div>
        </div>
      ) : items.length === 0 ? (
        <div className="panel">
          <div className="empty-state">
            <span className="empty-state-icon">
              <IconRFQ size={20} />
            </span>
            <strong>{tab === "all" ? "No RFQs yet" : `No ${TABS.find((t) => t.value === tab)?.label.toLowerCase()} RFQs`}</strong>
            <p>
              {tab === "all"
                ? "Post what you want to buy or sell and we'll find counterparties that fit."
                : "Try another filter, or create a new RFQ."}
            </p>
            {tab === "all" && (
              <Link className="button" to="/rfqs/new">
                Create your first RFQ
              </Link>
            )}
          </div>
        </div>
      ) : (
        <div className="panel rfq-list">
          <div className="rfq-list-head" aria-hidden="true">
            <span>Listing</span>
            <span>Quantity</span>
            <span>Target price</span>
            <span>Deadline</span>
            <span>Status</span>
            <span />
          </div>
          {items.map((rfq) => {
            const busy = busyId === rfq.id;
            const canMatch = rfq.status !== "draft";
            return (
              <article key={rfq.id} className="rfq-row">
                <div className="rfq-row-main">
                  <Link
                    to={canMatch ? `/rfqs/${rfq.id}/matches` : `/rfqs/${rfq.id}/edit`}
                    className="rfq-row-title"
                  >
                    {rfq.title}
                  </Link>
                  <div className="rfq-row-meta">
                    <span className={`badge badge-${rfq.role}`}>
                      {rfq.role === "buyer" ? "Buying" : "Selling"}
                    </span>
                    <span>{rfq.category}</span>
                    {rfq.location?.city && <span>{rfq.location.city}</span>}
                    {rfq.pending_connections > 0 && (
                      <Link to="/messages" className="rfq-row-alert">
                        {rfq.pending_connections} pending{" "}
                        {rfq.pending_connections === 1 ? "request" : "requests"}
                      </Link>
                    )}
                  </div>
                </div>
                <dl className="rfq-row-facts">
                  <div>
                    <dt>Quantity</dt>
                    <dd>{formatQuantity(rfq)}</dd>
                  </div>
                  <div>
                    <dt>Target price</dt>
                    <dd>{formatPrice(rfq)}</dd>
                  </div>
                  <div>
                    <dt>Deadline</dt>
                    <dd>{formatDeadline(rfq)}</dd>
                  </div>
                </dl>
                <div className="rfq-row-status">
                  <span className={`badge badge-${rfq.status}`}>{STATUS_LABEL[rfq.status]}</span>
                </div>
                <div className="rfq-row-actions">
                  {rfq.status === "draft" ? (
                    <button
                      type="button"
                      className="primary small-btn"
                      disabled={busy}
                      onClick={() => act(rfq.id, "publish")}
                    >
                      {busy ? "Publishing…" : "Publish"}
                    </button>
                  ) : (
                    <Link className="button secondary small-btn" to={`/rfqs/${rfq.id}/matches`}>
                      Find {counterpart(rfq)}
                    </Link>
                  )}
                  <Menu
                    label={`More actions for ${rfq.title}`}
                    items={[
                      { label: "Edit", onSelect: () => navigate(`/rfqs/${rfq.id}/edit`) },
                      ...(rfq.status === "active" || rfq.status === "draft"
                        ? [
                            {
                              label: "Close RFQ",
                              tone: "danger" as const,
                              disabled: busy,
                              onSelect: () => void act(rfq.id, "close"),
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              </article>
            );
          })}
        </div>
      )}

      {!loading && items.length < total && (
        <div className="load-more-row">
          <button type="button" className="secondary" disabled={loadingMore} onClick={loadMore}>
            {loadingMore ? "Loading…" : `Show ${Math.min(PAGE_SIZE, total - items.length)} more`}
          </button>
        </div>
      )}
    </section>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { connections, matching, rfqs as rfqApi } from "@/api/endpoints";
import { IconChevronLeft, IconSparkles } from "@/components/icons";
import { MatchCard } from "@/components/MatchCard";
import { useFeedback } from "@/context/useFeedback";
import type { Connection, MatchCandidate, RFQ } from "@/types";

const PAGE_SIZE = 6;

/** Fold a newly created connection back into the card it came from. */
export function applyConnection(
  results: MatchCandidate[],
  connection: Connection,
): MatchCandidate[] {
  return results.map((candidate) =>
    candidate.rfq_id === connection.rfq_id
      ? {
          ...candidate,
          counterparty: {
            ...candidate.counterparty,
            connection_id: connection.id,
            connection_status: connection.status,
          },
        }
      : candidate,
  );
}

export function Matches() {
  const { rfqId } = useParams<{ rfqId: string }>();
  const { toast, confirm } = useFeedback();

  const [rfq, setRfq] = useState<RFQ | null>(null);
  const [results, setResults] = useState<MatchCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [targetRole, setTargetRole] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!rfqId) return;
    setLoading(true);
    setError(null);
    try {
      const [ownRfq, response] = await Promise.all([
        rfqApi.get(rfqId),
        matching.forRfq(rfqId, PAGE_SIZE, 0),
      ]);
      setRfq(ownRfq);
      setResults(response.results);
      setTotal(response.total);
      setTargetRole(response.target_role);
    } catch (err) {
      setError(errorMessage(err, "Could not load matches"));
    } finally {
      setLoading(false);
    }
  }, [rfqId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    if (!rfqId) return;
    setLoadingMore(true);
    setError(null);
    try {
      // Continue from what is already shown rather than re-requesting page one.
      const response = await matching.forRfq(rfqId, PAGE_SIZE, results.length);
      setResults((current) => [...current, ...response.results]);
      setTotal(response.total);
    } catch (err) {
      setError(errorMessage(err, "Could not load more matches"));
    } finally {
      setLoadingMore(false);
    }
  }

  async function handleContact(targetRfqId: string) {
    setError(null);
    try {
      const connection = await connections.create(targetRfqId);
      // Patch the card in place: a blocking alert told the user nothing the
      // card could not show them, and left the button looking unpressed.
      setResults((current) => applyConnection(current, connection));
      toast("Request sent. Their contact details appear here once they accept.");
    } catch (err) {
      setError(errorMessage(err, "Could not send the connection request"));
    }
  }

  async function handleCloseRfq() {
    if (!rfq) return;
    const ok = await confirm({
      title: `Close "${rfq.title}"?`,
      message:
        "It stops appearing in matches and the marketplace. Existing conversations stay open.",
      confirmLabel: "Close RFQ",
      tone: "danger",
    });
    if (!ok) return;

    setClosing(true);
    try {
      const updated = await rfqApi.close(rfq.id);
      setRfq(updated);
      toast("RFQ closed successfully.");
    } catch (err) {
      toast(errorMessage(err, "Could not close the RFQ"), "error");
    } finally {
      setClosing(false);
    }
  }

  const side = targetRole === "seller" ? "sellers" : "buyers";

  return (
    <section className="page">
      <header>
        <Link to="/rfqs" className="back-link">
          <IconChevronLeft size={16} />
          My RFQs
        </Link>
        <div className="page-header">
          <div>
            <h1>Matching {loading ? "counterparties" : side}</h1>
            {rfq && (
              <p>
                For &ldquo;{rfq.title}&rdquo;
                {rfq.quantity ? ` · ${rfq.quantity.value.toLocaleString()} ${rfq.quantity.unit}` : ""}
                {rfq.location?.city ? ` · ${rfq.location.city}` : ""}. Request contact to
                start a conversation — details are shared once they accept.
              </p>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
            {rfq && rfq.status === "closed" && (
              <span className="badge badge-muted">Closed</span>
            )}
            {rfq && (rfq.status === "active" || rfq.status === "draft") && (
              <button
                type="button"
                className="button danger small-btn"
                disabled={closing}
                onClick={handleCloseRfq}
              >
                {closing ? "Closing…" : "Close RFQ"}
              </button>
            )}
            {!loading && results.length > 0 && (
              <span className="result-meta">
                {total} {total === 1 ? "match" : "matches"}, best first
              </span>
            )}
          </div>
        </div>
      </header>

      {error && <p className="error">{error}</p>}

      {loading ? (
        <div className="loading-state">
          <span className="spinner" />
          Finding matches…
        </div>
      ) : results.length === 0 && !error ? (
        <div className="panel">
          <div className="empty-state">
            <span className="empty-state-icon">
              <IconSparkles size={20} />
            </span>
            <strong>No matches yet</strong>
            <p>
              Matching only looks at live RFQs on the other side of the market. We'll keep
              checking as new listings are posted.
            </p>
            <Link to="/marketplace" className="button secondary">
              Browse the marketplace
            </Link>
          </div>
        </div>
      ) : (
        <div className="match-grid">
          {results.map((candidate) => (
            <MatchCard key={candidate.rfq_id} candidate={candidate} onContact={handleContact} />
          ))}
        </div>
      )}

      {!loading && results.length < total && (
        <div className="load-more-row">
          <button type="button" className="secondary" disabled={loadingMore} onClick={loadMore}>
            {loadingMore ? "Loading…" : `Show ${Math.min(PAGE_SIZE, total - results.length)} more`}
          </button>
        </div>
      )}
    </section>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { errorMessage } from "@/api/client";
import { connections, matching, rfqs as rfqApi } from "@/api/endpoints";
import { MatchCard } from "@/components/MatchCard";
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

  const [rfq, setRfq] = useState<RFQ | null>(null);
  const [results, setResults] = useState<MatchCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [targetRole, setTargetRole] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
    setNotice(null);
    try {
      const connection = await connections.create(targetRfqId);
      // Patch the card in place: a blocking alert told the user nothing the
      // card could not show them, and left the button looking unpressed.
      setResults((current) => applyConnection(current, connection));
      setNotice(
        "Request sent. Their contact details appear here once they accept — you will find the thread under Messages.",
      );
    } catch (err) {
      setError(errorMessage(err, "Could not send the connection request"));
    }
  }

  if (loading) return <p className="muted">Finding matches&hellip;</p>;

  return (
    <section>
      <p className="muted">
        <Link to="/rfqs">&larr; My RFQs</Link>
      </p>

      <h1>Matching {targetRole === "seller" ? "sellers" : "buyers"}</h1>
      {rfq && (
        <p className="muted">
          For your {rfq.role} RFQ &ldquo;{rfq.title}&rdquo;
          {rfq.quantity ? ` · ${rfq.quantity.value.toLocaleString()} ${rfq.quantity.unit}` : ""}
          {rfq.location?.city ? ` · ${rfq.location.city}` : ""}
        </p>
      )}

      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      {results.length === 0 && !error ? (
        <p className="muted">
          No counterparties yet. Matching only considers published RFQs on the other side of
          the market.
        </p>
      ) : (
        <p className="muted">
          {total} viable {total === 1 ? "match" : "matches"} &middot; showing {results.length}
        </p>
      )}

      <div className="match-grid">
        {results.map((candidate) => (
          <MatchCard key={candidate.rfq_id} candidate={candidate} onContact={handleContact} />
        ))}
      </div>

      {results.length < total && (
        <button type="button" className="secondary" disabled={loadingMore} onClick={loadMore}>
          {loadingMore ? "Loading…" : `Show ${Math.min(PAGE_SIZE, total - results.length)} more`}
        </button>
      )}
    </section>
  );
}

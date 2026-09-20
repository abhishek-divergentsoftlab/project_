import { isAxiosError } from "axios";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { connections, directSearch } from "@/api/endpoints";
import { MatchCard } from "@/components/MatchCard";
import { applyConnection } from "@/pages/Matches";
import type { MatchCandidate, SearchRequirements } from "@/types";

interface Turn {
  id: number;
  who: "you" | "assistant";
  text: string;
  results?: MatchCandidate[];
  total?: number;
}

const EXAMPLES = [
  "i want usb type c cable of white color in indore within 7 days at price 2 dollar per unit",
  "i need 6000 pcs of red type-c cables in indore under rs 200 per piece",
  "i can supply 10000 corrugated boxes 5 ply from pune",
];

/** Everything understood so far, as chips. Makes refinement obvious. */
function Understood({ requirements }: { requirements: SearchRequirements }) {
  const chips: string[] = [];
  if (requirements.product) chips.push(requirements.product);
  if (requirements.category) chips.push(requirements.category);

  for (const [key, value] of Object.entries(requirements.attributes)) {
    if (value === true) chips.push(key.replace(/_/g, " "));
    else if (value !== false && value !== null) chips.push(`${key}: ${String(value)}`);
  }

  if (requirements.quantity) {
    chips.push(`${requirements.quantity.value.toLocaleString()} ${requirements.quantity.unit}`);
  }
  if (requirements.price) {
    const { amount, currency, per_unit } = requirements.price;
    chips.push(`${amount.toLocaleString()} ${currency}${per_unit ? `/${per_unit}` : ""}`);
  }
  if (requirements.city) chips.push(requirements.city);
  if (requirements.deadline_days !== null) chips.push(`${requirements.deadline_days} days`);

  if (chips.length === 0) return null;

  return (
    <div className="understood">
      <span className="muted">
        Looking for {requirements.role === "buyer" ? "sellers" : "buyers"} &middot;
      </span>
      {chips.map((chip) => (
        <span key={chip} className="tag">
          {chip}
        </span>
      ))}
      {requirements.skipped.map((field) => (
        <span key={field} className="tag skipped">
          {field}: any
        </span>
      ))}
    </div>
  );
}

export function Search() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [requirements, setRequirements] = useState<SearchRequirements | null>(null);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const [blockReason, setBlockReason] = useState<string | null>(null);
  const [blockCategory, setBlockCategory] = useState<string | null>(null);

  const endRef = useRef<HTMLDivElement>(null);
  const resultsEndRef = useRef<HTMLDivElement>(null);
  const nextId = useRef(0);

  // Display the active search results from the latest search turn,
  // replacing previous results when a new search or query update occurs.
  const latestSearchTurn = [...turns].reverse().find(
    (t) => t.results && t.results.length > 0
  );
  const activeResults: MatchCandidate[] = latestSearchTurn?.results || [];
  const hasResults = activeResults.length > 0;

  // Toggle sidebar visibility: hide when results are showing so chat + results
  // get the full viewport width in a 50/50 split.
  useEffect(() => {
    const appEl = document.querySelector(".app");
    if (!appEl) return;
    if (hasResults) {
      appEl.classList.add("sidebar-hidden");
    } else {
      appEl.classList.remove("sidebar-hidden");
    }
    return () => {
      appEl.classList.remove("sidebar-hidden");
    };
  }, [hasResults]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  useEffect(() => {
    resultsEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeResults.length]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending || blocked) return;

    setDraft("");
    setError(null);
    setNotice(null);
    setSending(true);
    setTurns((current) => [...current, { id: nextId.current++, who: "you", text: message }]);

    try {
      let response;
      try {
        response = await directSearch.send(message, conversationId);
      } catch (err) {
        // The conversation the tab was holding is gone -- the API restarted, or
        // it was never ours. Start a fresh one with the same message rather
        // than leaving the composer permanently broken.
        if (!isAxiosError(err) || err.response?.status !== 404 || !conversationId) throw err;
        setConversationId(undefined);
        response = await directSearch.send(message);
        setNotice("That earlier search had expired, so this started a new one.");
      }

      setConversationId(response.conversation_id);
      setRequirements(response.requirements);

      if (response.blocked) {
        setBlocked(true);
        setBlockReason(response.block_reason || "Prohibited items detected.");
        setBlockCategory(response.block_category || null);
      }

      setTurns((current) => [
        ...current,
        {
          id: nextId.current++,
          who: "assistant",
          text: response.reply,
          results: response.results,
          total: response.total,
        },
      ]);
    } catch (err) {
      if (isAxiosError(err) && (err.response?.status === 422 || err.response?.data?.blocked)) {
        const detail = err.response?.data?.detail;
        const msg = typeof detail === "string" ? detail : (detail?.message || errorMessage(err, "Policy violation"));
        setError(msg);
        setBlocked(true);
        setBlockReason(msg);
      } else {
        setError(errorMessage(err, "Could not run that search"));
      }
    } finally {
      setSending(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void send(draft);
  }

  async function handleContact(rfqId: string) {
    setError(null);
    setNotice(null);
    try {
      const connection = await connections.create(rfqId);
      // Every turn that showed this listing updates, so scrolling back does not
      // reveal a stale "Request contact" button for a request already sent.
      setTurns((current) =>
        current.map((turn) =>
          turn.results ? { ...turn, results: applyConnection(turn.results, connection) } : turn,
        ),
      );
      setNotice("Request sent. Their contact details appear here once they accept.");
    } catch (err) {
      setError(errorMessage(err, "Could not send the connection request"));
    }
  }

  function reset() {
    setTurns([]);
    setRequirements(null);
    setConversationId(undefined);
    setError(null);
    setNotice(null);
    setBlocked(false);
    setBlockReason(null);
    setBlockCategory(null);
  }

  // ─── Chat panel (always rendered) ──────────────────────────────────────────
  const chatPanel = (
    <div className="search-chat-panel">
      <div className="section-head">
        <div>
          <h1>Search</h1>
          {!hasResults && (
            <p className="muted">
              Describe what you need in your own words. Nothing is published &mdash; this
              creates no RFQ.
            </p>
          )}
        </div>
        {turns.length > 0 && (
          <button type="button" className="secondary" onClick={reset}>
            New search
          </button>
        )}
      </div>

      {requirements && <Understood requirements={requirements} />}

      <div className="chat">
        {turns.length === 0 && (
          <div className="examples">
            <p className="muted">Try one of these:</p>
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="secondary example"
                onClick={() => void send(example)}
              >
                {example}
              </button>
            ))}
          </div>
        )}

        {turns.map((turn) => {
          const isViolation =
            turn.who === "assistant" &&
            (turn.text.includes("Safety Moderation") || turn.text.includes("prohibited items") || turn.text.startsWith("⚠️"));
          return (
            <div key={turn.id} className={`turn turn-${turn.who}${isViolation ? " turn-violation" : ""}`}>
              <div className={`bubble${isViolation ? " bubble-violation" : ""}`}>{turn.text}</div>
              {/* In split mode, results show in the right panel instead */}
              {!hasResults && turn.results && turn.results.length > 0 && (
                <div className="match-grid">
                  {turn.results.map((candidate) => (
                    <MatchCard
                      key={candidate.rfq_id}
                      candidate={candidate}
                      onContact={handleContact}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {sending && <div className="turn turn-assistant"><div className="bubble muted">Searching…</div></div>}
        <div ref={endRef} />
      </div>

      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      {blocked && (
        <div className="search-blocked-banner">
          <div className="blocked-banner-header">
            <span className="blocked-icon">🛡️</span>
            <div className="blocked-title-group">
              <span className="blocked-title">AI Safety Policy Violation &mdash; Session Locked</span>
              {blockCategory && <span className="blocked-badge">{blockCategory.replace(/_/g, " ")}</span>}
            </div>
          </div>
          <p className="blocked-desc">
            {blockReason || "This search session has been terminated and locked due to prohibited items policy violations. You cannot send further messages in this session."}
          </p>
          <div className="blocked-action-row">
            <button type="button" className="btn-reset-session" onClick={reset}>
              + Start Clean New Search
            </button>
          </div>
        </div>
      )}

      <form className={`composer${blocked ? " composer-blocked" : ""}`} onSubmit={handleSubmit}>
        <input
          aria-label="Your message"
          placeholder={
            blocked
              ? "Session locked due to safety policy violation. Click 'Start Clean New Search' to reset."
              : turns.length === 0
              ? "e.g. white usb type-c cables in Indore within 7 days"
              : "Refine it — e.g. show me black instead, deadline 9 days"
          }
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={sending || blocked}
        />
        <button type="submit" disabled={sending || blocked || !draft.trim()}>
          {blocked ? "Locked" : "Send"}
        </button>
      </form>
    </div>
  );

  // ─── Results panel (only when results exist) ───────────────────────────────
  const resultsPanel = hasResults ? (
    <div className="search-results-panel">
      <div className="search-results-head">
        <h2>Results</h2>
        <span className="muted">{activeResults.length} match{activeResults.length !== 1 ? "es" : ""}</span>
      </div>
      <div className="search-results-scroll">
        <div className="search-results-grid">
          {activeResults.map((candidate) => (
            <MatchCard
              key={candidate.rfq_id}
              candidate={candidate}
              onContact={handleContact}
            />
          ))}
        </div>
        <div ref={resultsEndRef} />
      </div>
    </div>
  ) : null;

  return (
    <section className={`search-page${hasResults ? " search-split" : ""}`}>
      {chatPanel}
      {resultsPanel}
    </section>
  );
}


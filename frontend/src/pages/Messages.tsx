import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { connections as connApi } from "@/api/endpoints";
import type { Connection, ConnectionMessage } from "@/types";

const POLL_MS = 5000;

function describe(connection: Connection): string {
  const who = connection.counterparty.company_name ?? connection.counterparty.contact_name;
  if (who) return who;
  // The list used to print a sliced UUID here, which told nobody anything.
  return connection.direction === "sent" ? "The listing owner" : "A buyer";
}

function statusLabel(connection: Connection): string {
  switch (connection.status) {
    case "accepted":
      return "Connected";
    case "rejected":
      return connection.direction === "sent" ? "Declined" : "You declined";
    default:
      return connection.direction === "sent" ? "Awaiting reply" : "Needs your answer";
  }
}

/** Contact details, shown only once the request has been accepted. */
function ContactStrip({ connection }: { connection: Connection }) {
  const { counterparty } = connection;
  if (connection.status !== "accepted") return null;

  const parts: { label: string; value: string; href?: string }[] = [];
  if (counterparty.contact_name) parts.push({ label: "Contact", value: counterparty.contact_name });
  if (counterparty.email) {
    parts.push({ label: "Email", value: counterparty.email, href: `mailto:${counterparty.email}` });
  }
  if (counterparty.phone) {
    parts.push({
      label: "Phone",
      value: counterparty.phone,
      href: `tel:${counterparty.phone.replace(/\s/g, "")}`,
    });
  }
  if (parts.length === 0) return null;

  return (
    <dl className="contact-rows">
      {parts.map(({ label, value, href }) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{href ? <a href={href}>{value}</a> : value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Messages() {
  const [items, setItems] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await connApi.listAll());
    } catch (err) {
      setError(errorMessage(err, "Could not load your connections"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(id: string, action: "accept" | "reject") {
    setBusyId(id);
    setError(null);
    try {
      const updated =
        action === "accept" ? await connApi.accept(id) : await connApi.reject(id);
      setItems((current) => current.map((c) => (c.id === id ? updated : c)));
    } catch (err) {
      setError(errorMessage(err, `Could not ${action} that request`));
      // The server is the authority on status, so resync rather than guess.
      await load();
    } finally {
      setBusyId(null);
    }
  }

  const active = items.find((c) => c.id === activeId) ?? null;

  if (loading) return <p className="muted">Loading&hellip;</p>;

  return (
    <section className="messages-page">
      <div className="section-head">
        <div>
          <h1>Messages</h1>
          <p className="muted">
            Contact details are shared once a request is accepted &mdash; by both sides.
          </p>
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      {items.length === 0 ? (
        <p className="muted">
          No connection requests yet. Ask for one from any match, or wait for somebody to
          ask about a listing of yours.
        </p>
      ) : (
        <div className={active ? "messages-layout split" : "messages-layout"}>
          <div className="connection-list">
            {items.map((connection) => {
              const answerable =
                connection.direction === "received" && connection.status === "pending";
              return (
                <article
                  key={connection.id}
                  className={
                    connection.id === activeId ? "connection-card selected" : "connection-card"
                  }
                >
                  <button
                    type="button"
                    className="connection-open"
                    onClick={() => setActiveId(connection.id === activeId ? null : connection.id)}
                    aria-expanded={connection.id === activeId}
                  >
                    <span className="connection-title">{describe(connection)}</span>
                    <span className={`badge badge-${connection.status}`}>
                      {statusLabel(connection)}
                    </span>
                  </button>

                  {connection.rfq_title && (
                    <p className="muted connection-subject">
                      {connection.direction === "sent" ? "About their" : "About your"}{" "}
                      {connection.rfq_role ?? ""} listing &ldquo;{connection.rfq_title}&rdquo;
                    </p>
                  )}

                  <ContactStrip connection={connection} />

                  {answerable && (
                    <div className="rfq-actions">
                      <button
                        type="button"
                        disabled={busyId === connection.id}
                        onClick={() => void decide(connection.id, "accept")}
                      >
                        Accept
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busyId === connection.id}
                        onClick={() => void decide(connection.id, "reject")}
                      >
                        Decline
                      </button>
                    </div>
                  )}

                  {connection.status === "pending" && !answerable && (
                    <p className="muted">Waiting for them to accept.</p>
                  )}
                  {connection.status === "rejected" && (
                    <p className="muted">This request was declined, so no messages can be sent.</p>
                  )}
                </article>
              );
            })}
          </div>

          {active && (
            <div className="chat-panel">
              <ChatBox connection={active} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function ChatBox({ connection }: { connection: Connection }) {
  const [messages, setMessages] = useState<ConnectionMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const connectionId = connection.id;
  const open = connection.status === "accepted";
  // `direction` already says which side of this row the signed-in account is
  // on, so the bubbles do not need to reach for the auth context.
  const myId = connection.direction === "sent" ? connection.sender_id : connection.receiver_id;

  const loadMessages = useCallback(async () => {
    try {
      setMessages(await connApi.listMessages(connectionId));
      setError(null);
    } catch (err) {
      // A failing poll used to go to console.error only, so the panel sat
      // silently empty while the caller had no idea anything had gone wrong.
      setError(errorMessage(err, "Could not load this conversation"));
    }
  }, [connectionId]);

  useEffect(() => {
    setLoading(true);
    setMessages([]);
    void loadMessages().finally(() => setLoading(false));
  }, [loadMessages]);

  useEffect(() => {
    // Only poll a conversation that can actually receive anything.
    if (!open) return;
    const timer = setInterval(() => void loadMessages(), POLL_MS);
    return () => clearInterval(timer);
  }, [loadMessages, open]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function handleSend(event: FormEvent) {
    event.preventDefault();
    const content = text.trim();
    if (!content || sending) return;

    setSending(true);
    setError(null);
    try {
      const created = await connApi.sendMessage(connectionId, content);
      setText("");
      setMessages((current) => [...current, created]);
    } catch (err) {
      setError(errorMessage(err, "Could not send that message"));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="chat-box">
      <h2>{describe(connection)}</h2>

      {error && <p className="error">{error}</p>}

      <div className="chat-scroll">
        {loading && <p className="muted">Loading conversation&hellip;</p>}
        {!loading && messages.length === 0 && (
          <p className="muted">
            {open ? "No messages yet. Say hello." : "Messages open once the request is accepted."}
          </p>
        )}
        {messages.map((message) => {
          const mine = message.sender_id === myId;
          return (
            <div key={message.id} className={mine ? "chat-line mine" : "chat-line theirs"}>
              <div className="chat-bubble">
                <p>{message.content}</p>
                <time dateTime={message.created_at}>
                  {new Date(message.created_at).toLocaleString()}
                </time>
              </div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={handleSend}>
        <input
          aria-label="Your message"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={open ? "Type a message…" : "Accept the request first"}
          maxLength={4000}
          disabled={!open || sending}
        />
        <button type="submit" disabled={!open || sending || !text.trim()}>
          {sending ? "Sending…" : "Send"}
        </button>
      </form>
    </div>
  );
}

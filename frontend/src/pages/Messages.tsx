import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { errorMessage, tokenStore } from "@/api/client";
import { connections as connApi, quotations as quoteApi, reviews as reviewsApi } from "@/api/endpoints";
import type { Connection, ConnectionMessage, Incoterm, Quotation, QuotationCreatePayload, Review, ReviewCreatePayload } from "@/types";
import { normalizeCurrency } from "@/utils/currency";

const POLL_FALLBACK_MS = 15000;

function describe(connection: Connection): string {
  const who = connection.counterparty.company_name ?? connection.counterparty.contact_name;
  if (who) return who;
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
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(searchParams.get("connection"));
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await connApi.listAll();
      setItems(data);
      const urlConn = searchParams.get("connection");
      if (urlConn && data.some((c) => c.id === urlConn)) {
        setActiveId(urlConn);
      }
    } catch (err) {
      setError(errorMessage(err, "Could not load your connections"));
    } finally {
      setLoading(false);
    }
  }, [searchParams]);

  useEffect(() => {
    void load();
  }, [load]);

  function handleSelectConnection(id: string | null) {
    setActiveId(id);
    if (id) {
      setSearchParams({ connection: id });
    } else {
      setSearchParams({});
    }
  }

  async function decide(id: string, action: "accept" | "reject") {
    setBusyId(id);
    setError(null);
    try {
      const updated =
        action === "accept" ? await connApi.accept(id) : await connApi.reject(id);
      setItems((current) => current.map((c) => (c.id === id ? updated : c)));
    } catch (err) {
      setError(errorMessage(err, `Could not ${action} that request`));
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
          <h1>Deal Room &amp; Messages</h1>
          <p className="muted">
            Direct real-time negotiations, formal quotations, and confirmed purchase orders.
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
                    onClick={() => handleSelectConnection(connection.id === activeId ? null : connection.id)}
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
              <ChatAndDealBox connection={active} />
            </div>
          )}
        </div>
      )}
    </section>
  );
}

const INCOTERMS_OPTIONS: Incoterm[] = ["EXW", "FOB", "CIF", "CFR", "DDP", "CIP"];

function ChatAndDealBox({ connection }: { connection: Connection }) {
  const [messages, setMessages] = useState<ConnectionMessage[]>([]);
  const [quotes, setQuotes] = useState<Quotation[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showQuoteModal, setShowQuoteModal] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [isCounterpartyTyping, setIsCounterpartyTyping] = useState(false);
  const [showRatingModal, setShowRatingModal] = useState(false);
  const [quoteReviews, setQuoteReviews] = useState<Review[]>([]);
  const [ratingForm, setRatingForm] = useState<ReviewCreatePayload>({
    rating: 5,
    communication_rating: 5,
    delivery_rating: 5,
    quality_rating: 5,
    comment: "",
  });

  // Quote form state
  const [quoteForm, setQuoteForm] = useState<{
    unit_price: string;
    currency: string;
    quantity: string;
    quantity_unit: string;
    lead_time_days: string;
    incoterms: Incoterm;
    payment_terms: string;
    notes: string;
  }>({
    unit_price: "",
    currency: "INR",
    quantity: "",
    quantity_unit: "pcs",
    lead_time_days: "7",
    incoterms: "FOB",
    payment_terms: "Net 30",
    notes: "",
  });

  // Live Camera state
  const [showCameraModal, setShowCameraModal] = useState(false);
  const [cameraStream, setCameraStream] = useState<MediaStream | null>(null);
  const [capturedBlob, setCapturedBlob] = useState<Blob | null>(null);
  const [capturedPreviewUrl, setCapturedPreviewUrl] = useState<string | null>(null);
  const [cameraCaption, setCameraCaption] = useState("");
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [zoomedImage, setZoomedImage] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  const endRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const typingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);


  const connectionId = connection.id;
  const open = connection.status === "accepted";
  const myId = connection.direction === "sent" ? connection.sender_id : connection.receiver_id;

  // Load initial messages and quotes
  const reloadData = useCallback(async () => {
    try {
      const [msgData, quoteData] = await Promise.all([
        connApi.listMessages(connectionId),
        open ? quoteApi.list(connectionId) : Promise.resolve([]),
      ]);
      setMessages(msgData);
      setQuotes(quoteData);

      const activeQuote = quoteData[0];
      if (activeQuote && (activeQuote.status === "received" || activeQuote.status === "delivered" || activeQuote.status === "completed")) {
        const revs = await reviewsApi.listQuoteReviews(connectionId, activeQuote.id);
        setQuoteReviews(revs);
      } else {
        setQuoteReviews([]);
      }
      setError(null);
    } catch (err) {
      setError(errorMessage(err, "Could not load deal data"));
    }
  }, [connectionId, open]);

  useEffect(() => {
    setLoading(true);
    setMessages([]);
    setQuotes([]);
    setQuoteReviews([]);
    void reloadData().finally(() => setLoading(false));
  }, [reloadData]);

  // WebSocket Live Real-Time Integration
  useEffect(() => {
    if (!open) return;

    const token = tokenStore.access();
    if (!token) return;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const wsUrl = `${proto}//${host}/api/v1/ws/connections/${connectionId}?token=${encodeURIComponent(token)}`;

    const socket = new WebSocket(wsUrl);
    wsRef.current = socket;

    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as { type: string; data?: unknown };
        if (payload.type === "chat_message" && payload.data) {
          const msg = payload.data as ConnectionMessage;
          setMessages((current) => {
            if (current.some((m) => m.id === msg.id)) return current;
            return [...current, msg];
          });
        } else if (
          payload.type === "quote_created" ||
          payload.type === "quote_accepted" ||
          payload.type === "quote_rejected" ||
          payload.type === "quote_dispatched" ||
          payload.type === "quote_delivered" ||
          payload.type === "delivery_accepted" ||
          payload.type === "review_submitted"
        ) {
          void reloadData();
          if (payload.type === "quote_created") {
            setNotice("A new formal quotation was issued!");
          } else if (payload.type === "quote_accepted") {
            setNotice("Quotation accepted! Purchase Order confirmed.");
          } else if (payload.type === "quote_dispatched") {
            setNotice("Step 1: Order has been dispatched by seller!");
          } else if (payload.type === "quote_delivered") {
            setNotice("Step 2: Order marked as delivered at destination!");
          } else if (payload.type === "delivery_accepted") {
            setNotice("Step 3: Buyer accepted delivery! Ratings are now unlocked.");
          } else if (payload.type === "review_submitted") {
            setNotice("Counterparty submitted a review!");
          }
        } else if (payload.type === "typing") {
          setIsCounterpartyTyping(true);
          if (typingTimerRef.current) clearTimeout(typingTimerRef.current);
          typingTimerRef.current = setTimeout(() => setIsCounterpartyTyping(false), 2500);
        }
      } catch {
        // Ignored malformed messages
      }
    };

    // Backup polling only if websocket is disconnected
    const fallbackTimer = setInterval(() => {
      if (socket.readyState !== WebSocket.OPEN) {
        void reloadData();
      }
    }, POLL_FALLBACK_MS);

    return () => {
      clearInterval(fallbackTimer);
      if (typingTimerRef.current) clearTimeout(typingTimerRef.current);
      socket.close();
      wsRef.current = null;
    };
  }, [connectionId, open, reloadData]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isCounterpartyTyping]);

  // Notify counterparty of typing
  function handleTextChange(val: string) {
    setText(val);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: "typing" }));
      } catch {
        // Non-blocking
      }
    }
  }

  async function handleSend(event: FormEvent) {
    event.preventDefault();
    const content = text.trim();
    if (!content || sending) return;

    setSending(true);
    setError(null);
    try {
      const created = await connApi.sendMessage(connectionId, content);
      setText("");
      setMessages((current) => {
        if (current.some((m) => m.id === created.id)) return current;
        return [...current, created];
      });
    } catch (err) {
      setError(errorMessage(err, "Could not send that message"));
    } finally {
      setSending(false);
    }
  }

  // Handle Quotation Actions
  async function handleAcceptQuote(quoteId: string) {
    setError(null);
    setNotice(null);
    try {
      const updated = await quoteApi.accept(connectionId, quoteId);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice(`Quotation accepted! Purchase Order generated: ${updated.purchase_order_reference}`);
    } catch (err) {
      setError(errorMessage(err, "Could not accept quotation"));
    }
  }

  async function handleRejectQuote(quoteId: string) {
    const reason = window.prompt("Reason for declining quotation (optional):") ?? undefined;
    setError(null);
    setNotice(null);
    try {
      const updated = await quoteApi.reject(connectionId, quoteId, reason);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice("Quotation was declined.");
    } catch (err) {
      setError(errorMessage(err, "Could not decline quotation"));
    }
  }

  async function handleDispatch(quoteId: string) {
    setError(null);
    setNotice(null);
    try {
      const updated = await reviewsApi.dispatchQuote(connectionId, quoteId);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice("Step 1 Complete: Order marked as Dispatched! In transit to buyer.");
    } catch (err) {
      setError(errorMessage(err, "Could not mark order as dispatched"));
    }
  }

  async function handleMarkDelivered(quoteId: string) {
    setError(null);
    setNotice(null);
    try {
      const updated = await reviewsApi.markDelivered(connectionId, quoteId);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice("Step 2 Complete: Order marked as Delivered at destination! Awaiting buyer acceptance.");
    } catch (err) {
      setError(errorMessage(err, "Could not mark order as delivered"));
    }
  }

  async function handleAcceptDelivery(quoteId: string) {
    setError(null);
    setNotice(null);
    try {
      const updated = await reviewsApi.acceptDelivery(connectionId, quoteId);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice("Step 3 Complete: Order delivery accepted & confirmed! Ratings are now unlocked.");
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Could not accept order delivery"));
    }
  }

  // Live Camera handlers
  async function openLiveCamera() {
    setCameraError(null);
    setCapturedBlob(null);
    setCapturedPreviewUrl(null);
    setCameraCaption("");
    setShowCameraModal(true);

    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("Camera API is not supported in this browser environment.");
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          facingMode: "environment",
        },
        audio: false,
      });
      setCameraStream(stream);
    } catch (err) {
      setCameraError(
        "Camera access denied or unavailable. To protect against fraud, pre-saved gallery uploads are disabled and only live camera snapshots are accepted. Please allow camera permissions."
      );
    }
  }

  function handleCloseCamera() {
    if (cameraStream) {
      cameraStream.getTracks().forEach((track) => track.stop());
      setCameraStream(null);
    }
    if (capturedPreviewUrl) {
      URL.revokeObjectURL(capturedPreviewUrl);
      setCapturedPreviewUrl(null);
    }
    setCapturedBlob(null);
    setShowCameraModal(false);
    setCameraError(null);
  }

  useEffect(() => {
    if (cameraStream && videoRef.current) {
      videoRef.current.srcObject = cameraStream;
      videoRef.current.play().catch(() => {});
    }
  }, [cameraStream]);

  useEffect(() => {
    return () => {
      if (cameraStream) {
        cameraStream.getTracks().forEach((track) => track.stop());
      }
    };
  }, [cameraStream]);

  function handleCaptureSnapshot() {
    const video = videoRef.current;
    if (!video) return;

    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 720;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // 1. Draw live camera frame
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    // 2. Add authenticated verification watermark footer
    const barHeight = Math.max(38, Math.floor(canvas.height * 0.065));
    ctx.fillStyle = "rgba(15, 23, 42, 0.88)";
    ctx.fillRect(0, canvas.height - barHeight, canvas.width, barHeight);

    ctx.fillStyle = "#10b981";
    ctx.font = `bold ${Math.max(14, Math.floor(barHeight * 0.42))}px sans-serif`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("● VERIFIED LIVE WEBCAM CAPTURE", 16, canvas.height - barHeight / 2);

    ctx.fillStyle = "#e2e8f0";
    ctx.font = `${Math.max(12, Math.floor(barHeight * 0.36))}px sans-serif`;
    ctx.textAlign = "right";
    const timestamp = new Date().toLocaleString();
    ctx.fillText(timestamp, canvas.width - 16, canvas.height - barHeight / 2);

    // 3. Convert to blob and preview URL
    canvas.toBlob(
      (blob) => {
        if (blob) {
          setCapturedBlob(blob);
          setCapturedPreviewUrl(URL.createObjectURL(blob));
          if (cameraStream) {
            cameraStream.getTracks().forEach((t) => t.stop());
            setCameraStream(null);
          }
        }
      },
      "image/jpeg",
      0.9
    );
  }

  function handleRetakePhoto() {
    if (capturedPreviewUrl) {
      URL.revokeObjectURL(capturedPreviewUrl);
      setCapturedPreviewUrl(null);
    }
    setCapturedBlob(null);
    void openLiveCamera();
  }

  async function handleSendLivePhoto() {
    if (!capturedBlob) return;
    setSending(true);
    try {
      const newMsg = await connApi.sendLiveCapture(connectionId, capturedBlob, cameraCaption);
      setMessages((prev) => {
        if (prev.some((m) => m.id === newMsg.id)) return prev;
        return [...prev, newMsg];
      });
      handleCloseCamera();
      setNotice("Live camera photo sent to counterparty!");
    } catch (err) {
      setCameraError(errorMessage(err, "Failed to upload live snapshot"));
    } finally {
      setSending(false);
    }
  }

  async function handleSubmitRating(e: FormEvent, quoteId: string) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await reviewsApi.rate(connectionId, quoteId, ratingForm);
      setNotice("Thank you! Your rating and feedback have been submitted.");
      setShowRatingModal(false);
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Could not submit rating"));
    }
  }

  function openNewQuoteModal(prefill?: Quotation) {
    if (prefill) {
      setQuoteForm({
        unit_price: String(prefill.unit_price),
        currency: prefill.currency,
        quantity: String(prefill.quantity),
        quantity_unit: prefill.quantity_unit,
        lead_time_days: prefill.lead_time_days ? String(prefill.lead_time_days) : "7",
        incoterms: prefill.incoterms || "FOB",
        payment_terms: prefill.payment_terms || "Net 30",
        notes: `Counter-offer to ${prefill.quote_number}`,
      });
    }
    setShowQuoteModal(true);
  }

  async function handleSubmitQuote(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    const price = parseFloat(quoteForm.unit_price);
    const qty = parseFloat(quoteForm.quantity);
    if (isNaN(price) || price <= 0 || isNaN(qty) || qty <= 0) {
      setError("Please enter valid price and quantity");
      return;
    }

    const payload: QuotationCreatePayload = {
      unit_price: price,
      currency: normalizeCurrency(quoteForm.currency) || quoteForm.currency || "INR",
      quantity: qty,
      quantity_unit: quoteForm.quantity_unit,
      lead_time_days: quoteForm.lead_time_days ? parseInt(quoteForm.lead_time_days, 10) : undefined,
      incoterms: quoteForm.incoterms,
      payment_terms: quoteForm.payment_terms,
      notes: quoteForm.notes || undefined,
    };

    try {
      const created = await quoteApi.create(connectionId, payload);
      setQuotes((current) => [created, ...current.filter((q) => q.status !== "pending")]);
      setShowQuoteModal(false);
      setNotice(`Quotation ${created.quote_number} issued successfully.`);
    } catch (err) {
      setError(errorMessage(err, "Could not submit quotation"));
    }
  }

  const latestQuote = quotes[0] ?? null;
  const isBuyer =
    connection.rfq_role === "buyer"
      ? connection.direction === "received"
      : connection.rfq_role === "seller"
      ? connection.direction === "sent"
      : latestQuote ? !latestQuote.is_sender : true;

  const myReview = quoteReviews.find((r) => r.reviewer_id === myId);
  const counterpartyReview = quoteReviews.find((r) => r.reviewee_id === myId);
  const buyerHasReviewed = isBuyer ? Boolean(myReview) : Boolean(counterpartyReview);
  const sellerHasReviewed = isBuyer ? Boolean(counterpartyReview) : Boolean(myReview);

  return (
    <div className="chat-box deal-room-container">
      <div className="deal-room-header">
        <div>
          <h2>{describe(connection)}</h2>
          <span className="live-indicator">
            <span className="live-dot" /> Real-time Deal Room
          </span>
        </div>
        {open && (
          <button
            type="button"
            className="secondary"
            onClick={() => openNewQuoteModal()}
          >
            + Issue Quote / Offer
          </button>
        )}
      </div>

      {/* Active Deal / Quotation Banner */}
      {open && latestQuote && (
        <div className={`active-quote-card status-${latestQuote.status}`}>
          <div className="quote-badge-row">
            <span className="quote-number">{latestQuote.quote_number}</span>
            <span className={`badge badge-${latestQuote.status}`}>
              {latestQuote.status.toUpperCase()}
            </span>
            {latestQuote.status === "accepted" && latestQuote.purchase_order_reference && (
              <span className="badge badge-po">
                Confirmed PO: {latestQuote.purchase_order_reference}
              </span>
            )}
          </div>

          <div className="quote-details-grid">
            <div>
              <span className="quote-label">Unit Price</span>
              <strong>
                {latestQuote.currency} {latestQuote.unit_price.toLocaleString()} /{latestQuote.quantity_unit}
              </strong>
            </div>
            <div>
              <span className="quote-label">Volume</span>
              <strong>
                {latestQuote.quantity.toLocaleString()} {latestQuote.quantity_unit}
              </strong>
            </div>
            <div>
              <span className="quote-label">Total Value</span>
              <strong className="quote-total">
                {latestQuote.currency} {latestQuote.total_amount.toLocaleString()}
              </strong>
            </div>
            <div>
              <span className="quote-label">Terms</span>
              <span>
                {latestQuote.incoterms ?? "Standard"} &middot; {latestQuote.payment_terms ?? "Standard"}
                {latestQuote.lead_time_days ? ` · ${latestQuote.lead_time_days}d lead time (excl. transport)` : ""}
              </span>
            </div>
          </div>

          {latestQuote.notes && <p className="quote-notes">&ldquo;{latestQuote.notes}&rdquo;</p>}

          <div className="quote-actions">
            {latestQuote.status === "pending" && !latestQuote.is_sender && (
              <>
                <button
                  type="button"
                  onClick={() => void handleAcceptQuote(latestQuote.id)}
                >
                  ✓ Accept Quote &amp; Issue PO
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => openNewQuoteModal(latestQuote)}
                >
                  ⇄ Counter-Offer
                </button>
                <button
                  type="button"
                  className="secondary danger-btn"
                  onClick={() => void handleRejectQuote(latestQuote.id)}
                >
                  ✕ Decline
                </button>
              </>
            )}
            {/* 3-Step Sequential Order Fulfillment Stepper */}
            {["accepted", "dispatched", "delivered", "received", "completed"].includes(latestQuote.status) && (
              <div className="order-fulfillment-stepper">
                <div className={`step-item ${["accepted", "dispatched", "delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                  <span className="step-badge">Step 1</span>
                  <span className="step-label">Dispatch</span>
                </div>
                <span className="step-arrow">&rarr;</span>
                <div className={`step-item ${["dispatched", "delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                  <span className="step-badge">Step 2</span>
                  <span className="step-label">Deliver</span>
                </div>
                <span className="step-arrow">&rarr;</span>
                <div className={`step-item ${["delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                  <span className="step-badge">Step 3</span>
                  <span className="step-label">Buyer Accept</span>
                </div>
                <span className="step-arrow">&rarr;</span>
                <div className={`step-item ${["received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                  <span className="step-badge">★</span>
                  <span className="step-label">Ratings</span>
                </div>
              </div>
            )}

            {/* Step 1: Accepted -> Seller Dispatches */}
            {latestQuote.status === "accepted" && (
              <div className="delivery-action-banner">
                {!isBuyer ? (
                  <>
                    <div className="delivery-action-text">
                      <strong>🚚 Step 1: Dispatch Order</strong>
                      <span className="muted small"> &middot; Purchase order confirmed. Mark goods as dispatched once shipped from your facility.</span>
                    </div>
                    <button
                      type="button"
                      className="deliver-btn primary-action"
                      onClick={() => void handleDispatch(latestQuote.id)}
                    >
                      🚚 Mark Order Dispatched
                    </button>
                  </>
                ) : (
                  <>
                    <div className="delivery-action-text">
                      <strong>⏳ Step 1: Awaiting Seller Dispatch</strong>
                      <span className="muted small"> &middot; Purchase order confirmed. Waiting for seller to dispatch shipment.</span>
                    </div>
                    <span className="badge badge-kyc-pending">⏳ Awaiting Dispatch</span>
                  </>
                )}
              </div>
            )}

            {/* Step 2: Dispatched -> Seller Marks Delivered */}
            {latestQuote.status === "dispatched" && (
              <div className="delivery-action-banner">
                {!isBuyer ? (
                  <>
                    <div className="delivery-action-text">
                      <strong>📦 Step 2: Update Order Delivered</strong>
                      <span className="muted small"> &middot; Consignment dispatched. Once goods arrive at destination, update status to delivered.</span>
                    </div>
                    <button
                      type="button"
                      className="deliver-btn primary-action"
                      onClick={() => void handleMarkDelivered(latestQuote.id)}
                    >
                      📦 Mark Order Delivered
                    </button>
                  </>
                ) : (
                  <>
                    <div className="delivery-action-text">
                      <strong>🚚 Step 2: Consignment In Transit</strong>
                      <span className="muted small"> &middot; The seller has dispatched your order. Delivery is underway.</span>
                    </div>
                    <span className="badge badge-cert-mini">🚚 Order En Route</span>
                  </>
                )}
              </div>
            )}

            {/* Step 3: Delivered -> Buyer Accepts Delivery */}
            {latestQuote.status === "delivered" && (
              <div className="delivery-action-banner">
                {isBuyer ? (
                  <>
                    <div className="delivery-action-text">
                      <strong>✓ Step 3: Confirm Delivery Received</strong>
                      <span className="muted small"> &middot; Goods have been delivered! Inspect the shipment and click below to accept order delivery.</span>
                    </div>
                    <button
                      type="button"
                      className="deliver-btn primary-action"
                      onClick={() => void handleAcceptDelivery(latestQuote.id)}
                    >
                      ✓ Accept Order Delivery
                    </button>
                  </>
                ) : (
                  <>
                    <div className="delivery-action-text">
                      <strong>⏳ Step 3: Awaiting Buyer Acceptance</strong>
                      <span className="muted small"> &middot; You marked the order delivered. Waiting for the buyer to inspect and accept delivery.</span>
                    </div>
                    <span className="badge badge-kyc-pending">⏳ Awaiting Buyer Acceptance</span>
                  </>
                )}
              </div>
            )}

            {/* Step 4: Received -> Mutual Sequential Ratings */}
            {latestQuote.status === "received" && (
              <div className="delivery-card-banner">
                {isBuyer ? (
                  !myReview ? (
                    <>
                      <div>
                        <strong>📦 Order Delivery Accepted &middot; Rate Seller</strong>
                        <span className="muted small">
                          {" "}
                          &middot; You accepted order delivery. Please rate the seller to complete your review.
                        </span>
                      </div>
                      <button
                        type="button"
                        className="rate-btn"
                        onClick={() => setShowRatingModal(true)}
                      >
                        ★ Rate Seller
                      </button>
                    </>
                  ) : (
                    <>
                      <div>
                        <strong>📦 Order Delivery Accepted</strong>
                        <span className="muted small">
                          {" "}
                          &middot; You rated this seller ({myReview.rating}★).{" "}
                          {sellerHasReviewed
                            ? `Seller also submitted feedback (${counterpartyReview?.rating}★)!`
                            : "Waiting for seller's rating in return."}
                        </span>
                      </div>
                      <span className="badge badge-accepted">
                        ✓ You Rated Seller ({myReview.rating}★)
                      </span>
                    </>
                  )
                ) : !buyerHasReviewed ? (
                  <>
                    <div className="waiting-buyer-notice">
                      <strong>📦 Order Accepted &middot; Awaiting Buyer's Review First</strong>
                      <span className="muted small">
                        {" "}
                        &middot; Per platform trust policy, the buyer must submit their review before you can rate them.
                      </span>
                    </div>
                    <span
                      className="badge badge-kyc-pending"
                      title="Ratings unlock automatically once buyer submits their review"
                    >
                      ⏳ Waiting for Buyer's Review First
                    </span>
                  </>
                ) : !myReview ? (
                  <>
                    <div>
                      <strong>🌟 Buyer Reviewed Your Delivery ({counterpartyReview?.rating}★)</strong>
                      <span className="muted small">
                        {" "}
                        &middot; The buyer has reviewed this delivery! You can now rate the buyer in return.
                      </span>
                    </div>
                    <button
                      type="button"
                      className="rate-btn"
                      onClick={() => setShowRatingModal(true)}
                    >
                      ★ Rate Buyer in Return
                    </button>
                  </>
                ) : (
                  <>
                    <div>
                      <strong>✓ Mutual Ratings Complete</strong>
                      <span className="muted small">
                        {" "}
                        &middot; You rated the buyer ({myReview.rating}★). Buyer rated you ({counterpartyReview?.rating}★).
                      </span>
                    </div>
                    <span className="badge badge-accepted">
                      ✓ You Rated Buyer ({myReview.rating}★)
                    </span>
                  </>
                )}
              </div>
            )}
            {latestQuote.status === "completed" && (
              <div className="completed-card-banner">
                <div className="delivery-card-banner">
                  <div>
                    <strong>✓ Order Completed &amp; Mutually Reviewed</strong>
                    <span className="muted small">
                      {" "}
                      &middot; All 3 fulfillment steps and mutual ratings have been successfully completed.
                    </span>
                  </div>
                  <span className="badge badge-accepted">
                    {myReview ? `✓ Your Rating: ${myReview.rating}★` : "✓ Order Completed"}
                  </span>
                </div>
              </div>
            )}
            {latestQuote.status === "pending" && latestQuote.is_sender && (
              <span className="muted">
                Sent to counterparty &middot; Waiting for their review
              </span>
            )}
            {quotes.length > 1 && (
              <button
                type="button"
                className="link-button"
                onClick={() => setShowHistory(!showHistory)}
              >
                {showHistory ? "Hide negotiation history" : `View negotiation history (${quotes.length} versions)`}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Negotiation History Timeline */}
      {showHistory && quotes.length > 1 && (
        <div className="quote-history-panel">
          <h3>Negotiation History</h3>
          {quotes.map((q) => (
            <div key={q.id} className="history-item">
              <div className="history-head">
                <strong>{q.quote_number}</strong>
                <span className={`badge badge-${q.status}`}>{q.status}</span>
                <span className="muted">{new Date(q.created_at).toLocaleDateString()}</span>
              </div>
              <p>
                {q.currency} {q.unit_price} &times; {q.quantity} {q.quantity_unit} ={" "}
                <strong>{q.currency} {q.total_amount.toLocaleString()}</strong>
              </p>
              {q.notes && <p className="muted small">&ldquo;{q.notes}&rdquo;</p>}
            </div>
          ))}
        </div>
      )}

      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      {/* Chat messages */}
      <div className="chat-scroll">
        {loading && <p className="muted">Loading conversation&hellip;</p>}
        {!loading && messages.length === 0 && (
          <p className="muted">
            {open ? "No messages yet. Say hello or submit a quotation." : "Messages open once the request is accepted."}
          </p>
        )}
        {messages.map((message) => {
          const mine = message.sender_id === myId;
          return (
            <div key={message.id} className={mine ? "chat-line mine" : "chat-line theirs"}>
              <div className="chat-bubble">
                {message.image_url && (
                  <div className="chat-media-card">
                    <div className="live-camera-badge">
                      <span className="live-dot" /> 📸 Live Camera Capture
                    </div>
                    <img
                      src={message.image_url}
                      alt="Live camera snapshot"
                      className="chat-media-img"
                      onClick={() => setZoomedImage(message.image_url ?? null)}
                      title="Click to view full size"
                    />
                  </div>
                )}
                {message.content && message.content !== "📸 Live Camera Snapshot" && (
                  <p>{message.content}</p>
                )}
                <time dateTime={message.created_at}>
                  {new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </time>
              </div>
            </div>
          );
        })}
        {isCounterpartyTyping && (
          <div className="chat-line theirs">
            <div className="chat-bubble typing-indicator">
              <span>Counterparty is typing…</span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={handleSend}>
        <input
          aria-label="Your message"
          value={text}
          onChange={(event) => handleTextChange(event.target.value)}
          placeholder={open ? "Type a real-time message…" : "Accept the request first"}
          maxLength={4000}
          disabled={!open || sending}
        />
        <button
          type="button"
          className="camera-snap-btn"
          title="Live camera capture (pre-saved gallery uploads are disabled to prevent fraud)"
          onClick={() => void openLiveCamera()}
          disabled={!open || sending}
        >
          📸 Live Camera
        </button>
        <button type="submit" disabled={!open || sending || !text.trim()}>
          {sending ? "Sending…" : "Send"}
        </button>
      </form>

      {/* Quote / Counter-Offer Modal */}
      {showQuoteModal && (
        <div className="modal-overlay">
          <div className="modal-card">
            <h3>Issue Quotation / Counter-Offer</h3>
            <form onSubmit={handleSubmitQuote}>
              <div className="modal-row">
                <div>
                  <label htmlFor="quote-price">Unit Price</label>
                  <input
                    id="quote-price"
                    type="number"
                    step="0.01"
                    min="0.01"
                    required
                    value={quoteForm.unit_price}
                    onChange={(e) => setQuoteForm({ ...quoteForm, unit_price: e.target.value })}
                  />
                </div>
                <div>
                  <label htmlFor="quote-currency">Currency</label>
                  <select
                    id="quote-currency"
                    value={quoteForm.currency}
                    onChange={(e) => setQuoteForm({ ...quoteForm, currency: e.target.value })}
                  >
                    <option value="INR">INR (₹) · Indian Rupee</option>
                    <option value="USD">USD ($) · US Dollar</option>
                    <option value="EUR">EUR (€) · Euro</option>
                    <option value="GBP">GBP (£) · British Pound</option>
                    <option value="SEK">SEK (kr) · Swedish Krona</option>
                    <option value="NOK">NOK (kr) · Norwegian Krone</option>
                    <option value="DKK">DKK (kr) · Danish Krone</option>
                    <option value="CNY">CNY (¥) · Chinese Yuan</option>
                    <option value="AED">AED · UAE Dirham</option>
                    <option value="CAD">CAD (C$) · Canadian Dollar</option>
                    <option value="AUD">AUD (A$) · Australian Dollar</option>
                    <option value="JPY">JPY (¥) · Japanese Yen</option>
                    <option value="CHF">CHF · Swiss Franc</option>
                    <option value="KRW">KRW (₩) · Korean Won</option>
                    <option value="SGD">SGD · Singapore Dollar</option>
                    <option value="SAR">SAR · Saudi Riyal</option>
                  </select>
                </div>
              </div>

              <div className="modal-row">
                <div>
                  <label htmlFor="quote-qty">Quantity</label>
                  <input
                    id="quote-qty"
                    type="number"
                    step="1"
                    min="1"
                    required
                    value={quoteForm.quantity}
                    onChange={(e) => setQuoteForm({ ...quoteForm, quantity: e.target.value })}
                  />
                </div>
                <div>
                  <label htmlFor="quote-unit">Unit</label>
                  <input
                    id="quote-unit"
                    required
                    value={quoteForm.quantity_unit}
                    onChange={(e) => setQuoteForm({ ...quoteForm, quantity_unit: e.target.value })}
                  />
                </div>
              </div>

              {quoteForm.unit_price && quoteForm.quantity && (
                <div className="total-preview">
                  Total Order Value:{" "}
                  <strong>
                    {quoteForm.currency} {(parseFloat(quoteForm.unit_price) * parseFloat(quoteForm.quantity)).toLocaleString()}
                  </strong>
                </div>
              )}

              <div className="modal-row">
                <div>
                  <label htmlFor="quote-incoterms">Incoterms</label>
                  <select
                    id="quote-incoterms"
                    value={quoteForm.incoterms}
                    onChange={(e) => setQuoteForm({ ...quoteForm, incoterms: e.target.value as Incoterm })}
                  >
                    {INCOTERMS_OPTIONS.map((term) => (
                      <option key={term} value={term}>
                        {term}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="quote-lead">
                    Lead Time (Days)
                    <span className="excl-transport-tag" title="Excludes transport/transit days">Excl. Transport</span>
                  </label>
                  <input
                    id="quote-lead"
                    type="number"
                    min="1"
                    value={quoteForm.lead_time_days}
                    onChange={(e) => setQuoteForm({ ...quoteForm, lead_time_days: e.target.value })}
                  />
                  <small className="muted" style={{ display: "block", marginTop: "0.2rem" }}>
                    Ex-Factory lead time. Transport &amp; transit days are excluded.
                  </small>
                </div>
              </div>

              <div>
                <label htmlFor="quote-payment">Payment Terms</label>
                <input
                  id="quote-payment"
                  placeholder="e.g. Net 30 or 50% Advance"
                  value={quoteForm.payment_terms}
                  onChange={(e) => setQuoteForm({ ...quoteForm, payment_terms: e.target.value })}
                />
              </div>

              <div>
                <label htmlFor="quote-notes">Notes &amp; Packaging Specs</label>
                <textarea
                  id="quote-notes"
                  rows={2}
                  placeholder="Packaging details, quality assurances, delivery notes..."
                  value={quoteForm.notes}
                  onChange={(e) => setQuoteForm({ ...quoteForm, notes: e.target.value })}
                />
              </div>

              <div className="modal-actions">
                <button type="submit">Submit Quotation</button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowQuoteModal(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Counterparty Rating & Review Modal */}
      {showRatingModal && latestQuote && (
        <div className="modal-backdrop" onClick={() => setShowRatingModal(false)}>
          <div className="modal-card rating-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Rate &amp; Review {isBuyer ? "Seller (Supplier)" : "Buyer (Customer)"}</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowRatingModal(false)}
              >
                &times;
              </button>
            </div>
            <p className="muted small">
              Transaction: <strong>{latestQuote.quote_number}</strong> &middot; PO: {latestQuote.purchase_order_reference || "N/A"}
            </p>

            <form onSubmit={(e) => void handleSubmitRating(e, latestQuote.id)}>
              <div className="rating-field">
                <label>Overall Experience</label>
                <div className="star-picker">
                  {[1, 2, 3, 4, 5].map((star) => (
                    <button
                      type="button"
                      key={star}
                      className={ratingForm.rating >= star ? "star filled" : "star"}
                      onClick={() => setRatingForm({ ...ratingForm, rating: star })}
                    >
                      ★
                    </button>
                  ))}
                  <span className="star-label">{ratingForm.rating} / 5 Stars</span>
                </div>
              </div>

              <div className="row criteria-row">
                <div>
                  <label htmlFor="r-comm">Communication</label>
                  <select
                    id="r-comm"
                    value={ratingForm.communication_rating ?? 5}
                    onChange={(e) =>
                      setRatingForm({
                        ...ratingForm,
                        communication_rating: parseInt(e.target.value, 10),
                      })
                    }
                  >
                    <option value={5}>5 - Excellent</option>
                    <option value={4}>4 - Good</option>
                    <option value={3}>3 - Fair</option>
                    <option value={2}>2 - Poor</option>
                    <option value={1}>1 - Unresponsive</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="r-deliv">Delivery Speed</label>
                  <select
                    id="r-deliv"
                    value={ratingForm.delivery_rating ?? 5}
                    onChange={(e) =>
                      setRatingForm({
                        ...ratingForm,
                        delivery_rating: parseInt(e.target.value, 10),
                      })
                    }
                  >
                    <option value={5}>5 - On Time</option>
                    <option value={4}>4 - Minor Delay</option>
                    <option value={3}>3 - Acceptable</option>
                    <option value={2}>2 - Very Late</option>
                    <option value={1}>1 - Never Arrived</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="r-qual">Product Quality</label>
                  <select
                    id="r-qual"
                    value={ratingForm.quality_rating ?? 5}
                    onChange={(e) =>
                      setRatingForm({
                        ...ratingForm,
                        quality_rating: parseInt(e.target.value, 10),
                      })
                    }
                  >
                    <option value={5}>5 - Matches Specs</option>
                    <option value={4}>4 - High Quality</option>
                    <option value={3}>3 - Average</option>
                    <option value={2}>2 - Substandard</option>
                    <option value={1}>1 - Defective</option>
                  </select>
                </div>
              </div>

              <div>
                <label htmlFor="r-comment">Feedback &amp; Comments</label>
                <textarea
                  id="r-comment"
                  rows={3}
                  placeholder="Share details about the commercial transaction, packaging quality, and communication..."
                  value={ratingForm.comment ?? ""}
                  onChange={(e) => setRatingForm({ ...ratingForm, comment: e.target.value })}
                />
              </div>

              <div className="modal-actions">
                <button type="submit">Submit Feedback</button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowRatingModal(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Live Camera Viewfinder Modal */}
      {showCameraModal && (
        <div className="modal-overlay">
          <div className="modal-card camera-modal-card">
            <div className="camera-modal-header">
              <div className="camera-header-title">
                <h3>📸 Live Camera Proof-of-Inventory</h3>
                <span className="camera-live-pill">
                  <span className="pulse-red-dot" /> LIVE CAMERA ONLY
                </span>
              </div>
              <button type="button" className="close-btn" onClick={handleCloseCamera}>
                ✕
              </button>
            </div>

            <p className="muted small camera-disclaimer">
              🛡️ <strong>Anti-Fraud Protection:</strong> Pre-saved gallery/disk file uploads are strictly disabled.
              Images must be captured live with your camera to prove authentic inventory.
            </p>

            {cameraError && <div className="error camera-error-banner">{cameraError}</div>}

            <div className="camera-viewfinder-box">
              {!capturedPreviewUrl ? (
                <>
                  <video
                    ref={videoRef}
                    autoPlay
                    playsInline
                    muted
                    className="camera-video-feed"
                  />
                  <div className="viewfinder-hud">
                    <div className="hud-corner hud-tl" />
                    <div className="hud-corner hud-tr" />
                    <div className="hud-corner hud-bl" />
                    <div className="hud-corner hud-br" />
                    <div className="hud-target-crosshair" />
                    <div className="hud-watermark-overlay">
                      ● LIVE WEBCAM ACTIVE &middot; {new Date().toLocaleDateString()}
                    </div>
                  </div>
                </>
              ) : (
                <div className="camera-preview-box">
                  <img
                    src={capturedPreviewUrl}
                    alt="Live capture snapshot"
                    className="captured-preview-img"
                  />
                  <div className="preview-watermark-badge">
                    ✓ Verified Live Photo Captured
                  </div>
                </div>
              )}
            </div>

            {capturedPreviewUrl && (
              <div className="camera-caption-input">
                <label htmlFor="cam-caption">Caption or note (optional)</label>
                <input
                  id="cam-caption"
                  type="text"
                  placeholder="e.g. Sample batch ready in warehouse..."
                  value={cameraCaption}
                  maxLength={200}
                  onChange={(e) => setCameraCaption(e.target.value)}
                />
              </div>
            )}

            <div className="camera-modal-actions">
              {!capturedPreviewUrl ? (
                <>
                  <button
                    type="button"
                    className="secondary"
                    onClick={handleCloseCamera}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="primary shutter-btn"
                    onClick={handleCaptureSnapshot}
                    disabled={!cameraStream}
                  >
                    📸 Capture Photo
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="secondary"
                    onClick={handleRetakePhoto}
                    disabled={sending}
                  >
                    🔄 Retake Photo
                  </button>
                  <button
                    type="button"
                    className="primary send-snap-btn"
                    onClick={() => void handleSendLivePhoto()}
                    disabled={sending}
                  >
                    {sending ? "Sending Photo..." : "🚀 Send Live Photo to Chat"}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Lightbox Zoom Modal */}
      {zoomedImage && (
        <div className="lightbox-overlay" onClick={() => setZoomedImage(null)}>
          <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="lightbox-close"
              onClick={() => setZoomedImage(null)}
            >
              ✕
            </button>
            <img src={zoomedImage} alt="Expanded snapshot" className="lightbox-img" />
            <div className="lightbox-footer">
              📸 <strong>Verified Live Camera Snapshot</strong> &middot; Authenticated on Antigravity B2B Marketplace
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

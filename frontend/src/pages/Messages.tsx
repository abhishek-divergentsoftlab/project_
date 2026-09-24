import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { errorMessage, tokenStore } from "@/api/client";
import {
  connections as connApi,
  escrow as escrowApi,
  logistics as logisticsApi,
  quotations as quoteApi,
  reviews as reviewsApi,
  translation as translationApi,
} from "@/api/endpoints";
import {
  IconAlert,
  IconCamera,
  IconCheck,
  IconChevronLeft,
  IconClock,
  IconClose,
  IconDownload,
  IconFileText,
  IconHistory,
  IconLanguages,
  IconLock,
  IconMapPin,
  IconMessages,
  IconPackage,
  IconPlus,
  IconSend,
  IconStar,
  IconSwap,
  IconTruck,
  IconX,
} from "@/components/icons";

import { useSidebarData } from "@/context/SidebarDataContext";
import { useFeedback } from "@/context/useFeedback";
import { useEscapeKey } from "@/hooks/useEscapeKey";
import type {
  Connection,
  ConnectionMessage,
  DealDispute,
  DealIssue,
  DealIssueCategory,
  DealIssueSeverity,
  EscrowAccount,
  EscrowMilestone,
  FreightEstimateRequest,
  FreightEstimateResponse,
  Incoterm,
  LanguageInfo,
  Quotation,
  QuotationCreatePayload,
  Review,
  ReviewCreatePayload,
  Shipment,
  ShipmentCreatePayload,
  ShipmentStatus,
  ShipmentStatusUpdatePayload,
  ShippingMode,
} from "@/types";
import { normalizeCurrency } from "@/utils/currency";
import { formatDate, formatNumber } from "@/utils/format";

/** "in_transit" -> "In transit" */
function humanize(value: string): string {
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function money(currency: string, amount: number | string): string {
  return `${currency} ${formatNumber(amount)}`;
}

const POLL_FALLBACK_MS = 15000;
const INCOTERMS_OPTIONS: Incoterm[] = ["EXW", "FOB", "CIF", "CFR", "DDP", "CIP"];

function describe(connection: Connection): string {
  const who = connection.counterparty.company_name ?? connection.counterparty.contact_name;
  if (who) return who;
  return connection.direction === "sent" ? "The listing owner" : "A buyer";
}

function getInitials(name: string): string {
  const clean = name.trim();
  if (!clean) return "U";
  const parts = clean.split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
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

export function Messages() {
  const [searchParams, setSearchParams] = useSearchParams();
  const {
    connections,
    loadingConnections,
    acceptConnection,
    rejectConnection,
    refreshConnections,
  } = useSidebarData();

  const urlConn = searchParams.get("connection");
  const [activeId, setActiveId] = useState<string | null>(urlConn);

  // Mobile navigation view: "chat" | "deal"
  const [mobileView, setMobileView] = useState<"users" | "chat" | "deal">("chat");

  useEffect(() => {
    if (urlConn) {
      setActiveId(urlConn);
    } else if (connections.length > 0 && !activeId) {
      setActiveId(connections[0].id);
      setSearchParams({ connection: connections[0].id }, { replace: true });
    }
  }, [urlConn, connections, activeId, setSearchParams]);

  const active = connections.find((c) => c.id === activeId) ?? connections[0] ?? null;

  if (loadingConnections && connections.length === 0) {
    return (
      <div className="loading-state messages-page-loading">
        <span className="spinner" />
        Loading conversations…
      </div>
    );
  }

  return (
    <section className="messages-page deal-room-full-page" data-mobile-view={mobileView}>
      {active ? (
        <ChatAndDealMain
          connection={active}
          onBackToUsers={() => {
            const trigger = document.querySelector<HTMLButtonElement>(".menu-trigger");
            if (trigger) trigger.click();
            else document.body.classList.add("nav-open");
          }}
          onOpenDeal={() => setMobileView("deal")}
          onBackToChat={() => setMobileView("chat")}
          mobileView={mobileView}
          onAcceptConnection={acceptConnection}
          onRejectConnection={rejectConnection}
          onDataChanged={refreshConnections}
        />
      ) : (
        <div className="chat-empty-hub">
          <div className="empty-state">
            <span className="empty-state-icon">
              <IconMessages size={18} />
            </span>
            <strong>No conversations yet</strong>
            <p>
              When you connect with a buyer or supplier, your conversation and deal room open here.
            </p>
            <Link to="/marketplace" className="button">
              Find counterparties
            </Link>
          </div>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Combined Middle (Chat UI) + Right (Negotiation Quotes & Issues) Component
// ---------------------------------------------------------------------------
interface ChatAndDealMainProps {
  connection: Connection;
  onBackToUsers: () => void;
  onOpenDeal: () => void;
  onBackToChat: () => void;
  mobileView: "users" | "chat" | "deal";
  onAcceptConnection?: (id: string) => Promise<void>;
  onRejectConnection?: (id: string) => Promise<void>;
  onDataChanged?: () => void;
}


function ChatAndDealMain({
  connection,
  onBackToUsers,
  onOpenDeal,
  onBackToChat,
  onAcceptConnection,
  onRejectConnection,
}: ChatAndDealMainProps) {
  const [busyDecide, setBusyDecide] = useState(false);
  const [messages, setMessages] = useState<ConnectionMessage[]>([]);
  const [quotes, setQuotes] = useState<Quotation[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Modals & Panels
  const [showQuoteModal, setShowQuoteModal] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showRatingModal, setShowRatingModal] = useState(false);
  const [showIssueModal, setShowIssueModal] = useState(false);
  const [isCounterpartyTyping, setIsCounterpartyTyping] = useState(false);

  // Right sidebar tab state: "quotes" | "escrow" | "logistics" | "issues"
  const [rightTab, setRightTab] = useState<"quotes" | "escrow" | "logistics" | "issues">("quotes");

  // Escrow & Milestone Payment State
  const [escrowAccount, setEscrowAccount] = useState<EscrowAccount | null>(null);
  const [fundingEscrow, setFundingEscrow] = useState(false);
  const [requestingMilestoneId, setRequestingMilestoneId] = useState<string | null>(null);
  const [releasingMilestoneId, setReleasingMilestoneId] = useState<string | null>(null);
  const [showFundModal, setShowFundModal] = useState(false);
  const [showMilestoneProofModal, setShowMilestoneProofModal] = useState<string | null>(null);
  const [milestoneProofNote, setMilestoneProofNote] = useState("");
  const [showDisputeModal, setShowDisputeModal] = useState(false);

  // Logistics, Consignment & Multi-Modal Freight State
  const [shipment, setShipment] = useState<Shipment | null>(null);
  const [showDispatchModal, setShowDispatchModal] = useState(false);
  const [showCheckpointModal, setShowCheckpointModal] = useState(false);
  const [downloadingWaybill, setDownloadingWaybill] = useState(false);
  const [dispatching, setDispatching] = useState(false);
  const [addingCheckpoint, setAddingCheckpoint] = useState(false);
  const [calculatingFreight, setCalculatingFreight] = useState(false);
  const [freightEstimate, setFreightEstimate] = useState<FreightEstimateResponse | null>(null);

  const [dispatchForm, setDispatchForm] = useState<ShipmentCreatePayload>({
    carrier_name: "DHL Global Forwarding",
    tracking_number: "",
    tracking_url: "",
    shipping_mode: "ocean",
    origin_city: "Mumbai",
    origin_country: "IN",
    destination_city: "Rotterdam",
    destination_country: "NL",
    incoterm: "FOB",
    gross_weight_kg: 1250,
    length_cm: 120,
    width_cm: 100,
    height_cm: 110,
    cbm: 1.32,
    packages_count: 50,
    shipping_cost: 450,
    customs_declaration_no: "",
    notes: "",
    trigger_escrow_milestone: true,
  });

  const [checkpointForm, setCheckpointForm] = useState<ShipmentStatusUpdatePayload>({
    status: "in_transit",
    location: "",
    note: "",
  });

  const [freightForm, setFreightForm] = useState<FreightEstimateRequest>({
    origin_country: "IN",
    origin_city: "Mumbai",
    destination_country: "NL",
    destination_city: "Rotterdam",
    gross_weight_kg: 1000,
    cbm: 1.2,
    incoterm: "FOB",
  });


  // Reviews
  const [quoteReviews, setQuoteReviews] = useState<Review[]>([]);
  const [ratingForm, setRatingForm] = useState<ReviewCreatePayload>({
    rating: 5,
    communication_rating: 5,
    delivery_rating: 5,
    quality_rating: 5,
    comment: "",
  });

  // Quotation form state
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

  // Issues & Disputes State
  const [issues, setIssues] = useState<DealIssue[]>([]);
  const [issueForm, setIssueForm] = useState<{
    title: string;
    category: DealIssueCategory;
    severity: DealIssueSeverity;
    description: string;
    suggested_resolution: string;
  }>({
    title: "",
    category: "quality",
    severity: "medium",
    description: "",
    suggested_resolution: "",
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
  const counterpartyName = describe(connection);

  // Multilingual Translation State
  const [supportedLangs, setSupportedLangs] = useState<LanguageInfo[]>([]);
  const [targetLang, setTargetLang] = useState<string>(() => {
    try {
      return localStorage.getItem("b2b_dealroom_target_lang") || "en";
    } catch {
      return "en";
    }
  });
  const [autoTranslate, setAutoTranslate] = useState<boolean>(() => {
    try {
      return localStorage.getItem("b2b_dealroom_auto_translate") === "true";
    } catch {
      return false;
    }
  });
  const [translations, setTranslations] = useState<
    Record<string, { translated_text: string; source_language: string; target_language: string }>
  >({});
  const [translatingIds, setTranslatingIds] = useState<Set<string>>(new Set());
  const [showOriginal, setShowOriginal] = useState<Record<string, boolean>>({});
  const [translatingDraft, setTranslatingDraft] = useState(false);

  // Deal-room feedback goes through toasts so it stays visible above dialogs.
  const { toast, confirm, prompt } = useFeedback();
  useEffect(() => {
    if (notice) {
      toast(notice);
      setNotice(null);
    }
  }, [notice, toast]);
  useEffect(() => {
    if (error) {
      toast(error, "error");
      setError(null);
    }
  }, [error, toast]);

  useEscapeKey(showQuoteModal, () => setShowQuoteModal(false));
  useEscapeKey(showIssueModal, () => setShowIssueModal(false));
  useEscapeKey(showRatingModal, () => setShowRatingModal(false));
  useEscapeKey(showFundModal, () => setShowFundModal(false));
  useEscapeKey(showDisputeModal, () => setShowDisputeModal(false));
  useEscapeKey(showDispatchModal, () => setShowDispatchModal(false));
  useEscapeKey(showCheckpointModal, () => setShowCheckpointModal(false));
  useEscapeKey(Boolean(showMilestoneProofModal), () => {
    setShowMilestoneProofModal(null);
    setMilestoneProofNote("");
  });
  useEscapeKey(Boolean(zoomedImage), () => setZoomedImage(null));
  useEscapeKey(showCameraModal, () => handleCloseCamera());

  // Fetch supported languages on mount
  useEffect(() => {
    let cancelled = false;
    translationApi
      .getSupportedLanguages()
      .then((langs) => {
        if (!cancelled && Array.isArray(langs) && langs.length > 0) {
          setSupportedLangs(langs);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSupportedLangs([
            { code: "en", name: "English", native_name: "English", flag: "🇺🇸" },
            { code: "es", name: "Spanish", native_name: "Español", flag: "🇪🇸" },
            { code: "zh", name: "Chinese (Simplified)", native_name: "简体中文", flag: "🇨🇳" },
            { code: "hi", name: "Hindi", native_name: "हिन्दी", flag: "🇮🇳" },
            { code: "de", name: "German", native_name: "Deutsch", flag: "🇩🇪" },
            { code: "fr", name: "French", native_name: "Français", flag: "🇫🇷" },
            { code: "ar", name: "Arabic", native_name: "العربية", flag: "🇦🇪" },
            { code: "ja", name: "Japanese", native_name: "日本語", flag: "🇯🇵" },
            { code: "ru", name: "Russian", native_name: "Русский", flag: "🇷🇺" },
            { code: "pt", name: "Portuguese", native_name: "Português", flag: "🇧🇷" },
            { code: "it", name: "Italian", native_name: "Italiano", flag: "🇮🇹" },
          ]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Auto-translate incoming counterparty messages
  useEffect(() => {
    if (!autoTranslate || !open) return;

    const untranslated = messages.filter(
      (m) =>
        m.sender_id !== myId &&
        m.content &&
        m.content !== "📸 Live Camera Snapshot" &&
        !m.content.startsWith("⚠️ [DEAL ISSUE") &&
        !m.content.startsWith("✓ [DEAL ISSUE") &&
        !translations[m.id] &&
        !translatingIds.has(m.id)
    );

    if (untranslated.length === 0) return;

    untranslated.forEach((msg) => {
      setTranslatingIds((prev) => new Set(prev).add(msg.id));
      translationApi
        .translateConnectionMessage(connectionId, msg.content, targetLang)
        .then((res) => {
          setTranslations((prev) => ({
            ...prev,
            [msg.id]: {
              translated_text: res.translated_text,
              source_language: res.source_language,
              target_language: res.target_language,
            },
          }));
        })
        .catch((err) => {
          console.error("Auto-translate error for message", msg.id, err);
        })
        .finally(() => {
          setTranslatingIds((prev) => {
            const next = new Set(prev);
            next.delete(msg.id);
            return next;
          });
        });
    });
  }, [messages, autoTranslate, targetLang, open, connectionId, myId, translations, translatingIds]);

  const handleTranslateMessage = async (msg: ConnectionMessage) => {
    if (translatingIds.has(msg.id)) return;
    setTranslatingIds((prev) => new Set(prev).add(msg.id));
    try {
      const res = await translationApi.translateConnectionMessage(connectionId, msg.content, targetLang);
      setTranslations((prev) => ({
        ...prev,
        [msg.id]: {
          translated_text: res.translated_text,
          source_language: res.source_language,
          target_language: res.target_language,
        },
      }));
      setShowOriginal((prev) => ({ ...prev, [msg.id]: false }));
    } catch (err) {
      setError(`Translation failed: ${errorMessage(err)}`);
    } finally {
      setTranslatingIds((prev) => {
        const next = new Set(prev);
        next.delete(msg.id);
        return next;
      });
    }
  };

  const handleTranslateComposer = async () => {
    if (!text.trim() || translatingDraft) return;
    setTranslatingDraft(true);
    try {
      const res = await translationApi.translateText(text.trim(), targetLang);
      if (res.translated_text) {
        setText(res.translated_text);
        setNotice(`Draft translated to ${targetLang.toUpperCase()}.`);
      }
    } catch (err) {
      setError(`Draft translation failed: ${errorMessage(err)}`);
    } finally {
      setTranslatingDraft(false);
    }
  };

  const handleToggleAutoTranslate = () => {
    const next = !autoTranslate;
    setAutoTranslate(next);
    try {
      localStorage.setItem("b2b_dealroom_auto_translate", String(next));
    } catch {
      // Ignored
    }
    if (next) {
      setTranslations({});
    }
  };

  const handleChangeTargetLang = (newLang: string) => {
    setTargetLang(newLang);
    try {
      localStorage.setItem("b2b_dealroom_target_lang", newLang);
    } catch {
      // Ignored
    }
    setTranslations({});
  };

  // Load issues from localStorage
  const loadIssues = useCallback(() => {
    try {
      const stored = localStorage.getItem(`marketplace_issues_${connectionId}`);
      if (stored) {
        setIssues(JSON.parse(stored) as DealIssue[]);
      } else {
        setIssues([]);
      }
    } catch {
      setIssues([]);
    }
  }, [connectionId]);

  const saveIssues = (updated: DealIssue[]) => {
    setIssues(updated);
    try {
      localStorage.setItem(`marketplace_issues_${connectionId}`, JSON.stringify(updated));
    } catch {
      // Ignored
    }
  };

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
      if (
        activeQuote &&
        (activeQuote.status === "received" ||
          activeQuote.status === "delivered" ||
          activeQuote.status === "completed")
      ) {
        const revs = await reviewsApi.listQuoteReviews(connectionId, activeQuote.id);
        setQuoteReviews(revs);
      } else {
        setQuoteReviews([]);
      }

      if (
        activeQuote &&
        ["accepted", "delivered", "received", "completed"].includes(activeQuote.status)
      ) {
        try {
          const escData = await escrowApi.getEscrow(connectionId);
          setEscrowAccount(escData);
        } catch {
          setEscrowAccount(null);
        }
      } else {
        setEscrowAccount(null);
      }

      if (open) {
        try {
          const shipData = await logisticsApi.getConnectionShipment(connectionId);
          setShipment(shipData);
        } catch {
          setShipment(null);
        }
      } else {
        setShipment(null);
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
    loadIssues();
    void reloadData().finally(() => setLoading(false));
  }, [reloadData, loadIssues]);

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
          // Reload issues if message is an issue event
          if (msg.content && msg.content.includes("[DEAL ISSUE")) {
            loadIssues();
          }
        } else if (
          payload.type === "quote_created" ||
          payload.type === "quote_accepted" ||
          payload.type === "quote_rejected" ||
          payload.type === "quote_dispatched" ||
          payload.type === "quote_delivered" ||
          payload.type === "delivery_accepted" ||
          payload.type === "review_submitted" ||
          payload.type === "escrow_funded" ||
          payload.type === "milestone_release_requested" ||
          payload.type === "milestone_released" ||
          payload.type === "dispute_raised" ||
          payload.type === "dispute_resolved" ||
          payload.type === "shipment_dispatched" ||
          payload.type === "shipment_updated"
        ) {
          void reloadData();
          if (payload.type === "quote_created") {
            setNotice(`${counterpartyName} sent a new quote.`);
          } else if (payload.type === "quote_accepted") {
            setNotice("Quote accepted. Purchase order created.");
          } else if (payload.type === "quote_dispatched") {
            setNotice("The order has been dispatched.");
          } else if (payload.type === "quote_delivered") {
            setNotice("The order was marked as delivered.");
          } else if (payload.type === "delivery_accepted") {
            setNotice("Delivery confirmed. You can now rate each other.");
          } else if (payload.type === "review_submitted") {
            setNotice(`${counterpartyName} left a review.`);
          } else if (payload.type === "escrow_funded") {
            setNotice("The buyer deposited the payment into escrow.");
          } else if (payload.type === "milestone_release_requested") {
            setNotice("The seller requested a milestone release.");
          } else if (payload.type === "milestone_released") {
            setNotice("A milestone payment was released to the seller.");
          } else if (payload.type === "dispute_raised") {
            setError("A dispute was opened. Escrow payments are paused.");
          } else if (payload.type === "dispute_resolved") {
            setNotice("The dispute was resolved.");
          } else if (payload.type === "shipment_dispatched") {
            setNotice("The seller dispatched the order.");
          } else if (payload.type === "shipment_updated") {
            setNotice("Shipment status updated.");
          }
        } else if (payload.type === "typing") {
          setIsCounterpartyTyping(true);
          if (typingTimerRef.current) clearTimeout(typingTimerRef.current);
          typingTimerRef.current = setTimeout(() => setIsCounterpartyTyping(false), 2500);
        }
      } catch {
        // Ignored
      }
    };

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
  }, [connectionId, open, reloadData, loadIssues]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isCounterpartyTyping]);

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

  async function handleSend(event?: FormEvent) {
    if (event) event.preventDefault();
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

  // Document download handlers
  const [downloadingPo, setDownloadingPo] = useState<string | null>(null);
  const [downloadingInv, setDownloadingInv] = useState<string | null>(null);

  async function handleDownloadPo(quoteId: string, poRef?: string | null) {
    setDownloadingPo(quoteId);
    setError(null);
    try {
      const blob = await quoteApi.downloadPoPdf(connectionId, quoteId);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Purchase_Order_${poRef || quoteId.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      setError(errorMessage(err, "Failed to download Purchase Order PDF"));
    } finally {
      setDownloadingPo(null);
    }
  }

  async function handleDownloadInvoice(quoteId: string, quoteNumber?: string | null) {
    setDownloadingInv(quoteId);
    setError(null);
    try {
      const blob = await quoteApi.downloadInvoicePdf(connectionId, quoteId);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Commercial_Invoice_${quoteNumber || quoteId.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      setError(errorMessage(err, "Failed to download Commercial Invoice PDF"));
    } finally {
      setDownloadingInv(null);
    }
  }

  // Logistics & Consignment handlers
  async function handleDownloadWaybill(shipmentId: string, trackingNumber?: string | null) {
    setDownloadingWaybill(true);
    setError(null);
    try {
      const blob = await logisticsApi.downloadWaybillPdf(shipmentId);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Waybill_${trackingNumber || shipmentId.slice(0, 8)}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      setError(errorMessage(err, "Failed to download Consignment Waybill PDF"));
    } finally {
      setDownloadingWaybill(false);
    }
  }

  async function handleDispatchShipment(e: FormEvent) {
    e.preventDefault();
    if (!dispatchForm.carrier_name || !dispatchForm.tracking_number) {
      setError("Please specify a carrier name and tracking number.");
      return;
    }
    setDispatching(true);
    setError(null);
    try {
      const payload: ShipmentCreatePayload = {
        ...dispatchForm,
        quotation_id: latestQuote ? latestQuote.id : undefined,
      };
      const created = await logisticsApi.createDispatch(connectionId, payload);
      setShipment(created);
      setShowDispatchModal(false);
      setNotice(`Dispatched with ${created.carrier_name}. Waybill ${created.tracking_number} issued.`);
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Failed to dispatch shipment"));
    } finally {
      setDispatching(false);
    }
  }

  async function handleAddCheckpoint(e: FormEvent) {
    e.preventDefault();
    if (!shipment) return;
    setAddingCheckpoint(true);
    setError(null);
    try {
      const updated = await logisticsApi.addTrackingEvent(shipment.id, checkpointForm);
      setShipment(updated);
      setShowCheckpointModal(false);
      setNotice(`Shipment marked as ${humanize(checkpointForm.status).toLowerCase()}.`);
      setCheckpointForm({ status: "in_transit", location: "", note: "" });
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Failed to record tracking event"));
    } finally {
      setAddingCheckpoint(false);
    }
  }

  async function handleCalculateFreight(e?: FormEvent) {
    if (e) e.preventDefault();
    setCalculatingFreight(true);
    setError(null);
    try {
      const res = await logisticsApi.estimateFreight(freightForm);
      setFreightEstimate(res);
      setNotice("Freight estimates ready.");
    } catch (err) {
      setError(errorMessage(err, "Failed to calculate freight estimate"));
    } finally {
      setCalculatingFreight(false);
    }
  }

  // Quotation handlers
  async function handleAcceptQuote(quoteId: string) {
    const quote = quotes.find((q) => q.id === quoteId);
    const ok = await confirm({
      title: "Accept this quote?",
      message: quote
        ? `This creates a purchase order for ${money(quote.currency, quote.total_amount)} on the quoted terms.`
        : "This creates a purchase order on the quoted terms.",
      confirmLabel: "Accept quote",
    });
    if (!ok) return;
    setError(null);
    setNotice(null);
    try {
      const updated = await quoteApi.accept(connectionId, quoteId);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice(`Quote accepted. Purchase order ${updated.purchase_order_reference} created.`);
    } catch (err) {
      setError(errorMessage(err, "Could not accept quotation"));
    }
  }


  async function handleRejectQuote(quoteId: string) {
    const entered = await prompt({
      title: "Decline this quote?",
      message: `${counterpartyName} will be notified. You can still send a counter-offer later.`,
      label: "Reason (optional)",
      placeholder: "e.g. Price is above our budget",
      confirmLabel: "Decline quote",
      tone: "danger",
    });
    if (entered === null) return;
    const reason = entered || undefined;
    setError(null);
    setNotice(null);
    try {
      const updated = await quoteApi.reject(connectionId, quoteId, reason);
      setQuotes((current) => current.map((q) => (q.id === quoteId ? updated : q)));
      setNotice("Quote declined.");
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
      setNotice("Marked as dispatched.");
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
      setNotice("Marked as delivered. Waiting for the buyer to confirm.");
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
      setNotice("Delivery confirmed. You can now rate the seller.");
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Could not accept order delivery"));
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
      setNotice(`Quote ${created.quote_number} sent.`);
    } catch (err) {
      setError(errorMessage(err, "Could not submit quotation"));
    }
  }

  async function handleSubmitRating(e: FormEvent, quoteId: string) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    try {
      await reviewsApi.rate(connectionId, quoteId, ratingForm);
      setNotice("Rating submitted.");
      setShowRatingModal(false);
      await reloadData();
    } catch (err) {
      setError(errorMessage(err, "Could not submit rating"));
    }
  }

  // Issue Reporting & Resolution Handlers
  async function handleReportIssueSubmit(e: FormEvent) {
    e.preventDefault();
    if (!issueForm.title.trim() || !issueForm.description.trim()) {
      setError("Please provide a title and description for the issue.");
      return;
    }

    const newIssue: DealIssue = {
      id: "issue_" + Date.now(),
      connection_id: connectionId,
      title: issueForm.title.trim(),
      category: issueForm.category,
      severity: issueForm.severity,
      description: issueForm.description.trim(),
      suggested_resolution: issueForm.suggested_resolution.trim() || undefined,
      status: "open",
      reported_by: myId,
      reporter_name: "You",
      created_at: new Date().toISOString(),
    };

    const updated = [newIssue, ...issues];
    saveIssues(updated);
    setShowIssueModal(false);
    setIssueForm({
      title: "",
      category: "quality",
      severity: "medium",
      description: "",
      suggested_resolution: "",
    });
    setNotice("Issue reported.");

    // Send real-time notification to the chat thread
    if (open) {
      try {
        const notifyMsg = `⚠️ [DEAL ISSUE REPORTED: ${newIssue.category.toUpperCase()} - ${newIssue.severity.toUpperCase()}]\nTitle: ${newIssue.title}\nDetails: ${newIssue.description}${newIssue.suggested_resolution ? `\nSuggested Resolution: ${newIssue.suggested_resolution}` : ""}`;
        const created = await connApi.sendMessage(connectionId, notifyMsg);
        setMessages((curr) => [...curr, created]);
      } catch {
        // Non-blocking
      }
    }
  }

  async function handleResolveIssue(issueId: string) {
    const entered = await prompt({
      title: "Mark this issue resolved?",
      message: "Both sides will see it as resolved. You can reopen it later.",
      label: "How was it resolved? (optional)",
      multiline: true,
      confirmLabel: "Mark resolved",
    });
    if (entered === null) return;
    const resolutionNotes = entered;
    const issueToResolve = issues.find((i) => i.id === issueId);
    const updated = issues.map((item) =>
      item.id === issueId
        ? {
          ...item,
          status: "resolved" as const,
          resolved_at: new Date().toISOString(),
          resolution_notes: resolutionNotes || "Issue marked as resolved by counterparty.",
        }
        : item
    );
    saveIssues(updated);
    setNotice(`Issue marked as resolved.`);

    if (open && issueToResolve) {
      try {
        const resolveMsg = `✓ [DEAL ISSUE RESOLVED]\n"${issueToResolve.title}" has been marked as resolved.${resolutionNotes ? ` Note: ${resolutionNotes}` : ""}`;
        const created = await connApi.sendMessage(connectionId, resolveMsg);
        setMessages((curr) => [...curr, created]);
      } catch {
        // Non-blocking
      }
    }
  }

  function handleReopenIssue(issueId: string) {
    const updated = issues.map((item) =>
      item.id === issueId
        ? {
          ...item,
          status: "open" as const,
          resolved_at: undefined,
        }
        : item
    );
    saveIssues(updated);
    setNotice(`Issue reopened.`);
  }

  // Escrow Vault Handlers
  async function handleFundEscrow() {
    setFundingEscrow(true);
    try {
      const updated = await escrowApi.fundEscrow(connectionId, { payment_method: "mock_instant" });
      setEscrowAccount(updated);
      setShowFundModal(false);
      setNotice(`${money(updated.currency, updated.funded_amount)} deposited into escrow.`);
      void reloadData();
    } catch (err) {
      setError(`Escrow deposit failed: ${errorMessage(err)}`);
    } finally {
      setFundingEscrow(false);
    }
  }

  async function handleRequestMilestonePayout(milestoneId: string) {
    setRequestingMilestoneId(milestoneId);
    try {
      const updated = await escrowApi.requestMilestoneRelease(connectionId, milestoneId, {
        proof_note: milestoneProofNote || "Work completed as agreed.",
      });
      setEscrowAccount(updated);
      setShowMilestoneProofModal(null);
      setMilestoneProofNote("");
      setNotice("Release requested. Waiting for the buyer to approve.");
      void reloadData();
    } catch (err) {
      setError(`Request failed: ${errorMessage(err)}`);
    } finally {
      setRequestingMilestoneId(null);
    }
  }

  async function handleReleaseMilestone(milestoneId: string) {
    const milestone = escrowAccount?.milestones.find((m) => m.id === milestoneId);
    const ok = await confirm({
      title: "Release this payment?",
      message: milestone && escrowAccount
        ? `${money(escrowAccount.currency, milestone.amount)} for "${milestone.title}" will be paid to the seller. This can't be undone.`
        : "The milestone amount will be paid to the seller. This can't be undone.",
      confirmLabel: "Release payment",
    });
    if (!ok) return;
    setReleasingMilestoneId(milestoneId);
    try {
      const updated = await escrowApi.releaseMilestoneFunds(connectionId, milestoneId, {
        note: "Approved by buyer after inspection.",
      });
      setEscrowAccount(updated);
      setNotice("Payment released to the seller.");
      void reloadData();
    } catch (err) {
      setError(`Release failed: ${errorMessage(err)}`);
    } finally {
      setReleasingMilestoneId(null);
    }
  }

  async function handleRaiseFormalDispute(e: FormEvent) {
    e.preventDefault();
    if (!issueForm.title.trim() || !issueForm.description.trim()) {
      setError("Please provide a dispute title and detailed reason.");
      return;
    }
    try {
      const dispute = await escrowApi.openDispute(connectionId, {
        title: issueForm.title.trim(),
        category: issueForm.category,
        reason: issueForm.description.trim(),
        severity: issueForm.severity,
        suggested_resolution: issueForm.suggested_resolution.trim() || undefined,
      });
      setShowDisputeModal(false);
      setIssueForm({ title: "", category: "quality", severity: "medium", description: "", suggested_resolution: "" });
      setError(`Formal Dispute opened: "${dispute.title}". Escrow releases are frozen.`);
      void reloadData();
    } catch (err) {
      setError(`Failed to open dispute: ${errorMessage(err)}`);
    }
  }

  async function handleResolveDisputeAction(disputeId: string, resolution: "release_funds" | "refund_buyer" | "mutual_settlement") {
    const labels = {
      release_funds: "release the held funds to the seller",
      refund_buyer: "refund the held funds to the buyer",
      mutual_settlement: "record a mutual settlement and resume payments",
    } as const;
    const entered = await prompt({
      title: "Resolve this dispute?",
      message: `This will ${labels[resolution]}.`,
      label: "Settlement notes",
      placeholder: "What did both sides agree?",
      multiline: true,
      confirmLabel: "Resolve dispute",
    });
    if (entered === null) return;
    const notes = entered || "Agreed settlement";
    try {
      await escrowApi.resolveDispute(connectionId, disputeId, { resolution, resolution_notes: notes });
      setNotice("Dispute resolved.");
      void reloadData();
    } catch (err) {
      setError(`Failed to resolve dispute: ${errorMessage(err)}`);
    }
  }


  // Live Camera Handlers
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
    } catch {
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
      videoRef.current.play().catch(() => { });
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

    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

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
    setCameraError(null);
    try {
      const newMsg = await connApi.sendLiveCapture(connectionId, capturedBlob, cameraCaption);
      setMessages((prev) => {
        if (prev.some((m) => m.id === newMsg.id)) return prev;
        return [...prev, newMsg];
      });
      handleCloseCamera();
      setNotice("Photo sent.");
    } catch (err) {
      setCameraError(errorMessage(err, "Failed to upload live snapshot"));
    } finally {
      setSending(false);
    }
  }

  const latestQuote = quotes[0] ?? null;
  const isBuyer =
    connection.rfq_role === "buyer"
      ? connection.direction === "received"
      : connection.rfq_role === "seller"
        ? connection.direction === "sent"
        : latestQuote
          ? !latestQuote.is_sender
          : true;

  const myReview = quoteReviews.find((r) => r.reviewer_id === myId);
  const counterpartyReview = quoteReviews.find((r) => r.reviewee_id === myId);
  const buyerHasReviewed = isBuyer ? Boolean(myReview) : Boolean(counterpartyReview);
  const sellerHasReviewed = isBuyer ? Boolean(counterpartyReview) : Boolean(myReview);

  const openIssuesCount = issues.filter((i) => i.status === "open").length;

  return (
    <>
      <div className="chat-deal-split">
        {/* ========================================================================= */}
        {/* 2. MIDDLE COLUMN: Real-Time Chat UI */}
        {/* ========================================================================= */}
        <div className="chat-main-panel">
          <div className="chat-main-header">
            <div className="chat-header-info">
              <button
                type="button"
                className="icon-btn mobile-back-btn"
                onClick={onBackToUsers}
                aria-label="Back to conversations"
              >
                <IconChevronLeft size={18} />
              </button>

              <div className="user-avatar-circle" aria-hidden="true">
                {getInitials(counterpartyName)}
              </div>

              <div className="chat-header-names">
                <div className="chat-header-title-row">
                  <h2>{counterpartyName}</h2>
                  {connection.status !== "accepted" && (
                    <span className={`badge badge-${connection.status}`}>{statusLabel(connection)}</span>
                  )}
                </div>
                <p className="chat-header-sub">
                  {connection.rfq_title && <span title={connection.rfq_title}>{connection.rfq_title}</span>}
                  {connection.counterparty.email && (
                    <a href={`mailto:${connection.counterparty.email}`}>{connection.counterparty.email}</a>
                  )}
                  {connection.counterparty.phone && (
                    <a href={`tel:${connection.counterparty.phone}`}>{connection.counterparty.phone}</a>
                  )}
                </p>
              </div>
            </div>

            <div className="chat-header-actions">
              <div className="chat-translation-header-widget" role="group" aria-label="Translation">
                <button
                  type="button"
                  className={`small-btn translation-toggle-chip ${autoTranslate ? "active" : ""}`}
                  onClick={handleToggleAutoTranslate}
                  aria-pressed={autoTranslate}
                  title={
                    autoTranslate
                      ? "Incoming messages are translated automatically. Click to turn off."
                      : "Translate incoming messages automatically"
                  }
                >
                  <IconLanguages size={15} />
                  <span className="hide-sm">Auto-translate</span>
                </button>
                <select
                  id="dealroom-target-lang"
                  className="translation-quick-select"
                  value={targetLang}
                  onChange={(e) => handleChangeTargetLang(e.target.value)}
                  aria-label="Translate into"
                  title="Translate into"
                >
                  {supportedLangs.map((lang) => (
                    <option key={lang.code} value={lang.code}>
                      {lang.native_name}
                    </option>
                  ))}
                </select>
              </div>

              <button
                type="button"
                className="secondary small-btn mobile-deal-btn"
                onClick={onOpenDeal}
              >
                <IconFileText size={15} />
                Deal
                {openIssuesCount > 0 && <span className="filter-count">{openIssuesCount}</span>}
              </button>
            </div>
          </div>

          {connection.status === "pending" && connection.direction === "received" && onAcceptConnection && (
            <div className="pending-connection-banner">
              <div className="pending-banner-info">
                <strong>{counterpartyName} wants to connect</strong>
                <span>
                  About {connection.rfq_title ? <em>{connection.rfq_title}</em> : "your listing"}. Accepting
                  shares your contact details and opens the chat.
                </span>
              </div>
              <div className="pending-banner-actions">
                {onRejectConnection && (
                  <button
                    type="button"
                    className="secondary small-btn"
                    disabled={busyDecide}
                    onClick={async () => {
                      setBusyDecide(true);
                      try {
                        await onRejectConnection(connection.id);
                      } finally {
                        setBusyDecide(false);
                      }
                    }}
                  >
                    Decline
                  </button>
                )}
                <button
                  type="button"
                  className="primary small-btn"
                  disabled={busyDecide}
                  onClick={async () => {
                    setBusyDecide(true);
                    try {
                      await onAcceptConnection(connection.id);
                    } finally {
                      setBusyDecide(false);
                    }
                  }}
                >
                  Accept
                </button>
              </div>
            </div>
          )}

          {connection.status === "pending" && connection.direction === "sent" && (
            <div className="pending-connection-banner sent">
              <div className="pending-banner-info">
                <strong>Waiting for {counterpartyName} to accept</strong>
                <span>
                  You can message each other once they accept your request
                  {connection.rfq_title ? (
                    <>
                      {" "}about <em>{connection.rfq_title}</em>
                    </>
                  ) : null}
                  .
                </span>
              </div>
            </div>
          )}

          {shipment && (
            <div className="deal-shipment-strip">
              <div className="deal-shipment-strip-left">
                <IconTruck size={15} />
                <span className="strip-carrier-tag">{shipment.carrier_name}</span>
                <span className={`badge badge-${shipment.status === "delivered" ? "success" : "info"}`}>
                  {humanize(shipment.status)}
                </span>
                <span className="strip-route">
                  {shipment.origin_city} → {shipment.destination_city}
                </span>
              </div>
              <button
                type="button"
                className="link-button strip-btn-track"
                onClick={() => {
                  setRightTab("logistics");
                  onOpenDeal();
                }}
              >
                Track
              </button>
            </div>
          )}

          <div className="chat-thread" aria-label="Message thread">
            <div className="chat-scroll">
              {loading && (
                <div className="loading-state">
                  <span className="spinner" />
                  Loading conversation…
                </div>
              )}
              {!loading && messages.length === 0 && (
                <div className="chat-empty-state">
                  <p className="chat-empty-title">
                    {open ? `Start the conversation with ${counterpartyName}` : "Chat opens once the request is accepted"}
                  </p>
                  <p className="muted">
                    {open
                      ? "Agree on terms here, then send a formal quote from the deal panel."
                      : "Messages, quotes and delivery tracking all live in this room."}
                  </p>
                </div>
              )}

              {messages.map((message) => {
                const mine = message.sender_id === myId;
                const isIssueNotification = message.content && message.content.startsWith("⚠️ [DEAL ISSUE");
                const isResolveNotification = message.content && message.content.startsWith("✓ [DEAL ISSUE");
                const isSystem = Boolean(isIssueNotification || isResolveNotification);
                const translation = translations[message.id];
                const showingTranslation = Boolean(translation) && !showOriginal[message.id];

                return (
                  <div
                    key={message.id}
                    className={`chat-line ${mine ? "mine" : "theirs"} ${isSystem ? "system-notice-line" : ""}`}
                  >
                    <div className="chat-bubble">
                      {message.image_url && (
                        <figure className="chat-media-card">
                          <img
                            src={message.image_url}
                            alt="Live camera photo"
                            className="chat-media-img"
                            onClick={() => setZoomedImage(message.image_url ?? null)}
                            title="View full size"
                          />
                          <figcaption className="live-camera-badge">
                            <IconCamera size={12} /> Live photo
                          </figcaption>
                        </figure>
                      )}
                      {message.content && message.content !== "📸 Live Camera Snapshot" && (
                        <p className="chat-bubble-text">
                          {showingTranslation ? translation!.translated_text : message.content}
                        </p>
                      )}
                    </div>
                    <div className="chat-line-meta">
                      <time dateTime={message.created_at}>
                        {new Date(message.created_at).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
                      {translation ? (
                        <>
                          <span>
                            {showingTranslation
                              ? `Translated from ${translation.source_language.toUpperCase()}`
                              : "Original"}
                          </span>
                          <button
                            type="button"
                            className="link-button msg-meta-action"
                            onClick={() =>
                              setShowOriginal((prev) => ({ ...prev, [message.id]: !prev[message.id] }))
                            }
                          >
                            {showingTranslation ? "Show original" : "Show translation"}
                          </button>
                        </>
                      ) : (
                        !mine &&
                        !isSystem &&
                        message.content &&
                        message.content !== "📸 Live Camera Snapshot" && (
                          <button
                            type="button"
                            className="link-button msg-meta-action"
                            disabled={translatingIds.has(message.id)}
                            onClick={() => void handleTranslateMessage(message)}
                          >
                            {translatingIds.has(message.id) ? "Translating…" : "Translate"}
                          </button>
                        )
                      )}
                    </div>
                  </div>
                );
              })}

              {isCounterpartyTyping && (
                <div className="typing-indicator-row">
                  <span className="typing-dots" aria-hidden="true">
                    <span />
                    <span />
                    <span />
                  </span>
                  {counterpartyName} is typing…
                </div>
              )}
              <div ref={endRef} />
            </div>

            <div className="composer-bar">
              <form className="composer" onSubmit={handleSend}>
                <input
                  aria-label="Message"
                  value={text}
                  onChange={(event) => handleTextChange(event.target.value)}
                  placeholder={open ? `Message ${counterpartyName}` : "You can message once the request is accepted"}
                  maxLength={4000}
                  disabled={!open || sending}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void handleSend();
                    }
                  }}
                />
                <div className="composer-actions">
                  <button
                    type="button"
                    className="icon-btn composer-translate-btn"
                    title={`Translate your draft into ${targetLang.toUpperCase()}`}
                    aria-label={`Translate your draft into ${targetLang.toUpperCase()}`}
                    onClick={() => void handleTranslateComposer()}
                    disabled={!open || sending || translatingDraft || !text.trim()}
                  >
                    {translatingDraft ? <span className="spinner" /> : <IconLanguages size={17} />}
                  </button>
                  <button
                    type="button"
                    className="icon-btn camera-snap-btn"
                    title="Send a live camera photo"
                    aria-label="Send a live camera photo"
                    onClick={() => void openLiveCamera()}
                    disabled={!open || sending}
                  >
                    <IconCamera size={17} />
                  </button>
                  <button
                    type="submit"
                    className="ai-send-btn"
                    disabled={!open || sending || !text.trim()}
                    aria-label="Send message"
                  >
                    <IconSend size={15} />
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>

        {/* ========================================================================= */}
        {/* 3. RIGHT SIDEBAR: Negotiation Quotes & Issues */}
        {/* ========================================================================= */}
        <aside className="negotiation-issues-sidebar" aria-label="Deal Negotiation Quotes and Issues">
          <div className="right-sidebar-header">
            <div className="right-sidebar-title">
              <button
                type="button"
                className="icon-btn mobile-back-to-chat-btn"
                onClick={onBackToChat}
                aria-label="Back to chat"
              >
                <IconChevronLeft size={18} />
              </button>
              <h3>Deal</h3>
            </div>

            {open && (
              <button
                type="button"
                className="secondary small-btn issue-quote-btn"
                onClick={() => openNewQuoteModal()}
              >
                <IconPlus size={14} /> {latestQuote ? "New quote" : "Send quote"}
              </button>
            )}
          </div>

          <div className="right-sidebar-tabs" role="tablist" aria-label="Deal sections">
            {(
              [
                ["quotes", "Quote"],
                ["escrow", "Escrow"],
                ["logistics", "Shipping"],
                ["issues", "Issues"],
              ] as const
            ).map(([key, label]) => {
              const openDisputes = escrowAccount?.disputes?.filter((d) => d.status === "open").length ?? 0;
              const count = key === "issues" ? openIssuesCount + openDisputes : 0;
              return (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={rightTab === key}
                  className={`right-tab-btn ${rightTab === key ? "active" : ""}`}
                  onClick={() => setRightTab(key)}
                >
                  {label}
                  {count > 0 && <span className="tab-badge-pill">{count}</span>}
                </button>
              );
            })}
          </div>

          <div className="right-sidebar-scroll">
            {/* TAB 1: QUOTES & NEGOTIATION */}
            {rightTab === "quotes" && (
              <>
                {open && latestQuote ? (
                  <div className={`active-quote-card status-${latestQuote.status}`}>
                    <div className="quote-badge-row">
                      <span className="quote-number">{latestQuote.quote_number}</span>
                      <span className={`badge badge-${latestQuote.status}`}>{humanize(latestQuote.status)}</span>
                    </div>
                    {latestQuote.purchase_order_reference && (
                      <p className="quote-po">PO {latestQuote.purchase_order_reference}</p>
                    )}

                    <div className="quote-total-row">
                      <span className="quote-label">Total</span>
                      <strong className="quote-total">{money(latestQuote.currency, latestQuote.total_amount)}</strong>
                    </div>

                    <dl className="quote-details-grid">
                      <div>
                        <dt className="quote-label">Unit price</dt>
                        <dd>
                          {money(latestQuote.currency, latestQuote.unit_price)} / {latestQuote.quantity_unit}
                        </dd>
                      </div>
                      <div>
                        <dt className="quote-label">Quantity</dt>
                        <dd>
                          {formatNumber(latestQuote.quantity)} {latestQuote.quantity_unit}
                        </dd>
                      </div>
                      <div>
                        <dt className="quote-label">Incoterm</dt>
                        <dd>{latestQuote.incoterms ?? "FOB"}</dd>
                      </div>
                      <div>
                        <dt className="quote-label">Payment</dt>
                        <dd>{latestQuote.payment_terms || "Standard"}</dd>
                      </div>
                      {latestQuote.lead_time_days ? (
                        <div>
                          <dt className="quote-label">Lead time</dt>
                          <dd>{latestQuote.lead_time_days} days</dd>
                        </div>
                      ) : null}
                    </dl>

                    {latestQuote.notes && <p className="quote-notes">{latestQuote.notes}</p>}

                    <div className="quote-actions">
                      {latestQuote.status === "pending" && !latestQuote.is_sender && (
                        <div className="quote-action-row">
                          <button
                            type="button"
                            className="primary"
                            onClick={() => void handleAcceptQuote(latestQuote.id)}
                          >
                            <IconCheck size={14} /> Accept quote
                          </button>
                          <div className="quote-action-secondary">
                            <button
                              type="button"
                              className="secondary small-btn"
                              onClick={() => openNewQuoteModal(latestQuote)}
                            >
                              <IconSwap size={14} /> Counter
                            </button>
                            <button
                              type="button"
                              className="ghost small-btn danger-btn"
                              onClick={() => void handleRejectQuote(latestQuote.id)}
                            >
                              <IconX size={14} /> Decline
                            </button>
                          </div>
                        </div>
                      )}

                      {latestQuote.status === "pending" && latestQuote.is_sender && (
                        <p className="quote-waiting">
                          <IconClock size={14} /> Waiting for {counterpartyName} to respond
                        </p>
                      )}

                      {["accepted", "dispatched", "delivered", "received", "completed"].includes(latestQuote.status) && (
                        <>
                          <div className="order-fulfillment-stepper" aria-label="Order fulfillment progress">
                            <div className={`step-item ${["accepted", "dispatched", "delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                              <span className="step-badge">1</span>
                              <span className="step-label">Accepted</span>
                            </div>
                            <div className={`step-item ${["dispatched", "delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                              <span className="step-badge">2</span>
                              <span className="step-label">Dispatched</span>
                            </div>
                            <div className={`step-item ${["delivered", "received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                              <span className="step-badge">3</span>
                              <span className="step-label">Delivered</span>
                            </div>
                            <div className={`step-item ${["received", "completed"].includes(latestQuote.status) ? "active" : ""}`}>
                              <span className="step-badge">4</span>
                              <span className="step-label">Received</span>
                            </div>
                          </div>

                          <div className="quote-docs-download-row">
                            <span className="quote-label">Documents</span>
                            <button
                              type="button"
                              className="doc-dl-btn"
                              disabled={downloadingPo === latestQuote.id}
                              onClick={() => void handleDownloadPo(latestQuote.id, latestQuote.purchase_order_reference)}
                            >
                              <IconDownload size={14} />
                              {downloadingPo === latestQuote.id ? "Preparing…" : "Purchase order"}
                            </button>
                            <button
                              type="button"
                              className="doc-dl-btn"
                              disabled={downloadingInv === latestQuote.id}
                              onClick={() => void handleDownloadInvoice(latestQuote.id, latestQuote.quote_number)}
                            >
                              <IconDownload size={14} />
                              {downloadingInv === latestQuote.id ? "Preparing…" : "Tax invoice"}
                            </button>
                            {shipment && (
                              <button
                                type="button"
                                className="doc-dl-btn"
                                disabled={downloadingWaybill}
                                onClick={() => void handleDownloadWaybill(shipment.id, shipment.tracking_number)}
                              >
                                <IconDownload size={14} />
                                {downloadingWaybill ? "Preparing…" : "Waybill"}
                              </button>
                            )}
                          </div>
                        </>
                      )}


                      {latestQuote.status === "accepted" && (
                        <div className="delivery-action-banner">
                          {!isBuyer ? (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconTruck size={15} /> Dispatch order</strong>
                                <span className="muted small">Mark goods dispatched from warehouse.</span>
                              </div>
                              <button
                                type="button"
                                className="primary small-btn deliver-btn"
                                onClick={() => void handleDispatch(latestQuote.id)}
                              >
                                <IconTruck size={15} /> Mark dispatched
                              </button>
                            </>
                          ) : (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconClock size={15} /> Awaiting dispatch</strong>
                                <span className="muted small">Waiting for the seller to dispatch.</span>
                              </div>
                              <span className="badge badge-pending">Waiting</span>
                            </>
                          )}
                        </div>
                      )}

                      {latestQuote.status === "dispatched" && (
                        <div className="delivery-action-banner">
                          {!isBuyer ? (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconPackage size={15} /> Mark delivered</strong>
                                <span className="muted small">Update status once the consignment arrives.</span>
                              </div>
                              <button
                                type="button"
                                className="primary small-btn deliver-btn"
                                onClick={() => void handleMarkDelivered(latestQuote.id)}
                              >
                                <IconPackage size={15} /> Mark delivered
                              </button>
                            </>
                          ) : (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconTruck size={15} /> In transit</strong>
                                <span className="muted small">Order has been dispatched by the seller.</span>
                              </div>
                              <span className="badge badge-info">In transit</span>
                            </>
                          )}
                        </div>
                      )}

                      {latestQuote.status === "delivered" && (
                        <div className="delivery-action-banner">
                          {isBuyer ? (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconCheck size={15} /> Confirm delivery</strong>
                                <span className="muted small">Inspect the consignment and confirm acceptance.</span>
                              </div>
                              <button
                                type="button"
                                className="primary small-btn deliver-btn"
                                onClick={() => void handleAcceptDelivery(latestQuote.id)}
                              >
                                <IconCheck size={15} /> Accept delivery
                              </button>
                            </>
                          ) : (
                            <>
                              <div className="delivery-action-text">
                                <strong><IconClock size={15} /> Awaiting acceptance</strong>
                                <span className="muted small">Waiting for the buyer to inspect and confirm receipt.</span>
                              </div>
                              <span className="badge badge-pending">Waiting</span>
                            </>
                          )}
                        </div>
                      )}

                      {latestQuote.status === "received" && (
                        <div className="delivery-card-banner">
                          {isBuyer ? (
                            !myReview ? (
                              <>
                                <div className="delivery-action-text">
                                  <strong>Rate seller</strong>
                                  <span className="muted small">Delivery accepted. Complete your review.</span>
                                </div>
                                <button
                                  type="button"
                                  className="primary small-btn rate-btn"
                                  onClick={() => setShowRatingModal(true)}
                                >
                                  <IconStar size={15} /> Rate seller
                                </button>
                              </>
                            ) : (
                              <div className="delivery-action-text">
                                <strong>You rated the seller ({myReview.rating}★)</strong>
                                <span className="muted small">
                                  {sellerHasReviewed
                                    ? `Seller rated you (${counterpartyReview?.rating}★).`
                                    : "Waiting for the seller's rating."}
                                </span>
                              </div>
                            )
                          ) : !buyerHasReviewed ? (
                            <div className="waiting-buyer-notice delivery-action-text">
                              <strong>Awaiting buyer's review</strong>
                              <span className="muted small">Rating unlocks once the buyer submits their review.</span>
                            </div>
                          ) : !myReview ? (
                            <>
                              <div className="delivery-action-text">
                                <strong>Buyer reviewed delivery ({counterpartyReview?.rating}★)</strong>
                                <span className="muted small">Rate the buyer in return.</span>
                              </div>
                              <button
                                type="button"
                                className="primary small-btn rate-btn"
                                onClick={() => setShowRatingModal(true)}
                              >
                                <IconStar size={15} /> Rate buyer
                              </button>
                            </>
                          ) : (
                            <div className="delivery-action-text">
                              <strong>Mutual ratings complete ({myReview.rating}★)</strong>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {quotes.length > 1 && (
                      <button
                        type="button"
                        className="link-button quote-history-toggle"
                        onClick={() => setShowHistory(!showHistory)}
                      >
                        <IconHistory size={14} />
                        {showHistory ? "Hide earlier versions" : `${quotes.length - 1} earlier ${quotes.length === 2 ? "version" : "versions"}`}
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="deal-empty">
                    <strong>No quote yet</strong>
                    <p>
                      {open
                        ? "When you've agreed on terms in chat, send a formal quote with price, quantity and Incoterms."
                        : "Quotes become available once the connection is accepted."}
                    </p>
                    {open && (
                      <button type="button" className="primary small-btn" onClick={() => openNewQuoteModal()}>
                        <IconPlus size={14} /> Send quote
                      </button>
                    )}
                  </div>
                )}

                {/* Negotiation History Panel */}
                {showHistory && quotes.length > 1 && (
                  <div className="quote-history-panel">
                    <h3>Earlier versions</h3>
                    {quotes.map((q) => (
                      <div key={q.id} className="history-item">
                        <div className="history-head">
                          <strong>{q.quote_number}</strong>
                          <span className={`badge badge-${q.status}`}>{humanize(q.status)}</span>
                          <span className="muted">{formatDate(q.created_at)}</span>
                        </div>
                        <p>
                          {money(q.currency, q.unit_price)} × {formatNumber(q.quantity)} {q.quantity_unit} ={" "}
                          <strong>{money(q.currency, q.total_amount)}</strong>
                        </p>
                        {q.notes && <p className="muted small">{q.notes}</p>}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}

            {/* TAB 2: ESCROW VAULT & MILESTONES */}
            {rightTab === "escrow" && (
              <div className="escrow-tab-pane">
                <div className="escrow-header-bar">
                  <h4>Status</h4>
                  {escrowAccount && (
                    <span
                      className={`badge badge-${
                        escrowAccount.status === "disputed"
                          ? "rejected"
                          : escrowAccount.status === "funded" || escrowAccount.status === "completed"
                            ? "accepted"
                            : "pending"
                      }`}
                    >
                      {escrowAccount.status === "pending_deposit" ? "Awaiting deposit" : humanize(escrowAccount.status)}
                    </span>
                  )}
                </div>

                {!escrowAccount ? (
                  <div className="deal-empty">
                    <strong>Protect this deal with escrow</strong>
                    <p>
                      The buyer's payment is held until agreed milestones — like dispatch and delivery — are
                      met, then released to the seller.
                    </p>
                    <ol className="escrow-steps-box">
                      <li>Agree on a quote</li>
                      <li>Buyer deposits the payment</li>
                      <li>Seller dispatches the goods</li>
                      <li>Funds are released on delivery</li>
                    </ol>
                    {latestQuote && ["accepted", "delivered", "received", "completed"].includes(latestQuote.status) ? (
                      <button type="button" className="primary small-btn" onClick={() => void reloadData()}>
                        Set up escrow
                      </button>
                    ) : (
                      <button type="button" className="secondary small-btn" onClick={() => setRightTab("quotes")}>
                        Go to quote
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="escrow-active-container">
                    {/* Disputed Alert Banner */}
                    {escrowAccount.status === "disputed" && (
                      <div className="escrow-dispute-alert">
                        <div className="escrow-dispute-alert-head">
                          <IconAlert size={15} />
                          <strong>Payments paused</strong>
                        </div>
                        <p>A dispute is open. No milestone can be released until it's resolved.</p>
                        <button type="button" className="link-button" onClick={() => setRightTab("issues")}>
                          Review the dispute
                        </button>
                      </div>
                    )}

                    {/* Financial Summary Card */}
                    <div className="escrow-financial-card">
                      <div className="escrow-financial-header">
                        <span className="escrow-deal-title">Deal value</span>
                        <strong className="escrow-deal-amount">
                          {money(escrowAccount.currency, escrowAccount.total_amount)}
                        </strong>
                      </div>

                      <div className="escrow-metrics-grid">
                        <div className="escrow-metric-box metric-funded">
                          <span className="metric-label">Deposited</span>
                          <span className="metric-val">{money(escrowAccount.currency, escrowAccount.funded_amount)}</span>
                        </div>
                        <div className="escrow-metric-box metric-released">
                          <span className="metric-label">Released</span>
                          <span className="metric-val">{money(escrowAccount.currency, escrowAccount.released_amount)}</span>
                        </div>
                        <div className="escrow-metric-box metric-balance">
                          <span className="metric-label">Held</span>
                          <span className="metric-val">
                            {money(
                              escrowAccount.currency,
                              Number(escrowAccount.funded_amount) -
                                Number(escrowAccount.released_amount) -
                                Number(escrowAccount.refunded_amount),
                            )}
                          </span>
                        </div>
                      </div>

                      {/* Deposit CTA if pending deposit */}
                      {escrowAccount.status === "pending_deposit" && (
                        <div className="escrow-deposit-cta">
                          {isBuyer ? (
                            <>
                              <p className="deposit-cta-text">
                                Deposit the payment so the seller can start production with payment guaranteed.
                              </p>
                              <button
                                type="button"
                                className="primary fund-vault-action-btn"
                                onClick={() => setShowFundModal(true)}
                                disabled={fundingEscrow}
                              >
                                <IconLock size={15} />
                                {fundingEscrow ? "Processing…" : `Deposit ${money(escrowAccount.currency, escrowAccount.total_amount)}`}
                              </button>
                            </>
                          ) : (
                            <p className="deposit-cta-muted">
                              <IconClock size={14} /> Waiting for the buyer to deposit{" "}
                              {money(escrowAccount.currency, escrowAccount.total_amount)}.
                            </p>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Milestones List */}
                    <div className="escrow-milestones-section">
                      <div className="milestones-section-header">
                        <h5>Milestones</h5>
                      </div>

                      <div className="milestones-list">
                        {escrowAccount.milestones.map((m: EscrowMilestone) => {
                          const isFrozen = escrowAccount.status === "disputed";
                          const isReleased = m.status === "released";
                          const isReleaseRequested = m.status === "release_requested";
                          const isFunded = m.status === "funded";

                          return (
                            <div
                              key={m.id}
                              className={`milestone-item-card milestone-status-${m.status}`}
                            >
                              <div className="milestone-item-top">
                                <div className="milestone-title-group">
                                  <span className="milestone-order-badge">{m.order_index + 1}</span>
                                  <div>
                                    <strong className="milestone-title">{m.title}</strong>
                                    <div className="milestone-amount-line">
                                      {money(escrowAccount.currency, m.amount)} · {formatNumber(m.percentage)}%
                                    </div>
                                  </div>
                                </div>
                                <span
                                  className={`badge ${
                                    isReleased
                                      ? "badge-accepted"
                                      : isReleaseRequested
                                        ? "badge-countered"
                                        : isFunded
                                          ? "badge-draft"
                                          : isFrozen
                                            ? "badge-rejected"
                                            : "badge-pending"
                                  }`}
                                >
                                  {isReleased
                                    ? "Released"
                                    : isReleaseRequested
                                      ? "Release requested"
                                      : isFunded
                                        ? "Funded"
                                        : humanize(m.status)}
                                </span>
                              </div>

                              {m.release_note && (
                                <div className="milestone-note-bubble">
                                  {m.release_note}
                                </div>
                              )}

                              {isReleased && m.released_at && (
                                <div className="milestone-released-time">
                                  Released {formatDate(m.released_at)}
                                </div>
                              )}

                              {/* Milestone Actions */}
                              <div className="milestone-actions-row">
                                {!isReleased && (
                                  <>
                                    {!isBuyer && isFunded && (
                                      <button
                                        type="button"
                                        className="secondary small-btn"
                                        onClick={() => setShowMilestoneProofModal(m.id)}
                                        disabled={isFrozen || requestingMilestoneId === m.id}
                                      >
                                        {requestingMilestoneId === m.id ? "Requesting…" : "Request release"}
                                      </button>
                                    )}

                                    {isBuyer && (isFunded || isReleaseRequested) && (
                                      <button
                                        type="button"
                                        className="primary small-btn"
                                        onClick={() => void handleReleaseMilestone(m.id)}
                                        disabled={isFrozen || releasingMilestoneId === m.id}
                                      >
                                        <IconCheck size={13} />
                                        {releasingMilestoneId === m.id
                                          ? "Releasing…"
                                          : isReleaseRequested
                                            ? "Approve release"
                                            : "Release to seller"}
                                      </button>
                                    )}
                                  </>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* Escrow Dispute Trigger */}
                    <div className="escrow-bottom-actions">
                      {escrowAccount.status !== "disputed" && (
                        <button
                          type="button"
                          className="ghost small-btn danger-btn"
                          onClick={() => setShowDisputeModal(true)}
                        >
                          <IconAlert size={14} /> Open a dispute
                        </button>
                      )}
                      <p className="escrow-bottom-guarantee">
                        Funds stay in escrow until each milestone is released.
                      </p>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* TAB: LOGISTICS & MULTI-MODAL FREIGHT */}
            {rightTab === "logistics" && (
              <div className="logistics-tab-pane">
                {/* 1. Live Consignment Tracking Card (if shipment exists) */}
                {shipment ? (
                  <div className="consignment-tracking-card">
                    <div className="consignment-header-row">
                      <div className="consignment-carrier-info">
                        <span className="carrier-mode-icon" aria-hidden="true">
                          {shipment.shipping_mode === "courier" ? <IconPackage size={16} /> : <IconTruck size={16} />}
                        </span>
                        <div>
                          <div className="carrier-name-title">{shipment.carrier_name}</div>
                          <div className="tracking-number-sub">
                            {humanize(shipment.shipping_mode)} · {shipment.tracking_number}
                          </div>
                        </div>
                      </div>
                      <span className={`badge badge-${shipment.status === "delivered" ? "accepted" : "info"}`}>
                        {humanize(shipment.status)}
                      </span>
                    </div>

                    {/* Route Visualizer */}
                    <div className="logistics-route-box">
                      <div className="route-point">
                        <span className="route-dot origin-dot" />
                        <div>
                          <div className="route-city">{shipment.origin_city}</div>
                          <div className="route-country">{shipment.origin_country}</div>
                        </div>
                      </div>
                      <div className="route-path-line">
                        <span className="route-incoterm-chip">{shipment.incoterm}</span>
                      </div>
                      <div className="route-point destination-point">
                        <span className="route-dot destination-dot" />
                        <div>
                          <div className="route-city">{shipment.destination_city}</div>
                          <div className="route-country">{shipment.destination_country}</div>
                        </div>
                      </div>
                    </div>

                    {/* Cargo Specs Grid */}
                    <div className="cargo-specs-grid">
                      <div className="cargo-spec-item">
                        <span className="spec-label">Gross weight</span>
                        <span className="spec-value">{shipment.gross_weight_kg ? `${shipment.gross_weight_kg.toLocaleString()} kg` : "—"}</span>
                      </div>
                      <div className="cargo-spec-item">
                        <span className="spec-label">Chargeable</span>
                        <span className="spec-value">{shipment.chargeable_weight_kg ? `${shipment.chargeable_weight_kg.toLocaleString()} kg` : "—"}</span>
                      </div>
                      <div className="cargo-spec-item">
                        <span className="spec-label">Volume</span>
                        <span className="spec-value">{shipment.cbm ? `${shipment.cbm} CBM` : "—"}</span>
                      </div>
                      <div className="cargo-spec-item">
                        <span className="spec-label">Packages</span>
                        <span className="spec-value">{shipment.packages_count ? `${shipment.packages_count} pkgs` : "—"}</span>
                      </div>
                    </div>

                    {/* Milestone Progress Stepper */}
                    <div className="shipment-progress-tracker" aria-label="Shipment tracking progress">
                      {[
                        { key: "booked", label: "Booked" },
                        { key: "dispatched", label: "Dispatched" },
                        { key: "in_transit", label: "In transit" },
                        { key: "customs_cleared", label: "Customs" },
                        { key: "delivered", label: "Delivered" },
                      ].map((step, idx, arr) => {
                        const statusOrder = ["booked", "dispatched", "in_transit", "customs_hold", "customs_cleared", "out_for_delivery", "delivered"];
                        const currentIdx = statusOrder.indexOf(shipment.status);
                        const stepIdx = statusOrder.indexOf(step.key);
                        const isDone = currentIdx >= stepIdx;
                        const isCurrent = shipment.status === step.key || (step.key === "customs_cleared" && shipment.status === "customs_hold");

                        return (
                          <div key={step.key} className={`shipment-step ${isDone ? "completed" : ""} ${isCurrent ? "current" : ""}`}>
                            <div className="step-marker">{isDone ? <IconCheck size={11} /> : idx + 1}</div>
                            <div className="step-name">{step.label}</div>
                            {idx < arr.length - 1 && <div className={`step-connector ${isDone && currentIdx > stepIdx ? "filled" : ""}`} />}
                          </div>
                        );
                      })}
                    </div>

                    {/* Official PDF Document & Action Buttons */}
                    <div className="shipment-card-actions">
                      {!isBuyer && shipment.status !== "delivered" && (
                        <button
                          type="button"
                          className="primary small-btn"
                          onClick={() => setShowCheckpointModal(true)}
                        >
                          Update status
                        </button>
                      )}
                      <button
                        type="button"
                        className="secondary small-btn"
                        onClick={() => void handleDownloadWaybill(shipment.id, shipment.tracking_number)}
                        disabled={downloadingWaybill}
                      >
                        <IconDownload size={14} />
                        {downloadingWaybill ? "Preparing…" : "Consignment note"}
                      </button>
                    </div>

                    {/* Checkpoint Timeline */}
                    <div className="checkpoint-timeline-section">
                      <h5>Tracking history</h5>
                      <div className="checkpoint-timeline-list">
                        {(shipment.tracking_events || []).slice().reverse().map((evt, idx) => (
                          <div key={idx} className="checkpoint-timeline-item">
                            <div className="checkpoint-dot" />
                            <div className="checkpoint-content">
                              <div className="checkpoint-header">
                                <span className="checkpoint-status-badge">{humanize(evt.status)}</span>
                                <span className="checkpoint-time">
                                  {new Date(evt.timestamp).toLocaleString(undefined, {
                                    month: "short",
                                    day: "numeric",
                                    hour: "2-digit",
                                    minute: "2-digit",
                                  })}
                                </span>
                              </div>
                              {evt.location && (
                                <div className="checkpoint-location">
                                  <IconMapPin size={12} /> {evt.location}
                                </div>
                              )}
                              {evt.note && <div className="checkpoint-note">{evt.note}</div>}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="deal-empty">
                    <strong>Not shipped yet</strong>
                    <p>
                      Once the order is accepted, the seller dispatches it here and both sides can track it
                      step by step.
                    </p>
                    {!isBuyer && latestQuote?.status === "accepted" && (
                      <button
                        type="button"
                        className="primary small-btn dispatch-trigger-btn"
                        onClick={() => {
                          setDispatchForm((prev) => ({
                            ...prev,
                            quotation_id: latestQuote.id,
                            incoterm: latestQuote.incoterms || "FOB",
                          }));
                          setShowDispatchModal(true);
                        }}
                      >
                        <IconTruck size={15} /> Dispatch order
                      </button>
                    )}
                  </div>
                )}

                {/* 2. Interactive Multi-Modal Freight Calculator & Landed Cost Assistant */}
                <details className="freight-calculator-card" open={!shipment}>
                  <summary className="freight-calculator-header">
                    <h4>Estimate freight</h4>
                    <span className="muted small">Road, ocean, air and courier</span>
                  </summary>

                  <form onSubmit={(e) => void handleCalculateFreight(e)} className="freight-calc-form">
                    <div className="freight-grid freight-grid-2">
                      <div>
                        <label htmlFor="fc-origin-country" className="calc-input-label">From (country code)</label>
                        <input
                          id="fc-origin-country"
                          type="text"
                          className="form-input"
                          placeholder="e.g. IN"
                          value={freightForm.origin_country}
                          onChange={(e) => setFreightForm({ ...freightForm, origin_country: e.target.value.toUpperCase() })}
                          required
                        />
                      </div>
                      <div>
                        <label htmlFor="fc-dest-country" className="calc-input-label">To (country code)</label>
                        <input
                          id="fc-dest-country"
                          type="text"
                          className="form-input"
                          placeholder="e.g. NL"
                          value={freightForm.destination_country}
                          onChange={(e) => setFreightForm({ ...freightForm, destination_country: e.target.value.toUpperCase() })}
                          required
                        />
                      </div>
                    </div>

                    <div className="freight-grid freight-grid-3">
                      <div>
                        <label htmlFor="fc-weight" className="calc-input-label">Weight (kg)</label>
                        <input
                          id="fc-weight"
                          type="number"
                          step="0.1"
                          className="form-input"
                          placeholder="1000"
                          value={freightForm.gross_weight_kg || ""}
                          onChange={(e) => setFreightForm({ ...freightForm, gross_weight_kg: parseFloat(e.target.value) || 0 })}
                          required
                        />
                      </div>
                      <div>
                        <label htmlFor="fc-cbm" className="calc-input-label">Volume (m³)</label>
                        <input
                          id="fc-cbm"
                          type="number"
                          step="0.01"
                          className="form-input"
                          placeholder="1.2"
                          value={freightForm.cbm || ""}
                          onChange={(e) => setFreightForm({ ...freightForm, cbm: parseFloat(e.target.value) || 0 })}
                        />
                      </div>
                      <div>
                        <label htmlFor="fc-incoterm" className="calc-input-label">Incoterm</label>
                        <select
                          id="fc-incoterm"
                          className="form-input"
                          value={freightForm.incoterm || "FOB"}
                          onChange={(e) => setFreightForm({ ...freightForm, incoterm: e.target.value })}
                        >
                          {INCOTERMS_OPTIONS.map((term) => (
                            <option key={term} value={term}>{term}</option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <button
                      type="submit"
                      className="secondary small-btn btn-block"
                      disabled={calculatingFreight}
                    >
                      {calculatingFreight ? "Calculating…" : "Calculate rates"}
                    </button>
                  </form>

                  {/* Freight Estimate Rates Display */}
                  {freightEstimate && (
                    <div className="freight-estimate-results">
                      <div className="results-route-header">
                        <span>{freightEstimate.origin} &rarr; {freightEstimate.destination}</span>
                        <span>{freightEstimate.gross_weight_kg.toLocaleString()} kg · {freightEstimate.cbm} m³</span>
                      </div>

                      <div className="freight-rates-stack">
                        {freightEstimate.rates.map((rate) => (
                          <div
                            key={rate.mode}
                            className={`rate-option-card ${rate.recommended ? "recommended" : ""}`}
                          >
                            <div className="rate-card-top">
                              <div className="rate-card-mode">
                                <div>
                                  <strong>{rate.mode_label}</strong>
                                  <div className="rate-transit-days">
                                    {rate.transit_days_min}–{rate.transit_days_max} days in transit
                                  </div>
                                </div>
                              </div>
                              <div className="rate-card-total">
                                <div className="rate-total-amount">${rate.total_estimated_usd.toLocaleString()}</div>
                                {rate.recommended && <span className="rate-rec-badge">Best value</span>}
                              </div>
                            </div>
                            <div className="rate-breakdown-row">
                              <span>Freight: ${rate.base_freight_usd.toLocaleString()}</span>
                              <span>Fuel: ${rate.fuel_surcharge_usd}</span>
                              <span>Customs: ${rate.customs_clearance_usd}</span>
                              <span>Basis: {rate.basis}</span>
                            </div>
                          </div>
                        ))}
                      </div>

                      {/* Incoterms Cost Allocation Card */}
                      {freightEstimate.incoterm_breakdown && (
                        <div className="incoterms-breakdown-card">
                          <div className="incoterms-card-title">
                            <span>
                              {freightEstimate.incoterm_breakdown.incoterm} · {freightEstimate.incoterm_breakdown.full_name}
                            </span>
                          </div>
                          <div className="incoterm-allocation-grid">
                            <div className="allocation-party seller-party">
                              <span className="party-title">Seller pays for</span>
                              <p className="party-desc">{freightEstimate.incoterm_breakdown.seller_responsibility}</p>
                              <div className="party-cost-tag">About ${freightEstimate.incoterm_breakdown.estimated_seller_logistics_usd.toLocaleString()}</div>
                            </div>
                            <div className="allocation-party buyer-party">
                              <span className="party-title">Buyer pays for</span>
                              <p className="party-desc">{freightEstimate.incoterm_breakdown.buyer_responsibility}</p>
                              <div className="party-cost-tag">About ${freightEstimate.incoterm_breakdown.estimated_buyer_logistics_usd.toLocaleString()}</div>
                            </div>
                          </div>
                          <div className="risk-transfer-note">
                            <strong>Risk transfers:</strong> {freightEstimate.incoterm_breakdown.risk_transfer_point}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </details>
              </div>
            )}

            {/* TAB 3: ISSUES & DISPUTES */}
            {rightTab === "issues" && (
              <>
                {open && (issues.length > 0 || (escrowAccount?.disputes?.length ?? 0) > 0) && (
                <div className="issues-header-bar">
                  <h4>
                    {openIssuesCount > 0 ? `${openIssuesCount} open` : "All resolved"}
                  </h4>
                    <button
                      type="button"
                      className="secondary small-btn report-issue-btn"
                      onClick={() => setShowIssueModal(true)}
                    >
                      <IconPlus size={14} /> Report issue
                    </button>
                </div>
                )}

                {/* Formal Escrow Disputes List */}
                {escrowAccount?.disputes && escrowAccount.disputes.length > 0 && (
                  <div className="issue-group">
                    <h5 className="issue-group-title">Escrow disputes</h5>
                    <div className="issue-stack">
                      {escrowAccount.disputes.map((dispute: DealDispute) => (
                        <div
                          key={dispute.id}
                          className={`issue-card severity-${dispute.severity} status-${dispute.status}`}
                        >
                          <div className="issue-card-head">
                            <div className="issue-badges-row">
                              <span className={`issue-sev-badge issue-sev-${dispute.severity}`}>
                                {humanize(dispute.severity)}
                              </span>
                              <span className="issue-cat-tag">{dispute.category}</span>
                            </div>
                            <span
                              className={`badge ${dispute.status.startsWith("resolved") ? "badge-accepted" : "badge-rejected"}`}
                            >
                              {dispute.status.startsWith("resolved") ? (
                                <>
                                  <IconCheck size={12} /> {humanize(dispute.status.replace("resolved_", ""))}
                                </>
                              ) : (
                                "Payments paused"
                              )}
                            </span>
                          </div>

                          <div className="issue-title">{dispute.title}</div>
                          <p className="issue-desc">{dispute.reason}</p>

                          {dispute.suggested_resolution && (
                            <div className="issue-resolution-box">
                              <strong>Suggested resolution:</strong> {dispute.suggested_resolution}
                            </div>
                          )}

                          {dispute.resolution_notes && (
                            <div className="issue-resolution-box is-resolved">
                              <strong>Settlement:</strong> {dispute.resolution_notes}
                            </div>
                          )}

                          <div className="issue-card-footer issue-card-footer-stack">
                            <span>Opened {formatDate(dispute.created_at)}</span>

                            {dispute.status === "open" && (
                              <div className="issue-resolve-actions">
                                <span className="quote-label">Resolve by</span>
                                <button
                                  type="button"
                                  className="secondary small-btn"
                                  onClick={() => void handleResolveDisputeAction(dispute.id, "release_funds")}
                                  title="Release the held funds to the seller"
                                >
                                  Release to seller
                                </button>
                                <button
                                  type="button"
                                  className="secondary small-btn"
                                  onClick={() => void handleResolveDisputeAction(dispute.id, "refund_buyer")}
                                  title="Refund the held funds to the buyer"
                                >
                                  Refund buyer
                                </button>
                                <button
                                  type="button"
                                  className="secondary small-btn"
                                  onClick={() => void handleResolveDisputeAction(dispute.id, "mutual_settlement")}
                                  title="Record an agreed settlement and resume payments"
                                >
                                  Settle mutually
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {issues.length === 0 && (!escrowAccount?.disputes || escrowAccount.disputes.length === 0) ? (
                  <div className="deal-empty">
                    <strong>No issues</strong>
                    <p>If something goes wrong with quality, delivery, packaging or payment, log it here so both sides can track it.</p>
                    {open && (
                      <button type="button" className="secondary small-btn" onClick={() => setShowIssueModal(true)}>
                        Report an issue
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="issue-stack">
                    {issues.map((issue) => (
                      <div
                        key={issue.id}
                        className={`issue-card severity-${issue.severity} status-${issue.status}`}
                      >
                        <div className="issue-card-head">
                          <div className="issue-badges-row">
                            <span className={`issue-sev-badge issue-sev-${issue.severity}`}>
                              {humanize(issue.severity)}
                            </span>
                            <span className="issue-cat-tag">{issue.category}</span>
                          </div>
                          <span
                            className={`badge ${issue.status === "resolved" ? "badge-accepted" : "badge-rejected"
                              }`}
                          >
                            {issue.status === "resolved" ? (
                              <>
                                <IconCheck size={12} /> Resolved
                              </>
                            ) : (
                              "Open"
                            )}
                          </span>
                        </div>

                        <div className="issue-title">{issue.title}</div>
                        <p className="issue-desc">{issue.description}</p>

                        {issue.suggested_resolution && (
                          <div className="issue-resolution-box">
                            <strong>Proposed resolution:</strong> {issue.suggested_resolution}
                          </div>
                        )}

                        {issue.resolution_notes && (
                          <div className="issue-resolution-box is-resolved">
                            <strong>Resolution:</strong> {issue.resolution_notes}
                          </div>
                        )}

                        <div className="issue-card-footer">
                          <span>
                            {issue.reporter_name} · {formatDate(issue.created_at)}
                          </span>

                          <div>
                            {issue.status === "open" ? (
                              <button
                                type="button"
                                className="secondary small-btn"
                                onClick={() => void handleResolveIssue(issue.id)}
                              >
                                <IconCheck size={13} /> Mark resolved
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="ghost small-btn"
                                onClick={() => handleReopenIssue(issue.id)}
                              >
                                Reopen
                              </button>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

              </>
            )}
          </div>
        </aside>
      </div>

      {/* ========================================================================= */}
      {/* MODALS */}
      {/* ========================================================================= */}

      {/* 1. Quote / Counter-Offer Modal */}
      {showQuoteModal && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Send a quote</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowQuoteModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>

            <form onSubmit={handleSubmitQuote}>
              <div className="modal-row">
                <div>
                  <label htmlFor="quote-price">Unit price</label>
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
                  <span>Total</span>
                  <strong>
                    {money(quoteForm.currency, parseFloat(quoteForm.unit_price) * parseFloat(quoteForm.quantity))}
                  </strong>
                </div>
              )}

              <div className="modal-row">
                <div>
                  <label htmlFor="quote-incoterms">Incoterms</label>
                  <select
                    id="quote-incoterms"
                    value={quoteForm.incoterms}
                    onChange={(e) =>
                      setQuoteForm({ ...quoteForm, incoterms: e.target.value as Incoterm })
                    }
                  >
                    {INCOTERMS_OPTIONS.map((term) => (
                      <option key={term} value={term}>
                        {term}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="quote-lead">Lead time (days)</label>
                  <input
                    id="quote-lead"
                    type="number"
                    min="1"
                    value={quoteForm.lead_time_days}
                    onChange={(e) => setQuoteForm({ ...quoteForm, lead_time_days: e.target.value })}
                  />
                  <small className="field-hint">Until goods are ready, not including transport.</small>
                </div>
              </div>

              <div>
                <label htmlFor="quote-payment">Payment terms</label>
                <input
                  id="quote-payment"
                  placeholder="e.g. Net 30 or 50% Advance"
                  value={quoteForm.payment_terms}
                  onChange={(e) => setQuoteForm({ ...quoteForm, payment_terms: e.target.value })}
                />
              </div>

              <div>
                <label htmlFor="quote-notes">Notes <span className="optional">Optional</span></label>
                <textarea
                  id="quote-notes"
                  rows={2}
                  placeholder="Packaging, quality or delivery details"
                  value={quoteForm.notes}
                  onChange={(e) => setQuoteForm({ ...quoteForm, notes: e.target.value })}
                />
              </div>

              <div className="modal-actions">
                <button type="button" className="secondary" onClick={() => setShowQuoteModal(false)}>
                  Cancel
                </button>
                <button type="submit">Send quote</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 2. Report Issue / Concern Modal */}
      {showIssueModal && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Report an issue</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowIssueModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>

            <p className="muted">{counterpartyName} is notified in the chat when you submit.</p>

            <form onSubmit={handleReportIssueSubmit}>
              <div>
                <label htmlFor="issue-title">What's wrong?</label>
                <input
                  id="issue-title"
                  type="text"
                  required
                  placeholder="e.g. Packaging damaged in transit"
                  value={issueForm.title}
                  maxLength={120}
                  onChange={(e) => setIssueForm({ ...issueForm, title: e.target.value })}
                />
              </div>

              <div className="modal-row">
                <div>
                  <label htmlFor="issue-cat">Category</label>
                  <select
                    id="issue-cat"
                    value={issueForm.category}
                    onChange={(e) =>
                      setIssueForm({ ...issueForm, category: e.target.value as DealIssueCategory })
                    }
                  >
                    <option value="quality">Quality &amp; Specifications</option>
                    <option value="delivery">Delivery &amp; Logistics Delay</option>
                    <option value="packaging">Packaging / Handling Damage</option>
                    <option value="payment">Payment &amp; Invoicing Terms</option>
                    <option value="documentation">Certifications &amp; Customs Docs</option>
                    <option value="other">Other Commercial Concern</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="issue-sev">Severity</label>
                  <select
                    id="issue-sev"
                    value={issueForm.severity}
                    onChange={(e) =>
                      setIssueForm({ ...issueForm, severity: e.target.value as DealIssueSeverity })
                    }
                  >
                    <option value="low">Low — minor</option>
                    <option value="medium">Medium — needs clarification</option>
                    <option value="high">High — action needed</option>
                  </select>
                </div>
              </div>

              <div>
                <label htmlFor="issue-desc">Details</label>
                <textarea
                  id="issue-desc"
                  rows={3}
                  required
                  placeholder="Describe the problem so the other side can act on it"
                  value={issueForm.description}
                  maxLength={1000}
                  onChange={(e) => setIssueForm({ ...issueForm, description: e.target.value })}
                />
              </div>

              <div>
                <label htmlFor="issue-res">Suggested fix <span className="optional">Optional</span></label>
                <input
                  id="issue-res"
                  type="text"
                  placeholder="e.g. 5% discount or a replacement batch"
                  value={issueForm.suggested_resolution}
                  maxLength={250}
                  onChange={(e) => setIssueForm({ ...issueForm, suggested_resolution: e.target.value })}
                />
              </div>

              <div className="modal-actions">
                <button type="button" className="secondary" onClick={() => setShowIssueModal(false)}>
                  Cancel
                </button>
                <button type="submit">Report issue</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 3. Rating & Review Modal */}
      {showRatingModal && latestQuote && (
        <div className="modal-backdrop" onClick={() => setShowRatingModal(false)}>
          <div className="modal-card rating-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Rate the {isBuyer ? "seller" : "buyer"}</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowRatingModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <p className="muted">
              {latestQuote.quote_number}
              {latestQuote.purchase_order_reference ? ` · PO ${latestQuote.purchase_order_reference}` : ""}
            </p>

            <form onSubmit={(e) => void handleSubmitRating(e, latestQuote.id)}>
              <div className="rating-field">
                <span className="field-label">Overall</span>
                <div className="star-picker">
                  {[1, 2, 3, 4, 5].map((star) => (
                    <button
                      type="button"
                      key={star}
                      className={ratingForm.rating >= star ? "star filled" : "star"}
                      onClick={() => setRatingForm({ ...ratingForm, rating: star })}
                      aria-label={`${star} star${star > 1 ? "s" : ""}`}
                      aria-pressed={ratingForm.rating >= star}
                    >
                      ★
                    </button>
                  ))}
                  <span className="star-label">{ratingForm.rating} of 5</span>
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
                  <label htmlFor="r-deliv">Delivery</label>
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
                  <label htmlFor="r-qual">Quality</label>
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
                <label htmlFor="r-comment">Comments <span className="optional">Optional</span></label>
                <textarea
                  id="r-comment"
                  rows={3}
                  placeholder="How did the deal go?"
                  value={ratingForm.comment ?? ""}
                  onChange={(e) => setRatingForm({ ...ratingForm, comment: e.target.value })}
                />
              </div>

              <div className="modal-actions">
                <button type="button" className="secondary" onClick={() => setShowRatingModal(false)}>
                  Cancel
                </button>
                <button type="submit">Submit rating</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 4. Live Camera Viewfinder Modal */}
      {showCameraModal && (
        <div className="modal-overlay">
          <div className="modal-card camera-modal-card">
            <div className="camera-modal-header">
              <div className="camera-header-title">
                <h3>Send a live photo</h3>
              </div>
              <button type="button" className="close-btn" onClick={handleCloseCamera} aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>

            <p className="muted camera-disclaimer">
              Photos must be taken live, not uploaded, so the other side knows the stock is real.
            </p>

            {cameraError && (
              <div
                className={`camera-error-banner ${
                  cameraError.includes("Content Warning") || cameraError.includes("flagged")
                    ? "camera-moderation-warning"
                    : ""
                }`}
              >
                <IconAlert size={16} />
                <div className="moderation-warning-content">
                  <strong>
                    {cameraError.includes("Content Warning")
                      ? "This photo can't be sent"
                      : "Something went wrong"}
                  </strong>
                  <p>{cameraError}</p>
                  {(cameraError.includes("Content Warning") || cameraError.includes("flagged")) && (
                    <div className="moderation-retake-tip">
                      Photos with explicit or violent content are blocked. Retake the photo to continue.
                    </div>
                  )}
                </div>
              </div>
            )}

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
                    <span className="hud-watermark-overlay">
                      <span className="status-dot is-danger" /> Live
                    </span>
                  </div>
                </>
              ) : (
                <div className="camera-preview-box">
                  <img
                    src={capturedPreviewUrl}
                    alt="Live capture snapshot"
                    className={`captured-preview-img ${
                      cameraError && (cameraError.includes("Content Warning") || cameraError.includes("flagged"))
                        ? "preview-flagged-blur"
                        : ""
                    }`}
                  />
                  {cameraError && (cameraError.includes("Content Warning") || cameraError.includes("flagged")) ? (
                    <div className="preview-blocked-badge">Blocked</div>
                  ) : (
                    <div className="preview-watermark-badge">
                      <IconCheck size={12} /> Live photo
                    </div>
                  )}
                </div>
              )}
            </div>

            {capturedPreviewUrl && (
              <div className="camera-caption-input">
                <label htmlFor="cam-caption">Caption <span className="optional">Optional</span></label>
                <input
                  id="cam-caption"
                  type="text"
                  placeholder="e.g. Sample batch ready in the warehouse"
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
                    <IconCamera size={15} /> Take photo
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className={`secondary ${cameraError && (cameraError.includes("Content Warning") || cameraError.includes("flagged")) ? "retake-highlight-btn" : ""}`}
                    onClick={handleRetakePhoto}
                    disabled={sending}
                  >
                    Retake
                  </button>
                  <button
                    type="button"
                    className="primary send-snap-btn"
                    onClick={() => void handleSendLivePhoto()}
                    disabled={sending}
                  >
                    {sending ? "Checking and sending…" : "Send photo"}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 5. Lightbox Zoom Modal */}
      {zoomedImage && (
        <div className="lightbox-overlay" onClick={() => setZoomedImage(null)}>
          <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="lightbox-close"
              onClick={() => setZoomedImage(null)}
              aria-label="Close"
            >
              <IconClose size={18} />
            </button>
            <img src={zoomedImage} alt="Expanded snapshot" className="lightbox-img" />
            <div className="lightbox-footer">
              <IconCamera size={14} /> Taken live in the deal room
            </div>
          </div>
        </div>
      )}

      {/* 6. Escrow Deposit Modal */}
      {showFundModal && escrowAccount && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Deposit into escrow</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowFundModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <div className="modal-body">
              <div className="escrow-modal-summary">
                <p>
                  Deposit <strong>{money(escrowAccount.currency, escrowAccount.total_amount)}</strong>. It stays
                  in escrow and is released to the seller milestone by milestone.
                </p>
                <ul className="escrow-modal-points">
                  <li>The seller is guaranteed payment once each milestone is met.</li>
                  <li>You're protected if the goods don't arrive as agreed.</li>
                  <li>Either side can open a dispute to pause payments.</li>
                </ul>
              </div>
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setShowFundModal(false)}
                disabled={fundingEscrow}
              >
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => void handleFundEscrow()}
                disabled={fundingEscrow}
              >
                {fundingEscrow ? "Depositing…" : `Deposit ${money(escrowAccount.currency, escrowAccount.total_amount)}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 7. Milestone Proof / Payout Request Modal */}
      {showMilestoneProofModal && escrowAccount && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Request milestone release</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => {
                  setShowMilestoneProofModal(null);
                  setMilestoneProofNote("");
                }}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <div className="modal-body">
              {(() => {
                const milestone = escrowAccount.milestones.find((m) => m.id === showMilestoneProofModal);
                return (
                  <div>
                    <p className="modal-lead">
                      Ask the buyer to release <strong>{money(escrowAccount.currency, milestone?.amount || 0)}</strong> for{" "}
                      <strong>{milestone?.title}</strong>.
                    </p>
                    <label htmlFor="proof-note">What's been completed?</label>
                    <textarea
                      id="proof-note"
                      rows={4}
                      className="form-textarea"
                      placeholder="e.g. Inspection passed, goods dispatched under B/L TR-88219"
                      value={milestoneProofNote}
                      onChange={(e) => setMilestoneProofNote(e.target.value)}
                    />
                  </div>
                );
              })()}
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setShowMilestoneProofModal(null);
                  setMilestoneProofNote("");
                }}
                disabled={Boolean(requestingMilestoneId)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => void handleRequestMilestonePayout(showMilestoneProofModal)}
                disabled={Boolean(requestingMilestoneId)}
              >
                {requestingMilestoneId ? "Sending…" : "Request release"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 8. Formal Dispute Modal */}
      {showDisputeModal && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Open a dispute</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowDisputeModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <form onSubmit={(e) => void handleRaiseFormalDispute(e)}>
              <div className="modal-body">
                <p className="alert alert-warning">
                  <IconAlert size={16} />
                  <span>Opening a dispute pauses every unreleased payment until it's resolved.</span>
                </p>
                <div>
                  <label htmlFor="dispute-title">
                    What's the problem?
                  </label>
                  <input
                    id="dispute-title"
                    type="text"
                    className="form-input"
                    placeholder="e.g. Batch quality out of tolerance / Delay in dispatch"
                    value={issueForm.title}
                    onChange={(e) => setIssueForm({ ...issueForm, title: e.target.value })}
                    required
                  />
                </div>
                <div className="modal-row">
                  <div>
                    <label htmlFor="dispute-cat">
                      Category
                    </label>
                    <select
                      id="dispute-cat"
                      className="form-input"
                      value={issueForm.category}
                      onChange={(e) => setIssueForm({ ...issueForm, category: e.target.value as DealIssueCategory })}
                    >
                      <option value="quality">Quality &amp; Specifications</option>
                      <option value="delivery">Dispatch &amp; Delivery Delay</option>
                      <option value="packaging">Packaging &amp; Damage</option>
                      <option value="payment">Payment &amp; Billing</option>
                      <option value="documentation">Documentation / Inspection</option>
                      <option value="other">Other Concern</option>
                    </select>
                  </div>
                  <div>
                    <label htmlFor="dispute-sev">
                      Severity
                    </label>
                    <select
                      id="dispute-sev"
                      className="form-input"
                      value={issueForm.severity}
                      onChange={(e) => setIssueForm({ ...issueForm, severity: e.target.value as DealIssueSeverity })}
                    >
                      <option value="low">Low — minor variance</option>
                      <option value="medium">Medium — commercial impact</option>
                      <option value="high">High — deal at risk</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label htmlFor="dispute-desc">
                    Details and evidence
                  </label>
                  <textarea
                    id="dispute-desc"
                    rows={3}
                    className="form-textarea"
                    placeholder="Provide specific details, defect counts, deviation from agreed specifications..."
                    value={issueForm.description}
                    onChange={(e) => setIssueForm({ ...issueForm, description: e.target.value })}
                    required
                  />
                </div>
                <div>
                  <label htmlFor="dispute-res">
                    Suggested fix <span className="optional">Optional</span>
                  </label>
                  <input
                    id="dispute-res"
                    type="text"
                    className="form-input"
                    placeholder="e.g. 15% discount on remaining milestone, or expedited replacement shipment"
                    value={issueForm.suggested_resolution}
                    onChange={(e) => setIssueForm({ ...issueForm, suggested_resolution: e.target.value })}
                  />
                </div>
              </div>
              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowDisputeModal(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="danger">
                  Open dispute
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 9. Consignment Dispatch Modal */}
      {showDispatchModal && (
        <div className="modal-overlay">
          <div className="modal-card modal-card-wide">
            <div className="modal-header">
              <h3>Dispatch order</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowDispatchModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <form onSubmit={(e) => void handleDispatchShipment(e)}>
              <div className="modal-body">
                <div className="modal-row">
                  <div>
                    <label htmlFor="disp-carrier">
                      Carrier
                    </label>
                    <input
                      id="disp-carrier"
                      type="text"
                      className="form-input"
                      placeholder="e.g. DHL Global Forwarding / Maersk"
                      value={dispatchForm.carrier_name}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, carrier_name: e.target.value })}
                      required
                    />
                  </div>
                  <div>
                    <label htmlFor="disp-mode">
                      Mode
                    </label>
                    <select
                      id="disp-mode"
                      className="form-input"
                      value={dispatchForm.shipping_mode}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, shipping_mode: e.target.value as ShippingMode })}
                    >
                      <option value="ocean">Ocean freight</option>
                      <option value="air">Air cargo</option>
                      <option value="road">Road freight</option>
                      <option value="courier">Express courier</option>
                    </select>
                  </div>
                </div>

                <div className="modal-row">
                  <div>
                    <label htmlFor="disp-track">
                      Tracking or B/L number
                    </label>
                    <input
                      id="disp-track"
                      type="text"
                      className="form-input"
                      placeholder="e.g. TRK-88291048-IN"
                      value={dispatchForm.tracking_number}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, tracking_number: e.target.value })}
                      required
                    />
                  </div>
                  <div>
                    <label htmlFor="disp-incoterm">
                      Incoterm
                    </label>
                    <select
                      id="disp-incoterm"
                      className="form-input"
                      value={dispatchForm.incoterm || "FOB"}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, incoterm: e.target.value })}
                    >
                      {INCOTERMS_OPTIONS.map((t) => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="modal-row">
                  <div>
                    <label htmlFor="disp-origin">
                      From
                    </label>
                    <div className="city-country-pair">
                      <input
                        id="disp-origin-city"
                        type="text"
                        className="form-input"
                        placeholder="City (e.g. Mumbai)"
                        value={dispatchForm.origin_city}
                        onChange={(e) => setDispatchForm({ ...dispatchForm, origin_city: e.target.value })}
                        required
                      />
                      <input
                        id="disp-origin-country"
                        type="text"
                        aria-label="Country code"
                        className="form-input"
                        placeholder="IN"
                        value={dispatchForm.origin_country}
                        onChange={(e) => setDispatchForm({ ...dispatchForm, origin_country: e.target.value.toUpperCase() })}
                        required
                      />
                    </div>
                  </div>
                  <div>
                    <label htmlFor="disp-dest">
                      To
                    </label>
                    <div className="city-country-pair">
                      <input
                        id="disp-dest-city"
                        type="text"
                        className="form-input"
                        placeholder="City (e.g. Rotterdam)"
                        value={dispatchForm.destination_city}
                        onChange={(e) => setDispatchForm({ ...dispatchForm, destination_city: e.target.value })}
                        required
                      />
                      <input
                        id="disp-dest-country"
                        type="text"
                        aria-label="Country code"
                        className="form-input"
                        placeholder="NL"
                        value={dispatchForm.destination_country}
                        onChange={(e) => setDispatchForm({ ...dispatchForm, destination_country: e.target.value.toUpperCase() })}
                        required
                      />
                    </div>
                  </div>
                </div>

                <div className="modal-row modal-row-3">
                  <div>
                    <label htmlFor="disp-weight">
                      Weight (kg)
                    </label>
                    <input
                      id="disp-weight"
                      type="number"
                      step="0.1"
                      className="form-input"
                      value={dispatchForm.gross_weight_kg || ""}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, gross_weight_kg: parseFloat(e.target.value) || 0 })}
                    />
                  </div>
                  <div>
                    <label htmlFor="disp-cbm">
                      Volume (m³)
                    </label>
                    <input
                      id="disp-cbm"
                      type="number"
                      step="0.01"
                      className="form-input"
                      value={dispatchForm.cbm || ""}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, cbm: parseFloat(e.target.value) || 0 })}
                    />
                  </div>
                  <div>
                    <label htmlFor="disp-pkgs">
                      Packages
                    </label>
                    <input
                      id="disp-pkgs"
                      type="number"
                      className="form-input"
                      value={dispatchForm.packages_count || ""}
                      onChange={(e) => setDispatchForm({ ...dispatchForm, packages_count: parseInt(e.target.value, 10) || 0 })}
                    />
                  </div>
                </div>

                <div>
                  <label htmlFor="disp-customs">
                    Customs declaration or seal no. <span className="optional">Optional</span>
                  </label>
                  <input
                    id="disp-customs"
                    type="text"
                    className="form-input"
                    placeholder="e.g. EXP-2026-99201"
                    value={dispatchForm.customs_declaration_no || ""}
                    onChange={(e) => setDispatchForm({ ...dispatchForm, customs_declaration_no: e.target.value })}
                  />
                </div>

                {/* Auto trigger escrow milestone */}
                <div className="checkbox-row">
                  <input
                    id="disp-trigger-escrow"
                    type="checkbox"
                    checked={Boolean(dispatchForm.trigger_escrow_milestone)}
                    onChange={(e) => setDispatchForm({ ...dispatchForm, trigger_escrow_milestone: e.target.checked })}
                  />
                  <label htmlFor="disp-trigger-escrow">
                    Request the dispatch milestone payment from escrow
                    <span className="field-hint">Sends the B/L as proof for the 40% dispatch payment.</span>
                  </label>
                </div>
              </div>
              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowDispatchModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={dispatching}
                >
                  {dispatching ? "Dispatching…" : "Dispatch and issue waybill"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 10. Update Tracking Checkpoint Modal */}
      {showCheckpointModal && (
        <div className="modal-overlay">
          <div className="modal-card">
            <div className="modal-header">
              <h3>Update shipment status</h3>
              <button
                type="button"
                className="close-btn"
                onClick={() => setShowCheckpointModal(false)}
              aria-label="Close">
                <IconClose size={18} />
              </button>
            </div>
            <form onSubmit={(e) => void handleAddCheckpoint(e)}>
              <div className="modal-body">
                <div>
                  <label htmlFor="cp-status">
                    Status
                  </label>
                  <select
                    id="cp-status"
                    className="form-input"
                    value={checkpointForm.status}
                    onChange={(e) => setCheckpointForm({ ...checkpointForm, status: e.target.value as ShipmentStatus })}
                  >
                    <option value="in_transit">In transit</option>
                    <option value="customs_hold">Held at customs</option>
                    <option value="customs_cleared">Cleared customs</option>
                    <option value="out_for_delivery">Out for delivery</option>
                    <option value="delivered">Delivered</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="cp-loc">
                    Location <span className="optional">Optional</span>
                  </label>
                  <input
                    id="cp-loc"
                    type="text"
                    className="form-input"
                    placeholder="e.g. Rotterdam Port, Terminal 3"
                    value={checkpointForm.location || ""}
                    onChange={(e) => setCheckpointForm({ ...checkpointForm, location: e.target.value })}
                  />
                </div>

                <div>
                  <label htmlFor="cp-note">
                    Notes <span className="optional">Optional</span>
                  </label>
                  <textarea
                    id="cp-note"
                    rows={3}
                    className="form-textarea"
                    placeholder="e.g. Container cleared phytosanitary customs check and transferred to local carrier."
                    value={checkpointForm.note || ""}
                    onChange={(e) => setCheckpointForm({ ...checkpointForm, note: e.target.value })}
                  />
                </div>
              </div>
              <div className="modal-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setShowCheckpointModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={addingCheckpoint}
                >
                  {addingCheckpoint ? "Saving…" : "Update status"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}


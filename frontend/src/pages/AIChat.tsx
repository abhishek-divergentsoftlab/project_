import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  aiChat,
  matching,
  connections,
  rfqs as rfqApi,
  type AIChatMessage,
  type AIChatChunk,
  type AIToolStep,
  type AICreatedRFQ,
  type AICounterpartyMessage,
  type MatchCandidate,
  type AgentInfo,
} from "@/api/endpoints";
import { MarkdownPreview } from "@/components/MarkdownPreview";
import { CounterpartyChatPane } from "@/components/CounterpartyChatPane";
import {
  IconCheck,
  IconClose,
  IconMessages,
  IconSend,
  IconShield,
  IconSparkles,
} from "@/components/icons";
import { useSidebarData } from "@/context/SidebarDataContext";
import { useFeedback } from "@/context/useFeedback";
import { stripLeadingEmoji } from "@/utils/format";

interface MessageItem extends AIChatMessage {
  id: string;
  timestamp: string;
  thinking?: string;
  thinkingAfter?: string;
  toolStep?: AIToolStep | null;
  createdRfq?: AICreatedRFQ | null;
  counterpartyMessage?: AICounterpartyMessage | null;
  isStreaming?: boolean;
}

export interface RfqMatchState {
  loading: boolean;
  error: string | null;
  candidates: MatchCandidate[];
  total: number;
  isOpen: boolean;
}

const MATCH_PROMPT_CHIPS = [
  { label: "Emails of the top 3", prompt: "give me mail id of top three" },
  { label: "Which suits best?", prompt: "which one suits best from this top three" },
  { label: "Compare prices", prompt: "compare prices and quantities of top suppliers" },
  { label: "Closest supplier", prompt: "who is the closest supplier in distance?" },
];

const SUGGESTED_PROMPTS = [
  { label: "Buy apples in Indore", prompt: "I want to buy apple in Indore" },
  { label: "Source 500 corrugated boxes", prompt: "I want to buy 500 corrugated boxes" },
  { label: "Sell stainless steel pipes", prompt: "I am a seller of industrial stainless steel pipes" },
  { label: "Negotiate payment terms", prompt: "How can I negotiate better payment terms with a supplier?" },
  { label: "Supplier KYC checklist", prompt: "What are the key supplier KYC and verification checks?" },
  { label: "FOB vs CIF vs EXW", prompt: "What is the difference between FOB, CIF, and EXW shipping terms?" },
];

const INITIAL_GREETING: MessageItem = {
  id: "initial-greeting",
  role: "assistant",
  content:
    "Hello — I'm your B2B business assistant. I can help draft RFQs, negotiate with suppliers, work through pricing, and clarify trade terms.\n\nAre you looking to **buy** or **sell**? Or describe your request directly, for example: *I want to buy apples in Indore*.",
  timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
};

function MatchCartView({
  rfqId,
  rfqRole,
  matchState,
  connectedMap,
  connectingRfqId,
  onConnect,
  onClose,
  onPromptClick,
}: {
  rfqId: string;
  rfqRole?: string;
  matchState?: RfqMatchState;
  connectedMap: Record<string, boolean>;
  connectingRfqId: string | null;
  onConnect: (targetRfqId: string) => void;
  onClose: () => void;
  onPromptClick: (prompt: string) => void;
}) {
  if (!matchState || !matchState.isOpen) return null;

  const targetLabel = rfqRole === "buyer" ? "suppliers" : "buyers";

  return (
    <div id={`matches-cart-${rfqId}`} className="ai-chat-matches-cart">
      <div className="ai-matches-cart-header">
        <h5 className="ai-matches-cart-title">
          {matchState.loading ? `Finding ${targetLabel}…` : `${matchState.total} matching ${targetLabel}`}
        </h5>
        <div className="ai-matches-header-actions">
          <Link to={`/rfqs/${rfqId}/matches`} className="ai-matches-header-link">
            See all
          </Link>
          <button
            type="button"
            className="icon-btn ai-matches-header-close"
            onClick={onClose}
            aria-label="Hide matches"
            title="Hide matches"
          >
            <IconClose size={15} />
          </button>
        </div>
      </div>

      {matchState.loading && (
        <div className="ai-matches-loading">
          <span className="spinner" />
          Scoring live listings against your RFQ…
        </div>
      )}

      {matchState.error && <p className="error">{matchState.error}</p>}

      {!matchState.loading && !matchState.error && matchState.candidates.length === 0 && (
        <div className="ai-matches-empty">
          No matching {targetLabel} yet. New listings are matched as they're posted.
        </div>
      )}

      {!matchState.loading && matchState.candidates.length > 0 && (
        <>
          <ul className="ai-match-cards-list">
            {matchState.candidates.map((candidate) => {
              const scorePct = Math.round((candidate.score?.total ?? 0) * 100);
              const where = [
                candidate.location?.city,
                candidate.location?.state,
                candidate.location?.country,
              ]
                .filter(Boolean)
                .slice(0, 2)
                .join(", ");
              const status = candidate.counterparty.connection_status;
              const isConnected =
                connectedMap[candidate.rfq_id] || status === "accepted" || status === "pending";

              return (
                <li key={candidate.rfq_id} className="ai-chat-match-card">
                  <span className="ai-match-card-score-box" title="Match score">
                    {scorePct}%
                  </span>
                  <div className="ai-match-card-main-info">
                    <span className="ai-match-card-title">{candidate.title}</span>
                    <span className="ai-match-card-company">
                      {candidate.counterparty.company_name || "Unnamed company"}
                      {candidate.counterparty.gst_verified && (
                        <span className="ai-match-verified" title="GST verified">
                          <IconShield size={12} />
                        </span>
                      )}
                      {where ? ` · ${where}` : ""}
                    </span>
                    <span className="ai-match-facts">
                      <span>
                        {candidate.price
                          ? `${candidate.price.amount.toLocaleString()} ${candidate.price.currency}${candidate.price.per_unit ? `/${candidate.price.per_unit}` : ""}`
                          : "Price negotiable"}
                      </span>
                      {candidate.quantity && (
                        <span>
                          {candidate.quantity.value.toLocaleString()} {candidate.quantity.unit}
                        </span>
                      )}
                      {candidate.distance_km != null && (
                        <span>{Math.round(candidate.distance_km).toLocaleString()} km</span>
                      )}
                      {candidate.counterparty.email && (
                        <a
                          href={`mailto:${candidate.counterparty.email}`}
                          className="ai-match-email-link"
                        >
                          {candidate.counterparty.email}
                        </a>
                      )}
                    </span>
                  </div>
                  <div className="ai-match-card-action">
                    {isConnected ? (
                      <span className="ai-match-connected-pill">
                        {status === "accepted" ? "Connected" : "Requested"}
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="secondary small-btn"
                        disabled={connectingRfqId === candidate.rfq_id}
                        onClick={() => onConnect(candidate.rfq_id)}
                      >
                        {connectingRfqId === candidate.rfq_id ? "Connecting…" : "Connect"}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="ai-match-query-suggestions">
            {MATCH_PROMPT_CHIPS.map((chip) => (
              <button
                key={chip.prompt}
                type="button"
                className="ai-prompt-chip"
                onClick={() => onPromptClick(chip.prompt)}
              >
                {chip.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function AIChat() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { toast, confirm } = useFeedback();
  const { refreshAiSessions } = useSidebarData();
  const urlConvId = searchParams.get("c");
  const isNewParam = searchParams.get("new");
  const urlPrompt = searchParams.get("prompt");

  // Starts empty even when the URL names a conversation: the effect below
  // loads it only when the two differ, so seeding it from the URL meant a
  // reload or deep link showed an empty chat.
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);

  const [activeConnectionId, setActiveConnectionId] = useState<string | null>(null);
  const [isSplitView, setIsSplitView] = useState<boolean>(false);
  const [lastAiDispatchedMessage, setLastAiDispatchedMessage] = useState<AICounterpartyMessage | null>(null);

  const [messages, setMessages] = useState<MessageItem[]>([INITIAL_GREETING]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [currentRfq, setCurrentRfq] = useState<Record<string, any>>({});
  const [readiness, setReadiness] = useState<Record<string, any> | null>(null);
  const [, setLastCreatedRfq] = useState<AICreatedRFQ | null>(null);
  const [rfqMatches, setRfqMatches] = useState<Record<string, RfqMatchState>>({});
  const [activeMatchedCandidates, setActiveMatchedCandidates] = useState<MatchCandidate[]>([]);
  const [connectingRfqId, setConnectingRfqId] = useState<string | null>(null);
  const [connectedMap, setConnectedMap] = useState<Record<string, boolean>>({});
  const urlAgentParam = searchParams.get("agent");
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>(urlAgentParam || "general");
  const activeConversationIdRef = useRef<string | null>(null);
  const loadingRef = useRef<boolean>(false);
  const currentRfqRef = useRef<Record<string, any>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (urlAgentParam) {
      setSelectedAgentId(urlAgentParam);
    }
  }, [urlAgentParam]);

  useEffect(() => {
    aiChat
      .listAgents()
      .then((data) => {
        if (data && data.length > 0) {
          setAgents(data);
        }
      })
      .catch((err) => {
        console.error("Failed to load agents:", err);
      });
  }, []);

  const currentAgent = agents.find((a) => a.id === selectedAgentId) || null;
  const activeSuggestedPrompts =
    currentAgent && currentAgent.suggested_prompts && currentAgent.suggested_prompts.length > 0
      ? currentAgent.suggested_prompts
      : SUGGESTED_PROMPTS;

  const handleSelectAgent = (agentId: string) => {
    setSelectedAgentId(agentId);
    const ag = agents.find((a) => a.id === agentId);
    if (messages.length <= 1 && ag) {
      setMessages([
        {
          id: `greeting-${Date.now()}`,
          role: "assistant",
          content: `**${ag.name}** — ${ag.description}\n\nHow can I help with ${ag.short_description.toLowerCase()}?`,
          timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        },
      ]);
    }
  };

  const loadConversation = async (id: string) => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setLoading(false);
    loadingRef.current = false;
    try {
      const detail = await aiChat.getConversation(id);
      activeConversationIdRef.current = detail.id;
      setActiveConversationId(detail.id);
      setSearchParams({ c: detail.id }, { replace: true });

      if (detail.messages && detail.messages.length > 0) {
        const mapped: MessageItem[] = detail.messages.map((m) => ({
          id: m.id,
          role: m.role as "user" | "assistant",
          content: m.content,
          thinking: m.thinking || "",
          thinkingAfter: m.thinking_after || "",
          toolStep: m.tool_step || null,
          createdRfq: m.created_rfq || null,
          counterpartyMessage: m.counterparty_message || null,
          timestamp: new Date(m.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        }));
        setMessages(mapped);

        const lastRfqMsg = [...detail.messages].reverse().find((m) => m.created_rfq);
        if (lastRfqMsg && lastRfqMsg.created_rfq) {
          setLastCreatedRfq(lastRfqMsg.created_rfq);
        } else {
          setLastCreatedRfq(null);
        }

        const lastCpMsg = [...detail.messages].reverse().find((m) => m.counterparty_message);
        if (lastCpMsg && lastCpMsg.counterparty_message) {
          setLastAiDispatchedMessage(lastCpMsg.counterparty_message);
          setActiveConnectionId(lastCpMsg.counterparty_message.connection_id);
          setIsSplitView(true);
        }
      } else {
        setMessages([INITIAL_GREETING]);
        setLastCreatedRfq(null);
      }

      const state = detail.state || {};
      if (state.active_connection_id) {
        setActiveConnectionId(state.active_connection_id);
        setIsSplitView(true);
      }
      const rfqDraft = state.rfq_draft || {};
      const readinessState = state.readiness || null;
      currentRfqRef.current = rfqDraft;
      setCurrentRfq(rfqDraft);
      setReadiness(readinessState);
      const savedMatches = state.matched_candidates || null;
      if (savedMatches && Array.isArray(savedMatches) && savedMatches.length > 0) {
        setActiveMatchedCandidates(savedMatches);
        if (detail.rfq_id) {
          setRfqMatches((prev) => ({
            ...prev,
            [detail.rfq_id!]: {
              loading: false,
              error: null,
              candidates: savedMatches,
              total: savedMatches.length,
              isOpen: true,
            },
          }));
        }
      } else {
        setActiveMatchedCandidates([]);
        setRfqMatches({});
      }
    } catch (err) {
      console.error("Failed to load conversation details:", err);
    }
  };

  useEffect(() => {
    if (isNewParam) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("new");
      setSearchParams(nextParams, { replace: true });
      handleNewChat();
      return;
    }
    if (urlConvId) {
      if (urlConvId !== activeConversationIdRef.current) {
        void loadConversation(urlConvId);
      }
    } else if (activeConversationIdRef.current && !loadingRef.current) {
      handleNewChat();
    }
  }, [urlConvId, isNewParam]);

  useEffect(() => {
    if (urlPrompt && !activeConversationId && messages.length <= 1) {
      setInput(urlPrompt);
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("prompt");
      setSearchParams(nextParams, { replace: true });
    }
  }, [urlPrompt, activeConversationId, messages.length, searchParams, setSearchParams]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [input]);

  function handleNewChat() {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setLoading(false);
    loadingRef.current = false;
    activeConversationIdRef.current = null;
    setActiveConversationId(null);
    setSearchParams({}, { replace: true });
    currentRfqRef.current = {};
    setCurrentRfq({});
    setReadiness(null);
    setLastCreatedRfq(null);
    setActiveMatchedCandidates([]);
    setRfqMatches({});
    setConnectedMap({});
    setConnectingRfqId(null);
    setInput("");
    setMessages([
      {
        ...INITIAL_GREETING,
        id: `greeting-${Date.now()}`,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      },
    ]);
    setTimeout(() => textareaRef.current?.focus(), 50);
  }

  async function handleCloseCreatedRfq(rfqId: string, rfqTitle?: string) {
    const ok = await confirm({
      title: rfqTitle ? `Close "${rfqTitle}"?` : "Close this RFQ?",
      message:
        "It stops appearing in matches and the marketplace. Existing conversations stay open.",
      confirmLabel: "Close RFQ",
      tone: "danger",
    });
    if (!ok) return;

    try {
      await rfqApi.close(rfqId);
      toast("RFQ closed successfully.");
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.createdRfq?.id === rfqId) {
            return {
              ...msg,
              createdRfq: { ...msg.createdRfq, status: "closed" },
            };
          }
          return msg;
        }),
      );
      setRfqMatches((prev) => ({
        ...prev,
        [rfqId]: { ...(prev[rfqId] || {}), isOpen: false, candidates: [], total: 0, loading: false, error: null },
      }));
    } catch (err) {
      toast("Could not close the RFQ.", "error");
    }
  }

  const handleToggleMatches = async (rfqId: string, _role?: string) => {
    const existing = rfqMatches[rfqId];
    if (existing && existing.candidates.length > 0) {
      const nextOpen = !existing.isOpen;
      setRfqMatches((prev) => ({
        ...prev,
        [rfqId]: { ...prev[rfqId], isOpen: nextOpen },
      }));
      if (nextOpen) {
        setActiveMatchedCandidates(existing.candidates);
        setTimeout(() => {
          const el = document.getElementById(`matches-cart-${rfqId}`);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }, 80);
      }
      return;
    }

    setRfqMatches((prev) => ({
      ...prev,
      [rfqId]: {
        loading: true,
        error: null,
        candidates: [],
        total: 0,
        isOpen: true,
      },
    }));

    try {
      const resp = await matching.forRfq(rfqId, 10, 0);
      const candidates = resp.results || [];
      setRfqMatches((prev) => ({
        ...prev,
        [rfqId]: {
          loading: false,
          error: null,
          candidates,
          total: resp.total || candidates.length,
          isOpen: true,
        },
      }));
      setActiveMatchedCandidates(candidates);
      setTimeout(() => {
        const el = document.getElementById(`matches-cart-${rfqId}`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 80);
    } catch (err) {
      console.error("Failed to load matches for RFQ:", err);
      setRfqMatches((prev) => ({
        ...prev,
        [rfqId]: {
          loading: false,
          error: "Could not fetch matching counterparties. Please try again.",
          candidates: [],
          total: 0,
          isOpen: true,
        },
      }));
    }
  };

  const handleConnectCandidate = async (targetRfqId: string) => {
    setConnectingRfqId(targetRfqId);
    try {
      const conn = await connections.create(targetRfqId);
      setConnectedMap((prev) => ({ ...prev, [targetRfqId]: true }));
      if (conn?.id) {
        setActiveConnectionId(conn.id);
        setIsSplitView(true);
      }
    } catch (err) {
      console.error("Failed to connect candidate:", err);
    } finally {
      setConnectingRfqId(null);
    }
  };


  function handleStop() {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setLoading(false);
    loadingRef.current = false;
    setMessages((prev) =>
      prev.map((msg) => (msg.isStreaming ? { ...msg, isStreaming: false } : msg))
    );
  }

  async function handleSend(textToSend?: string) {
    const query = (textToSend ?? input).trim();
    if (!query || loading) return;

    const userMessage: MessageItem = {
      id: `user-${Date.now()}`,
      role: "user",
      content: query,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    };

    const newHistory = [...messages, userMessage];
    const aiMessageId = `ai-${Date.now()}`;
    const initialAiMessage: MessageItem = {
      id: aiMessageId,
      role: "assistant",
      content: "",
      thinking: "",
      thinkingAfter: "",
      toolStep: null,
      createdRfq: null,
      counterpartyMessage: null,
      isStreaming: true,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    };

    setMessages([...newHistory, initialAiMessage]);
    if (!textToSend) setInput("");
    loadingRef.current = true;
    setLoading(true);

    const payloadMessages: AIChatMessage[] = newHistory.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    let currentConvId = activeConversationIdRef.current || activeConversationId;

    try {
      await aiChat.streamMessage(
        payloadMessages,
        (chunk: AIChatChunk) => {
          if (chunk.conversation_id && !currentConvId) {
            currentConvId = chunk.conversation_id;
            activeConversationIdRef.current = chunk.conversation_id;
            setActiveConversationId(chunk.conversation_id);
            setSearchParams({ c: chunk.conversation_id }, { replace: true });
          }

          if (chunk.rfq_draft && Object.keys(chunk.rfq_draft).length > 0) {
            currentRfqRef.current = chunk.rfq_draft;
            setCurrentRfq(chunk.rfq_draft);
          }
          if (chunk.readiness) {
            setReadiness(chunk.readiness);
          }

          if (chunk.created_rfq) {
            setLastCreatedRfq(chunk.created_rfq);
          }

          if (chunk.counterparty_message) {
            setLastAiDispatchedMessage(chunk.counterparty_message);
            if (chunk.counterparty_message.connection_id) {
              setActiveConnectionId(chunk.counterparty_message.connection_id);
              setIsSplitView(true);
            }
          }

          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.id !== aiMessageId) return msg;
              const nextContent = (msg.content || "") + (chunk.content || "");
              const nextThinking = (msg.thinking || "") + (chunk.thinking || "");
              const nextThinkingAfter = (msg.thinkingAfter || "") + (chunk.thinking_after || "");
              const nextToolStep = chunk.tool_step || msg.toolStep || null;
              const nextCreatedRfq = chunk.created_rfq || msg.createdRfq || null;
              const nextCounterpartyMsg = chunk.counterparty_message || msg.counterpartyMessage || null;
              return {
                ...msg,
                content: nextContent,
                thinking: nextThinking,
                thinkingAfter: nextThinkingAfter,
                toolStep: nextToolStep,
                createdRfq: nextCreatedRfq,
                counterpartyMessage: nextCounterpartyMsg,
                isStreaming: !chunk.done,
              };
            })
          );

          if (chunk.done) {
            void refreshAiSessions();
          }
        },
        currentRfqRef.current,
        abortController.signal,
        activeConversationId,
        activeMatchedCandidates.length > 0 ? activeMatchedCandidates : null,
        activeConnectionId,
        selectedAgentId === "general" ? null : selectedAgentId,
      );
    } catch (err: unknown) {
      if (abortController.signal.aborted) {
        return;
      }
      const errorMessage =
        err instanceof Error
          ? err.message
          : "Connection interrupted. Please try again.";

      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== aiMessageId) return msg;
          const currentContent = msg.content ? `${msg.content}\n\n` : "";
          return {
            ...msg,
            content: `${currentContent}**Connection interrupted:** ${errorMessage}`,
            isStreaming: false,
          };
        })
      );
    } finally {
      loadingRef.current = false;
      setLoading(false);
      abortControllerRef.current = null;
      setMessages((prev) =>
        prev.map((msg) => (msg.id === aiMessageId ? { ...msg, isStreaming: false } : msg))
      );
      void refreshAiSessions();
      setTimeout(() => textareaRef.current?.focus(), 50);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const hasDraft = Object.keys(currentRfq).length > 0;
  const showEmptyHero = messages.length <= 1 && !loading;
  const showSuggestions = messages.length <= 2 && !loading && !showEmptyHero;
  const isSpecialist = Boolean(currentAgent && currentAgent.id !== "general");

  const draftChips: string[] = [];
  if (currentRfq.category && currentRfq.product_details?.name) draftChips.push(currentRfq.category);
  if (currentRfq.location?.city) draftChips.push(currentRfq.location.city);
  if (currentRfq.quantity?.value) {
    draftChips.push(`${currentRfq.quantity.value} ${currentRfq.quantity.unit || ""}`.trim());
  }
  if (currentRfq.price_target?.amount) {
    draftChips.push(
      `${currentRfq.price_target.currency || "INR"} ${currentRfq.price_target.amount}${currentRfq.price_target.per_unit ? ` / ${currentRfq.price_target.per_unit}` : ""}`,
    );
  }
  if (currentRfq.deadline?.date || currentRfq.deadline?.in_days) {
    draftChips.push(currentRfq.deadline.date || `${currentRfq.deadline.in_days} days`);
  }

  return (
    <div className="ai-chat-layout full-width-layout">
      <div className={`ai-chat-main ${isSplitView ? "dual-chat-layout" : ""}`}>
        <div className="ai-chat-container">
          <header className="ai-chat-header">
            <div className="ai-chat-header-info">
              {agents.length === 0 && <h2 className="ai-chat-title">Business AI</h2>}
              {agents.length > 0 && (
                <select
                  className="ai-agent-select"
                  value={selectedAgentId}
                  onChange={(e) => handleSelectAgent(e.target.value)}
                  aria-label="Assistant"
                  title={currentAgent?.description}
                >
                  {agents.map((ag) => (
                    <option key={ag.id} value={ag.id}>
                      {ag.name}
                    </option>
                  ))}
                </select>
              )}
              {loading && (
                <span className="ai-header-live-dot" title="Responding">
                  <span className="spinner" />
                </span>
              )}
            </div>

            <div className="ai-chat-header-actions">
              {loading && (
                <button type="button" className="secondary small-btn" onClick={handleStop}>
                  <span className="stop-square" />
                  Stop
                </button>
              )}
              <button
                type="button"
                className={`small-btn ai-split-toggle-btn ${isSplitView ? "secondary active" : "ghost"}`}
                onClick={() => setIsSplitView((p) => !p)}
                aria-pressed={isSplitView}
                title={isSplitView ? "Hide the counterparty chat" : "Chat with a counterparty side by side"}
              >
                <IconMessages size={15} />
                <span className="hide-sm">Counterparty chat</span>
              </button>
              {/* <button
                type="button"
                className="ghost small-btn"
                onClick={handleNewChat}
                title="Start a new conversation"
              >
                <IconPlus size={15} />
                <span className="hide-sm">New chat</span>
              </button> */}
            </div>
          </header>

          {hasDraft && draftChips.length > 0 && (
            <div className="ai-rfq-ribbon" aria-label="RFQ draft">
              <span className="ai-rfq-ribbon-label">Draft RFQ</span>
              <div className="ai-rfq-details-tags">
                {draftChips.map((chip) => (
                  <span key={chip} className="ai-rfq-detail-chip">
                    {chip}
                  </span>
                ))}
              </div>
              {readiness?.required_complete && (
                <span className="ai-rfq-ready" title="All required fields are filled">
                  <IconCheck size={13} /> Ready to publish
                </span>
              )}
            </div>
          )}

          <div className="ai-chat-body" role="log" aria-live="polite">
            {showEmptyHero && (
              <div className="ai-empty-hero">
                <div className="ai-empty-hero-icon" aria-hidden="true">
                  <IconSparkles size={20} />
                </div>
                <h3 className="ai-empty-hero-title">
                  {isSpecialist ? currentAgent!.name : "How can I help?"}
                </h3>
                <p className="ai-empty-hero-desc">
                  {isSpecialist
                    ? currentAgent!.description
                    : "Draft an RFQ, find suppliers, negotiate a deal, or ask about trade terms."}
                </p>
                <div className="ai-empty-hero-grid">
                  {activeSuggestedPrompts.map((item) => (
                    <button
                      key={item.prompt}
                      type="button"
                      className="ai-hero-prompt-card"
                      onClick={() => handleSend(item.prompt)}
                    >
                      {stripLeadingEmoji(item.label)}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {!showEmptyHero &&
              messages.map((msg) => {
                const isUser = msg.role === "user";
                const isMsgStreaming = Boolean(msg.isStreaming);
                const showShimmer = !isUser && !msg.content && !msg.toolStep && isMsgStreaming;

                return (
                  <div key={msg.id} className={`ai-message-row ${isUser ? "user-row" : "ai-row"}`}>
                    {!isUser && (
                      <div className="ai-avatar" aria-hidden="true">
                        <IconSparkles size={14} />
                      </div>
                    )}

                    <div className={`ai-message-bubble ${isUser ? "user-bubble" : "ai-bubble"}`}>
                      {showShimmer ? (
                        <div className="ai-shimmer-placeholder" aria-label="Thinking">
                          <span />
                          <span />
                          <span />
                        </div>
                      ) : (
                        <div className="ai-message-text">
                          <MarkdownPreview content={msg.content} />
                          {isMsgStreaming && <span className="streaming-cursor" />}
                        </div>
                      )}

                      {msg.counterpartyMessage && (
                        <div className="ai-counterparty-msg-card">
                          <div className="ai-cp-card-header">
                            <span className="ai-cp-card-title">
                              Sent to <strong>{msg.counterpartyMessage.counterparty_name}</strong>
                              <span className="ai-cp-time">
                                {" · "}
                                {new Date(msg.counterpartyMessage.created_at).toLocaleTimeString([], {
                                  hour: "2-digit",
                                  minute: "2-digit",
                                })}
                              </span>
                            </span>
                            {!isSplitView && (
                              <button
                                type="button"
                                className="link-button"
                                onClick={() => {
                                  setActiveConnectionId(msg.counterpartyMessage!.connection_id);
                                  setIsSplitView(true);
                                }}
                              >
                                Open chat
                              </button>
                            )}
                          </div>
                          <p className="ai-cp-quote">{msg.counterpartyMessage.message}</p>
                        </div>
                      )}

                      {msg.createdRfq && (
                        <div className="ai-created-rfq-card">
                          <div className="ai-created-rfq-badge-row">
                            {msg.createdRfq.status === "closed" ? (
                              <span className="badge badge-muted">Closed</span>
                            ) : (
                              <span className="badge badge-success">
                                <IconCheck size={12} /> Published
                              </span>
                            )}
                            <span className="ai-created-rfq-id">
                              {msg.createdRfq.role === "buyer" ? "Buying" : "Selling"}
                              {msg.createdRfq.category ? ` · ${msg.createdRfq.category}` : ""}
                            </span>
                          </div>
                          <h4 className="ai-created-rfq-title">{msg.createdRfq.title}</h4>
                          <div className="ai-created-rfq-actions">
                            {msg.createdRfq.status !== "closed" && (
                              <button
                                type="button"
                                className="primary small-btn"
                                onClick={() => handleToggleMatches(msg.createdRfq!.id, msg.createdRfq!.role)}
                              >
                                {rfqMatches[msg.createdRfq.id]?.loading
                                  ? "Finding matches…"
                                  : rfqMatches[msg.createdRfq.id]?.isOpen
                                    ? "Hide matches"
                                    : `Show matching ${msg.createdRfq.role === "buyer" ? "suppliers" : "buyers"}${rfqMatches[msg.createdRfq.id]?.total ? ` (${rfqMatches[msg.createdRfq.id].total})` : ""}`}
                              </button>
                            )}
                            <Link to="/rfqs" className="button ghost small-btn">
                              View in My RFQs
                            </Link>
                            {msg.createdRfq.status !== "closed" && (
                              <button
                                type="button"
                                className="button danger small-btn"
                                onClick={() => handleCloseCreatedRfq(msg.createdRfq!.id, msg.createdRfq!.title)}
                              >
                                Close RFQ
                              </button>
                            )}
                          </div>
                        </div>
                      )}

                      {msg.createdRfq && (
                        <MatchCartView
                          rfqId={msg.createdRfq.id}
                          rfqRole={msg.createdRfq.role}
                          matchState={rfqMatches[msg.createdRfq.id]}
                          connectedMap={connectedMap}
                          connectingRfqId={connectingRfqId}
                          onConnect={handleConnectCandidate}
                          onClose={() => handleToggleMatches(msg.createdRfq!.id, msg.createdRfq!.role)}
                          onPromptClick={(p) => handleSend(p)}
                        />
                      )}

                      <span className="ai-hover-timestamp">{msg.timestamp}</span>
                    </div>
                  </div>
                );
              })}
            <div ref={messagesEndRef} />
          </div>

          <footer className="ai-chat-footer">
            {showSuggestions && (
              <div className="ai-chips-grid" aria-label="Suggestions">
                {activeSuggestedPrompts.map((item) => (
                  <button
                    key={item.prompt}
                    type="button"
                    className="ai-prompt-chip"
                    onClick={() => handleSend(item.prompt)}
                  >
                    {stripLeadingEmoji(item.label)}
                  </button>
                ))}
              </div>
            )}
            <form
              className="ai-chat-form"
              onSubmit={(e) => {
                e.preventDefault();
                handleSend();
              }}
            >
              <textarea
                ref={textareaRef}
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={`Message ${currentAgent?.name ?? "Business AI"}…`}
                disabled={loading}
                maxLength={5000}
                aria-label="Message"
              />
              <button
                type="submit"
                className="ai-send-btn"
                disabled={!input.trim() || loading}
                aria-label="Send message"
              >
                <IconSend size={15} />
              </button>
            </form>
            <p className="ai-composer-hint">
              Enter to send · Shift + Enter for a new line
            </p>
          </footer>
        </div>

        {isSplitView && (
          <CounterpartyChatPane
            activeConnectionId={activeConnectionId}
            onSelectConnection={(id) => setActiveConnectionId(id)}
            onClose={() => setIsSplitView(false)}
            lastAiDispatchedMessage={lastAiDispatchedMessage}
          />
        )}
      </div>
    </div>
  );
}

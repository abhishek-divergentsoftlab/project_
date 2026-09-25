import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { connections, translation as translationApi, type AICounterpartyMessage } from "@/api/endpoints";
import { tokenStore } from "@/api/client";
import { useAuth } from "@/context/useAuth";
import type { Connection, ConnectionMessage } from "@/types";
import {
  IconClose,
  IconExternalLink,
  IconMessages,
  IconSend,
  IconSparkles,
} from "@/components/icons";

interface CounterpartyChatPaneProps {
  activeConnectionId: string | null;
  onSelectConnection: (connectionId: string) => void;
  onClose: () => void;
  lastAiDispatchedMessage?: AICounterpartyMessage | null;
}

export function CounterpartyChatPane({
  activeConnectionId,
  onSelectConnection,
  onClose,
  lastAiDispatchedMessage,
}: CounterpartyChatPaneProps) {
  const { user } = useAuth();
  const [allConnections, setAllConnections] = useState<Connection[]>([]);
  const [activeConnection, setActiveConnection] = useState<Connection | null>(null);
  const [messages, setMessages] = useState<ConnectionMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [inputText, setInputText] = useState("");
  const [sending, setSending] = useState(false);
  const [isCounterpartyTyping, setIsCounterpartyTyping] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [paneTranslations, setPaneTranslations] = useState<Record<string, string>>({});
  const [translatingPaneMsgId, setTranslatingPaneMsgId] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const typingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Load all connections for connection switcher
  const loadConnections = async () => {
    try {
      const list = await connections.listAll();
      setAllConnections(list);
      // If no activeConnectionId is provided, select the latest accepted connection
      if (!activeConnectionId && list.length > 0) {
        const preferred = list.find((c) => c.status === "accepted") || list[0];
        onSelectConnection(preferred.id);
      }
    } catch (err) {
      console.error("Failed to load user connections:", err);
    }
  };

  useEffect(() => {
    void loadConnections();
  }, []);

  // Fetch connection detail and messages whenever activeConnectionId changes
  useEffect(() => {
    if (!activeConnectionId) {
      setActiveConnection(null);
      setMessages([]);
      return;
    }

    let isMounted = true;
    const fetchConnData = async () => {
      setLoadingMessages(true);
      try {
        const [connDetail, msgList] = await Promise.all([
          connections.get(activeConnectionId),
          connections.listMessages(activeConnectionId),
        ]);
        if (!isMounted) return;
        setActiveConnection(connDetail);
        setMessages(msgList);
      } catch (err) {
        console.error("Failed to load connection conversation:", err);
      } finally {
        if (isMounted) setLoadingMessages(false);
      }
    };

    void fetchConnData();

    return () => {
      isMounted = false;
    };
  }, [activeConnectionId]);

  // WebSocket Live Real-Time Integration
  useEffect(() => {
    if (!activeConnectionId) return;

    const token = tokenStore.access();
    if (!token) return;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const wsUrl = `${proto}//${host}/api/v1/ws/connections/${activeConnectionId}?token=${encodeURIComponent(token)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl);
      wsRef.current = socket;
    } catch (err) {
      console.error("WebSocket connection failed to initialize:", err);
      return;
    }

    socket.onopen = () => {
      setWsConnected(true);
    };

    socket.onclose = () => {
      setWsConnected(false);
    };

    socket.onerror = () => {
      setWsConnected(false);
    };

    socket.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as { type: string; data?: unknown };
        if (payload.type === "chat_message" && payload.data) {
          const newMsg = payload.data as ConnectionMessage;
          setMessages((current) => {
            if (current.some((m) => m.id === newMsg.id)) return current;
            return [...current, newMsg];
          });
        } else if (payload.type === "typing") {
          setIsCounterpartyTyping(true);
          if (typingTimerRef.current) clearTimeout(typingTimerRef.current);
          typingTimerRef.current = setTimeout(() => setIsCounterpartyTyping(false), 2500);
        }
      } catch (err) {
        console.warn("WebSocket parse error:", err);
      }
    };

    return () => {
      if (typingTimerRef.current) clearTimeout(typingTimerRef.current);
      socket.close();
      wsRef.current = null;
    };
  }, [activeConnectionId]);

  // Scroll to bottom whenever messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isCounterpartyTyping]);

  const handleSendMessage = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const content = inputText.trim();
    if (!content || !activeConnectionId || sending) return;

    setSending(true);
    try {
      const sent = await connections.sendMessage(activeConnectionId, content);
      setMessages((prev) => {
        if (prev.some((m) => m.id === sent.id)) return prev;
        return [...prev, sent];
      });
      setInputText("");
      inputRef.current?.focus();
    } catch (err) {
      console.error("Failed to send message:", err);
    } finally {
      setSending(false);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setInputText(e.target.value);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: "typing" }));
      } catch {
        // Ignore typing emit errors
      }
    }
  };

  const counterpartyName =
    activeConnection?.counterparty?.company_name ||
    activeConnection?.counterparty?.contact_name ||
    lastAiDispatchedMessage?.counterparty_name ||
    "Counterparty";

  const isAiDispatched = (msg: ConnectionMessage) => {
    if (lastAiDispatchedMessage && lastAiDispatchedMessage.status !== "draft") {
      if (lastAiDispatchedMessage.message_id && msg.id === lastAiDispatchedMessage.message_id) {
        return true;
      }
      if (msg.content.trim() === lastAiDispatchedMessage.message.trim()) {
        return true;
      }
    }
    return false;
  };

  const handleTranslatePaneMsg = async (msg: ConnectionMessage) => {
    if (!activeConnectionId || translatingPaneMsgId === msg.id) return;
    setTranslatingPaneMsgId(msg.id);
    try {
      const preferredLang = localStorage.getItem("b2b_dealroom_target_lang") || "en";
      const res = await translationApi.translateConnectionMessage(activeConnectionId, msg.content, preferredLang);
      setPaneTranslations((prev) => ({ ...prev, [msg.id]: res.translated_text }));
    } catch (err) {
      console.error("Failed to translate msg in pane", err);
    } finally {
      setTranslatingPaneMsgId(null);
    }
  };

  return (
    <aside className="counterparty-chat-pane" aria-label={`Chat with ${counterpartyName}`}>
      <div className="counterparty-pane-header">
        <div className="counterparty-header-main">
          <div className="counterparty-avatar" aria-hidden="true">
            {counterpartyName.charAt(0).toUpperCase()}
          </div>
          <div className="counterparty-header-details">
            <div className="counterparty-name-row">
              <h4 className="counterparty-name">{counterpartyName}</h4>
              {wsConnected && <span className="status-dot is-success" title="Live" />}
            </div>
            {activeConnection?.rfq_title && (
              <p className="counterparty-rfq-title" title={activeConnection.rfq_title}>
                {activeConnection.rfq_title}
              </p>
            )}
          </div>
        </div>

        <div className="counterparty-header-actions">
          {activeConnectionId && (
            <Link
              to={`/messages?connection=${encodeURIComponent(activeConnectionId)}`}
              className="icon-btn"
              title="Open in Messages"
              aria-label="Open in Messages"
            >
              <IconExternalLink size={15} />
            </Link>
          )}
          <button
            type="button"
            className="icon-btn"
            onClick={onClose}
            title="Close"
            aria-label="Close counterparty chat"
          >
            <IconClose size={17} />
          </button>
        </div>
      </div>

      {allConnections.length > 1 && (
        <div className="counterparty-switcher">
          <select
            className="counterparty-connection-select"
            value={activeConnectionId || ""}
            onChange={(e) => onSelectConnection(e.target.value)}
            aria-label="Switch conversation"
          >
            {allConnections.map((c) => {
              const name = c.counterparty?.company_name || c.counterparty?.contact_name || "Partner";
              return (
                <option key={c.id} value={c.id}>
                  {name}
                  {c.status !== "accepted" ? ` (${c.status})` : ""}
                </option>
              );
            })}
          </select>
        </div>
      )}

      <p className="counterparty-ai-copilot-banner">
        <IconSparkles size={13} />
        Ask the assistant to negotiate or draft messages — they'll appear here.
      </p>

      <div className="counterparty-messages-list">
        {loadingMessages ? (
          <div className="loading-state">
            <span className="spinner" />
            Loading conversation…
          </div>
        ) : !activeConnectionId ? (
          <div className="empty-state">
            <span className="empty-state-icon">
              <IconMessages size={18} />
            </span>
            <strong>No conversation selected</strong>
            <p>Connect with a match from the chat, or pick a conversation above.</p>
          </div>
        ) : messages.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state-icon">
              <IconMessages size={18} />
            </span>
            <strong>Start the conversation</strong>
            <p>Send a message, or ask the assistant to reach out to {counterpartyName} for you.</p>
          </div>
        ) : (
          messages.map((msg) => {
            const isMe = user && msg.sender_id === user.id;
            const dispatchedByAi = isAiDispatched(msg);

            return (
              <div
                key={msg.id}
                className={`counterparty-msg-item ${isMe ? "msg-outgoing" : "msg-incoming"}`}
              >
                <div className="counterparty-msg-bubble">
                  {dispatchedByAi && (
                    <div className="ai-dispatched-tag">
                      <IconSparkles size={12} />
                      Sent by assistant
                    </div>
                  )}
                  <p className="counterparty-msg-text">{paneTranslations[msg.id] ?? msg.content}</p>
                </div>
                <div className="counterparty-msg-meta">
                  <span>
                    {new Date(msg.created_at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  {paneTranslations[msg.id] ? (
                    <>
                      <span>Translated</span>
                      <button
                        type="button"
                        className="link-button msg-meta-action"
                        onClick={() => {
                          setPaneTranslations((prev) => {
                            const next = { ...prev };
                            delete next[msg.id];
                            return next;
                          });
                        }}
                      >
                        Show original
                      </button>
                    </>
                  ) : (
                    !isMe && (
                      <button
                        type="button"
                        className="link-button msg-meta-action"
                        onClick={() => void handleTranslatePaneMsg(msg)}
                        disabled={translatingPaneMsgId === msg.id}
                      >
                        {translatingPaneMsgId === msg.id ? "Translating…" : "Translate"}
                      </button>
                    )
                  )}
                </div>
              </div>
            );
          })
        )}

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

        <div ref={messagesEndRef} />
      </div>

      <div className="counterparty-composer">
        {activeConnectionId && messages.length < 3 && (
          <div className="counterparty-suggestion-chips">
            <button
              type="button"
              className="ai-prompt-chip"
              onClick={() => setInputText("Can you please provide your best bulk price quotation?")}
            >
              Request a quote
            </button>
            <button
              type="button"
              className="ai-prompt-chip"
              onClick={() => setInputText("What is the earliest delivery date for this order?")}
            >
              Delivery date
            </button>
            <button
              type="button"
              className="ai-prompt-chip"
              onClick={() => setInputText("Do you have product samples available for verification?")}
            >
              Ask for a sample
            </button>
          </div>
        )}
        {lastAiDispatchedMessage?.status === "draft" &&
          lastAiDispatchedMessage.connection_id === activeConnectionId && (
            <div className="counterparty-draft-banner">
              <div className="counterparty-draft-text">
                <span className="badge badge-warning">AI Draft</span>
                <span className="draft-preview-snippet" title={lastAiDispatchedMessage.message}>
                  {lastAiDispatchedMessage.message.length > 70
                    ? `${lastAiDispatchedMessage.message.slice(0, 70)}…`
                    : lastAiDispatchedMessage.message}
                </span>
              </div>
              <button
                type="button"
                className="counterparty-use-draft-btn"
                onClick={() => {
                  setInputText(lastAiDispatchedMessage.message);
                  inputRef.current?.focus();
                }}
              >
                Use Draft
              </button>
            </div>
          )}
        <form className="counterparty-composer-form" onSubmit={handleSendMessage}>
          <input
            ref={inputRef}
            type="text"
            className="counterparty-input"
            placeholder={activeConnectionId ? `Message ${counterpartyName}` : "Select a conversation first"}
            value={inputText}
            onChange={handleInputChange}
            disabled={!activeConnectionId || sending}
            aria-label="Message"
          />
          <button
            type="submit"
            className="ai-send-btn"
            disabled={!inputText.trim() || !activeConnectionId || sending}
            aria-label="Send message"
          >
            <IconSend size={15} />
          </button>
        </form>
      </div>
    </aside>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import {
  IconBrand,
  IconCheck,
  IconChevronDown,
  IconClose,
  IconDashboard,
  IconLogOut,
  IconMarketplace,
  IconMenu,
  IconMessages,
  IconPlus,
  IconProfile,
  IconRFQ,
  IconSparkles,
  IconTrash,
  IconX,
} from "@/components/icons";
import { NotificationBell } from "@/components/NotificationBell";
import { ThemeToggle } from "@/components/ThemeToggle";
import { useAuth } from "@/context/useAuth";
import { useFeedback } from "@/context/useFeedback";
import { useSidebarData } from "@/context/SidebarDataContext";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import type { Connection } from "@/types";
import { formatRelativeTime } from "@/utils/format";

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

export function Layout() {
  const { user, logout } = useAuth();
  const { confirm } = useFeedback();
  const navigate = useNavigate();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);

  const {
    connections,
    loadingConnections,
    acceptConnection,
    rejectConnection,
    aiSessions,
    loadingAiSessions,
    deleteAiSession,
    messagesOpen,
    setMessagesOpen,
    aiOpen,
    setAiOpen,
  } = useSidebarData();

  const [userSearch, setUserSearch] = useState("");
  // Below this width the sidebar is a drawer and the bell lives in the top bar;
  // rendering it once keeps a single poller running.
  const compactShell = useMediaQuery("(max-width: 72rem)");
  const isFullBleed =
    location.pathname.startsWith("/messages") || location.pathname.startsWith("/ai-chat");

  const queryParams = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const activeConnectionId = location.pathname.startsWith("/messages")
    ? queryParams.get("connection") || (connections[0]?.id ?? null)
    : null;
  const activeAiId = location.pathname.startsWith("/ai-chat") ? queryParams.get("c") : null;

  useEffect(() => {
    if (!navOpen) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setNavOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navOpen]);

  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  function handleSelectConnection(id: string) {
    navigate(`/messages?connection=${encodeURIComponent(id)}`);
    setNavOpen(false);
  }

  function handleNewAiChat() {
    navigate(`/ai-chat?new=${Date.now()}`);
    setNavOpen(false);
  }

  function handleSelectAiSession(id: string) {
    navigate(`/ai-chat?c=${encodeURIComponent(id)}`);
    setNavOpen(false);
  }

  async function handleDeleteSession(e: React.MouseEvent, id: string) {
    e.stopPropagation();
    const ok = await confirm({
      title: "Delete this chat?",
      message: "The conversation will be removed from your saved chats.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await deleteAiSession(id);
      if (activeAiId === id) {
        navigate("/ai-chat", { replace: true });
      }
    } catch (err) {
      console.error("Failed to delete session:", err);
    }
  }

  const filteredConnections = useMemo(() => {
    if (!userSearch.trim()) return connections;
    const q = userSearch.toLowerCase();
    return connections.filter((conn) => {
      const name = describe(conn).toLowerCase();
      const title = (conn.rfq_title ?? "").toLowerCase();
      const email = (conn.counterparty.email ?? "").toLowerCase();
      return name.includes(q) || title.includes(q) || email.includes(q);
    });
  }, [connections, userSearch]);

  const displayName =
    user?.profile?.company_name || user?.profile?.name || user?.email || "Account";

  const pendingCount = useMemo(
    () => connections.filter((c) => c.status === "pending" && c.direction === "received").length,
    [connections],
  );

  return (
    <div className="app">
      {navOpen && (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label="Close menu"
          onClick={() => setNavOpen(false)}
        />
      )}

      <aside className={`sidebar ${navOpen ? "open" : ""}`} aria-label="Main navigation">
        <div className="sidebar-header">
          <Link to="/dashboard" className="brand" onClick={() => setNavOpen(false)}>
            <IconBrand size={24} />
            <span>Marketplace</span>
          </Link>
          <div className="sidebar-header-actions">
            {!compactShell && <NotificationBell align="start" />}
            <button
              type="button"
              className="icon-btn sidebar-close"
              aria-label="Close menu"
              onClick={() => setNavOpen(false)}
            >
              <IconClose size={18} />
            </button>
          </div>
        </div>

        <nav className="sidebar-nav">
          {/* 0. Dashboard */}
          <NavLink
            to="/dashboard"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            onClick={() => setNavOpen(false)}
          >
            <IconDashboard size={18} className="nav-icon" />
            <span>Dashboard</span>
          </NavLink>

          {/* 1. Marketplace */}
          <NavLink
            to="/marketplace"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            onClick={() => setNavOpen(false)}
          >
            <IconMarketplace size={18} className="nav-icon" />
            <span>Marketplace</span>
          </NavLink>

          {/* 2. My RFQs */}
          <NavLink
            to="/rfqs"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            onClick={() => setNavOpen(false)}
          >
            <IconRFQ size={18} className="nav-icon" />
            <span>My RFQs</span>
          </NavLink>


          {/* 2. Messages & Deal Room with Dropdown */}
          <div className={`nav-dropdown-group ${messagesOpen ? "is-open" : ""}`}>
            <div className="nav-dropdown-header">
              <NavLink
                to="/messages"
                className={({ isActive }) =>
                  isActive ? "nav-link active nav-dropdown-link" : "nav-link nav-dropdown-link"
                }
                onClick={() => {
                  setMessagesOpen(true);
                  setNavOpen(false);
                }}
              >
                <IconMessages size={18} className="nav-icon" />
                <span>Messages</span>
                {pendingCount > 0 ? (
                  <span
                    className="nav-badge-pill is-alert"
                    title={`${pendingCount} connection ${pendingCount === 1 ? "request" : "requests"} waiting for you`}
                  >
                    {pendingCount}
                  </span>
                ) : (
                  connections.length > 0 && (
                    <span className="nav-badge-pill" title={`${connections.length} conversations`}>
                      {connections.length}
                    </span>
                  )
                )}
              </NavLink>
              <button
                type="button"
                className="nav-chevron-toggle"
                aria-label={messagesOpen ? "Collapse conversations" : "Expand conversations"}
                aria-expanded={messagesOpen}
                onClick={(e) => {
                  e.stopPropagation();
                  setMessagesOpen((prev) => !prev);
                }}
              >
                <IconChevronDown
                  size={14}
                  className={`chevron-arrow ${messagesOpen ? "rotated" : ""}`}
                />
              </button>
            </div>

            {messagesOpen && (
              <div className="nav-sub-container">
                {connections.length > 3 && (
                  <div className="nav-sub-search">
                    <input
                      type="search"
                      placeholder="Filter conversations"
                      value={userSearch}
                      onChange={(e) => setUserSearch(e.target.value)}
                      className="nav-sub-search-input"
                      aria-label="Filter chat users"
                    />
                  </div>
                )}

                <div className="nav-sub-list">
                  {loadingConnections && connections.length === 0 ? (
                    <div className="nav-sub-empty">Loading…</div>
                  ) : connections.length === 0 ? (
                    <div className="nav-sub-empty">
                      <span>No conversations yet</span>
                      <small>Connect with a match to start one</small>
                    </div>
                  ) : filteredConnections.length === 0 ? (
                    <div className="nav-sub-empty">No matches found</div>
                  ) : (
                    filteredConnections.map((conn) => {
                      const name = describe(conn);
                      const initials = getInitials(name);
                      const isSelected = conn.id === activeConnectionId;
                      const isPendingReceived =
                        conn.status === "pending" && conn.direction === "received";

                      return (
                        <div
                          key={conn.id}
                          className={`nav-user-item ${isSelected ? "selected" : ""}`}
                          onClick={() => handleSelectConnection(conn.id)}
                          role="button"
                          tabIndex={0}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              handleSelectConnection(conn.id);
                            }
                          }}
                        >
                          <div className="nav-user-avatar">
                            {initials}
                            <span
                              className={`nav-status-dot dot-${conn.status}`}
                              title={conn.status}
                            />
                          </div>

                          <div className="nav-user-details">
                            <span className="nav-user-name" title={name}>
                              {name}
                            </span>
                            <span className="nav-user-sub" title={conn.rfq_title || ""}>
                              {conn.rfq_title
                                ? `${conn.direction === "sent" ? "To: " : ""}${conn.rfq_title}`
                                : conn.status === "accepted"
                                  ? "Connected"
                                  : "Pending request"}
                            </span>
                          </div>

                          {isPendingReceived && (
                            <div
                              className="nav-user-actions"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <button
                                type="button"
                                className="nav-action-btn accept"
                                title="Accept request"
                                aria-label={`Accept request from ${name}`}
                                onClick={async () => {
                                  try {
                                    await acceptConnection(conn.id);
                                  } catch (err) {
                                    console.error(err);
                                  }
                                }}
                              >
                                <IconCheck size={14} />
                              </button>
                              <button
                                type="button"
                                className="nav-action-btn decline"
                                title="Decline request"
                                aria-label={`Decline request from ${name}`}
                                onClick={async () => {
                                  try {
                                    await rejectConnection(conn.id);
                                  } catch (err) {
                                    console.error(err);
                                  }
                                }}
                              >
                                <IconX size={14} />
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            )}
          </div>

          {/* 3. Business AI with Dropdown */}
          <div className={`nav-dropdown-group ${aiOpen ? "is-open" : ""}`}>
            <div className="nav-dropdown-header">
              <NavLink
                to="/ai-chat"
                className={({ isActive }) =>
                  isActive ? "nav-link active nav-dropdown-link" : "nav-link nav-dropdown-link"
                }
                onClick={() => {
                  setAiOpen(true);
                  setNavOpen(false);
                }}
              >
                <IconSparkles size={18} className="nav-icon" />
                <span>Business AI</span>
                {aiSessions.length > 0 && (
                  <span className="nav-badge-pill" title={`${aiSessions.length} sessions`}>
                    {aiSessions.length}
                  </span>
                )}
              </NavLink>
              <button
                type="button"
                className="nav-chevron-toggle"
                aria-label={aiOpen ? "Collapse saved chats" : "Expand saved chats"}
                aria-expanded={aiOpen}
                onClick={(e) => {
                  e.stopPropagation();
                  setAiOpen((prev) => !prev);
                }}
              >
                <IconChevronDown size={14} className={`chevron-arrow ${aiOpen ? "rotated" : ""}`} />
              </button>
            </div>

            {aiOpen && (
              <div className="nav-sub-container">
                <button
                  type="button"
                  className="nav-new-chat-btn"
                  onClick={handleNewAiChat}
                  title="Start a new chat session"
                >
                  <IconPlus size={14} />
                  <span>New chat</span>
                </button>

                <div className="nav-sub-list">
                  {loadingAiSessions && aiSessions.length === 0 ? (
                    <div className="nav-sub-empty">Loading…</div>
                  ) : aiSessions.length === 0 ? (
                    <div className="nav-sub-empty">
                      <span>No saved chats yet</span>
                      <small>Your conversations are saved here</small>
                    </div>
                  ) : (
                    aiSessions.map((session) => {
                      const isSelected = session.id === activeAiId;
                      return (
                        <div
                          key={session.id}
                          className={`nav-ai-item ${isSelected ? "selected" : ""}`}
                          onClick={() => handleSelectAiSession(session.id)}
                          role="button"
                          tabIndex={0}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              handleSelectAiSession(session.id);
                            }
                          }}
                        >
                          <div className="nav-ai-content">
                            <span
                              className="nav-ai-title"
                              title={session.title || "Business Chat"}
                            >
                              {session.title || "Business Chat"}
                            </span>
                            <span className="nav-ai-date">
                              {formatRelativeTime(session.updated_at)}
                            </span>
                          </div>

                          <button
                            type="button"
                            className="nav-ai-del-btn"
                            title="Delete session"
                            aria-label={`Delete ${session.title || "session"}`}
                            onClick={(e) => handleDeleteSession(e, session.id)}
                          >
                            <IconTrash size={13} />
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            )}
          </div>

          {/* 4. Profile */}
          <NavLink
            to="/profile"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            onClick={() => setNavOpen(false)}
          >
            <IconProfile size={18} className="nav-icon" />
            <span>Profile</span>
          </NavLink>
        </nav>

        <div className="sidebar-bottom">
          <Link
            to="/profile"
            className="sidebar-user"
            title={user?.email ?? undefined}
            onClick={() => setNavOpen(false)}
          >
            <span className="sidebar-user-avatar" aria-hidden="true">
              {getInitials(displayName)}
            </span>
            <span className="sidebar-user-text">
              <span className="sidebar-user-name">{displayName}</span>
              {user?.email && displayName !== user.email && (
                <span className="user-email">{user.email}</span>
              )}
            </span>
          </Link>
          <div className="sidebar-bottom-actions">
            <ThemeToggle compact />
            <button
              type="button"
              className="icon-btn sign-out-btn"
              onClick={handleLogout}
              aria-label="Sign out"
              title="Sign out"
            >
              <IconLogOut size={17} />
            </button>
          </div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn menu-trigger"
            aria-label="Open menu"
            aria-expanded={navOpen}
            onClick={() => setNavOpen(true)}
          >
            <IconMenu size={20} />
          </button>
          <Link to="/dashboard" className="topbar-brand brand">
            <IconBrand size={22} />
            <span>Marketplace</span>
          </Link>
          <div className="topbar-actions">
            {compactShell && <NotificationBell align="end" />}
            <ThemeToggle compact className="topbar-theme" />
          </div>
        </header>

        <main className={`content ${isFullBleed ? "content-full-bleed" : ""}`}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

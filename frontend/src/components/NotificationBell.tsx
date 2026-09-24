import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { notifications } from "@/api/endpoints";
import type { NotificationItem } from "@/types";
import { formatRelativeTime, tidyNumbers } from "@/utils/format";
import {
  IconBell,
  IconCheck,
  IconFileText,
  IconMessages,
  IconProfile,
  IconShield,
} from "@/components/icons";

function NotificationIcon({ type }: { type: string }) {
  switch (type) {
    case "connection_request":
      return <IconProfile size={15} />;
    case "connection_accepted":
      return <IconCheck size={15} />;
    case "new_message":
      return <IconMessages size={15} />;
    case "quotation_received":
    case "quotation_accepted":
      return <IconFileText size={15} />;
    default:
      return <IconShield size={15} />;
  }
}

/** `align` picks which edge the panel opens from: "end" in the top bar, "start" in the sidebar. */
export function NotificationBell({ align = "end" }: { align?: "start" | "end" }) {
  const [isOpen, setIsOpen] = useState(false);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const fetchUnreadCount = async () => {
    try {
      const res = await notifications.getUnreadCount();
      setUnreadCount(res.count);
    } catch {
      // Non-fatal
    }
  };

  const loadNotifications = async () => {
    setLoading(true);
    try {
      const res = await notifications.list({ limit: 20 });
      setItems(res.items);
    } catch {
      // Non-fatal
    } finally {
      setLoading(false);
    }
  };

  // Poll unread count on mount and interval
  useEffect(() => {
    void fetchUnreadCount();
    const interval = setInterval(fetchUnreadCount, 25000);
    return () => clearInterval(interval);
  }, []);

  // When dropdown opens, fetch latest notifications
  useEffect(() => {
    if (isOpen) {
      void loadNotifications();
    }
  }, [isOpen]);

  // Click outside to close
  useEffect(() => {
    if (!isOpen) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setIsOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  const handleItemClick = async (item: NotificationItem) => {
    if (!item.is_read) {
      try {
        await notifications.markRead(item.id);
        setItems((prev) =>
          prev.map((n) => (n.id === item.id ? { ...n, is_read: true } : n))
        );
        setUnreadCount((prev) => Math.max(0, prev - 1));
      } catch {
        // Continue navigation anyway
      }
    }
    setIsOpen(false);
    if (item.link) {
      navigate(item.link);
    }
  };

  const handleMarkAllRead = async () => {
    try {
      await notifications.markAllRead();
      setItems((prev) => prev.map((n) => ({ ...n, is_read: true })));
      setUnreadCount(0);
    } catch {
      // Non-fatal
    }
  };

  return (
    <div className="notif-bell-container" ref={containerRef}>
      <button
        type="button"
        className={`notif-bell-btn ${isOpen ? "active" : ""}`}
        aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
        aria-expanded={isOpen}
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <IconBell size={17} />
        {unreadCount > 0 && (
          <span className="notif-badge">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <div className={`notif-dropdown align-${align}`} role="dialog" aria-label="Notifications">
          <div className="notif-dropdown-header">
            <div className="notif-header-title">
              <span className="notif-title-text">Notifications</span>
              {unreadCount > 0 && <span className="notif-unread-pill">{unreadCount} unread</span>}
            </div>
            {unreadCount > 0 && (
              <button
                type="button"
                className="notif-mark-all-btn"
                onClick={handleMarkAllRead}
                title="Mark all as read"
              >
                <span>Mark all read</span>
              </button>
            )}
          </div>

          <div className="notif-list-body">
            {loading && items.length === 0 ? (
              <div className="notif-loading-state">
                <div className="spinner" />
                <span>Loading…</span>
              </div>
            ) : items.length === 0 ? (
              <div className="notif-empty-state">
                <span className="notif-empty-icon" aria-hidden="true">
                  <IconBell size={18} />
                </span>
                <p className="notif-empty-title">You're all caught up</p>
                <span className="notif-empty-desc">
                  Requests, messages and quotes will show up here.
                </span>
              </div>
            ) : (
              items.map((item) => (
                <div
                  key={item.id}
                  className={`notif-item ${item.is_read ? "read" : "unread"}`}
                  onClick={() => handleItemClick(item)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      void handleItemClick(item);
                    }
                  }}
                >
                  <div className="notif-item-icon" aria-hidden="true">
                    <NotificationIcon type={item.type} />
                  </div>
                  <div className="notif-item-content">
                    <div className="notif-item-top">
                      <span className="notif-item-title">{item.title}</span>
                      <span className="notif-item-time">
                        {formatRelativeTime(item.created_at)}
                      </span>
                    </div>
                    {item.body && (
                      <p className="notif-item-body">{tidyNumbers(item.body)}</p>
                    )}
                  </div>
                  {!item.is_read && <span className="notif-unread-dot" />}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

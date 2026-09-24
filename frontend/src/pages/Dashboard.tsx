import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { aiChat, dashboard } from "@/api/endpoints";
import {
  IconChevronRight,
  IconFileText,
  IconMessages,
  IconPlus,
  IconProfile,
  IconShield,
  IconSparkles,
} from "@/components/icons";
import { useAuth } from "@/context/useAuth";
import type { ActivityItem, AgentInfo, DashboardStats } from "@/types";
import { formatRelativeTime, tidyNumbers } from "@/utils/format";

function activityMeta(type: ActivityItem["type"], icon: string): { label: string; tone: string; glyph: ReactNode } {
  if (type === "connection") {
    const glyph = <IconProfile size={15} />;
    if (icon === "connection_accepted") return { label: "Accepted", tone: "success", glyph };
    if (icon === "connection_rejected") return { label: "Declined", tone: "danger", glyph };
    if (icon === "connection_received") return { label: "Request received", tone: "info", glyph };
    return { label: "Request sent", tone: "neutral", glyph };
  }
  if (type === "quotation") {
    const glyph = <IconFileText size={15} />;
    if (icon === "quote_accepted") return { label: "Quote accepted", tone: "success", glyph };
    if (icon === "quote_dispatched") return { label: "Dispatched", tone: "warning", glyph };
    if (icon === "quote_completed") return { label: "Completed", tone: "success", glyph };
    return { label: "Quotation", tone: "info", glyph };
  }
  return { label: "Message", tone: "neutral", glyph: <IconMessages size={15} /> };
}

export function Dashboard() {
  const { user } = useAuth();

  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadData() {
      setLoading(true);
      setError(null);
      try {
        const [statsData, activityData, agentsData] = await Promise.all([
          dashboard.getStats().catch(() => null),
          dashboard.getActivity(15).catch(() => []),
          aiChat.listAgents().catch(() => []),
        ]);

        if (!cancelled) {
          setStats(statsData);
          setActivities(activityData);
          setAgents(agentsData);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load dashboard data");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void loadData();
    return () => {
      cancelled = true;
    };
  }, []);

  const companyName = user?.profile?.company_name || user?.profile?.name || "there";
  const trustScore = user?.profile?.trust_score ?? stats?.trust_score ?? 50;
  const kycStatus = user?.profile?.kyc_status ?? "unverified";
  const pendingReceived = stats?.connections.pending_received ?? 0;
  const value = (n: number | undefined) => (loading ? "—" : (n ?? 0).toLocaleString());

  const attention: { key: string; to: string; tone: string; title: string; detail: string }[] = [];
  if (pendingReceived > 0) {
    attention.push({
      key: "requests",
      to: "/messages",
      tone: "is-accent",
      title: `${pendingReceived} connection ${pendingReceived === 1 ? "request" : "requests"}`,
      detail: "waiting for your reply",
    });
  }
  if (!loading && kycStatus !== "verified") {
    attention.push({
      key: "kyc",
      to: "/profile",
      tone: "is-warning",
      title: kycStatus === "pending" ? "Verification in review" : "Verify your business",
      detail:
        kycStatus === "pending"
          ? "we'll add your verified badge once it's approved"
          : "add your GST details to earn a verified badge",
    });
  }

  return (
    <section className="page dashboard">
      <header className="page-header">
        <div>
          <h1>Welcome back, {companyName}</h1>
          <p>Here's where your trading stands today.</p>
        </div>
        <div className="page-actions">
          <Link to="/ai-chat" className="button secondary">
            <IconSparkles size={16} />
            Ask AI
          </Link>
          <Link to="/rfqs/new" className="button">
            <IconPlus size={16} />
            New RFQ
          </Link>
        </div>
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {attention.length > 0 && (
        <div className="dash-attention" aria-label="Needs attention">
          {attention.map((item) => (
            <Link key={item.key} to={item.to} className="dash-attention-item">
              <span className={`status-dot ${item.tone}`} />
              <span className="dash-attention-text">
                <strong>{item.title}</strong>
                <span className="dash-attention-detail"> — {item.detail}</span>
              </span>
              <IconChevronRight size={16} className="dash-attention-arrow" />
            </Link>
          ))}
        </div>
      )}

      <section className="panel dash-stats" aria-label="Key metrics">
        <Link to="/rfqs" className="dash-stat">
          <span className="dash-stat-label">Live RFQs</span>
          <span className="dash-stat-value">{value(stats?.rfqs.active)}</span>
          <span className="dash-stat-meta">
            {stats ? `${stats.rfqs.total} posted · ${stats.rfqs.draft} draft` : " "}
          </span>
        </Link>
        <Link to="/messages" className="dash-stat">
          <span className="dash-stat-label">Connections</span>
          <span className="dash-stat-value">{value(stats?.connections.accepted)}</span>
          <span className="dash-stat-meta">
            {stats ? `${stats.connections.pending} pending` : " "}
          </span>
        </Link>
        <Link to="/messages" className="dash-stat">
          <span className="dash-stat-label">Open quotes</span>
          <span className="dash-stat-value">
            {value(stats ? stats.quotations.pending + stats.quotations.accepted : undefined)}
          </span>
          <span className="dash-stat-meta">
            {stats
              ? `${stats.quotations.in_transit} in transit · ${stats.quotations.completed} completed`
              : " "}
          </span>
        </Link>
        <Link to="/profile" className="dash-stat">
          <span className="dash-stat-label">Trust score</span>
          <span className="dash-stat-value">
            {loading ? "—" : trustScore}
            <span className="dash-stat-unit">/100</span>
          </span>
          <span className="dash-stat-meta">
            {kycStatus === "verified" ? (
              <span className="dash-verified">
                <IconShield size={13} /> Verified business
              </span>
            ) : kycStatus === "pending" ? (
              "Verification in review"
            ) : (
              "Not verified yet"
            )}
          </span>
        </Link>
      </section>

      <div className="dash-grid">
        <section className="panel">
          <div className="panel-head">
            <h2>Recent activity</h2>
          </div>

          {loading && activities.length === 0 ? (
            <div className="loading-state">
              <span className="spinner" />
              Loading activity…
            </div>
          ) : activities.length === 0 ? (
            <div className="empty-state">
              <span className="empty-state-icon">
                <IconMessages size={18} />
              </span>
              <strong>Nothing here yet</strong>
              <p>Requests, quotes and deliveries show up here as they happen.</p>
              <Link to="/marketplace" className="button secondary small-btn">
                Browse the marketplace
              </Link>
            </div>
          ) : (
            <ul className="activity-list">
              {activities.map((item) => {
                const meta = activityMeta(item.type, item.icon);
                const body = (
                  <>
                    <span className={`activity-icon tone-${meta.tone}`} aria-hidden="true">
                      {meta.glyph}
                    </span>
                    <span className="activity-content">
                      <span className="activity-title">{item.title}</span>
                      {item.body && <span className="activity-body">{tidyNumbers(item.body)}</span>}
                    </span>
                    <span className="activity-side">
                      <span className="activity-time">{formatRelativeTime(item.timestamp)}</span>
                      <span className={`activity-tag tone-${meta.tone}`}>{meta.label}</span>
                    </span>
                  </>
                );
                return (
                  <li key={item.id}>
                    {item.link ? (
                      <Link to={item.link} className="activity-item">
                        {body}
                      </Link>
                    ) : (
                      <div className="activity-item">{body}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>AI assistants</h2>
            <Link to="/ai-chat" className="panel-head-link">
              Open chat
            </Link>
          </div>
          {agents.length === 0 ? (
            <div className="empty-state">
              <p>{loading ? "Loading…" : "Assistants are unavailable right now."}</p>
            </div>
          ) : (
            <ul className="agent-list">
              {agents.map((agent) => (
                <li key={agent.id}>
                  <Link
                    to={`/ai-chat?agent=${encodeURIComponent(agent.id)}`}
                    className="agent-item"
                  >
                    <span className="agent-text">
                      <span className="agent-name">{agent.name}</span>
                      <span className="agent-desc">{agent.short_description}</span>
                    </span>
                    <IconChevronRight size={16} className="agent-arrow" />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </section>
  );
}

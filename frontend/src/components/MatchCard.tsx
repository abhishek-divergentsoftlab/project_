import { useState } from "react";
import { Link } from "react-router-dom";

import { IconShield, IconStar, IconTruck } from "@/components/icons";
import type { Counterparty, MatchCandidate, MatchScore } from "@/types";
import { formatDate, formatShortDate } from "@/utils/format";


/** Shared by the RFQ matches page and the direct-search chat. */

const DIMENSIONS: { key: keyof MatchScore; label: string }[] = [
  { key: "relevance", label: "Product" },
  { key: "attributes", label: "Attributes" },
  { key: "category", label: "Category" },
  { key: "price", label: "Price" },
  { key: "quantity", label: "Quantity" },
  { key: "location", label: "Location" },
  { key: "deadline", label: "Deadline" },
];

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

/** "444 km" reads as a fact; "73%" on its own reads as an opinion. */
function formatDistance(km: number | null): string | null {
  if (km === null) return null;
  if (km < 1) return "Same location";
  if (km < 100) return `${Math.round(km)} km away`;
  return `${Math.round(km).toLocaleString()} km away`;
}

/** Turns a dimension score into the reason a human would give. */
function explain(key: keyof MatchScore, value: number, distanceKm: number | null): string {
  switch (key) {
    case "quantity":
      return value >= 1 ? "Covers your full volume" : `Covers ${percent(value)} of your volume`;
    case "price":
      return value >= 1 ? "Within your target price" : "Above your target price";
    case "location": {
      const distance = formatDistance(distanceKm);
      if (distance) return distance;
      if (value >= 1) return "Same city";
      if (value >= 0.6) return "Same state";
      if (value >= 0.3) return "Same country";
      return "Different country";
    }
    case "deadline":
      return value >= 1 ? "Meets your deadline" : "Tighter than your deadline";
    case "category":
      return value >= 1 ? "Same category" : "Different category";
    case "attributes":
      if (value >= 1) return "Matches everything you asked for";
      return value >= 0.5 ? "Some details unstated" : "Differs on what you asked for";
    default:
      return value >= 0.8 ? "Very close product match" : "Related product";
  }
}

function ScoreBar({ score, distanceKm }: { score: MatchScore; distanceKm: number | null }) {
  return (
    <div className="score-grid">
      {DIMENSIONS.map(({ key, label }) => {
        const value = score[key];
        if (value === null || value === undefined) {
          return (
            <div key={key} className="score-row">
              <span className="score-label">{label}</span>
              <span className="score-na" title="Neither side filled this in, so it was left out of the total">
                Not compared
              </span>
            </div>
          );
        }
        return (
          <div key={key} className="score-row" title={explain(key, value, distanceKm)}>
            <span className="score-label">{label}</span>
            <span className="score-track">
              <span className="score-fill" style={{ width: percent(value) }} />
            </span>
            <span className="score-value">{percent(value)}</span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Contact details, once the other side has agreed to share them.
 *
 * Before that this is deliberately empty: the connection request is what buys
 * the email and phone number, and showing them unconditionally would make every
 * match page a scrapeable lead list.
 */
function ContactDetails({ counterparty }: { counterparty: Counterparty }) {
  const rows: { label: string; value: string; href?: string }[] = [];
  if (counterparty.contact_name) rows.push({ label: "Contact", value: counterparty.contact_name });
  if (counterparty.email) {
    rows.push({ label: "Email", value: counterparty.email, href: `mailto:${counterparty.email}` });
  }
  if (counterparty.phone) {
    rows.push({ label: "Phone", value: counterparty.phone, href: `tel:${counterparty.phone.replace(/\s/g, "")}` });
  }
  if (counterparty.address) rows.push({ label: "Address", value: counterparty.address });

  if (rows.length === 0) {
    return (
      <div className="contact-panel">
        <p className="muted">They haven't added contact details to their profile yet.</p>
      </div>
    );
  }

  return (
    <div className="contact-panel">
      <dl className="contact-rows">
        {rows.map(({ label, value, href }) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{href ? <a href={href}>{value}</a> : value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

interface MatchCardProps {
  candidate: MatchCandidate;
  /** Omit to render the Contact button disabled. */
  onContact?: (rfqId: string) => void | Promise<void>;
}

export function MatchCard({ candidate, onContact }: MatchCardProps) {
  const { score, counterparty, quantity, price, location, distance_km: distanceKm } = candidate;
  const [contacting, setContacting] = useState(false);

  const status = counterparty.connection_status;
  const distance = formatDistance(distanceKm);

  async function handleContact() {
    if (!onContact) return;
    setContacting(true);
    try {
      await onContact(candidate.rfq_id);
    } finally {
      setContacting(false);
    }
  }

  // The LISTING's location, not the company's registered address.
  //
  // This line used to prefer the profile city, so a card could read
  // "Indus Packaging · Ludhiana" for goods sitting in Bhopal -- while the
  // Location score, the distance and the delivery all referred to Bhopal. That
  // is what made a 500km match look like a perfect one.
  const where = [location?.city, location?.state, location?.country]
    .filter((part, index, all): part is string => Boolean(part) && all.indexOf(part) === index)
    .slice(0, 2)
    .join(", ");

  const tier = score.total >= 0.85 ? "Excellent fit" : score.total >= 0.7 ? "Strong fit" : "Possible fit";
  const rating = counterparty.average_rating;

  return (
    <article className="match-card">
      <header className="match-head">
        <div className="match-head-main">
          <h3 className="match-title">{candidate.title}</h3>
          <p className="match-company">
            <span className="match-company-name">{counterparty.company_name ?? "Unnamed company"}</span>
            {where && <span> · ships from {where}</span>}
          </p>
          <div className="match-trust">
            {counterparty.gst_verified && (
              <span className="match-trust-item is-verified" title="GSTIN verified">
                <IconShield size={13} />
                GST verified
              </span>
            )}
            <span
              className="match-trust-item"
              title={
                rating != null && rating > 0
                  ? `${rating.toFixed(1)} out of 5 from ${counterparty.total_reviews ?? 0} reviews`
                  : "No reviews yet"
              }
            >
              <IconStar size={13} />
              {rating != null && rating > 0
                ? `${rating.toFixed(1)} (${counterparty.total_reviews ?? 0})`
                : "No reviews yet"}
            </span>
          </div>
        </div>
        <div className="match-score" title="Overall match score">
          <span className="match-total">{percent(score.total)}</span>
          <span className="match-tier">{tier}</span>
        </div>
      </header>

      <dl className="meta-list match-facts">
        <div>
          <dt>Quantity</dt>
          <dd>{quantity ? `${quantity.value.toLocaleString()} ${quantity.unit}` : "—"}</dd>
        </div>
        <div>
          <dt>Price</dt>
          <dd>
            {price
              ? `${price.amount.toLocaleString()} ${price.currency}${price.per_unit ? `/${price.per_unit}` : ""}`
              : "—"}
          </dd>
        </div>
        <div>
          <dt title="Time to dispatch, not including transport">Dispatch by</dt>
          <dd>
            {formatDate(candidate.deadline?.date)}
          </dd>
        </div>
        <div>
          <dt>Distance</dt>
          <dd>{distance ?? "—"}</dd>
        </div>
      </dl>

      {candidate.logistics && (
        <p className="match-logistics" title={candidate.logistics.mode}>
          <IconTruck size={15} />
          <span>
            {candidate.logistics.label}
            {candidate.deadline?.estimated_delivery_at &&
              ` · arrives around ${formatShortDate(candidate.deadline.estimated_delivery_at)}`}
          </span>
          {candidate.logistics.customs_required && (
            <span className="badge badge-customs">Customs</span>
          )}
        </p>
      )}

      <details className="match-breakdown">
        <summary>Why this match</summary>
        <ScoreBar score={score} distanceKm={distanceKm} />
      </details>

      {status === "accepted" && <ContactDetails counterparty={counterparty} />}

      <div className="match-actions">
        {status === "accepted" ? (
          <>
            <span className="match-status">
              <span className="status-dot is-success" />
              Connected
            </span>
            {counterparty.connection_id && (
              <Link
                to={`/messages?connection=${counterparty.connection_id}`}
                className="button small-btn"
              >
                Open deal room
              </Link>
            )}
          </>
        ) : status === "pending" ? (
          <>
            <span className="match-status">
              <span className="status-dot is-warning" />
              Waiting for them to accept
            </span>
            <button type="button" className="secondary small-btn" disabled>
              Request sent
            </button>
          </>
        ) : status === "rejected" ? (
          <span className="match-status">
            <span className="status-dot" />
            They declined this request
          </span>
        ) : (
          <>
            <span />
            <button
              type="button"
              className="primary small-btn"
              onClick={handleContact}
              disabled={!onContact || contacting}
              title={onContact ? undefined : "Not available here"}
            >
              {contacting ? "Sending…" : "Request contact"}
            </button>
          </>
        )}
      </div>
    </article>
  );
}

import { useState } from "react";

import type { Counterparty, MatchCandidate, MatchScore } from "@/types";

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
                not comparable
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
        <span className="contact-heading">Connected</span>
        <p className="muted">They have not added contact details to their profile yet.</p>
      </div>
    );
  }

  return (
    <div className="contact-panel">
      <span className="contact-heading">Contact details</span>
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

  return (
    <article className="match-card">
      <div className="match-head">
        <span className="match-rank">#{candidate.rank}</span>
        <div>
          <h2>{candidate.title}</h2>
          <p className="muted">
            {counterparty.company_name ?? "Unnamed company"}
            {where ? ` · ships from ${where}` : ""}
          </p>
        </div>
        <span className="match-total" title="Overall match score">
          {percent(score.total)}
        </span>
      </div>

      <dl className="match-facts">
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
          <dt>Deadline</dt>
          <dd>
            {candidate.deadline?.date
              ? new Date(candidate.deadline.date).toLocaleDateString()
              : "—"}
          </dd>
        </div>
        <div>
          <dt>Distance</dt>
          <dd>{distance ?? "—"}</dd>
        </div>
      </dl>

      <ScoreBar score={score} distanceKm={distanceKm} />

      {candidate.search_tags.length > 0 && (
        <div className="tags">
          {candidate.search_tags.slice(0, 6).map((tag) => (
            <span key={tag} className="tag">
              {tag}
            </span>
          ))}
        </div>
      )}

      {status === "accepted" && <ContactDetails counterparty={counterparty} />}

      <div className="match-actions">
        {status === "accepted" ? (
          <span className="badge badge-accepted">Connected</span>
        ) : status === "pending" ? (
          <button type="button" className="secondary" disabled title="Waiting for them to accept">
            Request sent
          </button>
        ) : status === "rejected" ? (
          <button type="button" className="secondary" disabled title="They declined this request">
            Declined
          </button>
        ) : (
          <button
            type="button"
            className="secondary"
            onClick={handleContact}
            disabled={!onContact || contacting}
            title={
              onContact
                ? "Ask them to share their contact details"
                : "Not available here"
            }
          >
            {contacting ? "Sending…" : "Request contact"}
          </button>
        )}
      </div>
    </article>
  );
}

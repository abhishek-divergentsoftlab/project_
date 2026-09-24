/** Shared display formatting, so dates and times read the same on every screen. */

/** "Oct 19, 2026" */
export function formatDate(value: string | number | Date | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

/** "Oct 19" — for dates close enough that the year is implied. */
export function formatShortDate(value: string | number | Date | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "Just now", "5m ago", "3h ago", "Yesterday", "4d ago", then "Oct 19". */
export function formatRelativeTime(isoString: string): string {
  try {
    const date = new Date(isoString);
    const diffMs = Date.now() - date.getTime();
    if (Number.isNaN(diffMs) || diffMs < 0) return "Just now";
    const diffMin = Math.floor(diffMs / 60000);
    const diffHr = Math.floor(diffMin / 60);
    const diffDays = Math.floor(diffHr / 24);

    if (diffMin < 1) return "Just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHr < 24) return `${diffHr}h ago`;
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays}d ago`;
    return formatShortDate(date);
  } catch {
    return "";
  }
}

/** Strips a leading emoji (and the space after it) from a label. */
export function stripLeadingEmoji(label: string): string {
  return label.replace(/^(?:\p{Extended_Pictographic}|[\u{1F1E6}-\u{1F1FF}]|\uFE0F|\u200D)+\s*/u, "");
}

/** "10000.0000" -> "10,000", "216.5000" -> "216.5" — for numbers the API sends as fixed-point text. */
export function formatNumber(value: number | string | null | undefined, maxFractionDigits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: maxFractionDigits });
}

/** Tidies fixed-point numbers inside free text, e.g. activity descriptions. */
export function tidyNumbers(text: string): string {
  return text.replace(/\b\d+\.\d{3,}\b/g, (match) => formatNumber(match));
}

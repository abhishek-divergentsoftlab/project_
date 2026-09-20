/**
 * AI / Fuzzy Currency Normalizer
 * Converts natural language phrases (e.g. "india ruppes", "us dollar", "kr")
 * into standard ISO 4217 currency codes.
 */

const CURRENCY_SYNONYMS: Record<string, string> = {
  // INR
  inr: "INR",
  "₹": "INR",
  rs: "INR",
  "rs.": "INR",
  rupee: "INR",
  rupees: "INR",
  ruppes: "INR",
  rupes: "INR",
  rupess: "INR",
  ruppees: "INR",
  "india ruppes": "INR",
  "indian rupees": "INR",
  "indian rupee": "INR",
  "india rupee": "INR",
  "india rupees": "INR",

  // USD
  usd: "USD",
  $: "USD",
  "us$": "USD",
  dollar: "USD",
  dollars: "USD",
  buck: "USD",
  bucks: "USD",
  "us dollar": "USD",
  "us dollars": "USD",
  "u.s. dollar": "USD",
  "united states dollar": "USD",

  // EUR
  eur: "EUR",
  "€": "EUR",
  euro: "EUR",
  euros: "EUR",

  // GBP
  gbp: "GBP",
  "£": "GBP",
  pound: "GBP",
  pounds: "GBP",
  quid: "GBP",
  "british pound": "GBP",
  "british pounds": "GBP",
  "uk pound": "GBP",

  // Scandinavian (kr)
  kr: "SEK",
  krona: "SEK",
  kronor: "SEK",
  "swedish krona": "SEK",
  sek: "SEK",
  "norwegian krone": "NOK",
  nok: "NOK",
  krone: "NOK",
  kroner: "NOK",
  "danish krone": "DKK",
  dkk: "DKK",

  // JPY
  jpy: "JPY",
  "¥": "JPY",
  yen: "JPY",
  "japanese yen": "JPY",

  // CNY
  cny: "CNY",
  rmb: "CNY",
  yuan: "CNY",
  renminbi: "CNY",
  "chinese yuan": "CNY",

  // CAD
  cad: "CAD",
  "c$": "CAD",
  "canadian dollar": "CAD",

  // AUD
  aud: "AUD",
  "a$": "AUD",
  "australian dollar": "AUD",

  // AED
  aed: "AED",
  dirham: "AED",
  dirhams: "AED",
  "uae dirham": "AED",

  // SAR
  sar: "SAR",
  riyal: "SAR",
  "saudi riyal": "SAR",

  // CHF
  chf: "CHF",
  franc: "CHF",
  francs: "CHF",
  "swiss franc": "CHF",

  // KRW
  krw: "KRW",
  "₩": "KRW",
  won: "KRW",
  "korean won": "KRW",

  // SGD
  sgd: "SGD",
  "singapore dollar": "SGD",
};

export const CURRENCY_NAMES: Record<string, string> = {
  INR: "Indian Rupee (₹)",
  USD: "US Dollar ($)",
  EUR: "Euro (€)",
  GBP: "British Pound (£)",
  SEK: "Swedish Krona (kr)",
  NOK: "Norwegian Krone (kr)",
  DKK: "Danish Krone (kr)",
  JPY: "Japanese Yen (¥)",
  CNY: "Chinese Yuan (¥)",
  AED: "UAE Dirham",
  CAD: "Canadian Dollar (C$)",
  AUD: "Australian Dollar (A$)",
  CHF: "Swiss Franc",
  KRW: "South Korean Won (₩)",
  SAR: "Saudi Riyal",
  SGD: "Singapore Dollar",
};

export function normalizeCurrency(raw: string): string {
  if (!raw) return "";
  const cleaned = raw.trim().toLowerCase().replace(/\.+$/, "");

  // 1. Direct dictionary match
  if (CURRENCY_SYNONYMS[cleaned]) {
    return CURRENCY_SYNONYMS[cleaned];
  }

  // 2. Already uppercase 3-letter ISO code
  const upper = raw.trim().toUpperCase();
  if (CURRENCY_NAMES[upper] || upper.length === 3) {
    return upper;
  }

  // 3. Regex / typo fallbacks
  if (/^(?:india[n]?\s+)?rup+[e|p]*s*$/i.test(cleaned)) {
    return "INR";
  }
  if (/^(?:u\.?s\.?\s+)?(?:dollars?|bucks?)$/i.test(cleaned)) {
    return "USD";
  }
  if (/^(?:kr|krona|kronor|kroner|krone)$/i.test(cleaned)) {
    return "SEK";
  }

  return upper;
}

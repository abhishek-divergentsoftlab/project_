/** Mirrors the Pydantic schemas in backend/schemas. */

export type UserRole = "buyer" | "seller" | "both";
export type UserStatus = "pending_verification" | "active" | "suspended" | "deleted";
export type RFQRole = "buyer" | "seller";
export type RFQStatus = "draft" | "active" | "expired" | "closed";
export type EmbeddingStatus = "pending" | "indexed" | "failed" | "stale";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface Profile {
  name: string;
  company_name: string | null;
  phone: string | null;
  address: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  latitude: number | null;
  longitude: number | null;
}

export interface User {
  id: string;
  email: string;
  role: UserRole;
  status: UserStatus;
  created_at: string;
  profile: Profile | null;
}

export interface Quantity {
  value: number;
  unit: string;
}

export interface Money {
  amount: number;
  currency: string;
  per_unit: string | null;
}

export interface RFQLocation {
  city: string | null;
  state: string | null;
  country: string | null;
  latitude: number | null;
  longitude: number | null;
  raw: string | null;
}

export interface DeadlineOut {
  date: string | null;
  raw: string | null;
}

/** Free-form by design: attributes differ per product type. */
export type ProductDetails = Record<string, unknown>;

export interface RFQ {
  id: string;
  user_id: string;
  role: RFQRole;
  status: RFQStatus;
  category: string;
  title: string;
  description: string | null;
  quantity: Quantity | null;
  minimum_order: Quantity | null;
  price_target: Money | null;
  location: RFQLocation | null;
  deadline: DeadlineOut | null;
  product_details: ProductDetails;
  search_tags: string[];
  pending_connections: number;
  embedding_status: EmbeddingStatus;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RFQList {
  items: RFQ[];
  total: number;
  limit: number;
  offset: number;
}

export interface RFQCreatePayload {
  role: RFQRole;
  category: string;
  title: string;
  description?: string | null;
  quantity?: Quantity;
  minimum_order?: Quantity;
  price_target?: { amount: number; currency: string; per_unit?: string | null };
  location?: Partial<RFQLocation>;
  deadline?: { date?: string; in_days?: number; raw?: string };
  product_details?: ProductDetails;
  status?: RFQStatus;
}

/** Per-dimension score. A null dimension could not be evaluated. */
export interface MatchScore {
  total: number;
  relevance: number | null;
  attributes: number | null;
  category: number | null;
  price: number | null;
  quantity: number | null;
  location: number | null;
  deadline: number | null;
}

/**
 * Who is on the other side, and how much of them this viewer may see.
 * `email`/`phone`/`contact_name` are populated by the API only once
 * `connection_status` is "accepted".
 */
export interface Counterparty {
  company_name: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  member_since: string | null;
  connection_id: string | null;
  connection_status: ConnectionStatus | null;
  contact_name: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
}

export interface MatchCandidate {
  rfq_id: string;
  role: RFQRole;
  rank: number;
  title: string;
  category: string;
  description: string | null;
  quantity: Quantity | null;
  price: Money | null;
  location: RFQLocation | null;
  deadline: DeadlineOut | null;
  product_details: ProductDetails;
  search_tags: string[];
  /** Great-circle km, when both listings are geocoded. */
  distance_km: number | null;
  counterparty: Counterparty;
  score: MatchScore;
}

export interface MatchResponse {
  search_id: string;
  requester_role: RFQRole;
  target_role: RFQRole;
  results: MatchCandidate[];
  total: number;
  limit: number;
  offset: number;
}

export interface SearchRequirements {
  role: RFQRole;
  product: string | null;
  category: string | null;
  attributes: Record<string, unknown>;
  quantity: Quantity | null;
  price: Money | null;
  city: string | null;
  deadline_days: number | null;
  skipped: string[];
}

export interface DirectSearchResponse {
  conversation_id: string;
  reply: string;
  requirements: SearchRequirements;
  pending_question: string | null;
  missing: string[];
  results: MatchCandidate[];
  total: number;
  search_id: string | null;
}

export type ConnectionStatus = "pending" | "accepted" | "rejected";

export interface Connection {
  id: string;
  sender_id: string;
  receiver_id: string;
  rfq_id: string;
  status: ConnectionStatus;
  created_at: string;
  updated_at: string;
  /** Whose request this is, from the signed-in account's point of view. */
  direction: "sent" | "received";
  rfq_title: string | null;
  rfq_role: RFQRole | null;
  counterparty: Counterparty;
  message_count: number;
  last_message_at: string | null;
}

export interface ConnectionMessage {
  id: string;
  connection_id: string;
  sender_id: string;
  content: string;
  created_at: string;
}

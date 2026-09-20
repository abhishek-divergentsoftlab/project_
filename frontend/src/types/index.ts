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
  gst_number?: string | null;
  legal_business_name?: string | null;
  business_type?: string | null;
  registration_number?: string | null;
  year_established?: number | null;
  website?: string | null;
  pan_number?: string | null;
  signatory_name?: string | null;
  kyc_status?: KYCStatus;
  trust_score?: number;
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
  excludes_transport?: boolean;
  estimated_delivery_at?: string | null;
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
  average_rating?: number | null;
  total_reviews?: number;
  trust_score?: number | null;
  gst_verified?: boolean;
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
  logistics?: LogisticsEstimate | null;
  counterparty: Counterparty;
  score: MatchScore;
}

export interface LogisticsEstimate {
  mode: string;
  transit_days_min: number;
  transit_days_max: number;
  label: string;
  customs_required: boolean;
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
  state?: string | null;
  country?: string | null;
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
  blocked?: boolean;
  block_reason?: string | null;
  block_category?: string | null;
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
  image_url?: string | null;
  is_live_capture?: boolean;
  created_at: string;
}

export type QuotationStatus =
  | "pending"
  | "accepted"
  | "rejected"
  | "countered"
  | "expired"
  | "dispatched"
  | "delivered"
  | "received"
  | "completed";
export type Incoterm = "EXW" | "FOB" | "CIF" | "CFR" | "DDP" | "CIP";

export type KYCStatus = "unverified" | "pending" | "verified" | "flagged";
export type CertificationStatus = "pending" | "verified" | "rejected";

export interface Quotation {
  id: string;
  connection_id: string;
  sender_id: string;
  receiver_id: string;
  rfq_id: string;
  quote_number: string;
  version: number;
  status: QuotationStatus;
  unit_price: number;
  currency: string;
  quantity: number;
  quantity_unit: string;
  total_amount: number;
  lead_time_days?: number | null;
  incoterms?: Incoterm | null;
  payment_terms?: string | null;
  valid_until?: string | null;
  notes?: string | null;
  purchase_order_reference?: string | null;
  created_at: string;
  updated_at: string;
  is_sender: boolean;
}

export interface QuotationCreatePayload {
  unit_price: number;
  currency: string;
  quantity: number;
  quantity_unit: string;
  lead_time_days?: number;
  incoterms?: Incoterm;
  payment_terms?: string;
  valid_days?: number;
  notes?: string;
}

export interface Review {
  id: string;
  quotation_id: string;
  connection_id: string;
  reviewer_id: string;
  reviewee_id: string;
  rating: number;
  communication_rating?: number | null;
  delivery_rating?: number | null;
  quality_rating?: number | null;
  comment?: string | null;
  created_at: string;
  reviewer_name?: string | null;
  reviewer_company?: string | null;
}

export interface ReviewCreatePayload {
  rating: number;
  communication_rating?: number;
  delivery_rating?: number;
  quality_rating?: number;
  comment?: string;
}

export interface UserReviewStats {
  user_id: string;
  average_rating: number;
  total_reviews: number;
  rating_breakdown: Record<number, number>;
  recent_reviews: Review[];
}

export interface Certificate {
  id: string;
  user_id: string;
  name: string;
  issuing_body: string;
  certificate_number: string;
  issue_date: string;
  expiry_date?: string | null;
  document_url?: string | null;
  verification_status: CertificationStatus;
  created_at: string;
}

export interface CertificateCreatePayload {
  name: string;
  issuing_body: string;
  certificate_number: string;
  issue_date: string;
  expiry_date?: string;
  document_url?: string;
}

export interface KYCVerificationPayload {
  gst_number: string;
  legal_business_name: string;
  business_type: string;
  registration_number?: string;
  year_established?: number;
  website?: string;
  pan_number?: string;
  signatory_name?: string;
}

export interface KYCStatusOut {
  kyc_status: KYCStatus;
  trust_score: number;
  gst_number: string | null;
  legal_business_name: string | null;
  message: string;
}

export interface ModerationCheckResult {
  is_safe: boolean;
  category?: string | null;
  reason?: string | null;
  flagged_terms: string[];
}


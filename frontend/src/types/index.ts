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

export interface CertificateUploadResponse {
  document_url: string;
  filename: string;
  file_size: number;
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

export type DealIssueCategory =
  | "quality"
  | "delivery"
  | "packaging"
  | "payment"
  | "specs"
  | "documentation"
  | "other";

export type DealIssueSeverity = "low" | "medium" | "high";
export type DealIssueStatus = "open" | "in_discussion" | "resolved";

export interface DealIssue {
  id: string;
  connection_id: string;
  title: string;
  category: DealIssueCategory;
  severity: DealIssueSeverity;
  description: string;
  suggested_resolution?: string;
  status: DealIssueStatus;
  reported_by: string;
  reporter_name: string;
  created_at: string;
  resolved_at?: string;
  resolution_notes?: string;
}

export interface AgentInfo {
  id: string;
  name: string;
  icon: string;
  description: string;
  short_description: string;
  suggested_prompts: Array<{ label: string; prompt: string }>;
}

export interface DashboardStats {
  rfqs: {
    total: number;
    active: number;
    draft: number;
    closed: number;
    expired: number;
  };
  connections: {
    total: number;
    pending: number;
    accepted: number;
    rejected: number;
    pending_received: number;
  };
  quotations: {
    total: number;
    pending: number;
    accepted: number;
    in_transit: number;
    completed: number;
  };
  messages_sent: number;
  profile_completeness: number;
  trust_score: number;
  average_rating: number | null;
  total_reviews: number;
}

export interface ActivityItem {
  id: string;
  type: "connection" | "quotation" | "message";
  icon: string;
  title: string;
  body: string;
  link: string;
  timestamp: string;
}

export interface NotificationItem {
  id: string;
  user_id: string;
  type: string;
  title: string;
  body: string | null;
  link: string | null;
  is_read: boolean;
  created_at: string;
}

export interface NotificationList {
  items: NotificationItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CatalogCounterparty {
  company_name: string | null;
  contact_name: string | null;
  city: string | null;
  country: string | null;
  kyc_status: KYCStatus;
  trust_score: number;
  verified_certs: string[];
  connection_id: string | null;
  connection_status: "pending" | "accepted" | "rejected" | null;
  connection_direction: "sent" | "received" | null;
}

export interface CatalogItem {
  id: string;
  user_id: string;
  role: RFQRole;
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
  created_at: string;
  counterparty: CatalogCounterparty;
  distance_km: number | null;
}

export interface CatalogListResponse {
  items: CatalogItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CategoryCount {
  category: string;
  total_count: number;
  seller_count: number;
  buyer_count: number;
}

export interface CatalogFilterParams {
  q?: string;
  role?: RFQRole;
  category?: string;
  city?: string;
  country?: string;
  min_price?: number;
  max_price?: number;
  currency?: string;
  verified_only?: boolean;
  sort_by?: "newest" | "price_asc" | "price_desc" | "trust_desc";
  limit?: number;
  offset?: number;
}

export interface LanguageInfo {
  code: string;
  name: string;
  native_name: string;
  flag: string;
}

export interface TranslationResponse {
  original_text: string;
  translated_text: string;
  source_language: string;
  target_language: string;
  cached: boolean;
}

export interface BatchTranslationResponse {
  translations: TranslationResponse[];
}

export interface EscrowMilestone {
  id: string;
  escrow_account_id: string;
  title: string;
  percentage: number | string;
  amount: number | string;
  order_index: number;
  status: "pending" | "funded" | "release_requested" | "released" | "disputed" | "refunded";
  released_at?: string | null;
  release_note?: string | null;
  created_at: string;
}

export interface DealDispute {
  id: string;
  connection_id: string;
  escrow_account_id?: string | null;
  raised_by_id: string;
  title: string;
  category: string;
  reason: string;
  severity: "low" | "medium" | "high" | "critical";
  status: "open" | "under_review" | "resolved_release_funds" | "resolved_refund_buyer" | "resolved_mutual_settlement" | "cancelled" | string;
  suggested_resolution?: string | null;
  resolution_notes?: string | null;
  resolved_at?: string | null;
  created_at: string;
}

export interface EscrowAccount {
  id: string;
  connection_id: string;
  quotation_id: string;
  buyer_id: string;
  seller_id: string;
  currency: string;
  total_amount: number | string;
  funded_amount: number | string;
  released_amount: number | string;
  refunded_amount: number | string;
  status: "pending_deposit" | "funded" | "partially_released" | "completed" | "disputed" | "refunded";
  milestones: EscrowMilestone[];
  disputes: DealDispute[];
  created_at: string;
  updated_at: string;
}

export interface EscrowDepositPayload {
  payment_method?: string;
  notes?: string;
}

export interface MilestoneReleaseRequestPayload {
  proof_note?: string;
}

export interface MilestoneReleaseApprovePayload {
  note?: string;
}

export interface DealDisputeCreatePayload {
  title: string;
  category?: string;
  reason: string;
  severity?: string;
  suggested_resolution?: string;
}

export interface DealDisputeResolvePayload {
  resolution: "release_funds" | "refund_buyer" | "mutual_settlement";
  resolution_notes: string;
}

// ==========================================
// Logistics, Freight & Shipment Tracking
// ==========================================

export type ShippingMode = "road" | "ocean" | "air" | "courier";

export type ShipmentStatus =
  | "booked"
  | "dispatched"
  | "in_transit"
  | "customs_hold"
  | "customs_cleared"
  | "out_for_delivery"
  | "delivered"
  | "exception";

export interface TrackingEvent {
  status: string;
  location?: string;
  timestamp: string;
  note?: string;
}

export interface Shipment {
  id: string;
  connection_id: string;
  quotation_id?: string | null;
  sender_id: string;
  receiver_id: string;
  carrier_name: string;
  tracking_number: string;
  tracking_url?: string | null;
  shipping_mode: ShippingMode;
  origin_city: string;
  origin_country: string;
  destination_city: string;
  destination_country: string;
  incoterm: string;
  status: ShipmentStatus;
  gross_weight_kg?: number | null;
  chargeable_weight_kg?: number | null;
  cbm?: number | null;
  packages_count?: number | null;
  estimated_delivery?: string | null;
  actual_dispatch_date?: string | null;
  delivered_at?: string | null;
  shipping_cost?: number | null;
  currency: string;
  customs_declaration_no?: string | null;
  notes?: string | null;
  tracking_events: TrackingEvent[];
  created_at: string;
  updated_at: string;
}

export interface FreightRateOption {
  mode: ShippingMode | string;
  mode_id?: string;
  mode_label: string;
  mode_name?: string;
  carrier_sample?: string;
  transit_days_min: number;
  transit_days_max: number;
  base_freight_usd?: number;
  fuel_surcharge_usd?: number;
  documentation_fee_usd?: number;
  customs_clearance_usd?: number;
  total_estimated_usd: number;
  rate_amount?: number;
  chargeable_weight_kg: number;
  basis: string;
  recommended: boolean;
  is_recommended?: boolean;
  description?: string;
}

export interface IncotermCostBreakdown {
  incoterm: string;
  full_name?: string;
  seller_responsibility: string;
  buyer_responsibility: string;
  seller_pays?: string[];
  buyer_pays?: string[];
  risk_transfer_point: string;
  estimated_seller_logistics_usd: number;
  estimated_buyer_logistics_usd: number;
  seller_estimated_cost?: number;
  buyer_estimated_cost?: number;
  currency?: string;
}

export interface FreightEstimateRequest {
  origin_country: string;
  origin_city?: string;
  destination_country: string;
  destination_city?: string;
  gross_weight_kg?: number;
  weight_kg?: number;
  length_cm?: number;
  width_cm?: number;
  height_cm?: number;
  cbm?: number;
  volume_cbm?: number;
  incoterm?: string;
  cargo_value?: number;
  currency?: string;
}

export interface FreightEstimateResponse {
  origin: string;
  destination: string;
  distance_km?: number;
  is_cross_border?: boolean;
  gross_weight_kg: number;
  volumetric_weight_kg?: number;
  chargeable_weight_kg?: number;
  volume_cbm?: number;
  cbm?: number;
  currency?: string;
  rates: FreightRateOption[];
  rate_options?: FreightRateOption[];
  incoterm_breakdown: IncotermCostBreakdown;
}

export interface ShipmentCreatePayload {
  quotation_id?: string;
  carrier_name: string;
  tracking_number: string;
  tracking_url?: string;
  shipping_mode: ShippingMode;
  origin_city: string;
  origin_country: string;
  destination_city: string;
  destination_country: string;
  incoterm?: string;
  gross_weight_kg?: number;
  length_cm?: number;
  width_cm?: number;
  height_cm?: number;
  cbm?: number;
  packages_count?: number;
  estimated_delivery?: string;
  shipping_cost?: number;
  customs_declaration_no?: string;
  notes?: string;
  trigger_escrow_milestone?: boolean;
}

export interface ShipmentStatusUpdatePayload {
  status: ShipmentStatus;
  location?: string;
  note?: string;
}


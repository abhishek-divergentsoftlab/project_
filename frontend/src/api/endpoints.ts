import { api, tokenStore } from "@/api/client";
import type {
  DirectSearchResponse,
  MatchResponse,
  Profile,
  RFQ,
  RFQCreatePayload,
  RFQList,
  RFQStatus,
  TokenPair,
  User,
  UserRole,
  Connection,
  ConnectionMessage,
  ConnectionStatus,
  Quotation,
  QuotationCreatePayload,
  Certificate,
  CertificateCreatePayload,
  KYCStatusOut,
  KYCVerificationPayload,
  ModerationCheckResult,
  Review,
  ReviewCreatePayload,
  UserReviewStats,
} from "@/types";


export interface SignupPayload {
  email: string;
  password: string;
  name: string;
  role: UserRole;
  company_name?: string;
  phone?: string;
  city?: string;
}

export const auth = {
  async signup(payload: SignupPayload): Promise<TokenPair> {
    const { data } = await api.post<TokenPair>("/auth/signup", payload);
    tokenStore.save(data);
    return data;
  },
  async login(email: string, password: string): Promise<TokenPair> {
    const { data } = await api.post<TokenPair>("/auth/login", { email, password });
    tokenStore.save(data);
    return data;
  },
  async me(): Promise<User> {
    const { data } = await api.get<User>("/auth/me");
    return data;
  },
  logout() {
    tokenStore.clear();
  },
};

export const users = {
  async updateProfile(payload: Partial<Profile>): Promise<Profile> {
    const { data } = await api.patch<Profile>("/users/me/profile", payload);
    return data;
  },
  /** Which side of the market this account may post on. */
  async updateRole(role: UserRole): Promise<User> {
    const { data } = await api.patch<User>("/users/me", { role });
    return data;
  },
};

export const matching = {
  /** Direction is decided by the RFQ's own role, never by the caller. */
  async forRfq(rfqId: string, limit = 10, offset = 0): Promise<MatchResponse> {
    const { data } = await api.post<MatchResponse>(`/rfqs/${rfqId}/matches`, null, {
      params: { limit, offset },
    });
    return data;
  },
};

export const directSearch = {
  /** Omit conversationId to start fresh; pass it back to refine the same search. */
  async send(message: string, conversationId?: string): Promise<DirectSearchResponse> {
    const { data } = await api.post<DirectSearchResponse>("/search", {
      message,
      conversation_id: conversationId ?? null,
    });
    return data;
  },
};

export const rfqs = {
  async list(params: { status?: RFQStatus; limit?: number; offset?: number } = {}): Promise<RFQList> {
    const { data } = await api.get<RFQList>("/rfqs", { params });
    return data;
  },
  async get(id: string): Promise<RFQ> {
    const { data } = await api.get<RFQ>(`/rfqs/${id}`);
    return data;
  },
  async create(payload: RFQCreatePayload): Promise<RFQ> {
    const { data } = await api.post<RFQ>("/rfqs", payload);
    return data;
  },
  /** Partial: fields left out are untouched, so a typo can be fixed in place. */
  async update(id: string, payload: Partial<RFQCreatePayload>): Promise<RFQ> {
    const { data } = await api.patch<RFQ>(`/rfqs/${id}`, payload);
    return data;
  },
  async publish(id: string): Promise<RFQ> {
    const { data } = await api.post<RFQ>(`/rfqs/${id}/publish`);
    return data;
  },
  async close(id: string): Promise<RFQ> {
    const { data } = await api.post<RFQ>(`/rfqs/${id}/close`);
    return data;
  },
  async listConnections(id: string): Promise<Connection[]> {
    const { data } = await api.get<Connection[]>(`/rfqs/${id}/connections`);
    return data;
  },
};

export const connections = {
  async listAll(params: { status?: ConnectionStatus } = {}): Promise<Connection[]> {
    const { data } = await api.get<Connection[]>("/connections", { params });
    return data;
  },
  async create(rfqId: string): Promise<Connection> {
    const { data } = await api.post<Connection>("/connections", { rfq_id: rfqId });
    return data;
  },
  async get(connectionId: string): Promise<Connection> {
    const { data } = await api.get<Connection>(`/connections/${connectionId}`);
    return data;
  },
  async accept(connectionId: string): Promise<Connection> {
    const { data } = await api.post<Connection>(`/connections/${connectionId}/accept`);
    return data;
  },
  async reject(connectionId: string): Promise<Connection> {
    const { data } = await api.post<Connection>(`/connections/${connectionId}/reject`);
    return data;
  },
  async listMessages(connectionId: string): Promise<ConnectionMessage[]> {
    const { data } = await api.get<ConnectionMessage[]>(`/connections/${connectionId}/messages`);
    return data;
  },
  async sendMessage(connectionId: string, content: string): Promise<ConnectionMessage> {
    const { data } = await api.post<ConnectionMessage>(`/connections/${connectionId}/messages`, { content });
    return data;
  },
  async sendLiveCapture(connectionId: string, imageBlob: Blob, caption?: string): Promise<ConnectionMessage> {
    const formData = new FormData();
    formData.append("image", imageBlob, "live_camera_snapshot.jpg");
    if (caption && caption.trim()) {
      formData.append("caption", caption.trim());
    }
    const { data } = await api.post<ConnectionMessage>(
      `/connections/${connectionId}/messages/live-capture`,
      formData,
      { headers: { "Content-Type": "multipart/form-data" } }
    );
    return data;
  },
};

export const quotations = {
  async list(connectionId: string): Promise<Quotation[]> {
    const { data } = await api.get<Quotation[]>(`/connections/${connectionId}/quotes`);
    return data;
  },
  async create(connectionId: string, payload: QuotationCreatePayload): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes`, payload);
    return data;
  },
  async accept(connectionId: string, quoteId: string): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes/${quoteId}/accept`);
    return data;
  },
  async reject(connectionId: string, quoteId: string, reason?: string): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes/${quoteId}/reject`, { reason });
    return data;
  },
};

export const reviews = {
  async listQuoteReviews(connectionId: string, quoteId: string): Promise<Review[]> {
    const { data } = await api.get<Review[]>(`/connections/${connectionId}/quotes/${quoteId}/reviews`);
    return data;
  },
  async dispatchQuote(connectionId: string, quoteId: string): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes/${quoteId}/dispatch`);
    return data;
  },
  async markDelivered(connectionId: string, quoteId: string): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes/${quoteId}/deliver`);
    return data;
  },
  async acceptDelivery(connectionId: string, quoteId: string): Promise<Quotation> {
    const { data } = await api.post<Quotation>(`/connections/${connectionId}/quotes/${quoteId}/accept-delivery`);
    return data;
  },
  async rate(connectionId: string, quoteId: string, payload: ReviewCreatePayload): Promise<Review> {
    const { data } = await api.post<Review>(`/connections/${connectionId}/quotes/${quoteId}/rate`, payload);
    return data;
  },
  async getUserReviews(userId: string): Promise<UserReviewStats> {
    const { data } = await api.get<UserReviewStats>(`/users/${userId}/reviews`);
    return data;
  },
};

export const certifications = {
  async list(): Promise<Certificate[]> {
    const { data } = await api.get<Certificate[]>("/certifications");
    return data;
  },
  async create(payload: CertificateCreatePayload): Promise<Certificate> {
    const { data } = await api.post<Certificate>("/certifications", payload);
    return data;
  },
  async delete(certificateId: string): Promise<void> {
    await api.delete(`/certifications/${certificateId}`);
  },
  async getPublic(userId: string): Promise<Certificate[]> {
    const { data } = await api.get<Certificate[]>(`/certifications/user/${userId}`);
    return data;
  },
};

export const kyc = {
  async verify(payload: KYCVerificationPayload): Promise<KYCStatusOut> {
    const { data } = await api.post<KYCStatusOut>("/users/me/kyc/verify", payload);
    return data;
  },
};

export const moderation = {
  async check(title: string, description?: string, category?: string): Promise<ModerationCheckResult> {
    const { data } = await api.post<ModerationCheckResult>("/moderation/check", {
      title,
      description,
      category,
    });
    return data;
  },
};


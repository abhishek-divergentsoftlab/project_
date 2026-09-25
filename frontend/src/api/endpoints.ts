import { api, tokenStore } from "@/api/client";
import type {
  MatchCandidate,
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
  CertificateUploadResponse,
  KYCStatusOut,
  KYCVerificationPayload,
  ModerationCheckResult,
  Review,
  ReviewCreatePayload,
  UserReviewStats,
  AgentInfo,
  DashboardStats,
  ActivityItem,
  NotificationItem,
  NotificationList,
  CatalogFilterParams,
  CatalogItem,
  CatalogListResponse,
  CategoryCount,
  LanguageInfo,
  TranslationResponse,
  BatchTranslationResponse,
  EscrowAccount,
  EscrowMilestone,
  DealDispute,
  EscrowDepositPayload,
  MilestoneReleaseRequestPayload,
  MilestoneReleaseApprovePayload,
  DealDisputeCreatePayload,
  DealDisputeResolvePayload,
  Shipment,
  TrackingEvent,
  ShippingMode,
  ShipmentStatus,
  FreightRateOption,
  IncotermCostBreakdown,
  FreightEstimateRequest,
  FreightEstimateResponse,
  ShipmentCreatePayload,
  ShipmentStatusUpdatePayload,
} from "@/types";

export type {
  AgentInfo,
  DashboardStats,
  ActivityItem,
  NotificationItem,
  NotificationList,
  CatalogFilterParams,
  CatalogItem,
  CatalogListResponse,
  CategoryCount,
  LanguageInfo,
  TranslationResponse,
  BatchTranslationResponse,
  EscrowAccount,
  EscrowMilestone,
  DealDispute,
  EscrowDepositPayload,
  MilestoneReleaseRequestPayload,
  MilestoneReleaseApprovePayload,
  DealDisputeCreatePayload,
  DealDisputeResolvePayload,
  Shipment,
  TrackingEvent,
  ShippingMode,
  ShipmentStatus,
  FreightRateOption,
  IncotermCostBreakdown,
  FreightEstimateRequest,
  FreightEstimateResponse,
  ShipmentCreatePayload,
  ShipmentStatusUpdatePayload,
};



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
  async downloadPoPdf(connectionId: string, quoteId: string): Promise<Blob> {
    const { data } = await api.get(`/connections/${connectionId}/quotes/${quoteId}/po-pdf`, {
      responseType: "blob",
    });
    return data;
  },
  async downloadInvoicePdf(connectionId: string, quoteId: string): Promise<Blob> {
    const { data } = await api.get(`/connections/${connectionId}/quotes/${quoteId}/invoice-pdf`, {
      responseType: "blob",
    });
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
  async uploadDocument(file: File): Promise<CertificateUploadResponse> {
    const formData = new FormData();
    formData.append("file", file);
    const { data } = await api.post<CertificateUploadResponse>("/certifications/upload", formData, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return data;
  },
  async uploadToCertificate(certificateId: string, file: File): Promise<Certificate> {
    const formData = new FormData();
    formData.append("file", file);
    const { data } = await api.post<Certificate>(`/certifications/${certificateId}/document`, formData, {
      headers: { "Content-Type": "multipart/form-data" },
    });
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

export interface AICreatedRFQ {
  id: string;
  title: string;
  role: string;
  status: string;
  category?: string;
}

export type { MatchCandidate };

export interface AICounterpartyMessage {
  connection_id: string;
  counterparty_name: string;
  message: string;
  message_id?: string;
  status: string;
  created_at: string;
}

export interface AIChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AIChatResponse {
  reply: string;
  thinking?: string | null;
  rfq_draft?: Record<string, any> | null;
  readiness?: Record<string, any> | null;
  created_rfq?: AICreatedRFQ | null;
  counterparty_message?: AICounterpartyMessage | null;
  conversation_id?: string | null;
}

export interface AIToolStep {
  name: string;
  title: string;
  status: "running" | "completed";
  args?: Record<string, any> | string | null;
}

export interface AIChatChunk {
  content: string;
  thinking?: string;
  thinking_after?: string;
  tool_step?: AIToolStep | null;
  rfq_draft?: Record<string, any> | null;
  readiness?: Record<string, any> | null;
  created_rfq?: AICreatedRFQ | null;
  counterparty_message?: AICounterpartyMessage | null;
  conversation_id?: string | null;
  done: boolean;
}

export interface AIConversationSummary {
  id: string;
  title: string | null;
  type: string;
  state: Record<string, any>;
  rfq_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AIConversationMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  thinking?: string | null;
  thinking_after?: string | null;
  tool_step?: AIToolStep | null;
  created_rfq?: AICreatedRFQ | null;
  counterparty_message?: AICounterpartyMessage | null;
  created_at: string;
}

export interface AIConversationDetail {
  id: string;
  title: string | null;
  type: string;
  state: Record<string, any>;
  rfq_id: string | null;
  created_at: string;
  updated_at: string;
  messages: AIConversationMessage[];
}

export const aiChat = {
  async listAgents(): Promise<AgentInfo[]> {
    const { data } = await api.get<AgentInfo[]>("/ai-chat/agents");
    return data;
  },

  async listConversations(): Promise<AIConversationSummary[]> {
    const { data } = await api.get<AIConversationSummary[]>("/ai-chat/conversations");
    return data;
  },

  async getConversation(id: string): Promise<AIConversationDetail> {
    const { data } = await api.get<AIConversationDetail>(`/ai-chat/conversations/${id}`);
    return data;
  },

  async deleteConversation(id: string): Promise<void> {
    await api.delete(`/ai-chat/conversations/${id}`);
  },

  async markDraftSent(conversationId: string, messageId: string, sentMessageId?: string): Promise<void> {
    await api.post(`/ai-chat/conversations/${conversationId}/messages/${messageId}/mark-sent`, {
      sent_message_id: sentMessageId,
    });
  },

  async sendMessage(
    messages: AIChatMessage[],
    currentRfq?: Record<string, any> | null,
    conversationId?: string | null,
    matchedCandidates?: MatchCandidate[] | null,
    activeConnectionId?: string | null,
    agentId?: string | null,
  ): Promise<AIChatResponse> {
    const { data } = await api.post<AIChatResponse>("/ai-chat/message", {
      messages,
      current_rfq: currentRfq,
      conversation_id: conversationId,
      matched_candidates: matchedCandidates,
      active_connection_id: activeConnectionId,
      agent_id: agentId,
    });
    return data;
  },

  async streamMessage(
    messages: AIChatMessage[],
    onChunk: (chunk: AIChatChunk) => void,
    currentRfq?: Record<string, any> | null,
    signal?: AbortSignal,
    conversationId?: string | null,
    matchedCandidates?: MatchCandidate[] | null,
    activeConnectionId?: string | null,
    agentId?: string | null,
  ): Promise<void> {
    const token = tokenStore.access();
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "/api/v1";
    const response = await fetch(`${baseUrl}/ai-chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        messages,
        current_rfq: currentRfq,
        conversation_id: conversationId,
        matched_candidates: matchedCandidates,
        active_connection_id: activeConnectionId,
        agent_id: agentId,
      }),
      signal,
    });

    if (!response.ok) {
      const errText = await response.text().catch(() => "");
      throw new Error(`AI Chat streaming error (${response.status}): ${errText || response.statusText}`);
    }

    if (!response.body) {
      throw new Error("Streaming response body is unavailable.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed.startsWith("data: ")) {
            try {
              const chunk: AIChatChunk = JSON.parse(trimmed.slice(6));
              onChunk(chunk);
            } catch {
              // Ignore partial JSON
            }
          }
        }
      }

      if (buffer.trim().startsWith("data: ")) {
        try {
          const chunk: AIChatChunk = JSON.parse(buffer.trim().slice(6));
          onChunk(chunk);
        } catch {
          // Ignore
        }
      }
    } finally {
      reader.releaseLock();
    }
  },
};

export const dashboard = {
  async getStats(): Promise<DashboardStats> {
    const { data } = await api.get<DashboardStats>("/dashboard/stats");
    return data;
  },
  async getActivity(limit = 20): Promise<ActivityItem[]> {
    const { data } = await api.get<ActivityItem[]>("/dashboard/activity", { params: { limit } });
    return data;
  },
};

export const notifications = {
  async list(params: { limit?: number; offset?: number; unread_only?: boolean } = {}): Promise<NotificationList> {
    const { data } = await api.get<NotificationList>("/notifications", { params });
    return data;
  },
  async getUnreadCount(): Promise<{ count: number }> {
    const { data } = await api.get<{ count: number }>("/notifications/unread-count");
    return data;
  },
  async markRead(id: string): Promise<{ ok: boolean }> {
    const { data } = await api.post<{ ok: boolean }>(`/notifications/${id}/read`);
    return data;
  },
  async markAllRead(): Promise<{ marked: number }> {
    const { data } = await api.post<{ marked: number }>("/notifications/read-all");
    return data;
  },
};

export const marketplace = {
  async getCatalog(params: CatalogFilterParams = {}): Promise<CatalogListResponse> {
    const { data } = await api.get<CatalogListResponse>("/marketplace/catalog", { params });
    return data;
  },
  async getCategories(): Promise<CategoryCount[]> {
    const { data } = await api.get<CategoryCount[]>("/marketplace/categories");
    return data;
  },
};

export const translation = {
  async getSupportedLanguages(): Promise<LanguageInfo[]> {
    const { data } = await api.get<LanguageInfo[]>("/translation/languages");
    return data;
  },
  async translateText(text: string, targetLanguage: string, sourceLanguage?: string): Promise<TranslationResponse> {
    const { data } = await api.post<TranslationResponse>("/translation/translate", {
      text,
      target_language: targetLanguage,
      source_language: sourceLanguage,
    });
    return data;
  },
  async batchTranslate(texts: string[], targetLanguage: string, sourceLanguage?: string): Promise<BatchTranslationResponse> {
    const { data } = await api.post<BatchTranslationResponse>("/translation/batch", {
      texts,
      target_language: targetLanguage,
      source_language: sourceLanguage,
    });
    return data;
  },
  async translateConnectionMessage(
    connectionId: string,
    text: string,
    targetLanguage: string,
    sourceLanguage?: string,
  ): Promise<TranslationResponse> {
    const { data } = await api.post<TranslationResponse>(`/translation/connections/${connectionId}/translate`, {
      text,
      target_language: targetLanguage,
      source_language: sourceLanguage,
    });
    return data;
  },
};

export const escrow = {
  async getEscrow(connectionId: string): Promise<EscrowAccount> {
    const { data } = await api.get<EscrowAccount>(`/connections/${connectionId}/escrow`);
    return data;
  },
  async fundEscrow(connectionId: string, payload: EscrowDepositPayload = {}): Promise<EscrowAccount> {
    const { data } = await api.post<EscrowAccount>(`/connections/${connectionId}/escrow/fund`, payload);
    return data;
  },
  async requestMilestoneRelease(
    connectionId: string,
    milestoneId: string,
    payload: MilestoneReleaseRequestPayload = {},
  ): Promise<EscrowAccount> {
    const { data } = await api.post<EscrowAccount>(
      `/connections/${connectionId}/escrow/milestones/${milestoneId}/request-release`,
      payload,
    );
    return data;
  },
  async releaseMilestoneFunds(
    connectionId: string,
    milestoneId: string,
    payload: MilestoneReleaseApprovePayload = {},
  ): Promise<EscrowAccount> {
    const { data } = await api.post<EscrowAccount>(
      `/connections/${connectionId}/escrow/milestones/${milestoneId}/release`,
      payload,
    );
    return data;
  },
  async openDispute(connectionId: string, payload: DealDisputeCreatePayload): Promise<DealDispute> {
    const { data } = await api.post<DealDispute>(`/connections/${connectionId}/disputes`, payload);
    return data;
  },
  async resolveDispute(
    connectionId: string,
    disputeId: string,
    payload: DealDisputeResolvePayload,
  ): Promise<DealDispute> {
    const { data } = await api.post<DealDispute>(`/connections/${connectionId}/disputes/${disputeId}/resolve`, payload);
    return data;
  },
};

export const logistics = {
  async estimateFreight(payload: FreightEstimateRequest): Promise<FreightEstimateResponse> {
    const weight = payload.weight_kg ?? payload.gross_weight_kg ?? 1000;
    const vol = payload.volume_cbm ?? payload.cbm ?? 1.2;
    const body: FreightEstimateRequest = {
      ...payload,
      weight_kg: weight,
      gross_weight_kg: weight,
      volume_cbm: vol,
      cbm: vol,
    };
    const { data } = await api.post<FreightEstimateResponse>("/logistics/estimate", body);
    return data;
  },
  async getConnectionShipment(connectionId: string): Promise<Shipment | null> {
    const { data } = await api.get<Shipment | null>(`/connections/${connectionId}/shipment`);
    return data;
  },
  async createDispatch(connectionId: string, payload: ShipmentCreatePayload): Promise<Shipment> {
    const { data } = await api.post<Shipment>(`/connections/${connectionId}/dispatch`, payload);
    return data;
  },
  async addTrackingEvent(
    shipmentId: string,
    payload: ShipmentStatusUpdatePayload,
  ): Promise<Shipment> {
    const { data } = await api.post<Shipment>(`/shipments/${shipmentId}/events`, payload);
    return data;
  },
  async downloadWaybillPdf(shipmentId: string): Promise<Blob> {
    const { data } = await api.get(`/shipments/${shipmentId}/waybill.pdf`, {
      responseType: "blob",
    });
    return data;
  },
};


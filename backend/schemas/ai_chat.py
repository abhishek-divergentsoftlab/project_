from datetime import datetime
from typing import Any, Literal, Optional
import uuid
from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    """Public descriptor for a specialised AI agent."""
    id: str
    name: str
    icon: str
    description: str
    short_description: str
    suggested_prompts: list[dict[str, str]] = Field(default_factory=list)


class AIChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=5000)


class AIChatRequest(BaseModel):
    messages: list[AIChatMessage] = Field(..., min_length=1, max_length=50)
    current_rfq: Optional[dict[str, Any]] = Field(
        default=None,
        description="Accumulated RFQ draft state maintained across the conversation.",
    )
    conversation_id: Optional[uuid.UUID] = Field(
        default=None,
        description="ID of the conversation to append to, or None to create a new session.",
    )
    matched_candidates: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Currently loaded matching candidates (counterparties) for the active RFQ.",
    )
    active_connection_id: Optional[uuid.UUID] = Field(
        default=None,
        description="ID of the currently active counterparty connection in the side-by-side view.",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="ID of the specialised agent to use (e.g. 'market_research', 'negotiation'). None = general assistant.",
    )


class AIChatResponse(BaseModel):
    reply: str
    thinking: Optional[str] = None
    rfq_draft: Optional[dict[str, Any]] = None
    readiness: Optional[dict[str, Any]] = None
    created_rfq: Optional[dict[str, Any]] = None
    counterparty_message: Optional[dict[str, Any]] = None
    conversation_id: Optional[uuid.UUID] = None


class AIConversationSummary(BaseModel):
    id: uuid.UUID
    title: Optional[str] = None
    type: str
    state: dict[str, Any] = Field(default_factory=dict)
    rfq_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime


class AIConversationMessage(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: Literal["user", "assistant", "system"]
    content: str
    thinking: Optional[str] = None
    thinking_after: Optional[str] = None
    tool_step: Optional[dict[str, Any]] = None
    created_rfq: Optional[dict[str, Any]] = None
    counterparty_message: Optional[dict[str, Any]] = None
    created_at: datetime


class AIConversationDetail(BaseModel):
    id: uuid.UUID
    title: Optional[str] = None
    type: str
    state: dict[str, Any] = Field(default_factory=dict)
    rfq_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    messages: list[AIConversationMessage] = Field(default_factory=list)

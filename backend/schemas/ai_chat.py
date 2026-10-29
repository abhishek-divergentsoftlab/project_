from datetime import datetime
from typing import Any, Literal, Optional
import uuid
from pydantic import BaseModel, Field, field_validator


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

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # A whitespace-only message must be a 422, not an empty "New Business Chat" conversation.
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be empty")
        return stripped


class AIChatRequest(BaseModel):
    messages: list[AIChatMessage] = Field(
        ..., min_length=1, max_length=50,
        description="Only the newest user message is used; the server loads the conversation history from its database.",
    )
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
        description="Accepted for backward compatibility only; the server ignores it (candidates are rebuilt from its own state).",
    )
    active_connection_id: Optional[uuid.UUID] = Field(
        default=None,
        description="ID of the currently active counterparty connection in the side-by-side view.",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="ID of the specialised agent to use (e.g. 'market_research', 'negotiation'). None = general assistant.",
    )
    rfq_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Listing the user is asking about (e.g. from 'Analyze this listing'); sent with the first message.",
    )


class AIChatResponse(BaseModel):
    reply: str
    thinking: Optional[str] = None
    rfq_draft: Optional[dict[str, Any]] = None
    readiness: Optional[dict[str, Any]] = None
    created_rfq: Optional[dict[str, Any]] = None
    counterparty_message: Optional[dict[str, Any]] = None
    conversation_id: Optional[uuid.UUID] = None
    routed_agent: Optional[dict[str, Any]] = None
    matched_candidates: Optional[list[dict[str, Any]]] = None
    draft_suggestions: Optional[dict[str, Any]] = None
    draft_moderation: Optional[dict[str, Any]] = None
    editing_rfq_id: Optional[str] = Field(
        default=None, description="Id of the saved RFQ being edited in this conversation, if any."
    )
    quick_replies: Optional[list[str]] = Field(
        default=None,
        description="Chips for the question the reply ends with; clicking one sends exactly that text as the user's next message.",
    )


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
    routed_agent: Optional[dict[str, Any]] = None
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

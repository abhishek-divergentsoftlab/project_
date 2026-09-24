import json
import uuid
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from api.deps import CurrentUser, DbSession
from schemas.ai_chat import (
    AgentInfo,
    AIChatRequest,
    AIChatResponse,
    AIConversationDetail,
    AIConversationSummary,
)
from services import ai_chat_service
from services.agent_registry import get_agent, list_agents

router = APIRouter(prefix="/ai-chat", tags=["ai-chat"])


@router.get(
    "/conversations",
    response_model=list[AIConversationSummary],
    summary="List previous business AI conversations",
)
async def list_conversations(
    current_user: CurrentUser,
    db: DbSession,
) -> list[AIConversationSummary]:
    """List business AI chat conversations belonging to the authenticated user."""
    convs = await ai_chat_service.list_user_conversations(db, current_user.id)
    return [
        AIConversationSummary(
            id=c.id,
            title=c.title,
            type=c.type.value if hasattr(c.type, "value") else str(c.type),
            state=c.state or {},
            rfq_id=c.rfq_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in convs
    ]


@router.get(
    "/conversations/{conversation_id}",
    response_model=AIConversationDetail,
    summary="Get details and messages of a business AI conversation",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> AIConversationDetail:
    """Retrieve conversation details and chronological messages with RFQ draft state."""
    detail = await ai_chat_service.get_conversation_with_messages(
        db, current_user.id, conversation_id
    )
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return AIConversationDetail(**detail)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a business AI conversation",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> None:
    """Delete a conversation and all its persisted messages."""
    deleted = await ai_chat_service.delete_user_conversation(
        db, current_user.id, conversation_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )


@router.get(
    "/agents",
    response_model=list[AgentInfo],
    summary="List available specialised AI agents",
)
async def list_available_agents() -> list[AgentInfo]:
    """Return the catalogue of specialised agents the chat UI can invoke."""
    return [
        AgentInfo(
            id=a.id,
            name=a.name,
            icon=a.icon,
            description=a.description,
            short_description=a.short_description,
            suggested_prompts=a.suggested_prompts,
        )
        for a in list_agents()
    ]


@router.post(
    "/message",
    response_model=AIChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Chat with the Business AI Assistant (GPT-OSS 20B)",
)
async def chat_message(
    payload: AIChatRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> AIChatResponse:
    """Send conversation messages to the business AI assistant.

    Requires authentication. Messages are evaluated under strict B2B business
    guardrails powered by local Ollama gpt-oss:20b. Persists conversation and
    messages to PostgreSQL.
    """
    raw_messages = [msg.model_dump() for msg in payload.messages]
    agent = get_agent(payload.agent_id)
    result = await ai_chat_service.generate_business_chat_reply(
        raw_messages,
        current_rfq=payload.current_rfq,
        db=db,
        user=current_user,
        conversation_id=payload.conversation_id,
        matched_candidates=payload.matched_candidates,
        active_connection_id=payload.active_connection_id,
        agent=agent,
    )
    return AIChatResponse(
        reply=result["reply"],
        thinking=result.get("thinking"),
        rfq_draft=result.get("rfq_draft"),
        readiness=result.get("readiness"),
        created_rfq=result.get("created_rfq"),
        counterparty_message=result.get("counterparty_message"),
        conversation_id=result.get("conversation_id"),
    )


@router.post(
    "/stream",
    summary="Stream chat with Business AI Assistant (GPT-OSS 20B)",
)
async def chat_stream(
    payload: AIChatRequest,
    current_user: CurrentUser,
    db: DbSession,
):
    """Stream token-by-token business conversation from gpt-oss:20b via Server-Sent Events."""
    raw_messages = [msg.model_dump() for msg in payload.messages]
    agent = get_agent(payload.agent_id)

    async def sse_generator():
        async for chunk in ai_chat_service.stream_business_chat_reply(
            raw_messages,
            current_rfq=payload.current_rfq,
            db=db,
            user=current_user,
            conversation_id=payload.conversation_id,
            matched_candidates=payload.matched_candidates,
            active_connection_id=payload.active_connection_id,
            agent=agent,
        ):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

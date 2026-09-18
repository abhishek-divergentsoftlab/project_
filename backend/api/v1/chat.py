from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import asyncio
import json
from models.schemas import ChatMessage

router = APIRouter()

async def mock_ai_stream(query: str):
    chunks = [
        {"id": 1, "content": "Let me analyze your query first...", "type": "thinking"},
        {"id": 2, "content": "Searching for similar products", "type": "steps"},
        {"id": 3, "content": "I found 3 potential matches based on your criteria.", "type": "text"},
        {"id": 4, "content": "Here are the top results:", "type": "response"}
    ]
    
    for chunk in chunks:
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.5)

@router.post("/ai/chat")
async def ai_chat(chat: ChatMessage):
    # Currently returning mock stream for testing UI
    return StreamingResponse(mock_ai_stream(chat.message), media_type="text/event-stream")

@router.post("/onboarding/chat")
async def onboarding_chat(chat: ChatMessage):
    # This will extract structured data and save to Qdrant
    return StreamingResponse(mock_ai_stream(chat.message), media_type="text/event-stream")

"""Importing this package registers every table on ``Base.metadata``.

Alembic autogeneration and ``create_all`` both depend on that, so new models
must be re-exported here.
"""

from models.conversation import Conversation, Message, MessageEvent
from models.enums import ConversationType, EmbeddingStatus, MessageRole, RFQRole, RFQStatus, UserRole, UserStatus, ConnectionStatus
from models.match import MatchResult, MatchSearch
from models.rfq import RFQ
from models.user import UserProfile, User
from models.connection import Connection, ConnectionMessage

__all__ = [
    "Conversation",
    "ConversationType",
    "EmbeddingStatus",
    "MatchResult",
    "MatchSearch",
    "Message",
    "MessageEvent",
    "MessageRole",
    "UserProfile",
    "RFQ",
    "RFQRole",
    "RFQStatus",
    "User",
    "UserRole",
    "UserStatus",
    "Connection",
    "ConnectionMessage",
    "ConnectionStatus",
]

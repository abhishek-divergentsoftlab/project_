"""Domain enums.

Roles and statuses are backed by native PostgreSQL enum types: they are stable
and benefit from database-level validation. Stream event types deliberately are
NOT an enum -- the event protocol is meant to grow without a migration.
"""

import enum


class UserRole(str, enum.Enum):
    """What an account is allowed to post.

    ``BOTH`` exists because a trader is frequently buyer and seller at once;
    matching direction is decided per RFQ, never by the account role.
    """

    BUYER = "buyer"
    SELLER = "seller"
    BOTH = "both"


class UserStatus(str, enum.Enum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class RFQRole(str, enum.Enum):
    """Which side of the market an RFQ sits on.

    A buyer RFQ is matched against seller RFQs and vice versa.
    """

    BUYER = "buyer"
    SELLER = "seller"

    @property
    def counterpart(self) -> "RFQRole":
        return RFQRole.SELLER if self is RFQRole.BUYER else RFQRole.BUYER


class RFQStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"


class EmbeddingStatus(str, enum.Enum):
    """Tracks Postgres -> Qdrant convergence so the index can be rebuilt."""

    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
    STALE = "stale"


class ConversationType(str, enum.Enum):
    ONBOARDING = "onboarding"
    DIRECT_SEARCH = "direct_search"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ConnectionStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

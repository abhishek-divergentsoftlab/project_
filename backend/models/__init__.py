"""Importing this package registers every table on ``Base.metadata``.

Alembic autogeneration and ``create_all`` both depend on that, so new models
must be re-exported here.
"""

from models.conversation import Conversation, Message, MessageEvent
from models.enums import (
    CertificationStatus,
    ConnectionStatus,
    ConversationType,
    EmbeddingStatus,
    Incoterm,
    KYCStatus,
    MessageRole,
    QuotationStatus,
    RFQRole,
    RFQStatus,
    UserRole,
    UserStatus,
)
from models.match import MatchResult, MatchSearch
from models.rfq import RFQ
from models.user import UserProfile, User
from models.connection import Connection, ConnectionMessage
from models.quotation import Quotation
from models.review import Review
from models.certificate import Certificate
from models.moderation import ModerationLog
from models.notification import Notification
from models.escrow import DealDispute, EscrowAccount, EscrowMilestone
from models.shipment import Shipment
from models.enums import ShipmentStatus, ShippingMode

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
    "Quotation",
    "QuotationStatus",
    "Incoterm",
    "Review",
    "Certificate",
    "CertificationStatus",
    "ModerationLog",
    "KYCStatus",
    "Notification",
    "EscrowAccount",
    "EscrowMilestone",
    "DealDispute",
    "Shipment",
    "ShippingMode",
    "ShipmentStatus",
]



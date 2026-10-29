"""What the chat agent remembers between turns: ``Conversation.state["agent"]`` (JSONB, no migration)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

PENDING_TTL_TURNS = 3  # a proposed action can be confirmed this many user turns later, no more
MAX_SHOWN = 60  # quick-reply options / tool-result strings kept as grounding sources
MAX_EVIDENCE = 80


@dataclass
class PendingAction:
    """A binding action the user has been shown a recap of and has not confirmed yet."""

    action: str  # create_rfq | publish_rfq | save_rfq_edit | close_rfq | connect | send_message | send_quote | accept_quote | reject_quote
    args: dict[str, Any]
    draft_hash: str  # the draft (or, for negotiation actions, the exact args + quote state) the recap showed; any change invalidates it
    recap: str = ""
    turn: int = 0  # the user turn it was proposed in
    facts: list[str] = field(default_factory=list)  # values (numbers, title, short id) the reply must carry; the recap is the fallback

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["PendingAction"]:
        if not isinstance(raw, dict) or not raw.get("action") or not raw.get("draft_hash"):
            return None
        return cls(
            action=str(raw["action"]), args=dict(raw.get("args") or {}), draft_hash=str(raw["draft_hash"]),
            recap=str(raw.get("recap") or ""), turn=int(raw.get("turn") or 0),
            facts=[str(f) for f in raw.get("facts") or [] if str(f).strip()] if isinstance(raw.get("facts"), list) else [],
        )


@dataclass
class AgentState:
    draft: dict[str, Any] = field(default_factory=dict)  # RFQCreate-shaped, the frontend's rfq_draft
    evidence: dict[str, str] = field(default_factory=dict)  # field path -> the user's words it came from
    pending: Optional[PendingAction] = None
    editing_rfq_id: Optional[str] = None
    editing_snapshot: Optional[dict[str, Any]] = None  # the RFQ as loaded, to send only the changes
    last_rfq_id: Optional[str] = None
    lang_hint: Optional[str] = None  # "en" | "hinglish": only a hint for the deterministic texts
    shown: list[str] = field(default_factory=list)  # options / lookup results shown earlier (grounding sources)
    turn: int = 0  # user turns so far

    @classmethod
    def from_dict(cls, raw: Any) -> "AgentState":
        raw = raw if isinstance(raw, dict) else {}
        snapshot = raw.get("editing_snapshot")
        return cls(
            draft=dict(raw["draft"]) if isinstance(raw.get("draft"), dict) else {},
            evidence={str(k): str(v) for k, v in (raw.get("evidence") or {}).items()} if isinstance(raw.get("evidence"), dict) else {},
            pending=PendingAction.from_dict(raw.get("pending")),
            editing_rfq_id=str(raw["editing_rfq_id"]) if raw.get("editing_rfq_id") else None,
            editing_snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
            last_rfq_id=str(raw["last_rfq_id"]) if raw.get("last_rfq_id") else None,
            lang_hint=raw.get("lang_hint") if raw.get("lang_hint") in ("en", "hinglish") else None,
            shown=[str(x) for x in raw.get("shown") or [] if str(x).strip()][-MAX_SHOWN:] if isinstance(raw.get("shown"), list) else [],
            turn=int(raw.get("turn") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["evidence"] = dict(list(self.evidence.items())[-MAX_EVIDENCE:])
        out["shown"] = self.shown[-MAX_SHOWN:]
        return json.loads(json.dumps(out, default=str))

    def remember_shown(self, items: list[str]) -> None:
        """Add options or lookup strings the user was shown; they may ground a later "yes, that one"."""
        for item in items:
            text = " ".join(str(item).split())
            if text and text not in self.shown:
                self.shown.append(text)
        self.shown = self.shown[-MAX_SHOWN:]

    def pending_valid(self) -> Optional[PendingAction]:
        """The pending action while it is still fresh, else None."""
        if self.pending and self.turn - self.pending.turn <= PENDING_TTL_TURNS:
            return self.pending
        return None

    def reset_draft(self) -> None:
        """After an RFQ was created or the draft was dropped: a clean slate (the last RFQ id is kept)."""
        self.draft, self.evidence, self.pending = {}, {}, None
        self.editing_rfq_id = self.editing_snapshot = None

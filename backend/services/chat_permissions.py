"""Who may write: the permission gate for the chat's action tools.

Reads never need permission. The assistant can look up any marketplace data on
every turn (see ``market_data_tools``), whatever the user is asking about.
Writes are different: they change what other people see (a published RFQ, a
closed RFQ, a message to a counterparty). So an action tool is offered to the
model only when the user's own text explicitly asks for it in THIS message, or
confirms a proposal the assistant made in the previous turn.

Rules, in one place:
- ``update_rfq_draft``: the user is giving RFQ details or is in a drafting flow.
  It only edits the private draft, so it is the loosest gate.
- ``create_rfq`` / ``close_rfq`` / ``publish_rfq`` / ``edit_rfq`` / ``draft_counterparty_message``: an explicit
  command (create/post/publish the RFQ, close/cancel it, publish a saved draft, edit a saved RFQ,
  message/contact the counterparty) or a bare "yes" that answers an assistant proposal of that action.
- Decisions use the user's text and the parser intent only. Assistant text is
  consulted solely to see whether a bare "yes" answers a proposal; model output
  can never grant itself a tool, and the model's tool arguments never change
  which tools are offered.

The explicit-command patterns below are the only keyword rules on the write
path. They are documented per action and deliberately narrow.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Intents (the parser's free-form label) that mean "look something up", not "edit my RFQ".
INFORMATION_INTENTS = frozenset(
    {"search", "price_check", "price_analysis", "market_research", "logistics", "verification"}
)
# Intents where the user states their own trade.
DRAFTING_INTENTS = frozenset({"buy_request", "sell_offer", "rfq_drafting"})

ALL_ACTION_TOOLS = (
    "update_rfq_draft", "create_rfq", "draft_counterparty_message", "close_rfq", "publish_rfq", "edit_rfq",
)

# "please include delivery to Pune", "change the quantity to 500": edits to the draft.
DRAFT_FIELD_UPDATE_RE = re.compile(
    r"^\s*(please\s+|pls\s+|also\s+|and\s+)?(include|add|set|change|update|make|specify|put|keep|mark|increase|decrease|reduce|raise)\b"
    r".*\b(delivery|deliver|location|city|quantity|qty|price|budget|deadline|specs?|specifications?|grade|packaging|moq|unit|units|kg|tonnes?|tons?|pcs|pieces|days?|weeks?)\b",
    re.IGNORECASE,
)
FIELD_UPDATE_WORDS_RE = re.compile(
    r"\b(quantity|qty|price|budget|location|city|deadline|specs?|role|buy|sell|delivery|deliver)\b", re.IGNORECASE
)

# A bare "yes" counts only if the assistant's previous turn proposed that action.
_PROPOSES_MESSAGE_RE = re.compile(r"\b(send|draft|compose|write)\b[^.?!]{0,60}\b(message|msg|note|email|mail)\b", re.IGNORECASE)
_PROPOSES_CLOSE_RE = re.compile(r"\b(close|cancel)\b[^.?!]{0,40}\b(rfq|listing)\b", re.IGNORECASE)

_CREATE_VERB = r"(?:create|crate|creat|craete|cerate|creste|make|post|submit|publish|generate|raise|rais|raize|riase)"

CREATE_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        rf"\b{_CREATE_VERB}\s+(the\s+|this\s+|an?\s+|my\s+|that\s+)?(rfq|listing)\b",
        r"\b(create|crate|creat|craete|cerate|creste)\s+(it|this)\b",
        r"\byes\s*,?\s*(please\s*)?(create|crate|creat|craete|cerate|publish|post|submit)\b",
        r"\bproceed\s+(to\s+|and\s+)?(create|crate|creat|craete|cerate|publish|post|submit)\b",
        r"\bproceed\s+with\s+((creating|crating|publishing)\s+)?(the\s+|this\s+|my\s+)?(rfq|listing)\b",
        r"\b(post|submit|publish)\s+(it|this)\b",
        r"\bconfirm\s+and\s+(create|crate|creat|publish|submit)\b",
        r"\bgo\s+ahead\s+and\s+(create|crate|creat|publish|submit|post)\b",
        r"^(yes|create|crate|creat|proceed|publish|submit)(\s+(the\s+)?rfq)?[.!]*$",
        # "raise the rfq", "rais the rfq", "raise it"
        r"\b(raise|rais|raize|riase|rise)\s+(the\s+|this\s+|an?\s+|my\s+)?(rfq|listing|request|it)\b",
        # Hinglish: "rfq bana do", "haan bana do", "publish kar do", "create kar do"
        r"\b(rfq|listing)\s+(bana|banao|banado|bana\s*do|bana\s+dijiye|bana\s+dena|create\s+kar|publish\s+kar|post\s+kar|daal|dal)\b",
        r"\b(bana|banao)\s+(do|de|dijiye|dena)\b",
        r"\bbanado\b",
        r"\b(create|publish|post|submit|raise|live|upload)\s+kar\s*(do|de|dijiye|dena|dain|den)\b",
        r"\b(create|publish|post|submit)\s+kardo\b",
        # Save as draft: still an explicit create request (the draft status is chosen by wants_draft_status).
        r"\b(save|keep|store)\s+(it\s+|this\s+|the\s+rfq\s+|my\s+rfq\s+)?(as\s+)?(an?\s+)?draft\b",
        r"\bdraft\s+(me\s+|mein\s+|main\s+)(save|rakh|rakho)\b",
        r"\bdraft\s+bana\s*(do|de|dijiye|dena|o)\b",
    ]
]

# "don't create", "not ready to proceed", "rfq mat banao", "abhi nahi".
_CREATE_NEGATION_RE = re.compile(
    r"\b(don'?t|dont|do\s+not|does\s+not|didn'?t|not|never|no\s+need|without|hold\s+off|stop|avoid|"
    r"mat|nahi|nahin|nai)\b",
    re.IGNORECASE,
)
# A polite request phrased as a question still asks for the action.
_CREATE_REQUEST_QUESTION_RE = re.compile(
    rf"^\s*(please\s+)?(can|could|would|will)\s+(you|u)\s+(please\s+)?(now\s+)?{_CREATE_VERB}\b"
    r"|^\s*kya\s+(aap|ap|tum)\b.*\b(bana|create|publish|post)\b.*\b(sakte|sakti|sakoge|doge|denge|do)\b",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(
    r"^\s*(how|what|why|when|where|which|who|whom|whose|should|shall|is|are|am|was|were|do|does|did|"
    r"can\s+i|could\s+i|may\s+i|would\s+it|will\s+it|is\s+it|kya|kaise|kyun|kab)\b",
    re.IGNORECASE,
)
_BARE_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ok|okay|sure|haan|han|ha|ji|ji\s+haan|confirm|confirmed|go\s+ahead|do\s+it)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NON_RFQ_OBJECT_RE = re.compile(
    r"\b(message|msg|mail|email|letter|note|reply|description|table|summary|list|chart|report)\b", re.IGNORECASE
)
_ASKED_TO_CREATE_RE = re.compile(r"\b(create|publish|post|submit|raise)\b", re.IGNORECASE)


# "save as draft", "keep it as a draft", "draft me save karo", "draft bana do".
_DRAFT_STATUS_RES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(save|keep|store|leave|put)\s+(it\s+|this\s+|the\s+rfq\s+|my\s+rfq\s+)?(as\s+|in\s+)?(an?\s+)?draft\b",
        r"\bas\s+(an?\s+)?draft\b",
        r"\bdraft\s+(me|mein|main|mai)\s+(save|rakh|rakho|daal|dal)\b",
        r"\bdraft\s+(bana|banao|banado)\b",
    ]
]
# "don't publish yet", "abhi publish mat karo": a hold-back on publishing means draft.
_HOLD_PUBLISH_RES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(don'?t|dont|do\s+not|not)\s+(publish|post|go\s+live)\b",
        r"\b(publish|post)\s+(mat|na)\s+(kar|karo|karna|karein)\b",
    ]
]


def wants_draft_status(user_message: str) -> bool:
    """True when the user asks to save the RFQ as a draft instead of publishing it."""
    text = (user_message or "").strip()
    if not text:
        return False
    if any(rx.search(text) for rx in _HOLD_PUBLISH_RES):
        return True
    # "don't save it as a draft" / "draft mat banao" negate the draft wish.
    if _CREATE_NEGATION_RE.search(text):
        return False
    return any(rx.search(text) for rx in _DRAFT_STATUS_RES)


def is_question(user_message: str) -> bool:
    """True for an interrogative message ("should I publish it?", "how do I ...")."""
    text = (user_message or "").strip()
    return bool(text) and (text.endswith("?") or bool(_QUESTION_START_RE.search(text)))


def is_negated_request(user_message: str) -> bool:
    return bool(_CREATE_NEGATION_RE.search(user_message or ""))


def check_create_rfq_intent(user_message: str, previous_assistant: Optional[str] = None) -> bool:
    """True only for an explicit request to publish the RFQ now.

    Negated ("don't create the rfq yet", "I am not ready to proceed") and
    questioning ("should I publish it or wait?", "how do I create a good
    description?") messages never count, apart from polite requests such as
    "can you create the rfq?". A bare "yes"/"haan" counts only when the previous
    assistant turn offered to create or publish (when that turn is known).
    """
    if not user_message:
        return False
    text = user_message.strip()
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not _CREATE_REQUEST_QUESTION_RE.search(text):
        return False
    if previous_assistant is not None and _BARE_AFFIRMATIVE_RE.match(text):
        return bool(_ASKED_TO_CREATE_RE.search(previous_assistant))
    # "create a message for the seller", "message bana do": the object is not the RFQ.
    if _NON_RFQ_OBJECT_RE.search(text) and not re.search(r"\b(rfq|listing)\b", text, re.IGNORECASE):
        return False
    return any(rx.search(text) for rx in CREATE_INTENT_REGEXES)


def is_bare_affirmative(user_message: str) -> bool:
    return bool(_BARE_AFFIRMATIVE_RE.match(user_message or ""))


_CLOSE_VERB = r"(?:close|closed|closing|cancel|cancell?ing|deactivate|remove|delete|withdraw|take\s+down|shut\s+down|unpublish|band\s+kar)"
_CLOSE_OBJECT = r"(?:rfqs?|listings?|postings?|posts?|requirements?|orders?|requests?)"

CLOSE_RFQ_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "close my cotton rfq", "cancel the order", "shut down that listing I posted"
        rf"\b{_CLOSE_VERB}\s+(?:(?:this|the|my|our|that|active|current)\s+)?(?:[\w-]+\s+){{0,3}}?{_CLOSE_OBJECT}\b",
        r"^(close|cancel|deactivate|remove)(\s+this)?(\s+rfq)?[.!]*$",
        # Hinglish: "rfq band kar do", "listing hata do"
        rf"\b{_CLOSE_OBJECT}\s+(band|hata|hatao|cancel|close)\s*(kar|karo|kardo|do|de|dijiye)?\b",
    ]
]

# "close the deal" is negotiation, not closing a listing.
_CLOSE_DEAL_RE = re.compile(r"\bclos(e|ing)\s+(the\s+|this\s+|a\s+)?deal\b", re.IGNORECASE)


def check_close_rfq_intent(user_message: str) -> bool:
    """Check if the user message expresses intent to close or cancel an RFQ."""
    if not user_message:
        return False
    text = user_message.strip()
    if _CLOSE_DEAL_RE.search(text) and not re.search(r"\b(rfq|listing)\b", text, re.IGNORECASE):
        return False
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not re.match(
        rf"^\s*(please\s+)?(can|could|would|will)\s+(you|u)\s+(please\s+)?{_CLOSE_VERB}\b", text, re.IGNORECASE
    ):
        return False
    return any(rx.search(text) for rx in CLOSE_RFQ_INTENT_REGEXES)


_CLOSE_TARGET_RE = re.compile(
    rf"\b{_CLOSE_VERB}\s+(?:(?:this|the|my|our|that|active|current)\s+)?((?:[\w-]+\s+){{0,3}}?){_CLOSE_OBJECT}\b", re.IGNORECASE
)
_TARGET_FILLER = frozenset(
    {"latest", "last", "recent", "newest", "previous", "old", "active", "current", "posted", "created", "just",
     "a", "an", "the", "my", "our", "this", "that", "all", "one", "i"}
)


def close_rfq_target(message: str) -> Optional[str]:
    """The listing the user names in a close command ("close my cotton rfq" -> "cotton"), or None.

    Used so that closing "the cotton RFQ" never closes a different, newer listing.
    """
    match = _CLOSE_TARGET_RE.search(message or "")
    if not match:
        return None
    words = [w for w in match.group(1).split() if w.lower() not in _TARGET_FILLER]
    return " ".join(words) or None


# ---- publish a saved draft RFQ, edit a saved RFQ ---------------------------
_PUBLISH_WORD_RE = re.compile(r"\b(publish|post|go\s+live|live)\b", re.IGNORECASE)
_PROPOSES_PUBLISH_RE = re.compile(r"\b(publish|post|go\s+live)\b", re.IGNORECASE)
_PROPOSES_SAVE_RE = re.compile(r"\bsave\b[^.?!]{0,40}\b(changes|edits|updates|it)\b", re.IGNORECASE)
_RFQ_OBJECT = r"(?:rfqs?|listings?|postings?|requirements?)"
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_POLITE_RE = r"^\s*(please\s+)?(can|could|would|will)\s+(you|u)\s+(please\s+)?"
_MY_WORDS = r"(?:this|the|my|our|that|draft|saved|existing|current|posted|active|meri|mera|mere|apni|apna)"

PUBLISH_RFQ_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "publish my rice rfq", "post the draft listing", "go live with my cotton rfq"
        rf"\b(publish|post|go\s+live\s+with|make\s+live)\s+(?:{_MY_WORDS}\s+)?(?:[\w-]+\s+){{0,3}}?{_RFQ_OBJECT}\b",
        r"\b(publish|post)\s+(it|this|that)\b",
        r"\byes\s*,?\s*(please\s*)?(publish|post)\b",
        r"^(publish|post)[.!]*$",
        # Hinglish: "meri rice rfq publish kar do", "rfq publish kar do", "publish kar do"
        rf"\b{_RFQ_OBJECT}\s+(publish|post|live)\s+kar\s*(do|de|dijiye|dena|dain|den|o)?\b",
        r"\b(publish|post|live)\s+kar\s*(do|de|dijiye|dena|dain|den|o)\b",
        r"\b(publish|post)\s+kardo\b",
    ]
]


def check_publish_rfq_intent(user_message: str, previous_assistant: Optional[str] = None) -> bool:
    """True for an explicit request to publish (post, go live with) the RFQ, same guards as create/close.

    Whether it means the chat draft or a saved draft RFQ is decided by the caller from the conversation.
    A bare "yes" counts only when the assistant's previous turn proposed publishing.
    """
    if not user_message:
        return False
    text = user_message.strip()
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not re.match(_POLITE_RE + r"(publish|post)\b", text, re.IGNORECASE):
        return False
    if _BARE_AFFIRMATIVE_RE.match(text):
        return previous_assistant is not None and bool(_PROPOSES_PUBLISH_RE.search(previous_assistant))
    if _NON_RFQ_OBJECT_RE.search(text) and not re.search(r"\b(rfq|listing)\b", text, re.IGNORECASE):
        return False
    return any(rx.search(text) for rx in PUBLISH_RFQ_INTENT_REGEXES)


def says_publish(user_message: str) -> bool:
    """The message uses a publish word (not merely "create"); a hold-back such as "don't publish" does not count."""
    text = user_message or ""
    return bool(_PUBLISH_WORD_RE.search(text)) and not any(rx.search(text) for rx in _HOLD_PUBLISH_RES)


_PUBLISH_TARGET_RE = re.compile(
    rf"\b(?:publish|post|go\s+live\s+with|make\s+live)\s+(?:{_MY_WORDS}\s+)?((?:[\w-]+\s+){{0,3}}?){_RFQ_OBJECT}\b", re.IGNORECASE
)
_HINGLISH_TARGET_RE = re.compile(
    rf"\b(?:meri|mera|mere|apni|apna)\s+((?:[\w-]+\s+){{0,3}}?){_RFQ_OBJECT}\b", re.IGNORECASE
)
_EDIT_TARGET_RES = [
    re.compile(
        rf"\b(?:edit|change|update|modify|amend|revise)\s+(?:{_MY_WORDS}\s+)?((?:[\w-]+\s+){{0,3}}?){_RFQ_OBJECT}\b", re.IGNORECASE
    ),
    re.compile(
        rf"\b(?:of|in|for|on)\s+(?:my|the|this|our|that)\s+((?:[\w-]+\s+){{0,3}}?){_RFQ_OBJECT}\b", re.IGNORECASE
    ),
]
_TARGET_FILLER |= frozenset({"draft", "saved", "existing", "posted", "published", "meri", "mera", "mere", "apni", "apna", "to", "of", "in", "for", "on", "rfq", "listing", "id"})


def _target_from(match: Optional[re.Match]) -> Optional[str]:
    if not match:
        return None
    words = [w for w in match.group(1).split() if w.lower() not in _TARGET_FILLER]
    return " ".join(words) or None


def publish_rfq_target(message: str) -> Optional[str]:
    """The saved RFQ named in a publish command (an id, or "rice" in "publish my rice rfq"), or None."""
    text = message or ""
    uid = _UUID_RE.search(text)
    if uid:
        return uid.group(0)
    return _target_from(_PUBLISH_TARGET_RE.search(text)) or _target_from(_HINGLISH_TARGET_RE.search(text))


EDIT_RFQ_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "edit my rice rfq", "update the cotton listing"
        rf"\b(edit|change|update|modify|amend|revise)\s+(?:{_MY_WORDS}\s+)?(?:[\w-]+\s+){{0,3}}?{_RFQ_OBJECT}\b",
        # "change the quantity of my rice rfq to 600 kg"
        rf"\b(edit|change|update|modify|amend|revise)\b.{{0,60}}?\b(of|in|for|on)\s+(my|the|this|our|that)\s+(?:[\w-]+\s+){{0,3}}?{_RFQ_OBJECT}\b",
        # Hinglish: "meri rice rfq badlo", "meri rfq edit karo", "rfq update kar do"
        rf"\b(meri|mera|mere|apni|apna)\s+(?:[\w-]+\s+){{0,3}}?{_RFQ_OBJECT}\s+(badlo|badal|badlao|edit|update|change|modify|sudhaar|sudharo|theek)\b",
        rf"\b{_RFQ_OBJECT}\s+(badlo|badal|edit|update|change|modify)\s+(kar|karo|kardo|do|de|dijiye)\b",
    ]
]
# "update the rfq to include delivery" mid-draft edits the chat draft: an edit needs a saved-RFQ cue.
_SAVED_CUE_RE = re.compile(
    rf"\b(my|our|meri|mera|mere|apni|apna|saved|existing|posted|published|active|draft)\s+(?:[\w-]+\s+){{0,3}}?{_RFQ_OBJECT}\b"
    rf"|\b(the|this|that)\s+(?:[\w-]+\s+){{1,3}}?{_RFQ_OBJECT}\b",
    re.IGNORECASE,
)


def check_edit_rfq_intent(user_message: str) -> bool:
    """True for an explicit request to edit a saved RFQ ("edit my rice RFQ", "meri rice RFQ badlo")."""
    if not user_message:
        return False
    text = user_message.strip()
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not re.match(_POLITE_RE + r"(edit|change|update|modify|amend|revise)\b", text, re.IGNORECASE):
        return False
    if not (_UUID_RE.search(text) or _SAVED_CUE_RE.search(text)):
        return False
    return any(rx.search(text) for rx in EDIT_RFQ_INTENT_REGEXES)


def edit_rfq_target(message: str) -> Optional[str]:
    """The saved RFQ named in an edit command (an id, or "rice" in "edit my rice rfq"), or None."""
    text = message or ""
    uid = _UUID_RE.search(text)
    if uid:
        return uid.group(0)
    for rx in _EDIT_TARGET_RES:
        target = _target_from(rx.search(text))
        if target:
            return target
    return _target_from(_HINGLISH_TARGET_RE.search(text))


_EDIT_FIELD_WORDS_RE = re.compile(
    r"\b(change|update|edit|set|modify|amend|revise|replace)\b.{0,60}?\b(description|title|notes?|hsn|category|min(?:imum)?\s+order|moq)\b",
    re.IGNORECASE,
)


def edit_command_has_change(message: str) -> bool:
    """The edit command also states the change ("change the quantity of my rice RFQ to 600 kg")."""
    text = _UUID_RE.sub(" ", message or "")
    return bool(DRAFT_FIELD_UPDATE_RE.search(text) or _EDIT_FIELD_WORDS_RE.search(text))


# Saving edits: only meaningful while an RFQ is being edited.
_SAVE_EDIT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^\s*(ok(ay)?\s*,?\s*)?(please\s+)?(save|update|apply|submit|done|confirm)(\s+(it|this|that|the\s+changes|changes|my\s+changes|all|the\s+rfq|rfq))?\s*[.!]*\s*$",
        r"\b(save|apply|confirm|commit)\s+(the\s+|my\s+|these\s+|all\s+)?(changes|edits|updates)\b",
        r"\bdone\s*,?\s*(please\s+)?(save|update)\b",
        r"\b(update|save)\s+(it|this|that|the\s+rfq|my\s+rfq)\b",
        r"\b(looks\s+good|that'?s\s+all)\s*,?\s*(please\s+)?(save|update)\b",
        # Hinglish: "changes save karo", "save kar do", "update kar do"
        r"\b(changes?|badlav|edits?)\s+(save|update)\s+kar\s*(o|do|de|dijiye|dena)?\b",
        r"\b(save|update)\s+kar\s*(o|do|de|dijiye|dena|dain)\b",
        r"\b(save|update)\s+kardo\b",
    ]
]
_CANCEL_EDIT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(cancel|discard|abandon|stop|exit|leave|drop|undo)\s+(the\s+|my\s+|this\s+|these\s+|all\s+)?(editing|edit|changes|edits)\b",
        r"\b(edit|changes?|edits)\s+(cancel|discard|band|ruk|rok)\s*(kar\s*(o|do|de|dijiye)?|karo)?\b",
        r"\bnever\s*mind\s+(the\s+)?(edit|changes)\b",
    ]
]


def check_save_edit_intent(user_message: str, previous_assistant: Optional[str] = None) -> bool:
    """Save (or publish/create, which means save while editing) the RFQ being edited.

    Negated and questioning messages never count; a bare "yes" counts when the assistant offered to save.
    """
    if not user_message:
        return False
    text = user_message.strip()
    if _CREATE_NEGATION_RE.search(text):
        return False
    if is_question(text) and not re.match(_POLITE_RE + r"(save|update|apply|publish|post)\b", text, re.IGNORECASE):
        return False
    if _BARE_AFFIRMATIVE_RE.match(text):
        return bool(previous_assistant) and bool(_PROPOSES_SAVE_RE.search(previous_assistant or "") or _PROPOSES_PUBLISH_RE.search(previous_assistant or ""))
    if any(rx.search(text) for rx in _SAVE_EDIT_REGEXES):
        return True
    return check_create_rfq_intent(text, previous_assistant) or check_publish_rfq_intent(text, previous_assistant)


def check_cancel_edit_intent(user_message: str) -> bool:
    """Leave edit mode without saving ("cancel edit", "discard changes")."""
    text = (user_message or "").strip()
    if not text:
        return False
    if is_question(text) and not re.match(_POLITE_RE + r"(cancel|discard)\b", text, re.IGNORECASE):
        return False
    return any(rx.search(text) for rx in _CANCEL_EDIT_REGEXES)


_CP_TARGET = r"(seller|supplier|buyer|counterparty|vendor|them|him|her|user|that\s+user|this\s+user|counter\s+user)s?"

COUNTERPARTY_MESSAGE_INTENT_REGEXES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # "pls draft something nice for them"
        r"\b(draft|compose|write|prepare)\b[^.?!\n]{0,30}\b(for|to)\s+(them|him|her)\b",
        # Negotiation keywords and typos: negotiate, nagitiation, nagotiation, natotiation, natogiation, draft, negociate, bargain, deal
        r"\b(send|write|drop|deliver|draft|compose|prepare)\s+(a\s+|an\s+|the\s+|this\s+|that\s+|my\s+)?(nagitiation|nagotiation|natotiation|natogiation|negotiation|bargain|counter|offer|proposal|deal|discount)?\s*(message|msg|text|note|email|mail)?\s*(to|for)\s+(the\s+|this\s+|that\s+|our\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor|them|him|her|user|that\s+user|this\s+user|counter\s+user)s?\b",
        # "send the message to this buyer", "write a short note for the supplier"
        r"\b(send|write|draft|compose|prepare|drop)\b[^.?!\n]{0,40}\b(message|msg|mail|email|note|reply)\b[^.?!\n]{0,30}\b(to|for)\s+(the\s+|this\s+|that\s+|our\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor)s?\b",
        r"\b(draft|compose|prepare|write)\s+(a\s+)?(message|msg|proposal|counter-offer|offer|negotiation|natotiation)\s+(for|to)\s+(the\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|counter\s+user)\b",
        r"\b(message|contact|reach\s+out\s+to|notify|ping|chat\s+with|reply\s+to|write\s+to)\s+(the\s+|this\s+|that\s+|my\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|that\s+user|this\s+user|counter\s+user)s?\b",
        r"\b(ask|tell|request|inquire|inform)\s+(the\s+)?(seller|supplier|buyer|counterparty|vendor|them|user|that\s+user|this\s+user|counter\s+user)\b",
        # "negotiate with the vendor", "help me close the deal with the supplier"
        rf"\b(negotiate|nagitiate|nagotiate|natotiate|natogiate|negociate|bargain)\s+(with\s+)?(the\s+|this\s+|that\s+|my\s+|our\s+)?{_CP_TARGET}\b",
        rf"\bdeal\s+with\s+(the\s+|this\s+|that\s+|my\s+|our\s+)?{_CP_TARGET}\b",
        r"\b(can\s+we|can\s+you)\s+deal\s+(it\s+)?(in|at|for)\b",
        r"\b(can\s+you\s+)?(reduce|decrease|lower|discount)\s+(the\s+)?price\b.*\b(to|by|in|for)\b",
        r"\b(send|say|write|draft)\s+to\s+(the\s+)?(seller|supplier|buyer|user|that\s+user|counter\s+user)\b",
        r"\b(behaviour|behavior|tone)\s+should\s+be\s+(like\s+)?polite\b",
        # Follow-up amendments and additions (e.g., "also mention that...", "aslo mantion that...")
        r"\b(also|aslo|and|please)?\s*(mention|mantion|clarify|state|note|highlight|stress)\s+(that|to\s+them)\b",
        r"\b(also|aslo)\s+(mention|mantion|clarify|note|highlight|say|write|include|specify)\b",
        r"\b(also|aslo|and|please)?\s*(tell|ask|inform|let)\s+(them|him|her|the\s+seller|the\s+supplier|the\s+buyer|the\s+user|that\s+user|counter\s+user)\b",
        r"\b(also|aslo|and)?\s*(say|write)\s+that\b",
        r"\b(also|aslo|and)?\s*(add|put)\s+(that|in\s+the\s+message|to\s+the\s+message)\b",
        r"\b(update|amend|revise|edit|change)\s+(the\s+)?(message|msg|note|text|draft\s+message|letter)\b",
        r"\b(send|dispatch)\s+(an?\s+)?(updated|new|another|follow-up|followup)\s+(message|msg)\b",
        # Counterparty location / terms corrections
        r"\b(location|address|delivery\s+place)\s+is\s+not\b",
        r"\bnot\s+[a-zA-Z]+(\s+[a-zA-Z]+)?\s+(its?|it\s+is)?\s*\d+\s*(km|kg|kilometer|kilometers|miles)\s+away\b",
        # Hinglish: "supplier ko 10% discount ke liye message kar do", "seller ko bolo"
        r"\b(seller|supplier|buyer|vendor|dealer|party)\s+ko\b[^.?!\n]{0,60}\b(message|msg|mail|bol|bolo|bolna|likh|likho|likhna|puch|pucho|poochho|batao|bata\s+do|kaho|keh\s+do)\b",
        r"\b(unko|usko|unhe|inko|isko)\b[^.?!\n]{0,60}\b(message|msg|mail|bol|bolo|bolna|likh|likho|puch|pucho|batao|kaho)\b",
        r"\b(message|msg|mail)\s+(kar|karo|kardo|kar\s+do|bhej|bhejo|bhej\s+do|likh\s+do|bana\s+do)\b",
    ]
]

# Inside an active thread, a message counts as an amendment only when it
# addresses the counterparty or the drafted message.
_THREAD_ADDRESS_RE = re.compile(
    r"\b(them|they|him|her|seller|supplier|vendor|buyer|counterparty|message|msg|letter|note|reply|draft\s+message)\b",
    re.IGNORECASE,
)
_THREAD_AMEND_START_RE = re.compile(
    r"^\s*(also|aslo|tell|ask|say|mention|mantion|clarify|inform|let\s+them|and\s+(say|mention|tell|add))\b",
    re.IGNORECASE,
)
_THREAD_CORRECTION_RE = re.compile(r"\b(away\s+from|instead\s+of|rather\s+than)\b", re.IGNORECASE)


def check_counterparty_message_intent(
    user_message: str,
    has_active_connection: bool = False,
    has_previous_counterparty_msg: bool = False,
) -> bool:
    """Check if the user message expresses intent to message/negotiate with a seller or counterparty.

    Supports initial outreach, price negotiation, follow-up additions (e.g. 'also mention that...'),
    and contextual follow-ups when an active counterparty thread exists. Draft edits
    such as "please include delivery to Pune" are not messages unless a thread is
    active and the text addresses the counterparty.
    """
    if not user_message:
        return False
    text = user_message.strip()
    if any(rx.search(text) for rx in COUNTERPARTY_MESSAGE_INTENT_REGEXES):
        return True

    # If in an active counterparty thread or follow-up turn, match conversational amendments.
    # "update the rfq from buyer to seller" edits the user's own listing, not the thread.
    edits_own_listing = bool(re.search(r"\b(rfq|draft|listing|role)\b", text, re.IGNORECASE)) and not re.search(
        r"\b(message|msg|mention|mantion|tell|ask|inform)\b", text, re.IGNORECASE
    )
    if (has_active_connection or has_previous_counterparty_msg) and not edits_own_listing:
        if _THREAD_AMEND_START_RE.search(text) or _THREAD_CORRECTION_RE.search(text):
            return True
        if _THREAD_ADDRESS_RE.search(text) and not is_question(text):
            return True

    return False


def normalise_intent(raw: Any) -> str:
    """Parser intents are free-form text; compare them as lower_snake_case."""
    return re.sub(r"[\s\-]+", "_", str(raw or "").strip().lower())


def effective_intent(message: str, parsed_intent: Optional[str] = None) -> str:
    """The parser's intent for ``message``, or the outage fallback when the parser gave none."""
    intent = normalise_intent(parsed_intent)
    if intent:
        return intent
    from services.agent_router import _fallback_intent

    return _fallback_intent(message)


def _is_lookup(text: str, intent: str) -> bool:
    """A lookup intent, except that a "search" label on a first-person trade statement ("I want to buy rice") is a trade."""
    from services.agent_router import states_own_trade

    return intent in INFORMATION_INTENTS and not (intent == "search" and states_own_trade(text))


def is_information_request(message: str) -> bool:
    """True when the message asks to look something up (search, prices, freight, trust) rather than to state a trade.

    Used by the draft pre-processor so a market question is never parsed as RFQ
    details. Reads the cached parser intent; when there is none, the outage fallback.
    """
    from services import ai_query_parser

    cached = ai_query_parser.cached(message or "")
    return _is_lookup(message or "", effective_intent(message, cached.intent if cached else None))


def _drafting_in_progress(draft: dict[str, Any]) -> bool:
    return bool(draft.get("role") or (draft.get("product_details") or {}).get("name"))


def _gives_rfq_details(text: str, draft: dict[str, Any], intent: str) -> bool:
    """Is the user giving RFQ details, or in a drafting flow, in this message?

    A quantity, price or deadline stated mid-draft is a draft update even if the
    parser read the message as a lookup; a question never is.
    """
    asks = is_question(text)
    if DRAFT_FIELD_UPDATE_RE.search(text) and not asks:
        return True
    if _drafting_in_progress(draft) and not asks:
        from services import query_extractor

        req = query_extractor.extract(text)
        if any(v is not None for v in (req.quantity_value, req.deadline_days, req.price_amount)):
            return True
    if _is_lookup(text, intent):
        return False
    if intent in DRAFTING_INTENTS:
        return True
    if not asks and FIELD_UPDATE_WORDS_RE.search(text):
        return True
    # RFQ inception: nothing recorded yet, so the message may carry the first detail.
    return not (draft.get("role") and (draft.get("product_details") or {}).get("name"))


def allowed_action_tools(
    message: str,
    draft: Optional[dict[str, Any]],
    conversation_state: Optional[dict[str, Any]],
    parsed_intent: Optional[str],
) -> set[str]:
    """Names of the action tools the model may be offered for this user message.

    ``conversation_state`` keys (all optional): ``previous_assistant`` (text of the
    assistant's last turn), ``active_connection_id``, ``last_counterparty_message``
    and ``has_previous_counterparty_msg`` (a counterparty thread is open).
    ``parsed_intent`` is ``AIParse.intent``; None means the parser was unavailable
    and the outage fallback decides.
    """
    text = (message or "").strip()
    draft = draft or {}
    state = conversation_state or {}
    previous = str(state.get("previous_assistant") or "")
    intent = effective_intent(text, parsed_intent)
    affirmative = is_bare_affirmative(text)
    thread_open = bool(
        state.get("active_connection_id")
        or state.get("last_counterparty_message")
        or state.get("has_previous_counterparty_msg")
    )

    wants_message = check_counterparty_message_intent(
        text,
        # A loose thread follow-up ("also mention ...") is not a command when the parser reads a lookup.
        has_active_connection=bool(state.get("active_connection_id")) and not _is_lookup(text, intent),
        has_previous_counterparty_msg=thread_open and not _is_lookup(text, intent),
    ) or (affirmative and bool(_PROPOSES_MESSAGE_RE.search(previous)))
    wants_create = check_create_rfq_intent(text, previous_assistant=previous)
    wants_close = check_close_rfq_intent(text) or (affirmative and bool(_PROPOSES_CLOSE_RE.search(previous)))
    wants_publish = check_publish_rfq_intent(text, previous_assistant=previous)
    wants_edit = check_edit_rfq_intent(text)
    editing = bool(state.get("editing_rfq_id"))

    allowed: set[str] = set()
    if wants_message:
        allowed.add("draft_counterparty_message")
    elif editing and (wants_create or wants_publish):
        # Saving an edit is never "create a new RFQ"; the chat turn handles it.
        allowed.add("update_rfq_draft")
    elif wants_edit and not _drafting_in_progress(draft):
        allowed.add("edit_rfq")
    elif wants_create:
        allowed.update({"create_rfq", "update_rfq_draft"})
    elif wants_publish:
        allowed.add("publish_rfq")
    elif wants_close:
        allowed.add("close_rfq")
    elif _gives_rfq_details(text, draft, intent):
        allowed.add("update_rfq_draft")
    logger.debug("chat permissions for %r (intent=%s): %s", text[:60], intent, sorted(allowed))
    return allowed

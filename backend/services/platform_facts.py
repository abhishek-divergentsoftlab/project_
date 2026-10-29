"""What this platform really does, for questions about the platform itself.

Every sentence in ``FACTS`` is taken from the code that implements it (the source is named next to it). The chat
model answers platform questions only from these facts; ``answer_verifier`` blocks fee, tier and policy claims that
are not in them; and where a fact is absent (there is no fee schedule anywhere in the code) the answer says so
instead of inventing one.
"""

from __future__ import annotations

# (fact, where it is implemented)
FACTS: tuple[tuple[str, str], ...] = (
    (
        "This is a B2B trade marketplace. Buyers and sellers post RFQs (a buy requirement or a sell offer with product, "
        "quantity, price, location and deadline). The platform matches them, they connect, exchange quotations in a deal "
        "room and close the order.",
        "models/rfq.py, services/match_service.py, models/connection.py, models/quotation.py",
    ),
    (
        "In this chat you can describe what you want to buy or sell in plain words (English or Hinglish). The assistant "
        "drafts the RFQ one question at a time, shows a summary, and posts it only after you confirm; you can also save "
        "it as a draft. It can also look up marketplace listings, prices, HSN codes and freight.",
        "services/ai_chat_service.py, services/chat_agent/recaps.py",
    ),
    (
        "Payments can run through an escrow vault opened per order. The buyer reports the deposit (method and reference); "
        "only a platform operator confirms the money arrived. The vault is released in three milestones: 30% production "
        "advance, 40% on dispatch with proof, 30% on final delivery and inspection. The supplier requests a release and "
        "the buyer approves it.",
        "services/escrow_service.py",
    ),
    (
        "If goods are bad or late, either side can raise a formal dispute on the order. That freezes the escrow vault until "
        "the dispute is resolved: by a concession (the seller refunds the buyer, or the buyer releases funds to the seller) "
        "or by a mutual settlement that both sides accept.",
        "services/escrow_service.py (raise_dispute, resolve_dispute)",
    ),
    (
        "KYC: users can add a GST number and PAN. They are checked for correct format only (GSTIN structure and check "
        "character, PAN pattern); there is no live government-registry lookup yet. A profile is unverified, pending, "
        "verified or flagged, and has a trust score.",
        "services/kyc_service.py, models/enums.py",
    ),
    (
        "Listings are screened for prohibited goods (weapons and ammunition, narcotics, endangered wildlife, hazardous or "
        "illegal goods, human trafficking). Such listings are blocked. Payment details outside the platform are not allowed.",
        "services/moderation_service.py",
    ),
    (
        "To reach a seller or buyer, open their listing and send a connection request; once it is accepted you can message "
        "each other in the deal room.",
        "services/connection_service.py",
    ),
    (
        "The platform has no fee, commission, membership tier, subscription or pricing plan defined anywhere in its system. "
        "The assistant has NO information about charges: it must not state any percentage or amount for platform fees and "
        "should say it does not have that information.",
        "absence verified in services/escrow_service.py, models/*, core/config.py",
    ),
)

FACTS_TEXT = "\n".join(f"- {fact}" for fact, _src in FACTS)

PROMPT_SECTION = (
    "\nPLATFORM FACTS (the ONLY source for questions about this platform: what it is, how to start, payments, escrow, "
    "disputes, KYC, fees, safety):\n"
    f"{FACTS_TEXT}\n"
    "Rules: answer in a few short sentences from these facts only. If a question is about something not covered "
    "(fees, commission, tiers, refunds timelines, legal terms), say plainly that you do not have that information and "
    "suggest the next step. Never invent percentages, amounts, tiers, time limits or guarantees.\n"
    "A newcomer asking what this app is gets one or two sentences of explanation and how to start (tell me what you "
    "want to buy or sell). A frustrated or rude message gets one calm sentence acknowledging it and one concrete offer "
    "of help; never a cold refusal.\n"
)


def about_reply(lang: str = "en") -> str:
    """A short explanation of the platform and how to start, for a newcomer (deterministic backstop)."""
    if lang == "hinglish":
        return (
            "Yeh ek B2B marketplace hai: buyers aur sellers apni requirement ya offer (RFQ) post karte hain, platform unhe "
            "match karta hai, phir woh quotation share karke deal close karte hain. Shuru karne ke liye bas bataiye ki aap "
            "kya kharidna ya bechna chahte hain (product, quantity, jagah) aur main RFQ draft kar dunga."
        )
    return (
        "This is a B2B marketplace: buyers and sellers post what they want to buy or sell (an RFQ), the platform matches "
        "them, and they exchange quotations and close the deal, with optional escrow for payment. To start, just tell me "
        "what you want to buy or sell (product, quantity, place) and I will draft the RFQ with you."
    )


def calm_reply(lang: str = "en") -> str:
    """De-escalation for a frustrated or rude message (deterministic backstop)."""
    if lang == "hinglish":
        return (
            "Maaf kijiye, aapko pareshani hui. Bataiye kahan atka hua hai, main seedha madad karunga: RFQ banana, "
            "listing dhundhna ya price check karna?"
        )
    return (
        "I am sorry it has been frustrating. Tell me what you were trying to do and I will help directly: drafting an RFQ, "
        "finding listings, or checking a price."
    )


def no_fee_info_reply(lang: str = "en") -> str:
    if lang == "hinglish":
        return "Mere paas platform ke fees ya commission ki koi jaankari nahi hai, isliye main koi number nahi bataunga."
    return "I do not have any information about platform fees or commission, so I will not quote any."

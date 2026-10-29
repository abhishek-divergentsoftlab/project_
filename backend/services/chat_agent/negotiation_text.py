"""Texts the code writes for negotiation recaps, chips and success lines, in english / hinglish (Latin) / hindi_devanagari.

Numbers, titles, ids and quote numbers are inserted verbatim by the caller: the facts check in the engine relies on them.
"""

from __future__ import annotations

from typing import Any

_EN: dict[str, Any] = {
    "role": {"buyer": "buyer", "seller": "seller"},
    "connect": "Send a connection request for '{title}' ({role} listing)?\nIts owner has to accept before you can message or quote each other.",
    "connect_chips": ["Send request", "Not now"],
    "message": "Send this message to {who}:\n\n\"{text}\"\n\nShall I send it?",
    "send_chips": ["Send it", "Change it"],
    "quote_head": "Send this quote to {who} on '{title}':",
    "price": "- Price: {price} per {unit}", "quantity": "- Quantity: {qty} {unit}", "total": "- Total: {total}",
    "lead": "- Lead time: {n} days", "incoterms": "- Incoterms: {v}", "payment": "- Payment terms: {v}",
    "valid": "- Valid for {n} days", "notes": "- Notes: {v}",
    "replaces": "This replaces your pending quote {old}; it is withdrawn.", "counter": "This is a counter-offer to their quote {old}.",
    "suggested": "The price is my suggestion, not a figure you gave.",
    "quote_ask": "This is a formal quotation the other side can accept. Shall I send it?",
    "desc": "{number} from {who}: {price} per {unit} x {qty} {unit} = {total}",
    "x_lead": "lead time {n} days", "x_incoterms": "incoterms {v}", "x_payment": "payment terms {v}",
    "accept": "Accept quote {desc}?\n\nThis is binding: it creates a purchase order and awards that quantity on the RFQ, and the other side is told right away.",
    "accept_chips": ["Accept it", "Not yet"],
    "reject": "Decline quote {desc}?{reason}\n\nThe other side is told it was declined.",
    "reason": "\nReason sent with it: \"{v}\"", "reject_chips": ["Decline it", "Keep it open"],
    "done_connect": "✅ Connection request sent (id `{id}`). You can message and quote once they accept.",
    "done_message": "✉️ Message sent (id `{id}`):\n\n> {text}",
    "done_quote": "✅ Quote {number} sent: {price} per {unit} x {qty} = {total} (id `{id}`).",
    "done_accept": "✅ Quote {number} accepted (id `{id}`); purchase order {po}.{note}",
    "po_created": "created", "over_award": " Note: the awarded quantity now exceeds the RFQ quantity.",
    "done_reject": "✅ Quote {number} declined (id `{id}`).",
}

_HINGLISH: dict[str, Any] = {
    "role": {"buyer": "buyer", "seller": "seller"},
    "connect": "'{title}' ({role} listing) ke liye connection request bhejun?\nMessage ya quote tabhi kar paoge jab uska owner accept kare.",
    "connect_chips": ["Request bhejo", "Abhi nahi"],
    "message": "{who} ko yeh message bhejun:\n\n\"{text}\"\n\nBhej dun?",
    "send_chips": ["Bhej do", "Badlo"],
    "quote_head": "'{title}' par {who} ko yeh quote bhejun:",
    "price": "- Price: {price} per {unit}", "quantity": "- Quantity: {qty} {unit}", "total": "- Total: {total}",
    "lead": "- Lead time: {n} din", "incoterms": "- Incoterms: {v}", "payment": "- Payment terms: {v}",
    "valid": "- {n} din tak valid", "notes": "- Notes: {v}",
    "replaces": "Yeh aapke pending quote {old} ki jagah lega; woh wapas ho jayega.", "counter": "Yeh unke quote {old} ka counter-offer hai.",
    "suggested": "Yeh price meri suggestion hai, aapne nahi bataya.",
    "quote_ask": "Yeh ek formal quotation hai jise doosri taraf accept kar sakti hai. Bhej dun?",
    "desc": "{number}, {who} se: {price} per {unit} x {qty} {unit} = {total}",
    "x_lead": "lead time {n} din", "x_incoterms": "incoterms {v}", "x_payment": "payment terms {v}",
    "accept": "Quote {desc} accept karun?\n\nYeh binding hai: isse purchase order banta hai aur RFQ par utni quantity award ho jati hai, aur doosri taraf ko turant pata chal jata hai.",
    "accept_chips": ["Accept karo", "Abhi nahi"],
    "reject": "Quote {desc} decline karun?{reason}\n\nDoosri taraf ko bata diya jayega ki decline hua.",
    "reason": "\nSaath mein bheja jane wala reason: \"{v}\"", "reject_chips": ["Decline karo", "Khula rehne do"],
    "done_connect": "✅ Connection request bhej di (id `{id}`). Unke accept karne ke baad aap message aur quote kar sakte hain.",
    "done_message": "✉️ Message bhej diya (id `{id}`):\n\n> {text}",
    "done_quote": "✅ Quote {number} bhej diya: {price} per {unit} x {qty} = {total} (id `{id}`).",
    "done_accept": "✅ Quote {number} accept ho gaya (id `{id}`); purchase order {po}.{note}",
    "po_created": "ban gaya", "over_award": " Dhyan dein: award ki gayi quantity ab RFQ quantity se zyada hai.",
    "done_reject": "✅ Quote {number} decline ho gaya (id `{id}`).",
}

_HI: dict[str, Any] = {
    "role": {"buyer": "खरीदार", "seller": "विक्रेता"},
    "connect": "'{title}' ({role} लिस्टिंग) के लिए कनेक्शन अनुरोध भेजूँ?\nएक-दूसरे को संदेश या कोटेशन तभी भेज पाएँगे जब इसका मालिक स्वीकार करे।",
    "connect_chips": ["अनुरोध भेजो", "अभी नहीं"],
    "message": "{who} को यह संदेश भेजूँ:\n\n\"{text}\"\n\nभेज दूँ?",
    "send_chips": ["भेज दो", "बदलो"],
    "quote_head": "'{title}' पर {who} को यह कोटेशन भेजूँ:",
    "price": "- कीमत: {price} प्रति {unit}", "quantity": "- मात्रा: {qty} {unit}", "total": "- कुल: {total}",
    "lead": "- डिलीवरी समय: {n} दिन", "incoterms": "- इन्कोटर्म्स: {v}", "payment": "- भुगतान शर्तें: {v}",
    "valid": "- {n} दिन तक मान्य", "notes": "- टिप्पणी: {v}",
    "replaces": "यह आपके लंबित कोटेशन {old} की जगह लेगा; वह वापस हो जाएगा।", "counter": "यह उनके कोटेशन {old} का जवाबी प्रस्ताव है।",
    "suggested": "यह कीमत मेरा सुझाव है, आपने नहीं बताई थी।",
    "quote_ask": "यह एक औपचारिक कोटेशन है जिसे दूसरा पक्ष स्वीकार कर सकता है। भेज दूँ?",
    "desc": "{number}, {who} से: {price} प्रति {unit} x {qty} {unit} = {total}",
    "x_lead": "डिलीवरी समय {n} दिन", "x_incoterms": "इन्कोटर्म्स {v}", "x_payment": "भुगतान शर्तें {v}",
    "accept": "कोटेशन {desc} स्वीकार करूँ?\n\nयह बाध्यकारी है: इससे खरीद आदेश बनता है और RFQ पर उतनी मात्रा आवंटित हो जाती है, और दूसरे पक्ष को तुरंत बता दिया जाता है।",
    "accept_chips": ["स्वीकार करो", "अभी नहीं"],
    "reject": "कोटेशन {desc} अस्वीकार करूँ?{reason}\n\nदूसरे पक्ष को बता दिया जाएगा कि यह अस्वीकार हुआ।",
    "reason": "\nसाथ में भेजा जाने वाला कारण: \"{v}\"", "reject_chips": ["अस्वीकार करो", "खुला रहने दो"],
    "done_connect": "✅ कनेक्शन अनुरोध भेज दिया (id `{id}`)। उनके स्वीकार करने के बाद आप संदेश और कोटेशन भेज सकते हैं।",
    "done_message": "✉️ संदेश भेज दिया (id `{id}`):\n\n> {text}",
    "done_quote": "✅ कोटेशन {number} भेज दिया: {price} प्रति {unit} x {qty} = {total} (id `{id}`)।",
    "done_accept": "✅ कोटेशन {number} स्वीकार हो गया (id `{id}`); खरीद आदेश {po}।{note}",
    "po_created": "बन गया", "over_award": " ध्यान दें: आवंटित मात्रा अब RFQ की मात्रा से ज़्यादा है।",
    "done_reject": "✅ कोटेशन {number} अस्वीकार हो गया (id `{id}`)।",
}

TEXTS = {"en": _EN, "hinglish": _HINGLISH, "hindi_devanagari": _HI}


def texts(lang: Any) -> dict[str, Any]:
    return TEXTS.get(lang if isinstance(lang, str) else "en", _EN)

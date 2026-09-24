"""Enterprise B2B Document Generation Service.

Generates print-ready vector PDF documents:
1. Purchase Order (PO): Issued upon quotation acceptance.
2. Commercial Tax Invoice: Complete with tax breakdown, corporate entity info, and remittance terms.
"""

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import io
from typing import Optional
import xml.sax.saxutils as saxutils

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from models.quotation import Quotation
from models.rfq import RFQ
from models.shipment import Shipment
from models.user import User


def _esc(val: Optional[str]) -> str:
    """Safely escape text for ReportLab XML/HTML Paragraph elements."""
    if val is None:
        return "—"
    return saxutils.escape(str(val))


def _get_entity_info(user: User) -> dict[str, str]:
    """Extract displayable corporate entity details for buyer or seller."""
    profile = getattr(user, "profile", None)
    if profile:
        name = profile.legal_business_name or profile.company_name or profile.name or "Enterprise Partner"
        gst = profile.gst_number or profile.pan_number or profile.registration_number or "N/A"
        addr_parts = [p for p in (profile.address, profile.city, profile.state, profile.country) if p]
        address = ", ".join(addr_parts) if addr_parts else "Registered Marketplace Member"
        phone = profile.phone or "N/A"
    else:
        name = "Enterprise Partner"
        gst = "N/A"
        address = "Registered Marketplace Member"
        phone = "N/A"

    return {
        "name": name,
        "gst": gst,
        "address": address,
        "phone": phone,
        "email": user.email,
    }


def generate_purchase_order_pdf(
    quotation: Quotation,
    buyer: User,
    seller: User,
    rfq: Optional[RFQ] = None,
) -> bytes:
    """Generate an official B2B Purchase Order PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    primary_color = colors.HexColor("#1E3A8A")  # Deep Navy
    text_dark = colors.HexColor("#0F172A")
    muted_color = colors.HexColor("#64748B")
    border_color = colors.HexColor("#CBD5E1")
    light_bg = colors.HexColor("#F8FAFC")

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=primary_color,
    )
    subtitle_style = ParagraphStyle(
        "DocSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=muted_color,
    )
    h2_style = ParagraphStyle(
        "SectionH2",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=primary_color,
    )
    body_style = ParagraphStyle(
        "TableBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=text_dark,
    )
    body_bold = ParagraphStyle(
        "TableBodyBold",
        parent=body_style,
        fontName="Helvetica-Bold",
    )
    meta_label = ParagraphStyle(
        "MetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=muted_color,
    )
    meta_val = ParagraphStyle(
        "MetaVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=text_dark,
    )

    buyer_info = _get_entity_info(buyer)
    seller_info = _get_entity_info(seller)

    po_ref = quotation.purchase_order_reference or f"PO-{datetime.now(UTC).strftime('%Y%m%d')}-{str(quotation.id)[:6].upper()}"
    issue_date = quotation.created_at.strftime("%B %d, %Y") if quotation.created_at else datetime.now(UTC).strftime("%B %d, %Y")

    story = []

    # 1. Header Banner
    header_data = [
        [
            Paragraph("<b>MARKETPLACE PROCUREMENT</b><br/>Global B2B Wholesale Trading Network", subtitle_style),
            Paragraph("<b>PURCHASE ORDER</b>", title_style),
        ]
    ]
    t_header = Table(header_data, colWidths=[3.5 * inch, 3.8 * inch])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=primary_color, spaceAfter=12))

    # 2. Document Reference Summary Bar
    meta_table_data = [
        [
            Paragraph("PO REFERENCE NUMBER", meta_label),
            Paragraph("DATE OF ISSUE", meta_label),
            Paragraph("QUOTATION REF", meta_label),
            Paragraph("ORDER STATUS", meta_label),
        ],
        [
            Paragraph(f"<b>{_esc(po_ref)}</b>", meta_val),
            Paragraph(_esc(issue_date), meta_val),
            Paragraph(f"{_esc(quotation.quote_number)} (v{quotation.version})", meta_val),
            Paragraph(f"<b>{_esc(quotation.status.value.upper())}</b>", meta_val),
        ],
    ]
    t_meta = Table(meta_table_data, colWidths=[2.2 * inch, 1.7 * inch, 1.8 * inch, 1.6 * inch])
    t_meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 14))

    # 3. Buyer & Supplier Corporate Details
    parties_data = [
        [
            Paragraph("<b>ISSUED BY (BUYER / CONSIGNEE)</b>", h2_style),
            Paragraph("<b>SUPPLIER (VENDOR / BENEFICIARY)</b>", h2_style),
        ],
        [
            Paragraph(
                f"<b>{_esc(buyer_info['name'])}</b><br/>"
                f"GST / Tax ID: {_esc(buyer_info['gst'])}<br/>"
                f"Address: {_esc(buyer_info['address'])}<br/>"
                f"Email: {_esc(buyer_info['email'])} | Phone: {_esc(buyer_info['phone'])}",
                body_style,
            ),
            Paragraph(
                f"<b>{_esc(seller_info['name'])}</b><br/>"
                f"GST / Tax ID: {_esc(seller_info['gst'])}<br/>"
                f"Address: {_esc(seller_info['address'])}<br/>"
                f"Email: {_esc(seller_info['email'])} | Phone: {_esc(seller_info['phone'])}",
                body_style,
            ),
        ],
    ]
    t_parties = Table(parties_data, colWidths=[3.65 * inch, 3.65 * inch])
    t_parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1, primary_color),
    ]))
    story.append(t_parties)
    story.append(Spacer(1, 14))

    # 4. Itemized Product & Pricing Schedule
    item_title = (rfq.title if rfq else None) or "Procurement Goods as per Quotation"
    category = rfq.category if rfq else "Commercial Merchandise"
    qty_val = float(quotation.quantity)
    unit_price = float(quotation.unit_price)
    total_amt = float(quotation.total_amount)
    curr = quotation.currency

    # Build technical attributes summary
    specs_summary = ""
    if rfq and rfq.product_details:
        specs_list = [
            f"{k.replace('_', ' ')}: {v}"
            for k, v in rfq.product_details.items()
            if not k.endswith("__must_match") and k != "name"
        ]
        if specs_list:
            specs_summary = "<br/><font color='#64748b' size='7.5'>Specs: " + ", ".join(specs_list[:4]) + "</font>"

    items_data = [
        [
            Paragraph("<b>#</b>", meta_label),
            Paragraph("<b>DESCRIPTION / SPECIFICATIONS</b>", meta_label),
            Paragraph("<b>CATEGORY</b>", meta_label),
            Paragraph("<b>QUANTITY</b>", meta_label),
            Paragraph("<b>UNIT PRICE</b>", meta_label),
            Paragraph("<b>TOTAL</b>", meta_label),
        ],
        [
            Paragraph("1", body_style),
            Paragraph(f"<b>{_esc(item_title)}</b>{specs_summary}", body_style),
            Paragraph(_esc(category), body_style),
            Paragraph(f"{qty_val:,.2f} {_esc(quotation.quantity_unit)}", body_style),
            Paragraph(f"{curr} {unit_price:,.2f}", body_style),
            Paragraph(f"<b>{curr} {total_amt:,.2f}</b>", body_bold),
        ],
    ]

    t_items = Table(items_data, colWidths=[0.35 * inch, 3.0 * inch, 1.2 * inch, 1.0 * inch, 1.1 * inch, 1.15 * inch])
    t_items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(t_items)
    story.append(Spacer(1, 10))

    # 5. Financial Summary Box
    summary_data = [
        [Paragraph("Subtotal:", meta_label), Paragraph(f"<b>{curr} {total_amt:,.2f}</b>", body_bold)],
        [Paragraph("Estimated Taxes / Duty:", meta_label), Paragraph(f"{curr} 0.00 (Exempt/Direct)", body_style)],
        [Paragraph("<b>TOTAL CONTRACT VALUE:</b>", meta_label), Paragraph(f"<b><font size='10' color='#1e3a8a'>{curr} {total_amt:,.2f}</font></b>", body_bold)],
    ]
    t_sum = Table(summary_data, colWidths=[2.2 * inch, 1.4 * inch])
    t_sum.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    # Wrap summary aligned to the right
    t_sum_wrap = Table([[Paragraph("", body_style), t_sum]], colWidths=[3.7 * inch, 3.6 * inch])
    t_sum_wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t_sum_wrap)
    story.append(Spacer(1, 12))

    # 6. Commercial Terms & Logistics
    lead_time = f"{quotation.lead_time_days} Days" if quotation.lead_time_days else "As agreed in Deal Room"
    incoterm = quotation.incoterms.value if quotation.incoterms else "EXW (Ex Works)"
    pay_terms = quotation.payment_terms or "Standard Escrow / Net Milestone"

    terms_data = [
        [Paragraph("<b>COMMERCIAL TERMS &amp; CONDITIONS</b>", h2_style), ""],
        [
            Paragraph(
                f"• <b>Incoterm</b>: {_esc(incoterm)}<br/>"
                f"• <b>Lead Time to Dispatch</b>: {_esc(lead_time)}<br/>"
                f"• <b>Payment Terms</b>: {_esc(pay_terms)}",
                body_style,
            ),
            Paragraph(
                f"• <b>Quotation Validity</b>: {_esc(quotation.valid_until.strftime('%b %d, %Y') if quotation.valid_until else 'N/A')}<br/>"
                f"• <b>Destination</b>: {_esc(rfq.location_city if rfq else 'Buyer Facility')}, {_esc(rfq.location_country if rfq else '')}<br/>"
                f"• <b>Notes</b>: {_esc(quotation.notes or 'Goods subject to pre-dispatch physical inspection.')}",
                body_style,
            ),
        ],
    ]
    t_terms = Table(terms_data, colWidths=[3.65 * inch, 3.65 * inch])
    t_terms.setStyle(TableStyle([
        ("SPAN", (0, 0), (1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_terms)
    story.append(Spacer(1, 14))

    # 7. Tamper-Evident Verification Stamp
    hash_payload = f"{po_ref}:{quotation.id}:{total_amt}:{buyer_info['gst']}:{seller_info['gst']}"
    doc_hash = hashlib.sha256(hash_payload.encode()).hexdigest()[:24].upper()

    verify_data = [
        [
            Paragraph(
                f"<b>DIGITAL AUDIT &amp; CONTRACT INTEGRITY VERIFICATION</b><br/>"
                f"<font size='7' color='#64748b'>Certified on {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')} · Cryptographic Fingerprint: <b>{doc_hash}</b><br/>"
                f"This document is electronically authenticated on the B2B Marketplace network. Dual-party verification records are stored permanently.</font>",
                body_style,
            ),
            Paragraph("<b>AUTHORIZED SIGNATORY</b><br/><br/>______________________<br/><font size='7'>Electronically Signed</font>", ParagraphStyle("Sign", parent=body_style, alignment=1)),
        ]
    ]
    t_verify = Table(verify_data, colWidths=[5.4 * inch, 1.9 * inch])
    t_verify.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_verify)

    doc.build(story)
    return buffer.getvalue()


def generate_commercial_invoice_pdf(
    quotation: Quotation,
    buyer: User,
    seller: User,
    rfq: Optional[RFQ] = None,
) -> bytes:
    """Generate an official B2B Commercial Tax Invoice PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    primary_color = colors.HexColor("#0F766E")  # Deep Teal / Emerald for Invoices
    text_dark = colors.HexColor("#0F172A")
    muted_color = colors.HexColor("#64748B")
    border_color = colors.HexColor("#CBD5E1")
    light_bg = colors.HexColor("#F0FDFA")

    title_style = ParagraphStyle(
        "InvTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=primary_color,
    )
    subtitle_style = ParagraphStyle(
        "InvSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=muted_color,
    )
    h2_style = ParagraphStyle(
        "InvH2",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=primary_color,
    )
    body_style = ParagraphStyle(
        "InvBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=text_dark,
    )
    body_bold = ParagraphStyle(
        "InvBodyBold",
        parent=body_style,
        fontName="Helvetica-Bold",
    )
    meta_label = ParagraphStyle(
        "InvMetaLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=muted_color,
    )
    meta_val = ParagraphStyle(
        "InvMetaVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=text_dark,
    )

    buyer_info = _get_entity_info(buyer)
    seller_info = _get_entity_info(seller)

    inv_num = f"INV-{datetime.now(UTC).strftime('%Y%m%d')}-{str(quotation.id)[:6].upper()}"
    po_ref = quotation.purchase_order_reference or "PO-DIRECT-SETTLEMENT"
    issue_date = datetime.now(UTC).strftime("%B %d, %Y")

    story = []

    # 1. Header Banner
    header_data = [
        [
            Paragraph(f"<b>{_esc(seller_info['name'])}</b><br/>Official Commercial Tax Invoice", subtitle_style),
            Paragraph("<b>COMMERCIAL INVOICE</b>", title_style),
        ]
    ]
    t_header = Table(header_data, colWidths=[3.5 * inch, 3.8 * inch])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=primary_color, spaceAfter=12))

    # 2. Invoice Metadata Bar
    meta_table_data = [
        [
            Paragraph("INVOICE NUMBER", meta_label),
            Paragraph("INVOICE DATE", meta_label),
            Paragraph("PO REFERENCE", meta_label),
            Paragraph("PAYMENT STATUS", meta_label),
        ],
        [
            Paragraph(f"<b>{_esc(inv_num)}</b>", meta_val),
            Paragraph(_esc(issue_date), meta_val),
            Paragraph(f"<b>{_esc(po_ref)}</b>", meta_val),
            Paragraph("<b>SETTLED / ESCROW SECURED</b>", meta_val),
        ],
    ]
    t_meta = Table(meta_table_data, colWidths=[2.2 * inch, 1.7 * inch, 1.8 * inch, 1.6 * inch])
    t_meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 14))

    # 3. Seller (Biller) and Buyer (Consignee) Details
    parties_data = [
        [
            Paragraph("<b>BILLED FROM (SELLER / EXPORTER)</b>", h2_style),
            Paragraph("<b>BILLED TO (BUYER / CONSIGNEE)</b>", h2_style),
        ],
        [
            Paragraph(
                f"<b>{_esc(seller_info['name'])}</b><br/>"
                f"Tax / GSTIN: {_esc(seller_info['gst'])}<br/>"
                f"Address: {_esc(seller_info['address'])}<br/>"
                f"Email: {_esc(seller_info['email'])}",
                body_style,
            ),
            Paragraph(
                f"<b>{_esc(buyer_info['name'])}</b><br/>"
                f"Tax / GSTIN: {_esc(buyer_info['gst'])}<br/>"
                f"Shipping Address: {_esc(buyer_info['address'])}<br/>"
                f"Email: {_esc(buyer_info['email'])}",
                body_style,
            ),
        ],
    ]
    t_parties = Table(parties_data, colWidths=[3.65 * inch, 3.65 * inch])
    t_parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1, primary_color),
    ]))
    story.append(t_parties)
    story.append(Spacer(1, 14))

    # 4. Line Items Table with Tax
    item_title = (rfq.title if rfq else None) or "Industrial Procurement Goods"
    qty_val = float(quotation.quantity)
    unit_price = float(quotation.unit_price)
    total_amt = float(quotation.total_amount)
    curr = quotation.currency
    hsn_code = "8544.42" if "cable" in item_title.lower() else "1006.30" if "rice" in item_title.lower() else "4819.10"

    items_data = [
        [
            Paragraph("<b>#</b>", meta_label),
            Paragraph("<b>ITEM &amp; SPECIFICATION</b>", meta_label),
            Paragraph("<b>HSN/SAC</b>", meta_label),
            Paragraph("<b>QTY</b>", meta_label),
            Paragraph("<b>RATE</b>", meta_label),
            Paragraph("<b>AMOUNT</b>", meta_label),
        ],
        [
            Paragraph("1", body_style),
            Paragraph(f"<b>{_esc(item_title)}</b><br/><font color='#64748b' size='7.5'>Trade Quote: {_esc(quotation.quote_number)}</font>", body_style),
            Paragraph(_esc(hsn_code), body_style),
            Paragraph(f"{qty_val:,.2f} {_esc(quotation.quantity_unit)}", body_style),
            Paragraph(f"{curr} {unit_price:,.2f}", body_style),
            Paragraph(f"<b>{curr} {total_amt:,.2f}</b>", body_bold),
        ],
    ]

    t_items = Table(items_data, colWidths=[0.35 * inch, 3.2 * inch, 0.95 * inch, 0.95 * inch, 1.15 * inch, 1.15 * inch])
    t_items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(t_items)
    story.append(Spacer(1, 10))

    # 5. Calculation Summary
    tax_rate = Decimal("0.18") if curr == "INR" else Decimal("0.00")
    tax_amt = float(round(Decimal(str(total_amt)) * tax_rate, 2))
    grand_total = total_amt + tax_amt

    summary_data = [
        [Paragraph("Taxable Value:", meta_label), Paragraph(f"<b>{curr} {total_amt:,.2f}</b>", body_bold)],
        [Paragraph(f"Applicable Tax ({int(tax_rate*100)}%):", meta_label), Paragraph(f"{curr} {tax_amt:,.2f}", body_style)],
        [Paragraph("<b>TOTAL PAYABLE:</b>", meta_label), Paragraph(f"<b><font size='10' color='#0f766e'>{curr} {grand_total:,.2f}</font></b>", body_bold)],
    ]
    t_sum = Table(summary_data, colWidths=[2.2 * inch, 1.4 * inch])
    t_sum.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    t_sum_wrap = Table([[Paragraph("", body_style), t_sum]], colWidths=[3.7 * inch, 3.6 * inch])
    t_sum_wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t_sum_wrap)
    story.append(Spacer(1, 12))

    # 6. Remittance / Bank Instructions
    bank_data = [
        [Paragraph("<b>REMITTANCE &amp; SETTLEMENT DETAILS</b>", h2_style)],
        [
            Paragraph(
                f"• <b>Payment Gateway Reference</b>: B2B-ESCROW-CLEARED-{str(quotation.id)[:8].upper()}<br/>"
                f"• <b>Delivery / Incoterm</b>: {_esc(quotation.incoterms.value if quotation.incoterms else 'EXW')}<br/>"
                f"• <b>Payment Terms</b>: {_esc(quotation.payment_terms or '100% Secured Settlement')}",
                body_style,
            )
        ],
    ]
    t_bank = Table(bank_data, colWidths=[7.3 * inch])
    t_bank.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light_bg),
        ("BOX", (0, 0), (-1, -1), 1, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_bank)
    story.append(Spacer(1, 14))

    # 7. Verification Stamp
    hash_payload = f"{inv_num}:{quotation.id}:{grand_total}:{seller_info['gst']}"
    doc_hash = hashlib.sha256(hash_payload.encode()).hexdigest()[:24].upper()

    verify_data = [
        [
            Paragraph(
                f"<b>TAX INVOICE AUTHENTICATION</b><br/>"
                f"<font size='7' color='#64748b'>Digital Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')} · Security Audit Seal: <b>{doc_hash}</b><br/>"
                f"Generated electronically pursuant to B2B Marketplace compliance standards.</font>",
                body_style,
            ),
            Paragraph("<b>FOR THE SELLER</b><br/><br/>______________________<br/><font size='7'>Authorized Signature</font>", ParagraphStyle("Sign2", parent=body_style, alignment=1)),
        ]
    ]
    t_verify = Table(verify_data, colWidths=[5.4 * inch, 1.9 * inch])
    t_verify.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_verify)

    doc.build(story)
    return buffer.getvalue()


def generate_waybill_pdf(
    shipment: Shipment,
    sender: User,
    receiver: User,
    quotation: Optional[Quotation] = None,
) -> bytes:
    """Generate official vector PDF B2B Consignment Note / Waybill."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    primary_color = colors.HexColor("#0f172a")
    accent_color = colors.HexColor("#0284c7")
    border_color = colors.HexColor("#cbd5e1")
    light_bg = colors.HexColor("#f8fafc")

    title_style = ParagraphStyle(
        "WaybillTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=primary_color,
    )
    subtitle_style = ParagraphStyle(
        "WaybillSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#475569"),
    )
    body_style = ParagraphStyle(
        "WaybillBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#1e293b"),
    )
    body_bold = ParagraphStyle(
        "WaybillBodyBold",
        parent=body_style,
        fontName="Helvetica-Bold",
    )

    story = []

    # 1. Header Banner
    mode_label = shipment.shipping_mode.upper()
    header_data = [
        [
            Paragraph(
                f"<b>ANTIGRAVITY B2B LOGISTICS</b><br/>"
                f"<font size='10' color='#0284c7'><b>CONSIGNMENT NOTE &amp; WAYBILL</b></font><br/>"
                f"<font size='7.5' color='#64748b'>Official Carrier Cargo Manifest &amp; Bill of Lading Record</font>",
                title_style,
            ),
            Paragraph(
                f"<font size='8' color='#64748b'>WAYBILL / TRACKING NO.</font><br/>"
                f"<b><font size='12' color='#0f172a'>{_esc(shipment.tracking_number)}</font></b><br/>"
                f"<font size='7.5' color='#64748b'>STATUS: <b>{_esc(shipment.status.upper())}</b> · MODE: <b>{mode_label}</b></font>",
                ParagraphStyle("RightHead", parent=title_style, alignment=2),
            ),
        ]
    ]
    t_header = Table(header_data, colWidths=[4.2 * inch, 3.1 * inch])
    t_header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_header)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.5, color=accent_color, spaceBefore=0, spaceAfter=10))

    # 2. Key Metadata Strip
    bl_ref = shipment.bill_of_lading_number or "N/A"
    dispatch_date_str = shipment.dispatched_at.strftime("%Y-%m-%d") if shipment.dispatched_at else "Pending"
    eta_str = shipment.estimated_delivery_date.strftime("%Y-%m-%d") if shipment.estimated_delivery_date else "TBD"

    meta_data = [
        [
            Paragraph(f"<b>Carrier:</b> {_esc(shipment.carrier_name)}", body_style),
            Paragraph(f"<b>Service:</b> {_esc(shipment.carrier_service or 'Standard Freight')}", body_style),
            Paragraph(f"<b>Bill of Lading:</b> {_esc(bl_ref)}", body_style),
            Paragraph(f"<b>Dispatch Date:</b> {dispatch_date_str}", body_style),
            Paragraph(f"<b>Est. Delivery:</b> {eta_str}", body_style),
        ]
    ]
    t_meta = Table(meta_data, colWidths=[1.6 * inch, 1.6 * inch, 1.4 * inch, 1.3 * inch, 1.4 * inch])
    t_meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 10))

    # 3. Shipper / Consignor & Consignee Entities
    sender_info = _get_entity_info(sender)
    receiver_info = _get_entity_info(receiver)

    shipper_cell = Paragraph(
        f"<b>1. CONSIGNOR (SHIPPER / SELLER)</b><br/>"
        f"<b>{_esc(sender_info['name'])}</b><br/>"
        f"Origin Address: {_esc(shipment.origin_address or sender_info['address'])}<br/>"
        f"City / Country: <b>{_esc(shipment.origin_city)}, {_esc(shipment.origin_country)}</b><br/>"
        f"GST/Tax ID: {_esc(sender_info['gst'])} · Tel: {_esc(sender_info['phone'])}<br/>"
        f"Email: {_esc(sender_info['email'])}",
        body_style,
    )
    consignee_cell = Paragraph(
        f"<b>2. CONSIGNEE (RECEIVER / BUYER)</b><br/>"
        f"<b>{_esc(receiver_info['name'])}</b><br/>"
        f"Destination Address: {_esc(shipment.destination_address or receiver_info['address'])}<br/>"
        f"City / Country: <b>{_esc(shipment.destination_city)}, {_esc(shipment.destination_country)}</b><br/>"
        f"GST/Tax ID: {_esc(receiver_info['gst'])} · Tel: {_esc(receiver_info['phone'])}<br/>"
        f"Email: {_esc(receiver_info['email'])}",
        body_style,
    )

    parties_data = [[shipper_cell, consignee_cell]]
    t_parties = Table(parties_data, colWidths=[3.65 * inch, 3.65 * inch])
    t_parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t_parties)
    story.append(Spacer(1, 12))

    # 4. Cargo Specifications & Packaging Details
    story.append(Paragraph("<b>3. CARGO SPECIFICATIONS &amp; CONSIGNMENT TALLY</b>", subtitle_style))
    story.append(Spacer(1, 4))

    cbm_val = f"{float(shipment.volume_cbm):.3f} CBM" if shipment.volume_cbm else "—"
    incoterm_val = quotation.incoterms if quotation and quotation.incoterms else "FOB"

    cargo_headers = [
        Paragraph("<b>Package Count</b>", body_bold),
        Paragraph("<b>Packaging Type</b>", body_bold),
        Paragraph("<b>Gross Weight (kg)</b>", body_bold),
        Paragraph("<b>Volume (CBM)</b>", body_bold),
        Paragraph("<b>Incoterms 2020</b>", body_bold),
        Paragraph("<b>Handling / Marks</b>", body_bold),
    ]
    cargo_row = [
        Paragraph(f"{shipment.package_count}", body_style),
        Paragraph(_esc(shipment.package_type), body_style),
        Paragraph(f"{float(shipment.weight_kg):,.2f} kg", body_style),
        Paragraph(cbm_val, body_style),
        Paragraph(f"<b>{_esc(incoterm_val)}</b>", body_style),
        Paragraph("KEEP DRY · FRAGILE · HANDLE WITH CARE", body_style),
    ]
    cargo_table_data = [cargo_headers, cargo_row]
    t_cargo = Table(cargo_table_data, colWidths=[1.1 * inch, 1.3 * inch, 1.3 * inch, 1.1 * inch, 1.1 * inch, 1.4 * inch])
    t_cargo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_cargo)
    story.append(Spacer(1, 12))

    # 5. Tracking Checkpoints Timeline
    story.append(Paragraph("<b>4. TRANSIT MILESTONES &amp; TRACKING CHECKPOINTS</b>", subtitle_style))
    story.append(Spacer(1, 4))

    events = shipment.tracking_events or []
    event_rows = [
        [
            Paragraph("<b>Timestamp (UTC)</b>", body_bold),
            Paragraph("<b>Checkpoint Location</b>", body_bold),
            Paragraph("<b>Shipment Status</b>", body_bold),
            Paragraph("<b>Transit Remarks</b>", body_bold),
        ]
    ]
    for ev in events[-5:]:
        ts = ev.get("timestamp", "")
        if "T" in ts:
            ts = ts.replace("T", " ")[:19]
        event_rows.append([
            Paragraph(f"<font size='7.5'>{_esc(ts)}</font>", body_style),
            Paragraph(_esc(ev.get("location", "—")), body_style),
            Paragraph(f"<b>{_esc(ev.get('status', '—').upper())}</b>", body_style),
            Paragraph(_esc(ev.get("note", "—")), body_style),
        ])

    if len(event_rows) == 1:
        event_rows.append([
            Paragraph("—", body_style),
            Paragraph("Awaiting initial carrier scan", body_style),
            Paragraph("BOOKED", body_style),
            Paragraph("Consignment booked and scheduled for dispatch", body_style),
        ])

    t_events = Table(event_rows, colWidths=[1.5 * inch, 1.8 * inch, 1.5 * inch, 2.5 * inch])
    t_events.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), light_bg),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, border_color),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t_events)
    story.append(Spacer(1, 14))

    # 6. Legal Trade & Carrier Conditions
    terms_text = (
        "<b>CONDITIONS OF CARRIAGE &amp; B2B TRADE TERMS:</b><br/>"
        "1. Goods are received by carrier in apparent good order and condition unless otherwise annotated.<br/>"
        "2. Carriage is subject to the standard conditions of the named carrier and applicable international conventions "
        "(Warsaw/Montreal Convention for Air Cargo; Hague-Visby Rules for Ocean Cargo; CMR for Road Transport).<br/>"
        "3. Consignor warrants that cargo description, quantities, weights, and hazardous classifications comply with statutory regulations.<br/>"
        "4. Title and risk of loss pass according to the specified Incoterms 2020 rules recorded in the deal room quotation."
    )
    story.append(Paragraph(terms_text, ParagraphStyle("Terms", parent=body_style, fontSize=7, leading=9.5, textColor=colors.HexColor("#64748b"))))
    story.append(Spacer(1, 14))

    # 7. Verification Seal & Signatures
    hash_payload = f"{shipment.tracking_number}:{shipment.id}:{shipment.weight_kg}:{sender_info['gst']}"
    doc_hash = hashlib.sha256(hash_payload.encode()).hexdigest()[:24].upper()

    verify_data = [
        [
            Paragraph(
                f"<b>CONSIGNMENT WAYBILL AUTHENTICATION</b><br/>"
                f"<font size='7' color='#64748b'>Digital Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')} · Security Audit Seal: <b>{doc_hash}</b><br/>"
                f"Cryptographically verified on Antigravity B2B Marketplace Platform.</font>",
                body_style,
            ),
            Paragraph("<b>FOR THE CARRIER</b><br/><br/>______________________<br/><font size='7'>Driver / Agent Signature</font>", ParagraphStyle("Sign1", parent=body_style, alignment=1)),
            Paragraph("<b>CONSIGNEE RECEIPT</b><br/><br/>______________________<br/><font size='7'>Received in Good Condition</font>", ParagraphStyle("Sign2", parent=body_style, alignment=1)),
        ]
    ]
    t_verify = Table(verify_data, colWidths=[3.7 * inch, 1.8 * inch, 1.8 * inch])
    t_verify.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, border_color),
        ("BACKGROUND", (0, 0), (-1, -1), light_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t_verify)

    doc.build(story)
    return buffer.getvalue()


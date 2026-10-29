"""Title rule, quotation GST, off-request-path HSN classification, internal-key stripping (no live LLM, no DB)."""
import asyncio
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from models.enums import EmbeddingStatus
from schemas.marketplace import CatalogItemOut
from schemas.quotation import QuotationCreate
from schemas.rfq import RFQOut, clean_title, public_product_details
from services import document_generator, hsn_service, rfq_service, scheduler


def run(coro):
    return asyncio.run(coro)


# ---- 1. titles
@pytest.mark.parametrize("t", ["PP", "MS", "GI", "CR", "HR", "Rice", "M8"])
def test_short_product_codes_allowed(t):
    assert clean_title(t) == t


@pytest.mark.parametrize("t", ["a", "aa", "xxxx", "0000", "<b></b>", "!!", "😀😀", "   "])
def test_junk_titles_refused(t):
    with pytest.raises(ValueError):
        clean_title(t)


# ---- 2. GST
BASE = {"unit_price": "10", "quantity": "5"}


@pytest.mark.parametrize("rate", ["0", "0.1", "0.25", "1.5", "3", "5", "12", "18", "28"])
def test_gst_slabs_accepted(rate):
    assert QuotationCreate(**BASE, gst_rate=rate).gst_rate == Decimal(rate)


@pytest.mark.parametrize("rate", ["7", "29", "-1", "18.5"])
def test_gst_other_rates_rejected(rate):
    with pytest.raises(ValidationError) as e:
        QuotationCreate(**BASE, gst_rate=rate)
    assert "GST slabs" in str(e.value)


def test_gst_optional():
    assert QuotationCreate(**BASE).gst_rate is None


def _quote(gst):
    return SimpleNamespace(
        id=uuid.uuid4(), quote_number="QT-1", version=1, unit_price=Decimal("100"), currency="INR",
        quantity=Decimal("10"), quantity_unit="kg", total_amount=Decimal("1000"), gst_rate=gst,
        lead_time_days=None, incoterms=None, payment_terms=None, valid_until=None, notes=None,
        rfq_snapshot={"title": "Rice", "hsn": "1006"}, created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC), purchase_order_reference=None,
        sender_id=uuid.uuid4(), receiver_id=uuid.uuid4(), rfq_id=uuid.uuid4(), connection_id=uuid.uuid4(),
    )


def test_declared_rate_is_percent():
    order = SimpleNamespace(product_details={})
    assert document_generator._declared_tax_rate(_quote(Decimal("0.1")), order) == Decimal("0.001")
    assert document_generator._declared_tax_rate(_quote(Decimal("18")), order) == Decimal("0.18")
    assert document_generator._declared_tax_rate(_quote(None), order) is None


def _party():
    return SimpleNamespace(id=uuid.uuid4(), profile=None, email="a@x.com")


def _render(monkeypatch, gst):
    """Render the invoice PDF for real, capturing every Paragraph's text (no PDF text extractor is installed)."""
    texts = []
    real = document_generator.Paragraph

    def spy(text, *a, **k):
        texts.append(str(text))
        return real(text, *a, **k)

    monkeypatch.setattr(document_generator, "Paragraph", spy)
    pdf = document_generator.generate_commercial_invoice_pdf(_quote(gst), _party(), _party(), None)
    assert pdf.startswith(b"%PDF")
    return " | ".join(texts)


def test_pdf_with_rate(monkeypatch):
    out = _render(monkeypatch, Decimal("18"))
    assert "GST (18%)" in out and "180.00" in out and "1,180.00" in out and "TOTAL PAYABLE" in out


def test_pdf_without_rate(monkeypatch):
    out = _render(monkeypatch, None)
    assert "As applicable (HSN 1006)" in out and "EXCL. TAXES" in out


# ---- 3. HSN pending lifecycle
def _rfq(pd=None, title="Steel rod"):
    return SimpleNamespace(
        id=uuid.uuid4(), title=title, description=None, product_details=pd or {},
        embedding_status=EmbeddingStatus.INDEXED if hasattr(EmbeddingStatus, "INDEXED") else EmbeddingStatus.STALE,
    )


def test_mark_pending_rules():
    r = _rfq()
    rfq_service._mark_hsn_pending(r)
    assert r.product_details["_hsn_pending"] is True
    user = _rfq({"hsn_code": "7214", "hsn_source": "user"})
    rfq_service._mark_hsn_pending(user, refresh_ai=True)
    assert "_hsn_pending" not in user.product_details
    ai = _rfq({"hsn_code": "7214", "hsn_source": "ai"})
    rfq_service._mark_hsn_pending(ai)
    assert "_hsn_pending" not in ai.product_details
    rfq_service._mark_hsn_pending(ai, refresh_ai=True)
    assert ai.product_details["_hsn_pending"] is True


class FakeDB:
    def __init__(self, rows):
        self.rows = {r.id: r for r in rows}
        self.commits = 0

    async def scalars(self, _q):
        return SimpleNamespace(all=lambda: list(self.rows))

    async def get(self, _model, rid):
        return self.rows.get(rid)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def flush(self):
        pass


def test_complete_pending_fills_and_clears(monkeypatch):
    async def classify(_text):
        return "7214"

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", classify)
    reindexed = []
    monkeypatch.setattr(rfq_service, "refresh_index_fields", lambda r: reindexed.append(r.id))
    row = _rfq({"_hsn_pending": True, "color": "red"})
    assert run(rfq_service.complete_hsn_pending(FakeDB([row]))) == 1
    assert row.product_details == {"color": "red", "hsn_code": "7214", "hsn_source": "ai"}
    assert row.embedding_status == EmbeddingStatus.STALE and reindexed == [row.id]


def test_complete_pending_keeps_mark_on_failure_clears_on_none(monkeypatch):
    async def boom(_text):
        raise RuntimeError("model down")

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", boom)
    row = _rfq({"_hsn_pending": True})
    assert run(rfq_service.complete_hsn_pending(FakeDB([row]))) == 0
    assert row.product_details.get("_hsn_pending") is True

    async def none(_text):
        return None

    monkeypatch.setattr(hsn_service, "classify_hsn_with_ai", none)
    assert run(rfq_service.complete_hsn_pending(FakeDB([row]))) == 1
    assert "_hsn_pending" not in row.product_details and "hsn_code" not in row.product_details


def test_job_registered():
    assert "complete_hsn" in [j.name for j in scheduler.default_jobs()]


# ---- 4. internal keys
def test_public_product_details_strips_internal():
    d = {"color": "red", "_canon": {"x": 1}, "_canon_pending": True, "_hsn_pending": True, "_negated": ["a"]}
    assert public_product_details(d) == {"color": "red"}
    assert "_canon" in d  # storage untouched


def test_rfq_out_and_catalog_out_strip_internal():
    rfq = RFQOut.model_construct(product_details={"a": 1, "_canon": {}, "_hsn_pending": True})
    assert rfq.model_dump()["product_details"] == {"a": 1}
    item = CatalogItemOut.model_construct(product_details={"a": 1, "_canon_pending": True})
    assert item.model_dump()["product_details"] == {"a": 1}

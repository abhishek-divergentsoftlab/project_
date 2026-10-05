"""Pytest integration for the 1,000+ Case Black-Box Conversational AI QA Suite.

Tests real-world blind-user interactions across 25 industry sectors:
- Listing analysis & market benchmark purity (zero cross-category pollution)
- Multi-turn RFQ drafting and field modifications
- Seller stock announcements & role identification
- Commercial trade, logistics, Incoterms, and escrow inquiries
- Informal, typo-ridden & Hinglish user messages
- Guardrail boundary enforcement and zero false-positive refusals
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from run_ai_conversational_qa import ConversationalQAEvaluationEngine


@pytest.mark.asyncio
async def test_suite_1_listing_and_price_benchmark_sanity(db: AsyncSession):
    """Test 250 real-world product listing evaluations for price sanity and zero pollution."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_1_price_and_listing_benchmarks(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 1: {[f.failure_reason for f in failed[:5]]}"


@pytest.mark.asyncio
async def test_suite_2_multiturn_rfq_drafting(db: AsyncSession):
    """Test 250 multi-turn user conversation sequences creating and updating RFQs."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_2_multiturn_rfq_drafting(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 2: {[f.failure_reason for f in failed[:5]]}"


@pytest.mark.asyncio
async def test_suite_3_seller_inquiries(db: AsyncSession):
    """Test 150 seller stock announcements and buyer demand matching inquiries."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_3_seller_inquiries(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 3: {[f.failure_reason for f in failed[:5]]}"


@pytest.mark.asyncio
async def test_suite_4_commercial_and_escrow(db: AsyncSession):
    """Test 150 commercial negotiation, logistics, Incoterms, and escrow guidance cases."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_4_commercial_and_escrow(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 4: {[f.failure_reason for f in failed[:5]]}"


@pytest.mark.asyncio
async def test_suite_5_informal_and_hinglish(db: AsyncSession):
    """Test 100 informal, typo-ridden, and Hindi/Hinglish user queries."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_5_informal_and_hinglish(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 5: {[f.failure_reason for f in failed[:5]]}"


@pytest.mark.asyncio
async def test_suite_6_guardrails_and_boundaries(db: AsyncSession):
    """Test 100 guardrail boundary cases ensuring strict B2B refusal and 0 false refusals."""
    engine = ConversationalQAEvaluationEngine()
    await engine.run_suite_6_guardrails_and_boundaries(db)
    failed = [r for r in engine.results if not r.passed]
    assert len(failed) == 0, f"{len(failed)} tests failed in Suite 6: {[f.failure_reason for f in failed[:5]]}"

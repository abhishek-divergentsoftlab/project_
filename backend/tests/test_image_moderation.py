"""Tests for AI image content moderation using qwen3-vl:8b."""

from unittest.mock import MagicMock, patch

import pytest
from core.config import settings
from services.image_moderator import _extract_json, inspect_live_image


@pytest.fixture(autouse=True)
def enable_moderation_for_tests():
    """Ensure image moderation is enabled for moderator tests."""
    with patch.object(settings, "IMAGE_MODERATION_ENABLED", True):
        yield


def test_extract_json_variants():
    # Plain JSON
    assert _extract_json('{"flagged": false, "reason": ""}') == {"flagged": False, "reason": ""}

    # Markdown code fence
    fence = '```json\n{"flagged": true, "category": "violence", "reason": "weapons detected"}\n```'
    assert _extract_json(fence) == {"flagged": True, "category": "violence", "reason": "weapons detected"}

    # Mixed text / thinking reasoning followed by JSON
    mixed = 'Thinking process: analyzing image carefully...\n{"flagged": true, "category": "sexual", "reason": "nudity"}'
    assert _extract_json(mixed) == {"flagged": True, "category": "sexual", "reason": "nudity"}

    # Invalid / empty
    assert _extract_json("") is None
    assert _extract_json("not json at all") is None


@pytest.mark.asyncio
async def test_inspect_live_image_safe():
    fake_response = {
        "response": '{"flagged": false, "category": null, "reason": "Authentic warehouse inventory batch"}',
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        is_safe, warning, details = await inspect_live_image(b"fake_image_bytes", "image/jpeg")
        assert is_safe is True
        assert warning is None
        assert details["flagged"] is False


@pytest.mark.asyncio
async def test_inspect_live_image_flagged_vulgar():
    fake_response = {
        "response": "",
        "thinking": '{"flagged": true, "category": "vulgarity", "reason": "Explicit vulgar text and profanity"}',
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        is_safe, warning, details = await inspect_live_image(b"vulgar_bytes", "image/jpeg")
        assert is_safe is False
        assert "Content Warning" in warning
        assert "vulgarity" in warning
        assert details["flagged"] is True


@pytest.mark.asyncio
async def test_inspect_live_image_flagged_violence():
    fake_response = {
        "response": '{"flagged": true, "category": "violence", "reason": "Weapons and violent threats displayed"}',
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        is_safe, warning, details = await inspect_live_image(b"violent_bytes", "image/jpeg")
        assert is_safe is False
        assert "violence" in warning
        assert details["flagged"] is True


@pytest.mark.asyncio
async def test_inspect_live_image_flagged_sexual():
    fake_response = {
        "response": "",
        "thinking": '{"flagged": true, "category": "sexual", "reason": "Explicit sexual/adult content"}',
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_post.return_value = mock_resp

        is_safe, warning, details = await inspect_live_image(b"sexual_bytes", "image/jpeg")
        assert is_safe is False
        assert "sexual" in warning
        assert details["flagged"] is True


@pytest.mark.asyncio
async def test_inspect_live_image_disabled():
    with patch.object(settings, "IMAGE_MODERATION_ENABLED", False):
        is_safe, warning, details = await inspect_live_image(b"anything", "image/jpeg")
        assert is_safe is True
        assert warning is None
        assert details.get("skipped") is True


@pytest.mark.asyncio
async def test_live_capture_endpoint_blocks_flagged_image(accepted_pair):
    buyer, seller, conn_id, _listing = accepted_pair

    fake_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00"
    files = {"image": ("flagged.jpg", fake_image_bytes, "image/jpeg")}
    data = {"caption": "Inappropriate test"}

    with patch(
        "services.connection_service.inspect_live_image",
        return_value=(
            False,
            "⚠️ Content Warning: Image flagged by AI safety moderation for vulgarity. Upload rejected. Reason: Explicit profanity",
            {"flagged": True},
        ),
    ):
        res = await buyer.post(
            f"/connections/{conn_id}/messages/live-capture",
            files=files,
            data=data,
        )
        assert res.status_code == 400
        assert "Content Warning" in res.json()["detail"]
        assert "vulgarity" in res.json()["detail"]

    # Verify no message was stored in database
    messages = (await seller.get(f"/connections/{conn_id}/messages")).json()
    assert len(messages) == 0

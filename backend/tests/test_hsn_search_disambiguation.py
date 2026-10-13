"""GET /hsn/search must let a user tell look-alike rows apart (real HSN master dataset)."""

import pytest

from services.hsn_service import enrich_search_results



async def _search(client, q, limit=50):
    r = await client.get("/hsn/search", params={"q": q, "limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


async def test_apple_levels_collapse_and_rows_are_labelled(client):
    rows = await _search(client, "apple")
    by_code = {r["code"]: r for r in rows}

    # 080810 (6-digit) is the same thing as 08081000: one row, most specific code, old code kept.
    assert "080810" not in by_code and "081330" not in by_code
    fresh, dried = by_code["08081000"], by_code["08133000"]
    assert fresh["also_codes"] == ["080810"] and dried["also_codes"] == ["081330"]
    assert fresh["context"] == "fresh" and fresh["display_title"] == "Apples — fresh"
    assert dried["context"] == "dried" and dried["display_title"] == "Apples — dried"
    assert fresh["parent_description"].lower().startswith("apples, pears and quinces")
    # Every row that used to read just "Apples" is now distinguishable.
    apple_titles = [r["display_title"] for r in rows if r["formatted_title"].lower() in ("apple", "apples")]
    assert len(apple_titles) == len(set(apple_titles)) >= 3

    # Fresh apples recommended and first; the heading row sorts after its specific child.
    assert rows[0]["code"] == "08081000" and rows[0]["recommended"] is True
    assert [r["code"] for r in rows if r["recommended"]] == ["08081000"]
    assert [r["code"] for r in rows].index("0808") > 0


async def test_query_without_collisions_is_unchanged(client):
    rows = await _search(client, "custard apple")
    assert rows, "dataset should know custard apple"
    for r in rows:
        assert r["display_title"] == r["formatted_title"]
        assert not r["context"] and not r["also_codes"]
    # Browsing by code is a plain level listing: nothing collapsed.
    rows = await _search(client, "0808")
    assert {"0808", "080810", "08081000"} <= {r["code"] for r in rows}
    assert all(r["display_title"] == r["formatted_title"] for r in rows)


@pytest.mark.parametrize("query", ["apple", "banana", "milk", "rice", "cotton", "wheat", "sugar", "tea", "steel"])
async def test_display_titles_are_distinct(client, query):
    rows = await _search(client, query)
    titles = [r["display_title"] for r in rows]
    assert all(titles)
    assert len(titles) == len(set(titles)), [t for t in titles if titles.count(t) > 1]
    # No code is both a row and another row's also_code.
    codes = {r["code"] for r in rows}
    for r in rows:
        assert not (set(r["also_codes"] or []) & codes)


async def test_recommendation_follows_hsn_suggest(client):
    rows = await _search(client, "milk")
    assert rows[0]["code"] == "0401" and rows[0]["recommended"] is True
    rows = await _search(client, "rice")
    assert rows[0]["code"] == "1006" and rows[0]["recommended"] is True


def _row(code, title, level):
    return {"code": code, "formatted_title": title, "level": level}


def test_enrich_keeps_children_with_different_descriptions_separate():
    rows = [_row("100610", "Seed rice", 6), _row("10061010", "Seed rice basmati", 8), _row("10061020", "Seed rice other", 8)]
    out = enrich_search_results(rows, {})
    assert {r["code"] for r in out} == {"100610", "10061010", "10061020"}
    assert all(not r["also_codes"] for r in out)


def test_enrich_collapses_chain_to_most_specific_code():
    rows = [_row("0808", "Apples", 4), _row("080810", "APPLES.", 6), _row("08081000", "Apples", 8)]
    out = enrich_search_results(rows, {})
    assert [r["code"] for r in out] == ["08081000"]
    assert out[0]["also_codes"] == ["0808", "080810"]


def test_enrich_context_falls_back_to_truncated_ancestor():
    long = "Preparations of things, not elsewhere specified or included, whether or not put up in packings"
    rows = [_row("11111111", "Widgets", 8), _row("22222222", "Widgets", 8)]
    out = enrich_search_results(rows, {"1111": "Fresh widgets", "2222": long}, "widgets")
    ctx = {r["code"]: r["context"] for r in out}
    assert ctx["11111111"] == "Fresh widgets"  # no usable qualifier on both sides: truncated description
    assert len(ctx["22222222"]) <= 60 and ctx["22222222"].endswith("…")

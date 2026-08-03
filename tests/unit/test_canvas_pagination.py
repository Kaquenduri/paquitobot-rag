"""Unit tests for the pagination follower (PR 4 task 4.3).

Coverage:

- ``per_page=100`` is added to the first-page query string.
- A two-page fixture consumes every item exactly once across the
  full iteration.
- The third page (no ``next`` link) terminates the loop.
- A self-referential ``next`` link is detected and terminates the
  loop without an infinite hang.
- 4xx responses raise ``CanvasRequestError`` so the pipeline can
  classify the failure.
- Non-JSON responses raise ``httpx.HTTPError``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.canvas.client import CanvasClient
from app.canvas.pagination import (
    DEFAULT_PER_PAGE,
    _dedup_key,
    _parse_link_header,
    paginate,
)


# ---------------------------------------------------------------------------
# Link header parsing helpers
# ---------------------------------------------------------------------------


def test_parse_link_header_extracts_next() -> None:
    header = (
        '<https://canvas.test/api/v1/courses?page=2>; rel="next", '
        '<https://canvas.test/api/v1/courses?page=10>; rel="last"'
    )
    assert _parse_link_header(header) == {
        "next": "https://canvas.test/api/v1/courses?page=2",
        "last": "https://canvas.test/api/v1/courses?page=10",
    }


def test_parse_link_header_empty_returns_empty_dict() -> None:
    assert _parse_link_header(None) == {}
    assert _parse_link_header("") == {}
    assert _parse_link_header("malformed") == {}


def test_dedup_key_strips_page_argument() -> None:
    """Stable across page=1 and page=2 cursor echoes."""
    assert _dedup_key("https://canvas.test/x?page=1") == _dedup_key(
        "https://canvas.test/x?page=2"
    )


def test_dedup_key_keeps_other_query_args() -> None:
    assert (
        _dedup_key("https://canvas.test/x?per_page=100&page=4")
        == "https://canvas.test/x?per_page=100"
    )


# ---------------------------------------------------------------------------
# Two-page pagination
# ---------------------------------------------------------------------------


def _paged_transport(
    pages: list[dict[str, Any]],
    *,
    next_links: list[str | None] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Build a mock transport that emits the supplied pages.

    ``next_links`` accepts either a raw URL (which we wrap in the
    RFC 5988 link header format) or a pre-formatted ``Link`` header
    value. The test fixture in production-grade form would write
    ``<https://canvas.test/api/v1/courses?page=2>; rel="next"``;
    passing the bare URL is a convenience for tests.
    """
    requests: list[httpx.Request] = []
    if next_links is None:
        next_links = [None] * len(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page_index = len(requests) - 1
        if page_index >= len(pages):
            return httpx.Response(200, json=[])
        link = next_links[page_index] if page_index < len(next_links) else None
        if link is None:
            headers: dict[str, str] = {}
        elif link.startswith("<") or "rel=" in link:
            # Already wrapped; pass through.
            headers = {"link": link}
        else:
            # Bare URL — wrap in the RFC 5988 link header format.
            headers = {"link": f'<{link}>; rel="next"'}
        return httpx.Response(200, json=pages[page_index], headers=headers)

    return httpx.MockTransport(handler), requests


def test_paginate_emits_per_page_100_on_first_call() -> None:
    page1 = [{"id": 1}, {"id": 2}]
    transport, requests = _paged_transport([page1])

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=transport,
    )

    async def _run() -> list[dict[str, Any]]:
        return [item async for item in paginate(client, "/courses")]

    items = asyncio.run(_run())
    assert items == page1
    assert "per_page=100" in str(requests[0].url)


def test_paginate_default_per_page_constant() -> None:
    assert DEFAULT_PER_PAGE == 100


def test_paginate_consumes_two_pages_exactly_once() -> None:
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}, {"id": 4}]
    next_link = "https://canvas.test/api/v1/courses?page=2"
    transport, requests = _paged_transport(
        [page1, page2],
        next_links=[next_link, None],
    )

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=transport,
    )

    async def _run() -> list[dict[str, Any]]:
        return [item async for item in paginate(client, "/courses")]

    items = asyncio.run(_run())
    assert items == page1 + page2
    assert len(items) == 4
    # Each page hit the transport exactly once.
    assert len(requests) == 2


def test_paginate_respects_per_page_argument() -> None:
    page1 = [{"id": 1}]
    transport, requests = _paged_transport([page1])

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=transport,
    )

    async def _run() -> None:
        async for _ in paginate(client, "/courses", per_page=50):
            pass

    asyncio.run(_run())
    assert "per_page=50" in str(requests[0].url)


def test_paginate_breaker_on_self_referential_next_link() -> None:
    """If Canvas echoes the same URL on the next page, we stop."""
    page1 = [{"id": 1}]
    # The next link matches the first request URL (after the dedup
    # key strips the ``page`` arg). The follower refuses to fetch the
    # same logical page twice.
    next_link = "https://canvas.test/api/v1/courses?per_page=100&page=1"
    transport, requests = _paged_transport([page1], next_links=[next_link])

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=transport,
    )

    async def _run() -> int:
        count = 0
        async for _ in paginate(client, "/courses"):
            count += 1
        return count

    # The follower must not loop forever. Exactly one page is
    # fetched; the dedup guard stops the loop on the second pass.
    assert asyncio.run(_run()) == 1
    assert len(requests) == 1


def test_paginate_raises_on_4xx() -> None:
    from app.canvas.client import CanvasRequestError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"message": "invalid"}]})

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=httpx.MockTransport(handler),
    )

    async def _run() -> None:
        async for _ in paginate(client, "/courses"):
            pass

    with pytest.raises(CanvasRequestError):
        asyncio.run(_run())


def test_paginate_raises_on_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
        transport=httpx.MockTransport(handler),
    )

    async def _run() -> None:
        async for _ in paginate(client, "/courses"):
            pass

    with pytest.raises(httpx.HTTPError):
        asyncio.run(_run())


def test_paginate_rejects_non_positive_per_page() -> None:
    client = CanvasClient(
        base_url="https://canvas.test/api/v1",
        token_provider=lambda: "x",
    )

    async def _run() -> None:
        async for _ in paginate(client, "/courses", per_page=0):
            pass

    with pytest.raises(ValueError):
        asyncio.run(_run())

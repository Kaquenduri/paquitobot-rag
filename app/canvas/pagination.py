"""Canvas pagination follower (PR 4 task 4.3).

Canvas follows RFC 5988 / RFC 5988bis for ``Link`` headers::

    Link: <https://canvas.example.com/api/v1/courses?page=2>; rel="next",
          <https://canvas.example.com/api/v1/courses?page=10>; rel="last"

The follower yields each item exactly once per iteration regardless of
the upstream page shape::

    async for course in paginate(client, "/courses", params={"per_page": 100}):
        ...

The cursor loops until Canvas either:

- omits a ``rel="next"`` link (final page), or
- emits a ``next`` link that points to a URL we have already visited.

The circuit-breaker on visited URLs is the safety net for the rare
case where Canvas returns a self-referential ``next`` link — staying
in the loop would otherwise exhaust the retry budget.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.canvas.client import CanvasClient
from app.core.logging import get_logger

logger = get_logger("app.canvas.pagination")


# Default Canvas page size. The design locks this at 100 so the
# boundary is observable in tests.
DEFAULT_PER_PAGE = 100


class CanvasPaginationError(ValueError):
    """Raised when a successful paginated response is not a JSON list."""


def _strip_query(url: str) -> tuple[str, dict[str, list[str]]]:
    """Return ``(path?query, parsed_query)`` for an absolute URL."""
    parsed = urlparse(url)
    # We keep the full URL for the next request; the dedup key is
    # the URL minus the ``page`` query arg so a stable cursor works
    # across runs.
    return url, parse_qs(parsed.query)


def _dedup_key(url: str) -> str:
    """Return the legacy logical-resource key used by callers/tests.

    This helper strips the cursor page number.  The paginator itself uses
    :func:`_visited_key` so later page numbers remain significant and a
    multi-page response cannot be truncated.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("page", None)
    pairs = [
        (key, value)
        for key in sorted(params)
        for value in params[key]
    ]
    query = urlencode(pairs, doseq=True)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?{query}" if query else base


def _visited_key(url: str) -> str:
    """Return a cursor-safe key: normalize only the first page."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if params.get("page") == ["1"]:
        params.pop("page", None)
    pairs = [
        (key, value)
        for key in sorted(params)
        for value in params[key]
    ]
    query = urlencode(pairs, doseq=True)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return f"{base}?{query}" if query else base


def _resolve_for_base(client: CanvasClient, url: str) -> str:
    """Resolve ``url`` against the client's base URL when relative.

    The :class:`CanvasClient` exposes its ``_base_url`` for tests
    that need to compute the dedup key against the URL that will
    actually be requested. Production callers should rely on the
    client itself to resolve.
    """
    if url.startswith(("http://", "https://")):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return f"{client._base_url}{url}"


def _candidate_request_url(
    client: CanvasClient,
    next_url: str,
    original_path: str,
    merged: dict[str, Any],
) -> str:
    """Compute the URL the client will actually request.

    For the first page (``next_url is original_path``), the client
    will merge ``merged`` (the ``per_page`` and any caller-supplied
    params) onto the resolved URL. For subsequent pages, the
    ``next_url`` carries its own query string and no params are
    merged.
    """
    base = _resolve_for_base(client, next_url)
    if next_url is original_path and merged:
        # Reconstruct the query string from the merged params.
        from urllib.parse import urlencode

        return f"{base}?{urlencode(merged, doseq=True)}"
    return base


def _parse_link_header(header: str | None) -> dict[str, str]:
    """Parse a ``Link`` header into ``{rel: url}``.

    Defensive: returns ``{}`` when the header is missing or malformed
    so the caller can treat the absence of ``next`` as "no more pages".
    """
    if not header:
        return {}
    rels: dict[str, str] = {}
    for part in header.split(","):
        section = part.strip()
        if not section:
            continue
        if ";" not in section:
            continue
        url_part, *params = section.split(";")
        url = url_part.strip().strip("<>").strip()
        if not url:
            continue
        for param in params:
            key, _, value = param.strip().partition("=")
            if key == "rel" and value:
                rels[value.strip().strip('"')] = url
    return rels


def _extract_next_url(response: httpx.Response) -> str | None:
    """Return the ``next`` URL from a Canvas response, or ``None``."""
    # httpx exposes a convenience property when the ``link`` parsing
    # is available; fall back to the raw header for older versions.
    links = getattr(response, "links", None)
    if isinstance(links, dict) and "next" in links:
        return str(links["next"].get("url") or links["next"])
    raw = response.headers.get("link") or response.headers.get("Link")
    parsed = _parse_link_header(raw)
    return parsed.get("next")


async def paginate(
    client: CanvasClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every item across all pages until Canvas stops emitting ``next``.

    Parameters
    ----------
    client:
        A :class:`CanvasClient` to issue GET requests with.
    path:
        Canvas endpoint path (e.g. ``/users/self/favorites/courses``).
    params:
        Additional query parameters. ``per_page`` is set/overridden
        from the ``per_page`` argument so the caller's value wins.
    per_page:
        Page size. Defaults to :data:`DEFAULT_PER_PAGE` (100).

    Yields
    ------
    dict
        A single Canvas item. The caller is responsible for parsing
        into a DTO.
    """

    if per_page <= 0:
        raise ValueError("per_page must be positive")

    merged: dict[str, Any] = {"per_page": per_page}
    if params:
        for key, value in params.items():
            merged.setdefault(key, value)

    next_url: str | None = path
    visited: set[str] = set()
    iterations = 0
    last_requested_url: str | None = None

    while next_url is not None:
        iterations += 1
        if iterations > 1000:  # pragma: no cover - safety net
            raise RuntimeError(
                "paginate() exceeded 1000 iterations; aborting to avoid infinite loop"
            )

        # The dedup key is computed AFTER the request so the first
        # request URL (with ``per_page=100``) is distinct from the
        # next link's dedup key (typically without ``per_page``).
        # The pre-flight check uses the URL we would request (path
        # + params) so a self-referential next link is caught
        # BEFORE the network round-trip.
        candidate_url = _candidate_request_url(
            client, next_url, path, merged
        )
        candidate_key = _visited_key(candidate_url)
        if candidate_key in visited:
            return

        # ``_request`` always uses GET; the trailing ``?page`` is
        # already on the URL when we are following a ``next`` link.
        response = await client.get(
            next_url, params=merged if next_url is path else None
        )
        last_requested_url = candidate_url
        # Mark the URL we ACTUALLY fetched as visited (after the
        # request) so the dedup guard never falsely stops on the
        # very first page.
        visited.add(candidate_key)

        # 4xx is folded into a typed exception via the client; we
        # only need to handle success here.
        if response.status_code >= 400:
            from app.canvas.client import CanvasRequestError

            raise CanvasRequestError(
                method="GET",
                path=next_url,
                status_code=response.status_code,
                message=f"Canvas page failed: {response.status_code}",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise httpx.HTTPError(
                f"Canvas returned non-JSON body on {next_url}"
            ) from exc

        if not isinstance(payload, list):
            raise CanvasPaginationError(
                f"Canvas page at {next_url} must be a JSON list"
            )
        for item in payload:
            yield item

        # ---- Follow the next cursor ----
        next_link = _extract_next_url(response)
        if next_link is None:
            return

        # The next URL is absolute; pass through so the client can
        # skip the base_url prefix. The query string is preserved.
        next_url = next_link

        # Strip the page arg from the merged params we hit on the
        # first call so the next request carries the cursor's own
        # query string unchanged.
        merged.pop("page", None)

        # Defensive: if Canvas echoes the exact same URL we just
        # fetched, stop without making another request.
        if next_url == last_requested_url:
            return


__all__ = [
    "DEFAULT_PER_PAGE",
    "CanvasPaginationError",
    "paginate",
]


def _selftest() -> None:
    """Run a two-page cursor assertion with an in-memory HTTP transport."""
    import asyncio

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "link": '<https://canvas.test/api/v1/courses?page=2>; rel="next"'
                },
            )
        return httpx.Response(200, json=[{"id": 2}])

    async def run() -> list[dict[str, Any]]:
        client = CanvasClient(
            base_url="https://canvas.test/api/v1",
            token_provider=lambda: "selftest-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            return [item async for item in paginate(client, "/courses")]
        finally:
            await client.aclose()

    assert asyncio.run(run()) == [{"id": 1}, {"id": 2}]
    assert len(requests) == 2


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()

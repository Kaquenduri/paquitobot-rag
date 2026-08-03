"""Ingestion helpers for PR 6 — HTML sanitization + document chunking.

The design (§8, spec ``ingestion``) is explicit: assignment
``description`` and own-submission ``body`` MUST be sanitized BEFORE
they reach chunking or embedding. Unsafe markup, executable content,
and credential fragments MUST NOT enter retrieval documents.

The module exposes three building blocks:

- :func:`sanitize_html`: bleach-backed sanitizer with two presets —
  ``strip_all`` (no tags at all) and ``readable`` (the minimal
  readable subset ``p``, ``br``, ``strong``, ``em``, ``ul``, ``ol``,
  ``li``, ``a`` with ``href``).
- :func:`strip_all` / :func:`readable`: thin presets around
  :func:`sanitize_html`.
- :func:`prepare_assignment_chunks`: assemble a list of
  :class:`langchain_core.documents.Document` for a Canvas
  ``Assignment`` row, sanitizing ``description`` AND the related
  own-submission ``body`` (when present) and skipping empty content.

The module is dependency-light (just ``bleach`` and ``langchain_core``),
which keeps ``_selftest`` self-contained and fast.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import bleach
from langchain_core.documents import Document

# ---------------------------------------------------------------------------
# Presets (public; re-exported for callers + selftests)
# ---------------------------------------------------------------------------

# ``strip_all``: removes every tag and every attribute.
STRIP_ALL_TAGS: list[str] = []
STRIP_ALL_ATTRS: dict[str, list[str]] = {}

# ``readable``: minimal readable subset used for sanitizing Canvas HTML
# before chunking/embedding. The ``href`` attribute is the only one
# allowed on ``<a>`` tags so links survive; ``on*``, ``style``, and
# ``class`` are deliberately excluded so embedded scripts or CSS
# shenanigans cannot reach retrieval.
READABLE_TAGS: list[str] = ["p", "br", "strong", "em", "ul", "ol", "li", "a"]
READABLE_ATTRS: dict[str, list[str]] = {"a": ["href"]}


# ---------------------------------------------------------------------------
# Core sanitizer
# ---------------------------------------------------------------------------


def sanitize_html(
    text: str | None,
    *,
    allowed_tags: Iterable[str] | None = None,
    allowed_attrs: dict[str, Iterable[str]] | None = None,
    strip: bool = False,
) -> str:
    """Return ``text`` with every tag/attribute outside the allow-list removed.

    Behavior:

    - ``allowed_tags=None`` is treated as "no tags allowed". Bleach
      would otherwise default to its built-in tag list, which we never
      want for content that is going to be embedded.
    - ``allowed_attrs=None`` removes every attribute — the conservative
      default. Pass an explicit dict to whitelist per-tag attributes.
    - ``strip=True`` strips disallowed tags instead of escaping them,
      so the output never contains angle-bracket markup. The
      ``readable`` preset keeps ``strip=False`` because some callers
      may want the surviving tags to be real HTML for downstream
      rendering.
    - Comments are always stripped; bleach's default keeps them.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        # Coerce defensively so Pydantic or DB strings do not blow up
        # the embedder with non-string content. ``bleach.clean`` is
        # happy with arbitrary objects via ``__str__``, but we want
        # deterministic behaviour.
        text = str(text)

    tags = list(allowed_tags) if allowed_tags is not None else []
    attrs: dict[str, list[str]] = {}
    if allowed_attrs is not None:
        attrs = {
            str(tag): list(per_tag) for tag, per_tag in allowed_attrs.items()
        }

    return bleach.clean(
        text,
        tags=tags,
        attributes=attrs,
        strip=strip,
        strip_comments=True,
    )


# ---------------------------------------------------------------------------
# Preset helpers (thin wrappers around ``sanitize_html``)
# ---------------------------------------------------------------------------


def strip_all(text: str | None) -> str:
    """Strip every HTML tag; keep only plain text."""
    return sanitize_html(
        text,
        allowed_tags=STRIP_ALL_TAGS,
        allowed_attrs=STRIP_ALL_ATTRS,
        strip=True,
    )


def readable(text: str | None) -> str:
    """Keep only the minimal readable subset (``p``, ``br``, ``strong``,
    ``em``, ``ul``, ``ol``, ``li``, ``a`` with ``href``)."""
    return sanitize_html(
        text,
        allowed_tags=READABLE_TAGS,
        allowed_attrs=READABLE_ATTRS,
        strip=False,
    )


# ---------------------------------------------------------------------------
# Document assembly for retrieval
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str:
    """Coerce ``value`` to a stripped string; empty on falsy input."""
    if value is None:
        return ""
    return str(value).strip()


def _resolve_submission_body(assignment: Any) -> str | None:
    """Pull ``body`` off the assignment's related submission if any.

    The PR 6 spec requires that an assignment chunker sanitize BOTH
    ``assignment.description`` and the related ``submission.body``.
    The two values can be passed in three shapes:

    - ``assignment.submission`` — an ORM-like object with ``.body``.
    - ``assignment.submission_body`` — already-extracted string.
    - ``assignment.body`` — for callers that already merged the two.
    """
    submission = getattr(assignment, "submission", None)
    if submission is not None:
        body = getattr(submission, "body", None)
        if body is not None:
            return body
    direct = getattr(assignment, "submission_body", None)
    if direct is not None:
        return direct
    fallback = getattr(assignment, "body", None)
    return fallback


def prepare_assignment_chunks(assignment: Any) -> list[Document]:
    """Build retrieval ``Document``s for an assignment + own submission.

    Returns one document per non-empty source after sanitization:

    - The sanitized ``description`` becomes a single Document.
    - The sanitized submission ``body`` becomes another Document when
      present and non-empty.

    Empty content is omitted (spec scenario "Omit empty content") so
    downstream chunkers do not embed placeholder strings. Every
    document is tagged with the originating ``tenant_id`` (if
    available) and ``canvas_id`` so vector filters remain tenant-safe.
    """
    if assignment is None:
        return []

    tenant_id = getattr(assignment, "tenant_id", None)
    canvas_id = getattr(assignment, "canvas_id", None)
    assignment_id = getattr(assignment, "id", None)

    documents: list[Document] = []

    description = sanitize_html(
        getattr(assignment, "description", None),
        allowed_tags=READABLE_TAGS,
        allowed_attrs=READABLE_ATTRS,
    )
    if description.strip():
        documents.append(
            Document(
                page_content=description,
                metadata={
                    "source": "assignment_description",
                    "tenant_id": _safe_str(tenant_id),
                    "canvas_id": canvas_id,
                    "assignment_id": _safe_str(assignment_id),
                },
            )
        )

    body = sanitize_html(
        _resolve_submission_body(assignment),
        allowed_tags=READABLE_TAGS,
        allowed_attrs=READABLE_ATTRS,
    )
    if body.strip():
        documents.append(
            Document(
                page_content=body,
                metadata={
                    "source": "submission_body",
                    "tenant_id": _safe_str(tenant_id),
                    "canvas_id": canvas_id,
                    "assignment_id": _safe_str(assignment_id),
                },
            )
        )

    return documents


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "READABLE_ATTRS",
    "READABLE_TAGS",
    "STRIP_ALL_ATTRS",
    "STRIP_ALL_TAGS",
    "prepare_assignment_chunks",
    "readable",
    "sanitize_html",
    "strip_all",
]


# ---------------------------------------------------------------------------
# Self-test (executable via ``python -m app.services.ingest_service``)
# ---------------------------------------------------------------------------


def _selftest() -> None:
    """Run self-contained sanitizer and chunker assertions."""
    # 1. ``<script>`` and on* attributes are removed; ``javascript:``
    #    URIs are stripped because bleach sanitizes URL schemes.
    malicious = (
        '<p>Hello <script>alert("xss")</script> world</p>'
        '<a href="javascript:alert(1)" onclick="alert(1)">click</a>'
    )
    cleaned = readable(malicious)
    assert "<script" not in cleaned
    assert "onclick" not in cleaned.lower()
    # ``javascript:`` URIs are stripped by bleach so the payload never
    # travels to the embedded text.
    assert "javascript:" not in cleaned.lower()
    # Plain text content survives.
    assert "Hello" in cleaned and "world" in cleaned and "click" in cleaned

    # 2. ``strip_all`` removes every tag.
    no_tags = strip_all("<p>Hello <b>world</b></p>")
    assert "<" not in no_tags and ">" not in no_tags
    assert "Hello" in no_tags and "world" in no_tags

    # 3. Empty input is empty; None is empty.
    assert sanitize_html(None) == ""
    assert sanitize_html("") == ""
    assert strip_all("") == ""

    # 4. ``prepare_assignment_chunks`` returns one Document per
    #    non-empty source, with tenant metadata.
    class _FakeSubmission:
        body = "  <p>My submission <script>x</script></p>  "

    class _FakeAssignment:
        id = "assignment-1"
        canvas_id = 42
        tenant_id = "tenant-1"
        description = "<p>Assignment <strong>details</strong></p>"
        submission = _FakeSubmission()

    docs = prepare_assignment_chunks(_FakeAssignment())
    assert len(docs) == 2
    sources = {doc.metadata["source"] for doc in docs}
    assert sources == {"assignment_description", "submission_body"}
    for doc in docs:
        assert doc.metadata["tenant_id"] == "tenant-1"
        assert doc.metadata["canvas_id"] == 42
        assert "<script" not in doc.page_content

    # 5. Empty content is omitted (spec scenario "Omit empty content").
    class _EmptyAssignment:
        id = "empty-1"
        canvas_id = 7
        tenant_id = "tenant-2"
        description = ""  # already empty
        submission = None

    assert prepare_assignment_chunks(_EmptyAssignment()) == []

    class _WhitespaceAssignment:
        id = "ws-1"
        canvas_id = 8
        tenant_id = "tenant-3"
        description = "   \n  "  # whitespace only
        submission = None

    assert prepare_assignment_chunks(_WhitespaceAssignment()) == []

    # 6. ``None`` assignment → empty list (no crash).
    assert prepare_assignment_chunks(None) == []


if __name__ == "__main__":  # pragma: no cover - manual executable assertion
    _selftest()
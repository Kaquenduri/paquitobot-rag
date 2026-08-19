"""PGVector adapter with tenant-scoped retrieval and hash upserts."""

from __future__ import annotations

import hashlib
import time
from typing import Any

_HEALTH_TTL_SECONDS = 30  # re-probe Ollama at most once per 30 s


class VectorStore:
    def __init__(self, store: Any = None, *, embedder: Any = None) -> None:
        self._store = store
        self._embedder = embedder
        self._last_health = False
        self._last_probe_ts: float = 0.0  # epoch seconds of the last probe
        self._hashes: dict[tuple[str, str], Any] = {}

    def provider_health(self) -> bool:
        now = time.monotonic()
        # Skip the network round-trip if we probed recently.
        if now - self._last_probe_ts < _HEALTH_TTL_SECONDS:
            return self._last_health
        self._last_probe_ts = now
        try:
            if self._embedder is None:
                self._last_health = False
            else:
                probe = getattr(self._embedder, "embed_query", None)
                # Catch *all* exceptions: httpx.ReadTimeout, ConnectionError,
                # RuntimeError, etc. so a missing Ollama never crashes the
                # request — it just marks the provider as unavailable.
                self._last_health = bool(callable(probe) and probe("health probe"))
        except Exception:  # noqa: BLE001
            self._last_health = False
        return self._last_health

    def similarity_search(self, query: str, *, tenant_id, k: int = 8, **kwargs):
        if not self._last_health and self._embedder is not None:
            self.provider_health()
        if not self._last_health:
            return []
        filters = dict(kwargs.pop("filter", {}) or {})
        filters["tenant_id"] = str(tenant_id)
        if self._store is None:
            return []
        # ``store.similarity_search`` may return either ``Document`` objects
        # or ``(Document, score)`` tuples depending on the backend.  Always
        # normalise to ``Document`` so callers can rely on ``page_content``.
        raw = self._store.similarity_search(query, k=k, filter=filters, **kwargs)
        normalised = []
        for item in raw:
            if isinstance(item, tuple) and len(item) >= 1:
                normalised.append(item[0])
            else:
                normalised.append(item)
        return normalised

    def upsert(self, documents, *, tenant_id):
        if self._store is None:
            return []
        changed = []
        for document in documents:
            content = getattr(document, "page_content", str(document))
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            key = (str(tenant_id), digest)
            if key not in self._hashes:
                metadata = dict(getattr(document, "metadata", {}) or {})
                metadata["tenant_id"] = str(tenant_id)
                metadata["content_hash"] = digest
                try:
                    document.metadata = metadata
                except (AttributeError, TypeError):
                    pass
                changed.append(document)
                self._hashes[key] = document
        if changed and hasattr(self._store, "add_documents"):
            self._store.add_documents(changed)
        return changed


PGVectorStore = VectorStore


def _selftest() -> None:
    class Stub:
        def embed_query(self, value): return [1.0]
        def similarity_search(self, query, **kwargs):
            from types import SimpleNamespace

            return [
                SimpleNamespace(page_content="hola", metadata=kwargs.get("filter", {})),
            ]
    store = VectorStore(Stub(), embedder=Stub())
    docs = store.similarity_search("q", tenant_id="t")
    assert len(docs) == 1
    assert docs[0].page_content == "hola"
    assert docs[0].metadata["tenant_id"] == "t"


if __name__ == "__main__":
    _selftest()

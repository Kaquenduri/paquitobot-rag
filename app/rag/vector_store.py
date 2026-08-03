"""PGVector adapter with tenant-scoped retrieval and hash upserts."""

from __future__ import annotations

import hashlib
from typing import Any


class VectorStore:
    def __init__(self, store: Any = None, *, embedder: Any = None) -> None:
        self._store = store
        self._embedder = embedder
        self._last_health = False
        self._hashes: dict[tuple[str, str], Any] = {}

    def provider_health(self) -> bool:
        try:
            if self._embedder is None:
                self._last_health = False
            else:
                probe = getattr(self._embedder, "embed_query", None)
                self._last_health = bool(callable(probe) and probe("health probe"))
        except (RuntimeError, ValueError, OSError):
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
        return self._store.similarity_search(query, k=k, filter=filters, **kwargs)

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
        def similarity_search(self, query, **kwargs): return kwargs
    store = VectorStore(Stub(), embedder=Stub())
    assert store.similarity_search("q", tenant_id="t")["filter"]["tenant_id"] == "t"


if __name__ == "__main__":
    _selftest()

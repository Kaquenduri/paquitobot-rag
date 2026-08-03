"""Hybrid vector recall followed by ID-restricted relational retrieval."""

from __future__ import annotations

from typing import Any, ClassVar


def retrieve_hybrid(question, *, tenant_id, vector_store, sql_callback, k=20):
    documents = vector_store.similarity_search(question, tenant_id=tenant_id, k=k)
    ids = []
    for document in documents:
        metadata = getattr(document, "metadata", {}) or {}
        if metadata.get("id") is not None:
            ids.append(metadata["id"])
    return sql_callback(ids=ids, tenant_id=tenant_id)


def _selftest() -> None:
    class D:
        metadata: ClassVar[dict[str, Any]] = {"id": 1}

    class V:
        def similarity_search(self, *args, **kwargs):
            return [D()]

    assert retrieve_hybrid("q", tenant_id="t", vector_store=V(), sql_callback=lambda **x: x)["ids"] == [1]


if __name__ == "__main__":
    _selftest()

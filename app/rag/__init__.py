"""RAG package."""
from .router import RAGRouter, RouteDecision
from .vector_store import VectorStore

__all__ = ["RAGRouter", "RouteDecision", "VectorStore"]
def _selftest():
    assert RAGRouter().route("count assignments").route == "relational"
if __name__ == "__main__": _selftest()

"""Deterministic-first RAG question router."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

ROUTES = frozenset({"relational", "semantic", "hybrid", "unsupported"})


@dataclass(frozen=True)
class RouteDecision:
    route: str
    language: str
    deterministic: bool


class RAGRouter:
    def __init__(self, classifier: Callable[[str], str] | None = None, *, embedding_available: bool = True):
        self.classifier = classifier
        self.embedding_available = embedding_available

    def deterministic_rule(self, question: str) -> str | None:
        q = question.lower()
        if re.search(r"\b(score|grade|count|how many|average|mean|maximum|minimum|date|due|status|submitted|late|missing|promedio|cuánt|cuándo|calificación|nota|estado|entreg)\b", q):
            if re.search(r"\b(explain|summarize|describe|explica|resum|describe)\b", q):
                return "hybrid"
            return "relational"
        if re.search(r"\b(explain|summarize|describe|meaning of|qué es|explica|resum)\b", q):
            return "semantic"
        if re.search(r"\b(course|assignment)\s*#?\d+\b", q):
            return "relational"
        return None

    def route(self, question: str, *, language: str = "en") -> RouteDecision:
        route = self.deterministic_rule(question)
        deterministic = route is not None
        if route is None:
            try:
                route = self.classifier(question) if self.classifier else "relational"
            except (RuntimeError, ValueError, TypeError):
                route = "relational"
            if route not in ROUTES:
                route = "relational"
        if route in {"semantic", "hybrid"} and not self.embedding_available:
            route = "unsupported" if route == "semantic" else "relational"
        return RouteDecision(route, language, deterministic)

    dispatch = route


def _selftest() -> None:
    calls = []
    router = RAGRouter(lambda q: calls.append(q) or "semantic")
    assert router.route("How many assignments?").route == "relational"
    assert not calls
    assert router.route("Tell me something unclear").route == "semantic"
    assert len(calls) == 1
    assert RAGRouter(lambda q: "DROP").route("unclear").route == "relational"


if __name__ == "__main__":
    _selftest()

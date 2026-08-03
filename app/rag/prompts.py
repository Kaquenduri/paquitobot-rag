"""Language detection and bounded multilingual prompts."""

from __future__ import annotations

import re


def detect_language(question: str) -> str:
    if re.search(r"\b(el|la|los|las|qué|como|cómo|promedio|cuánt|estado|entrega)\b", question.lower()):
        return "es"
    return "en"


def _prompt(kind: str, lang: str) -> str:
    language = "Spanish" if lang == "es" else "English"
    return f"Answer in {language}. Use only supplied evidence. Do not fabricate. Task: {kind}."


def sql_prompt(lang="en"): return _prompt("relational answer", lang)
def vector_prompt(lang="en"): return _prompt("semantic answer", lang)
def hybrid_prompt(lang="en"): return _prompt("hybrid answer", lang)
def refusal_prompt(lang="en"): return _prompt("bounded refusal", lang)


def bounded_refusal(lang="en"):
    return "No puedo responder con evidencia disponible." if lang == "es" else "I cannot answer that from the available evidence."


def _selftest() -> None:
    assert detect_language("¿Cuál es el promedio?") == "es"
    assert detect_language("Explain the assignment") == "en"
    assert "Spanish" in refusal_prompt("es")


if __name__ == "__main__":
    _selftest()

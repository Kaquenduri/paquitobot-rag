"""Canvas RAG backend application package.

Boot entry point is ``app.main:app`` (invoked by ``uvicorn``). The legacy
script mode is preserved by the top-level ``main.py`` wrapper when
``LEGACY_MODE=1``.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

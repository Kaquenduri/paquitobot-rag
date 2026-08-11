"""Top-level bootstrap.

PR 1 introduces this wrapper. Behaviour is controlled by ``LEGACY_MODE``:

* ``LEGACY_MODE=0`` (default): invoke ``uvicorn app.main:app`` and exit when
  the server stops. This is the new path. uvicorn picks up ``--host`` /
  ``--port`` from the command line (or its built-in defaults).
* ``LEGACY_MODE=1``: fall back to the original script-mode pipeline
  (``src.extract_canvas_data`` → ``src.chroma_db`` / ``src.supabase`` →
  similarity search → MiniMax answer). Preserved verbatim so existing
  workflows keep running until they are retired by a follow-up change.

The wrapper itself NEVER logs or echoes environment values.
"""

from __future__ import annotations

import os
import sys


def _run_uvicorn() -> None:
    """Programmatically launch the FastAPI app via uvicorn."""
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


def _run_legacy_script() -> None:
    """Run the original script-mode pipeline (LEGACY_MODE=1)."""
    from langchain.chat_models import init_chat_model
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_ollama import OllamaEmbeddings

    from src.supabase import save_to_pgvector_db
    from src.text_processor import chunking

    processed_documents = chunking()
    print(f"{len(processed_documents)} chunks")

    embedding_model = OllamaEmbeddings(
        model="qwen3-embedding:8b",
        dimensions=1024,
    )

    db = save_to_pgvector_db(processed_documents, embedding_model)

    query = "¿Cual es la fecha de entrega de cualquier curso?"
    docs = db.similarity_search_with_score(query, k=10)
    print(docs)

    context = "\n\n---\n\n".join([doc.page_content for doc, score in docs])

    PROMPT_TEMPLATE = """
Eres PaquitoBot un asistente de IA que ayuda a responder preguntas basadas en el contexto proporcionado - un asistente de ia que ayuda al usuario de tecsup con sus tareas y dudas de sus cursos.:
{context}
Responde segun la respueta: {question}
Sea una resuesta detallada
NO incluir informacion irrelevante o inventada, si al pregunta no puede ser respondida con informacion que se consigue de la base de datos, simplemente decir que no se puede responder dicha solicitud.
Eso quiere decir que si se pregunta algo como "cual es el color del sol" y no hay informacion en la base de datos sobre el color del sol, simplemente decir que no se puede responder dicha solicitud.
Indicar tu proposito en caso ocurra lo antes mencionado.
"""

    prompt_template = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
    prompt = prompt_template.format(context=context, question=query)
    model = init_chat_model(
        "anthropic:MiniMax-M3",
        api_key=os.environ["MINIMAX_API_KEY"],
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"),
    )
    response = model.invoke(prompt)
    print(response.content)


def main() -> int:
    legacy_mode = os.environ.get("LEGACY_MODE", "0") == "1"
    if legacy_mode:
        _run_legacy_script()
    else:
        _run_uvicorn()
    return 0


if __name__ == "__main__":
    sys.exit(main())

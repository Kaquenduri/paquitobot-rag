import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )
import os

from langchain_core.documents import Document
from langchain_postgres import PGEngine, PGVectorStore

# Cadena de conexión a PostgreSQL de Supabase
DATABASE_URL = os.getenv("SUPABASE_DATABASE_URL")


TABLE_NAME = "documents"


def save_to_pgvector_db(
    chunks: list[Document],
    embedding_model
) -> PGVectorStore:

    # Crear el pool de conexiones
    pg_engine = PGEngine.from_connection_string(
        url=DATABASE_URL
    )

    # # Crear la tabla si no existe
    # pg_engine.init_vectorstore_table(
    #     table_name=TABLE_NAME,
    #     vector_size=1024
    # )

    # Conectar al Vector Store
    vector_store = PGVectorStore.create_sync(
        engine=pg_engine,
        table_name='documents',
        embedding_service=embedding_model
    )
    
    

    # # Insertar documentos en lotes para evitar cuellos de botella
    # batch_size = 100
    # total_batches = (len(chunks) + batch_size - 1) // batch_size
    
    # for i in range(0, len(chunks), batch_size):
    #     batch = chunks[i:i + batch_size]
    #     print(f"Subiendo lote {i // batch_size + 1} de {total_batches} (Documentos {i} a {i + len(batch)})...")
    #     vector_store.add_documents(batch)

    # print(f"Saved {len(chunks)} documents to PGVector in batches of {batch_size}.")

    return vector_store
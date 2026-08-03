from langchain_community.document_loaders import (
    DirectoryLoader,
    PyPDFLoader,
    TextLoader,
)
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Nombre de la carpeta donde se guardan la data (PDFs, Markdowns, etc)
DOCUMENTS_PATH = "src/data"

def chunking () -> list[Document]:
    # permite cargar una carpeta entera de archivos segun su tipo. 
    pdfs = DirectoryLoader(
        DOCUMENTS_PATH, # directorio raiz donde se encuentran los documentos
        glob="**/*.pdf", # permite buscar en subdirectorios y cargar todos los archivos con extension .pdf
        loader_cls=PyPDFLoader # Como leer el archivo PDF, en este caso con PyPDFLoader
    )

    markdowns = DirectoryLoader(
        DOCUMENTS_PATH,
        glob="**/*.md",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"}
    )

    txts = DirectoryLoader(
        DOCUMENTS_PATH,
        glob="**/*.txt",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"}
    )

    jsons = DirectoryLoader(
        DOCUMENTS_PATH,
        glob="**/*.json",
        loader_cls=TextLoader,
        loader_kwargs={"encoding": "utf-8"}
    )
    

    # ya carga todos los documentos en la carpeta y los guarda en una lista de Documentos
    documents = (
        pdfs.load() +
        markdowns.load() +
        txts.load() +
        jsons.load()
    )
    
    #Especificaciones del chunking, tamaño de cada chunk y overlap entre chunks
    #overlap = quiere decir que repite los 500 caracteres del final del chunk anterior en el siguiente chunk, para que no se pierda contexto
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=500,
        length_function=len,
        add_start_index=True
    )
    # procesos de chunking 
    chunks = text_splitter.split_documents(documents)
    return chunks 

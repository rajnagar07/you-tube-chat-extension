"""
YouTube Video RAG Backend
--------------------------
Wraps the transcript -> chunk -> embed -> retrieve -> LLM pipeline behind a
small FastAPI service that the Chrome extension talks to.

Run with:
    uvicorn main:app --reload --port 8000
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableParallel, RunnablePassthrough, RunnableLambda
from langchain_core.output_parsers import StrOutputParser

load_dotenv()

if not os.environ.get("GROQ_API_KEY"):
    raise RuntimeError(
        "GROQ_API_KEY is not set. Copy backend/.env.example to backend/.env "
        "and fill in your own keys (never commit real keys)."
    )

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="YouTube RAG Backend")

# The extension runs from a chrome-extension:// origin, and requests are
# made from the popup, which browsers treat as a null/opaque-ish origin in
# some cases. Allowing all origins is fine for a purely local dev backend;
# tighten this if you deploy the backend publicly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0.3)

prompt = PromptTemplate(
    template="""
    You are a helpful assistant.
    Answer ONLY from the provided transcript context.
    If the context is insufficient, just say you don't know.

    {context}
    Question: {question}
    """,
    input_variables=["context", "question"],
)


def format_docs(retrieved_docs):
    return "\n\n".join(doc.page_content for doc in retrieved_docs)


def extract_video_id(raw: str) -> str:
    """Accept either a bare video id or a full YouTube URL."""
    match = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    raise HTTPException(status_code=400, detail="Could not parse a YouTube video id from input.")


def get_vector_store(video_id: str) -> FAISS:
    """Load a cached FAISS index for this video, or build + cache one."""
    index_path = CACHE_DIR / video_id

    if index_path.exists():
        return FAISS.load_local(
            str(index_path), embeddings, allow_dangerous_deserialization=True
        )

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.fetch(video_id, languages=["en", "hi"])
        transcript = " ".join(chunk.text for chunk in transcript_list)
    except TranscriptsDisabled:
        raise HTTPException(status_code=422, detail="Captions are disabled for this video.")
    except NoTranscriptFound:
        raise HTTPException(status_code=422, detail="No transcript found for this video.")

    if not transcript.strip():
        raise HTTPException(status_code=422, detail="Transcript was empty.")

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = splitter.create_documents([transcript])

    vector_store = FAISS.from_documents(chunks, embeddings)
    vector_store.save_local(str(index_path))
    return vector_store


class AskRequest(BaseModel):
    video: str  # video id or full URL
    question: str


class AskResponse(BaseModel):
    answer: str
    video_id: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    video_id = extract_video_id(req.video)
    vector_store = get_vector_store(video_id)
    retriever = vector_store.as_retriever(search_type="similarity", search_kwargs={"k": 3})

    chain = (
        RunnableParallel(
            {
                "context": retriever | RunnableLambda(format_docs),
                "question": RunnablePassthrough(),
            }
        )
        | prompt
        | llm
        | StrOutputParser()
    )

    answer = chain.invoke(req.question)
    return AskResponse(answer=answer, video_id=video_id)

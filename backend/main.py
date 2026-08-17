"""
YouTube Video RAG Backend
--------------------------
Wraps the transcript -> chunk -> embed -> retrieve -> LLM pipeline behind a
small FastAPI service that the Chrome extension talks to.

Run with:
    uvicorn main:app --reload --port 8000
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

import pymupdf as fitz  # PyMuPDF — `fitz` import name is deprecated
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from groq import RateLimitError
from pydantic import BaseModel

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)
from langchain_core.documents import Document
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


topic_label_prompt = PromptTemplate(
    template="""
    Give a short topic label (3-6 words, no punctuation at the end) for the
    following textbook excerpt. Answer with ONLY the label, nothing else.

    Excerpt:
    {chunk}
    """,
    input_variables=["chunk"],
)
topic_label_chain = topic_label_prompt | llm | StrOutputParser()


compare_prompt = PromptTemplate(
    template="""
    You are a helpful assistant. You have two sources: a YouTube video's
    transcript and the user's uploaded notes/textbook (PDF). Answer the
    question using BOTH sources. If asked to compare, explicitly call out
    what's covered in one but not the other. If the context is insufficient,
    say you don't know.

    Video transcript excerpts:
    {video_context}

    Notes/PDF excerpts:
    {pdf_context}

    Question: {question}
    """,
    input_variables=["video_context", "pdf_context", "question"],
)


def format_docs(retrieved_docs):
    return "\n\n".join(doc.page_content for doc in retrieved_docs)


def invoke_with_retry(runnable, input_data, max_retries: int = 4, base_delay: float = 10.0):
    """
    Groq's free tier has a low tokens-per-minute cap, so bursts of calls
    (like labeling every PDF chunk) can hit a 429. Retry with backoff
    instead of letting the request crash.
    """
    for attempt in range(max_retries):
        try:
            return runnable.invoke(input_data)
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


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

    if (index_path / "index.faiss").exists():
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


def get_timed_transcript_chunks(video_id: str) -> list[Document]:
    """
    Build (or load from a cached JSON sidecar) transcript chunks that keep
    each chunk's start time, so a playback timestamp can be mapped straight
    to "what was being said around here" without re-scraping captions live.
    """
    sidecar_path = CACHE_DIR / video_id / "timed_chunks.json"

    if sidecar_path.exists():
        raw = json.loads(sidecar_path.read_text())
        return [Document(page_content=d["text"], metadata=d["metadata"]) for d in raw]

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.fetch(video_id, languages=["en", "hi"])
    except TranscriptsDisabled:
        raise HTTPException(status_code=422, detail="Captions are disabled for this video.")
    except NoTranscriptFound:
        raise HTTPException(status_code=422, detail="No transcript found for this video.")

    # Group raw caption snippets into ~30s windows so each chunk is a
    # meaningful unit to match against, not a single caption line.
    window_seconds = 30
    windows: list[dict] = []
    current_text: list[str] = []
    window_start = None

    for snippet in transcript_list:
        if window_start is None:
            window_start = snippet.start
        if snippet.start - window_start >= window_seconds and current_text:
            windows.append({"start": window_start, "text": " ".join(current_text)})
            current_text = []
            window_start = snippet.start
        current_text.append(snippet.text)

    if current_text:
        windows.append({"start": window_start, "text": " ".join(current_text)})

    if not windows:
        raise HTTPException(status_code=422, detail="Transcript was empty.")

    docs = [
        Document(page_content=w["text"], metadata={"start": w["start"]})
        for w in windows
    ]

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps([{"text": d.page_content, "metadata": d.metadata} for d in docs])
    )
    return docs


def chunk_at_time(video_id: str, seconds: float) -> Document:
    """Return the transcript chunk whose window covers the given timestamp."""
    chunks = get_timed_transcript_chunks(video_id)
    best = chunks[0]
    for doc in chunks:
        if doc.metadata["start"] <= seconds:
            best = doc
        else:
            break
    return best


PDF_CACHE_DIR = Path(__file__).parent / "pdf_cache"
PDF_CACHE_DIR.mkdir(exist_ok=True)


def build_pdf_index(pdf_bytes: bytes) -> str:
    """
    Extract text per page, chunk it, generate a short topic label per chunk
    with the LLM, embed everything, and save a FAISS index under a new
    session id. Returns the session id.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        raise HTTPException(status_code=422, detail="PDF has no pages.")

    splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)
    pdf_documents: list[Document] = []

    for page_number, page in enumerate(doc, start=1):
        page_text = page.get_text().strip()
        if not page_text:
            continue
        for piece in splitter.split_text(page_text):
            pdf_documents.append(
                Document(page_content=piece, metadata={"page": page_number})
            )

    if not pdf_documents:
        raise HTTPException(status_code=422, detail="Couldn't extract any text from the PDF.")

    # Generate a short topic label per chunk. Sequential calls keep this
    # simple; for very large PDFs this is the slow part of indexing.
    for d in pdf_documents:
        try:
            label = invoke_with_retry(topic_label_chain, {"chunk": d.page_content})
            d.metadata["topic"] = label.strip() if label else f"Page {d.metadata['page']}"
        except Exception:
            d.metadata["topic"] = f"Page {d.metadata['page']}"
        time.sleep(0.6)  # small proactive gap to avoid bursting the free-tier TPM cap

    session_id = uuid.uuid4().hex[:12]
    vector_store = FAISS.from_documents(pdf_documents, embeddings)
    vector_store.save_local(str(PDF_CACHE_DIR / session_id))
    return session_id


def get_pdf_vector_store(pdf_session: str) -> FAISS:
    index_path = PDF_CACHE_DIR / pdf_session
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Unknown pdf_session. Upload the PDF again.")
    return FAISS.load_local(str(index_path), embeddings, allow_dangerous_deserialization=True)


class AskRequest(BaseModel):
    video: str  # video id or full URL
    question: str
    pdf_session: str | None = None


class AskResponse(BaseModel):
    answer: str
    video_id: str


class UploadPdfResponse(BaseModel):
    pdf_session: str
    chunks_indexed: int


class CurrentTopicRequest(BaseModel):
    video: str  # video id or full URL
    pdf_session: str
    seconds: float


class CurrentTopicResponse(BaseModel):
    video_id: str
    transcript_snippet: str
    matched_page: int
    matched_topic: str
    matched_snippet: str
    confidence: float


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    video_id = extract_video_id(req.video)
    vector_store = get_vector_store(video_id)
    video_retriever = vector_store.as_retriever(search_type="similarity", search_kwargs={"k": 3})

    if req.pdf_session:
        pdf_store = get_pdf_vector_store(req.pdf_session)
        pdf_retriever = pdf_store.as_retriever(search_type="similarity", search_kwargs={"k": 3})

        chain = (
            RunnableParallel(
                {
                    "video_context": video_retriever | RunnableLambda(format_docs),
                    "pdf_context": pdf_retriever | RunnableLambda(format_docs),
                    "question": RunnablePassthrough(),
                }
            )
            | compare_prompt
            | llm
            | StrOutputParser()
        )
    else:
        chain = (
            RunnableParallel(
                {
                    "context": video_retriever | RunnableLambda(format_docs),
                    "question": RunnablePassthrough(),
                }
            )
            | prompt
            | llm
            | StrOutputParser()
        )

    answer = invoke_with_retry(chain, req.question)
    return AskResponse(answer=answer, video_id=video_id)


@app.post("/upload-pdf", response_model=UploadPdfResponse)
async def upload_pdf(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    pdf_bytes = await file.read()
    session_id = build_pdf_index(pdf_bytes)
    vector_store = get_pdf_vector_store(session_id)
    chunks_indexed = vector_store.index.ntotal

    return UploadPdfResponse(pdf_session=session_id, chunks_indexed=chunks_indexed)


@app.post("/current-topic", response_model=CurrentTopicResponse)
def current_topic(req: CurrentTopicRequest):
    """
    Given a playback timestamp, find the transcript chunk covering that
    moment, then find the closest-matching PDF chunk so the extension can
    show "the video is currently explaining <topic>, see page <n>".
    """
    video_id = extract_video_id(req.video)
    transcript_chunk = chunk_at_time(video_id, req.seconds)

    pdf_store = get_pdf_vector_store(req.pdf_session)
    results = pdf_store.similarity_search_with_score(transcript_chunk.page_content, k=1)

    if not results:
        raise HTTPException(status_code=404, detail="No matching PDF content found.")

    best_doc, distance = results[0]
    # FAISS returns L2 distance for this index type — smaller is a better
    # match. Squash it into a rough 0-1 "confidence" for display purposes.
    confidence = 1.0 / (1.0 + distance)

    return CurrentTopicResponse(
        video_id=video_id,
        transcript_snippet=transcript_chunk.page_content[:200],
        matched_page=best_doc.metadata.get("page", -1),
        matched_topic=best_doc.metadata.get("topic", "Untitled section"),
        matched_snippet=best_doc.page_content[:200],
        confidence=round(confidence, 3),
    )

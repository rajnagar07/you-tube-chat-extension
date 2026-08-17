# YouTube RAG Chat — Chrome Extension

Chrome extension that turns any YouTube video into something you can talk
to, and — the interesting part — compares it live against your own
textbook or notes.

While Gemini's built-in YouTube integration can chat with a video's
transcript, it can't cross-reference that against a document you provide.
This does: upload a PDF alongside a video, and the extension shows which
page/topic of your notes the video is currently explaining as it plays,
and answers questions like *"what does the video cover that my notes
don't?"*

## Features

- **Chat with a video** — ask questions, answered only from its transcript
  (RAG: transcript → chunk → embed → retrieve → LLM).
- **Upload a PDF** — index a textbook/notes doc alongside the video.
- **Live "Now Explaining"** — as the video plays, a panel shows the
  matching page and topic label from your PDF in near real time, without
  re-scraping captions — it maps playback time straight onto pre-indexed
  transcript windows.
- **Compare mode** — once a PDF is loaded, chat questions pull context from
  *both* the video and the PDF, so you can ask what's covered in one but
  not the other.

## How it works

```
YouTube tab                              Backend (FastAPI)
┌─────────────────────┐                  ┌──────────────────────────────┐
│ content.js reads     │ ── currentTime ─▶│ chunk_at_time()                │
│ video.currentTime    │                  │  → nearest transcript window  │
└─────────────────────┘                  │  → embed it                   │
                                          │  → similarity search PDF index│
┌─────────────────────┐                  │  → return topic + page        │
│ popup.js polls every │◀── topic/page ──│                               │
│ ~6s, renders panel   │                  └──────────────────────────────┘
└─────────────────────┘

PDF upload:  PDF → per-page text (PyMuPDF) → chunk → LLM topic label per
             chunk → embed → FAISS index (its own session, keyed by id)

Transcript:  YouTube Transcript API → grouped into ~30s timed windows →
             embedded once, cached as JSON + FAISS index per video id
```

Two separate FAISS indexes exist per session: one for the video transcript
(cached under `backend/cache/<video_id>/`), one per uploaded PDF (cached
under `backend/pdf_cache/<pdf_session>/`). The live-matching endpoint reads
from both but never re-embeds the video side — playback time is looked up
against the timestamped index built once at upload.

## Project structure

```
yt-rag-extension/
├── backend/
│   ├── main.py              FastAPI app — see API reference below
│   ├── requirements.txt
│   ├── .env.example          copy to .env, fill in your own keys
│   ├── cache/                 per-video transcript FAISS indexes (gitignored)
│   └── pdf_cache/              per-upload PDF FAISS indexes (gitignored)
└── extension/
    ├── manifest.json          Manifest V3, permissions, content script registration
    ├── popup.html / .css / .js   the extension UI + polling logic
    └── content.js              reads video.currentTime from the YouTube page
```

## 1. Run the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your GROQ_API_KEY (and HF token if you have one)

uvicorn main:app --reload --port 8000
```

Leave this running. First question/upload for a given video or PDF is
slower (builds and caches embeddings); everything after reuses the cache.

## 2. Load the extension in Chrome

1. Go to `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**, select the `extension/` folder
4. Open a YouTube video, click the extension icon
5. Optionally upload a PDF — once indexed, the "Now Explaining" panel
   starts polling automatically

If your backend runs somewhere other than `http://localhost:8000`, open
the extension's ⚙ settings and update the backend URL (saved in
`chrome.storage.local`).

**After reloading the extension in `chrome://extensions`, refresh any
already-open YouTube tabs.** Chrome doesn't retroactively inject content
scripts into tabs that were open before a reload, so live polling won't
work until the page reloads.

## API reference

| Endpoint | Method | Body | Notes |
|---|---|---|---|
| `/health` | GET | — | Sanity check |
| `/ask` | POST | `{video, question, pdf_session?}` | Answers from the video transcript alone, or from both video + PDF if `pdf_session` is included |
| `/upload-pdf` | POST | multipart `file` | Returns `{pdf_session, chunks_indexed}` |
| `/current-topic` | POST | `{video, pdf_session, seconds}` | Returns the PDF page/topic matching that playback timestamp |

## Known limitations

- **Groq free tier is tight (8000 TPM).** Uploading a PDF fires one LLM
  call per chunk (topic labeling), so large PDFs uploaded back-to-back can
  hit a 429. There's retry-with-backoff on both PDF indexing and `/ask`,
  plus a small delay between topic-label calls, but a big PDF will still
  be slow to index on the free tier.
- **Confidence score is a rough squashed L2 distance**, not calibrated —
  useful to eyeball, not yet reliable enough to auto-hide low-quality
  matches (e.g. during video intros/outros where nothing in the PDF
  matches well).
- **Live polling interval (6s) and transcript window size (30s) are
  starting points**, not tuned — worth adjusting once you see it running
  against real videos.
- CORS is wide open (`allow_origins=["*"]`) since this is meant to run
  locally. Tighten this before deploying the backend publicly.
- Videos without captions return a 422 with a clear error message.

## Roadmap ideas

- Only surface a "Now Explaining" update when match confidence crosses a
  real threshold, to cut noise during weak-match stretches.
- Highlight the matched region directly inside a rendered PDF view instead
  of just naming a page number.
- Batch topic-label generation instead of one LLM call per chunk, to
  reduce indexing time and token usage on large PDFs.
- Deploy the backend (Render/Railway/a VPS) and publish the extension to
  the Chrome Web Store so it's usable by anyone, not just localhost.

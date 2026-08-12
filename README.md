# YouTube RAG Chat — Chrome Extension

Ask questions about the YouTube video you're currently watching, answered
only from its transcript (RAG over the transcript, using your notebook's
pipeline: YouTube Transcript API → chunk → HuggingFace embeddings → FAISS →
Groq LLM via LangChain).


## 1. Run the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your NEW keys

uvicorn main:app --reload --port 8000
```

Leave this running. It exposes:
- `GET /health` — sanity check
- `POST /ask` — body `{"video": "<video id or full URL>", "question": "..."}`

The first question for a given video will be slower (it fetches the
transcript, builds embeddings, and saves a FAISS index under
`backend/cache/<video_id>/`). Every question after that reuses the cached
index, so it's much faster.

## 2. Load the extension in Chrome

1. Go to `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the `extension/` folder
5. Pin the extension, open any YouTube video, click the icon, and ask a
   question

If your backend runs somewhere other than `http://localhost:8000`, open the
extension's ⚙ settings and update the backend URL — it's saved in
`chrome.storage.local`.

## Notes / next steps

- The popup detects the video from the active tab's URL (`?v=` or
  `youtu.be/...`) — it needs a real YouTube tab open.
- CORS on the backend is wide open (`allow_origins=["*"]`) since this is
  meant to run locally. Tighten this before deploying the backend publicly.
- Videos without captions will return a 422 with a clear error message in
  the chat.
- To deploy later: host `backend/` anywhere that can run Python (Render,
  Railway, a VPS, etc.), keep your `.env` off git, and point the extension
  settings at the new URL.

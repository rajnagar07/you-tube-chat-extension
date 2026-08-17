const DEFAULT_BACKEND = "http://localhost:8000";

const chatEl = document.getElementById("chat");
const videoLabel = document.getElementById("videoLabel");
const statusDot = document.getElementById("statusDot");
const askForm = document.getElementById("askForm");
const questionInput = document.getElementById("questionInput");
const askBtn = document.getElementById("askBtn");
const settingsToggle = document.getElementById("settingsToggle");
const settingsPanel = document.getElementById("settingsPanel");
const backendUrlInput = document.getElementById("backendUrl");
const saveSettingsBtn = document.getElementById("saveSettings");
const pdfInput = document.getElementById("pdfInput");
const pdfUploadText = document.getElementById("pdfUploadText");
const nowExplaining = document.getElementById("nowExplaining");
const nowExplainingTopic = document.getElementById("nowExplainingTopic");
const nowExplainingMeta = document.getElementById("nowExplainingMeta");

let currentVideoId = null;
let currentTabId = null;
let backendUrl = DEFAULT_BACKEND;
let pdfSession = null;
let pollTimer = null;

const POLL_INTERVAL_MS = 6000;

function extractVideoId(url) {
  try {
    const u = new URL(url);
    if (u.hostname.includes("youtu.be")) {
      return u.pathname.slice(1);
    }
    if (u.hostname.includes("youtube.com")) {
      return u.searchParams.get("v");
    }
  } catch (e) {
    return null;
  }
  return null;
}

function addBubble(text, kind) {
  // clear empty state if present
  const emptyState = chatEl.querySelector(".empty-state");
  if (emptyState) emptyState.remove();

  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  div.textContent = text;
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

function showEmptyState(message) {
  chatEl.innerHTML = `<div class="empty-state">${message}</div>`;
}

async function init() {
  const stored = await chrome.storage.local.get(["backendUrl"]);
  backendUrl = stored.backendUrl || DEFAULT_BACKEND;
  backendUrlInput.value = backendUrl;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const videoId = tab && tab.url ? extractVideoId(tab.url) : null;

  if (!videoId) {
    videoLabel.textContent = "Open a YouTube video";
    statusDot.classList.remove("live");
    showEmptyState("Navigate to a YouTube video, then reopen this popup to ask questions about it.");
    questionInput.disabled = true;
    askBtn.disabled = true;
    return;
  }

  currentVideoId = videoId;
  currentTabId = tab.id;
  videoLabel.textContent = tab.title || videoId;
  statusDot.classList.add("live");
  showEmptyState("Ask a question about this video's transcript.");

  const storedPdf = await chrome.storage.local.get([`pdfSession:${videoId}`]);
  const savedSession = storedPdf[`pdfSession:${videoId}`];
  if (savedSession) {
    pdfSession = savedSession;
    pdfUploadText.textContent = "PDF loaded — replace";
    startLivePolling();
  }
}

async function ask(question) {
  addBubble(question, "user");
  const loadingBubble = addBubble("Thinking…", "loading");
  askBtn.disabled = true;

  try {
    const res = await fetch(`${backendUrl.replace(/\/$/, "")}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video: currentVideoId, question, pdf_session: pdfSession }),
    });

    loadingBubble.remove();

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      addBubble(err.detail || `Request failed (${res.status})`, "error");
      return;
    }

    const data = await res.json();
    addBubble(data.answer, "answer");
  } catch (e) {
    loadingBubble.remove();
    addBubble(
      `Couldn't reach the backend at ${backendUrl}. Is it running? (${e.message})`,
      "error"
    );
  } finally {
    askBtn.disabled = false;
  }
}

async function uploadPdf(file) {
  pdfUploadText.textContent = "Indexing PDF…";
  pdfInput.disabled = true;

  try {
    const formData = new FormData();
    formData.append("file", file);

    const res = await fetch(`${backendUrl.replace(/\/$/, "")}/upload-pdf`, {
      method: "POST",
      body: formData,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      pdfUploadText.textContent = "Upload failed — try again";
      addBubble(err.detail || `PDF upload failed (${res.status})`, "error");
      return;
    }

    const data = await res.json();
    pdfSession = data.pdf_session;
    pdfUploadText.textContent = `Indexed ${data.chunks_indexed} sections — replace PDF`;

    if (currentVideoId) {
      await chrome.storage.local.set({ [`pdfSession:${currentVideoId}`]: pdfSession });
    }

    startLivePolling();
  } catch (e) {
    pdfUploadText.textContent = "Upload failed — try again";
    addBubble(`Couldn't reach the backend at ${backendUrl}. (${e.message})`, "error");
  } finally {
    pdfInput.disabled = false;
  }
}

function getVideoCurrentTime() {
  return new Promise((resolve) => {
    if (!currentTabId) return resolve(null);
    chrome.tabs.sendMessage(currentTabId, { type: "GET_CURRENT_TIME" }, (response) => {
      if (chrome.runtime.lastError || !response || !response.ok) {
        resolve(null);
        return;
      }
      resolve(response);
    });
  });
}

async function pollCurrentTopic() {
  if (!currentVideoId || !pdfSession) return;

  const playback = await getVideoCurrentTime();
  if (!playback) return; // tab not on the video anymore, or video not ready

  try {
    const res = await fetch(`${backendUrl.replace(/\/$/, "")}/current-topic`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video: currentVideoId,
        pdf_session: pdfSession,
        seconds: playback.seconds,
      }),
    });

    if (!res.ok) return; // stay quiet on transient errors during live polling

    const data = await res.json();
    nowExplaining.classList.remove("hidden");
    nowExplainingTopic.textContent = data.matched_topic;
    nowExplainingMeta.textContent = `Page ${data.matched_page} · match ${Math.round(data.confidence * 100)}%`;
  } catch (e) {
    // Backend unreachable mid-poll — leave the last known topic showing.
  }
}

function startLivePolling() {
  if (pollTimer) clearInterval(pollTimer);
  nowExplaining.classList.remove("hidden");
  pollCurrentTopic();
  pollTimer = setInterval(pollCurrentTopic, POLL_INTERVAL_MS);
}

pdfInput.addEventListener("change", () => {
  const file = pdfInput.files[0];
  if (file) uploadPdf(file);
});

askForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const question = questionInput.value.trim();
  if (!question || !currentVideoId) return;
  questionInput.value = "";
  ask(question);
});

settingsToggle.addEventListener("click", () => {
  settingsPanel.classList.toggle("hidden");
});

saveSettingsBtn.addEventListener("click", async () => {
  const value = backendUrlInput.value.trim() || DEFAULT_BACKEND;
  backendUrl = value;
  await chrome.storage.local.set({ backendUrl: value });
  settingsPanel.classList.add("hidden");
});

init();

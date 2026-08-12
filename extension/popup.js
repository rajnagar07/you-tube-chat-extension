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

let currentVideoId = null;
let backendUrl = DEFAULT_BACKEND;

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
  videoLabel.textContent = tab.title || videoId;
  statusDot.classList.add("live");
  showEmptyState("Ask a question about this video's transcript.");
}

async function ask(question) {
  addBubble(question, "user");
  const loadingBubble = addBubble("Thinking…", "loading");
  askBtn.disabled = true;

  try {
    const res = await fetch(`${backendUrl.replace(/\/$/, "")}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video: currentVideoId, question }),
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

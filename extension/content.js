// Runs on youtube.com watch pages. Its only job is to report the current
// playback position of the video when asked — the popup can't reach the
// page's <video> element directly, so this bridges that gap.

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "GET_CURRENT_TIME") return;

  const video = document.querySelector("video.html5-main-video");
  if (!video) {
    sendResponse({ ok: false, error: "No video element found on this page." });
    return;
  }

  sendResponse({ ok: true, seconds: video.currentTime, paused: video.paused });
});

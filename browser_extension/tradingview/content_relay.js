const EVENT = "pa-agent-tv-chart-snapshot";
let latest = null;
let sendTimer = 0;

const publishLatest = () => {
  sendTimer = 0;
  if (!latest) return;
  chrome.runtime.sendMessage({ type: "PA_AGENT_PAGE_READER", payload: latest }).catch(() => {});
};

window.addEventListener(EVENT, (event) => {
  latest = event.detail;
  if (!sendTimer) sendTimer = setTimeout(publishLatest, 350);
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "PA_AGENT_POLL") return;
  window.dispatchEvent(new Event("pa-agent-tv-reader-poll"));
  setTimeout(() => {
    chrome.runtime.sendMessage({ type: "PA_AGENT_CHART_DATA", payload: latest }).catch(() => {});
  }, 80);
});

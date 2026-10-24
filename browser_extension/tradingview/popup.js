const el = (id) => document.getElementById(id);
const statusText = (message, bad = false) => {
  el("message").textContent = message;
  el("message").style.color = bad ? "var(--bad)" : "var(--muted)";
};
const request = (type, extra = {}) => new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, resolve));
const paint = (state) => {
  const data = state.latest || {};
  const diagnostics = data.diagnostics || {};
  el("enabled").checked = !!state.enabled;
  el("barCount").textContent = diagnostics.accepted ?? data.bars?.length ?? "—";
  el("rejected").textContent = diagnostics.rejected ?? "—";
  el("duplicates").textContent = diagnostics.duplicates ?? "—";
  el("lastUpdate").textContent = data.receivedAt ? new Date(data.receivedAt).toLocaleTimeString() : "—";
  el("packetCount").textContent = diagnostics.chartPackets ?? "—";
  el("chartStatus").textContent = data.symbol ? `${data.symbol} · ${data.resolution || "?"}` : "未检测到图表";
  if (state.bridge?.desiredSymbol) {
    el("target").textContent = `${state.bridge.desiredSymbol} · ${state.bridge.desiredTimeframe}`;
  }
  if (state.bridge?.displayLastBarTime) {
    el("effectiveBar").textContent = new Date(state.bridge.displayLastBarTime).toLocaleString();
  } else {
    el("effectiveBar").textContent = state.bridge?.historyCutoff ? "截止点前无 K 线" : "未设置历史截止时间";
  }
  const ok = !!state.bridge?.ok;
  el("backendStatus").textContent = ok ? "已连接" : state.bridgeError ? "连接失败" : "未连接";
  el("statusDot").className = `dot ${ok ? "ok" : "bad"}`;
  if (data.error) statusText(data.error, true);
  else if (state.bridgeError) statusText(`本机后端连接失败：${state.bridgeError}`, true);
  else if (diagnostics.reader === "waiting-for-chart-candles") statusText("尚未收到包含 K 线的 TradingView 图表数据包；请确认图表已加载。", true);
};

async function refresh() {
  const state = await request("PA_AGENT_POPUP_STATE");
  if (state) paint(state);
}

el("enabled").addEventListener("change", async () => {
  const result = await request("PA_AGENT_SET_ENABLED", { enabled: el("enabled").checked });
  el("enabled").checked = !!result?.enabled;
  statusText(result?.enabled ? "自动采集已开启。" : "自动采集已暂停。");
  setTimeout(refresh, 500);
});
el("captureNow").addEventListener("click", async () => {
  const button = el("captureNow");
  button.disabled = true;
  statusText("正在读取并校验图表数据…");
  const result = await request("PA_AGENT_CAPTURE_NOW");
  button.disabled = false;
  if (!result?.ok) statusText(result?.error || "同步失败", true);
  else {
    const cutoff = el("historyCutoff").value;
    statusText(cutoff
      ? `已同步 ${result.data.diagnostics.accepted} 根候选 K 线；截止 ${cutoff}，PA Agent 末根 K 线见上方。`
      : `已同步 ${result.data.diagnostics.accepted} 根 K 线；未设历史截止时间，PA Agent 会显示最新 K 线。`);
  }
  await refresh();
});
for (const id of ["symbolFilter", "timeframeFilter"]) {
  el(id).addEventListener("change", () => {
    chrome.storage.local.set({
      symbolFilter: el("symbolFilter").value.trim().toUpperCase() || "*",
      timeframeFilter: el("timeframeFilter").value
    });
  });
}
el("historyCutoff").addEventListener("change", () => {
  chrome.storage.local.set({ historyCutoff: el("historyCutoff").value });
});
chrome.storage.local.get(["symbolFilter", "timeframeFilter", "historyCutoff"], (values) => {
  if (values.symbolFilter) el("symbolFilter").value = values.symbolFilter;
  if (values.timeframeFilter) el("timeframeFilter").value = values.timeframeFilter;
  if (values.historyCutoff) el("historyCutoff").value = values.historyCutoff;
});
refresh();
setInterval(refresh, 2000);

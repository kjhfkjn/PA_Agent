const el = (id) => document.getElementById(id);
const statusText = (message, bad = false) => {
  el("message").textContent = message;
  el("message").style.color = bad ? "var(--bad)" : "var(--muted)";
};
const request = (type, extra = {}) => new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, resolve));
const toLocalInput = (time) => {
  if (!Number.isFinite(Number(time))) return "";
  const date = new Date(Number(time));
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
};
const paint = (state) => {
  const data = state.latest || {};
  const diagnostics = data.diagnostics || {};
  el("enabled").checked = !!state.enabled;
  el("autoCutoff").checked = state.autoCutoff !== false;
  el("historyCutoff").disabled = el("autoCutoff").checked;
  if (el("autoCutoff").checked) {
    const detectedCutoff = data.cutoffTime ?? state.bridge?.historyCutoff;
    if (detectedCutoff) el("historyCutoff").value = toLocalInput(detectedCutoff);
  }
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
  el("cutoffSource").textContent = data.cutoffSource || state.bridge?.historyCutoffSource || "—";
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
  const result = await request("PA_AGENT_CAPTURE_NOW", {
    historyCutoff: el("historyCutoff").value
  });
  button.disabled = false;
  if (!result?.ok) statusText(result?.error || "同步失败", true);
  else {
    const cutoff = result.data.cutoffTime ? new Date(result.data.cutoffTime).toLocaleString() : "最新数据";
    statusText(`已同步 ${result.data.diagnostics.accepted} 根候选 K 线；自动截止：${cutoff}，PA Agent 末根 K 线见上方。`);
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
el("autoCutoff").addEventListener("change", async () => {
  const autoCutoff = el("autoCutoff").checked;
  el("historyCutoff").disabled = autoCutoff;
  chrome.storage.local.set({ autoCutoff });
  statusText(autoCutoff ? "将自动使用图表最新 K 线作为截止点。" : "已关闭自动截止；请指定历史时间，或留空使用最新数据。");
  const result = await request("PA_AGENT_CAPTURE_NOW", { historyCutoff: el("historyCutoff").value });
  if (!result?.ok) statusText(result?.error || "同步失败", true);
  await refresh();
});
chrome.storage.local.get(["symbolFilter", "timeframeFilter", "historyCutoff", "autoCutoff"], (values) => {
  if (values.symbolFilter) el("symbolFilter").value = values.symbolFilter;
  if (values.timeframeFilter) el("timeframeFilter").value = values.timeframeFilter;
  if (values.historyCutoff) el("historyCutoff").value = values.historyCutoff;
  el("autoCutoff").checked = values.autoCutoff !== false;
  el("historyCutoff").disabled = el("autoCutoff").checked;
});
refresh();
setInterval(refresh, 2000);

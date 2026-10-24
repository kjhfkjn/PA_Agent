const el = (id) => document.getElementById(id);
const statusText = (message, bad = false) => {
  el("message").textContent = message;
  el("message").style.color = bad ? "var(--bad)" : "var(--muted)";
};
const request = (type, extra = {}) => new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, resolve));
const livePort = chrome.runtime.connect({ name: "pa-agent-live-stream" });
const streamFields = {
  stage1Thinking: "stage1Thinking",
  stage1Answer: "stage1Answer",
  stage2Thinking: "stage2Thinking",
  stage2Answer: "stage2Answer"
};
const renderDecision = (full) => {
  const decision = full?.decision || {};
  const lines = [];
  const order = [decision.order_direction, decision.order_type].filter(Boolean).join(" ");
  if (order) lines.push(`最终决策：${order}`);
  for (const [label, key] of [["入场", "entry_price"], ["止损", "stop_loss_price"], ["止盈", "take_profit_price"]]) {
    if (decision[key] !== undefined && decision[key] !== null && decision[key] !== "") {
      lines.push(`${label}：${decision[key]}`);
    }
  }
  if (decision.reasoning) lines.push(`理由：${decision.reasoning}`);
  if (Array.isArray(full?.decision_trace) && full.decision_trace.length) {
    lines.push("决策路径：");
    for (const item of full.decision_trace) {
      if (!item || typeof item !== "object") continue;
      lines.push([item.node_id, item.question, item.answer].filter(Boolean).join(" "));
    }
  }
  el("decisionResult").textContent = lines.join("\n") || "Agent 尚未输出决策。";
};
const renderLiveSnapshot = (state = {}) => {
  for (const [id, key] of Object.entries(streamFields)) {
    el(id).textContent = state[key] || "等待 Agent 输出…";
  }
  renderDecision(state.decision);
  el("liveStatus").textContent = state.error
    ? `${state.status || "分析失败"}：${state.error}`
    : (state.status || "等待 Agent");
  el("liveInstrument").textContent = state.symbol
    ? `${state.symbol} · ${state.timeframe || "?"} · 来自 PA Agent 实时推送`
    : "思考、回答与决策由 PA Agent 实时推送；关闭侧栏后内容不保留。";
};
livePort.onMessage.addListener((message) => {
  if (message?.type === "PA_AGENT_LIVE_SNAPSHOT") {
    renderLiveSnapshot(message.state);
    return;
  }
  if (message?.type !== "PA_AGENT_LIVE_EVENT") return;
  const event = message.event || {};
  if (event.event === "started") {
    renderLiveSnapshot({
      symbol: event.symbol,
      timeframe: event.timeframe,
      status: "分析中"
    });
  } else if (event.event === "token") {
    const key = `${event.stage}${event.channel === "thinking" ? "Thinking" : "Answer"}`;
    const target = el(streamFields[key]);
    if (target) {
      if (target.textContent === "等待 Agent 输出…") target.textContent = "";
      target.textContent += event.text || "";
      target.scrollTop = target.scrollHeight;
    }
    el("liveStatus").textContent = "分析中";
  } else if (event.event === "decision") {
    renderDecision(event.decision);
    el("liveStatus").textContent = "已完成";
  } else if (event.event === "failed") {
    el("liveStatus").textContent = event.error
      ? `分析失败（已记录）：${event.error}`
      : "分析失败（已记录）";
  } else if (event.event === "cancelled") {
    el("liveStatus").textContent = "分析已取消，回放保持在当前 K 线";
  }
});
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
  el("backtestMode").checked = !!state.backtestMode;
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
  else if (state.backtestMode && data.backtestStatus) statusText(data.backtestStatus);
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
el("backtestMode").addEventListener("change", async () => {
  const toggle = el("backtestMode");
  toggle.disabled = true;
  const result = await request("PA_AGENT_SET_BACKTEST", { enabled: toggle.checked });
  toggle.disabled = false;
  toggle.checked = !!result?.backtestMode;
  if (result?.error) statusText(result.error, true);
  else if (result?.ok) {
    statusText(
      toggle.checked
        ? "5 分钟回测已开启：当前 K 线分析完成后，扩展会收到 Agent 通知并推进下一根。"
        : "5 分钟回测已关闭。"
    );
  } else {
    statusText("回测模式切换失败", true);
  }
  await refresh();
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

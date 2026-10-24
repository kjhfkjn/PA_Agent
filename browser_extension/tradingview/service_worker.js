const BRIDGE = "http://127.0.0.1:18765";
const ALARM = "pa-agent-capture";
let bridgeToken = "";
let latest = { diagnostics: {}, symbol: "", resolution: "", receivedAt: 0, error: "" };
let enabled = false;
const lastObservedBarByChart = new Map();

const restoreCaptureState = () => {
  chrome.storage.local.get(["enabled"], (values) => {
    enabled = !!values.enabled;
    if (enabled) {
      chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
      capture().catch((error) => { latest.error = error.message; });
    } else {
      chrome.alarms.clear(ALARM);
    }
  });
};

const sendBridge = async (path, payload) => {
  if (!bridgeToken) {
    const response = await fetch(`${BRIDGE}/api/v1/status`, { cache: "no-store" });
    if (!response.ok) throw new Error(`PA Agent 状态请求失败 (${response.status})`);
    const status = await response.json();
    bridgeToken = status.token;
  }
  const response = await fetch(`${BRIDGE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PA-Token": bridgeToken },
    body: JSON.stringify(payload)
  });
  if (!response.ok) throw new Error(`PA Agent 接收失败 (${response.status})`);
  return response.json();
};

const queryActiveChart = async () => {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id || !/^https:\/\/[^/]*tradingview\.com\/chart\//.test(tab.url || "")) {
    throw new Error("请先切换到 TradingView 图表标签页");
  }
  const received = await new Promise((resolve) => {
    const listener = (message, sender) => {
      if (message?.type === "PA_AGENT_CHART_DATA" && sender.tab?.id === tab.id) {
        chrome.runtime.onMessage.removeListener(listener);
        resolve(message.payload);
      }
    };
    chrome.runtime.onMessage.addListener(listener);
    chrome.tabs.sendMessage(tab.id, { type: "PA_AGENT_POLL" }).catch(() => {});
    setTimeout(() => {
      chrome.runtime.onMessage.removeListener(listener);
      resolve(null);
    }, 900);
  });
  if (!received) throw new Error("尚未收到 TradingView 图表数据；请确认图表已加载并重试");
  return { tab, received };
};

const sendReceivedSnapshot = async (tab, received, explicitHistoryCutoff) => {
  latest = { ...received, receivedAt: Date.now(), error: "" };
  if (!received.bars?.length) throw new Error("尚未收到包含有效 K 线的图表数据包；悬停 OHLC 不会被当作历史数据");
  const stored = await chrome.storage.local.get(["symbolFilter", "timeframeFilter", "historyCutoff", "autoCutoff"]);
  const autoCutoff = stored.autoCutoff !== false;
  const manualCutoff = explicitHistoryCutoff === undefined ? stored.historyCutoff : explicitHistoryCutoff;
  const newest = received.bars.reduce((best, bar) => Number(bar?.time) > Number(best?.time) ? bar : best, null);
  const observedCutoff = Number(received.cutoffTimeCandidate);
  let cutoffTime = null;
  if (autoCutoff && (Number.isFinite(observedCutoff) || newest)) {
    cutoffTime = Number.isFinite(observedCutoff) ? observedCutoff : Number(newest.time);
    if (cutoffTime < 100000000000) cutoffTime *= 1000;
  } else if (manualCutoff) {
    cutoffTime = new Date(manualCutoff).getTime();
  }
  if (cutoffTime !== null && (!Number.isFinite(cutoffTime) || cutoffTime <= 0)) {
    throw new Error("历史截止时间无效，请重新选择日期和时间");
  }
  const symbolFilter = String(stored.symbolFilter ?? "MES!").toUpperCase();
  // Capturing all chart resolutions lets users preload higher-timeframe
  // context, then trade from one execution timeframe without switching back.
  const timeframeFilter = String(stored.timeframeFilter ?? "*");
  const symbol = String(received.symbol || "").toUpperCase().split(":").pop().replace("1!", "!");
  if (symbolFilter !== "*" && symbol !== symbolFilter) {
    throw new Error(`当前图表 ${symbol} 不符合品种过滤 ${symbolFilter}`);
  }
  if (timeframeFilter !== "*" && String(received.resolution) !== timeframeFilter) {
    throw new Error(`当前图表周期 ${received.resolution} 不符合周期过滤 ${timeframeFilter}`);
  }
  await sendBridge("/api/v1/heartbeat", { pageUrl: tab.url, symbol: received.symbol, resolution: received.resolution, diagnostics: received.diagnostics });
  await sendBridge("/api/v1/klines", {
    pageUrl: tab.url,
    symbol: received.symbol,
    resolution: received.resolution,
    source: "tradingview_edge_extension",
    bars: received.bars,
    cutoffTime,
    cutoffSource: received.cutoffSource || "unknown",
    diagnostics: received.diagnostics
  });
  latest = { ...latest, cutoffTime, cutoffMode: autoCutoff ? "auto" : (manualCutoff ? "manual" : "latest") };
  return latest;
};

const capture = async (explicitHistoryCutoff) => {
  const { tab, received } = await queryActiveChart();
  return sendReceivedSnapshot(tab, received, explicitHistoryCutoff);
};

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type === "PA_AGENT_POPUP_STATE") {
    Promise.all([
      chrome.storage.local.get(["enabled", "autoCutoff"]),
      fetch(`${BRIDGE}/api/v1/status`, { cache: "no-store" }).then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const value = await response.json();
        bridgeToken = value.token || bridgeToken;
        return { status: value, error: "" };
      }).catch((error) => ({ status: null, error: error.message }))
    ]).then(([values, bridge]) => respond({
      enabled: !!values.enabled,
      autoCutoff: values.autoCutoff !== false,
      latest,
      bridge: bridge.status,
      bridgeError: bridge.error
    }));
    return true;
  }
  if (message?.type === "PA_AGENT_SET_ENABLED") {
    enabled = !!message.enabled;
    chrome.storage.local.set({ enabled }, () => {
      if (enabled) {
        chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
        capture().catch((error) => { latest.error = error.message; });
      } else chrome.alarms.clear(ALARM);
      respond({ enabled });
    });
    return true;
  }
  if (message?.type === "PA_AGENT_CAPTURE_NOW") {
    capture(message.historyCutoff).then((data) => respond({ ok: true, data })).catch((error) => {
      latest.error = error.message;
      respond({ ok: false, error: error.message, data: latest });
    });
    return true;
  }
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "PA_AGENT_PAGE_READER" && sender.tab?.url?.includes("tradingview.com/chart/")) {
    latest = { ...message.payload, receivedAt: Date.now(), error: "" };
    const rows = message.payload?.bars || [];
    const newest = rows.reduce((best, bar) => Number(bar?.time) > Number(best?.time) ? bar : best, null);
    if (!newest || !sender.tab?.id) return;
    const key = `${sender.tab.id}|${message.payload.symbol}|${message.payload.resolution}`;
    const activeCutoff = Number(message.payload?.cutoffTimeCandidate);
    const currentTime = Number.isFinite(activeCutoff) && activeCutoff > 0
      ? activeCutoff
      : Number(newest.time);
    const previousTime = lastObservedBarByChart.get(key);
    lastObservedBarByChart.set(key, currentTime);
    // The active series edge moves both forward and backward in Bar Replay.
    // Push either transition immediately; the alarm remains a recovery path.
    if (previousTime !== currentTime) {
      chrome.storage.local.get(["enabled"], (values) => {
        enabled = !!values.enabled;
        if (!enabled) return;
        sendReceivedSnapshot({ id: sender.tab.id, url: sender.tab.url }, message.payload)
          .catch((error) => { latest.error = error.message; });
      });
    }
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) capture().catch((error) => { latest.error = error.message; });
});
chrome.runtime.onStartup.addListener(restoreCaptureState);
chrome.runtime.onInstalled.addListener(() => chrome.storage.local.get(["enabled"], (values) => {
  enabled = !!values.enabled;
  chrome.storage.local.get(["autoCutoff"], (stored) => {
    if (stored.autoCutoff === undefined) chrome.storage.local.set({ autoCutoff: true });
  });
  restoreCaptureState();
}));
restoreCaptureState();

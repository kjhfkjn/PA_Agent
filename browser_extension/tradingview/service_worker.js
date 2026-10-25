const BRIDGE = "http://127.0.0.1:18765";
const ALARM = "pa-agent-capture";
let bridgeToken = "";
let latest = { diagnostics: {}, symbol: "", resolution: "", receivedAt: 0, error: "" };
let enabled = false;
let backtestError = "";
const lastObservedBarByChart = new Map();
let analysisEventSocket = null;
let analysisEventConnectInProgress = false;
const liveStreamPorts = new Set();
let liveEventFloor = 0;
let analysisEventReconnectTimer = 0;
let analysisEventReconnectDelay = 1000;
let backtestAdvanceInProgress = false;
let captureRestoreInProgress = false;
let analysisEventQueue = Promise.resolve();
let snapshotSyncQueue = Promise.resolve();
let backtestOperationQueue = Promise.resolve();
let backtestOperationCount = 0;

const withBacktestAdvanceLock = (operation) => {
  backtestOperationCount += 1;
  backtestAdvanceInProgress = true;
  const task = backtestOperationQueue
    .catch(() => {})
    .then(operation)
    .finally(() => {
      backtestOperationCount = Math.max(0, backtestOperationCount - 1);
      backtestAdvanceInProgress = backtestOperationCount > 0;
    });
  backtestOperationQueue = task.catch(() => {});
  return task;
};

if (chrome.sidePanel?.setPanelBehavior) {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((error) => {
    console.warn("无法设置 PA Agent 侧栏行为", error);
  });
}

const restoreCaptureState = () => {
  if (captureRestoreInProgress) return;
  captureRestoreInProgress = true;
  chrome.storage.local.get([
    "enabled",
    "backtestMode",
    "backtestPending",
    "backtestFailureStreak"
  ], async (values) => {
    enabled = !!values.enabled;
    try {
      if (enabled && values.backtestMode) {
        chrome.alarms.clear(ALARM);
        await sendBridge("/api/v1/backtest", { enabled: true });
        connectAnalysisEventStream();
        if (Number(values.backtestFailureStreak || 0) >= 3) {
          backtestError = "连续 3 根 K 线分析失败，自动回测已暂停";
          latest.error = backtestError;
          latest.backtestStatus = "连续分析失败，自动回测已暂停";
        } else if (!values.backtestPending) {
          await startInitialReplayIfNeeded();
        }
      } else if (enabled) {
        stopAnalysisEventStream();
        chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
        capture().catch((error) => { latest.error = error.message; });
      } else {
        stopAnalysisEventStream();
        chrome.alarms.clear(ALARM);
      }
    } catch (error) {
      backtestError = error.message;
      latest.error = error.message;
      if (values.backtestMode) scheduleAnalysisEventReconnect();
    } finally {
      captureRestoreInProgress = false;
    }
  });
};

const refreshBridgeToken = async () => {
  const response = await fetch(`${BRIDGE}/api/v1/status`, { cache: "no-store" });
  if (!response.ok) throw new Error(`PA Agent 状态请求失败 (${response.status})`);
  const status = await response.json();
  bridgeToken = status.token;
  return status;
};

const sendBridge = async (path, payload) => {
  if (!bridgeToken) await refreshBridgeToken();
  const request = () => fetch(`${BRIDGE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PA-Token": bridgeToken },
    body: JSON.stringify(payload)
  });
  let response = await request();
  if (response.status === 401) {
    await refreshBridgeToken();
    response = await request();
  }
  if (!response.ok) throw new Error(`PA Agent 接收失败 (${response.status})`);
  return response.json();
};

const normalizeEventSymbol = (value) => String(value || "")
  .toUpperCase()
  .split(":")
  .pop()
  .replace("1!", "!")
  .trim();

const normalizeEventTimeframe = (value) => {
  const text = String(value || "").trim();
  if (/^\d+$/.test(text)) return `${Number(text)}m`;
  const minute = text.match(/^(\d+)m$/i);
  if (minute) return `${Number(minute[1])}m`;
  return text.toUpperCase();
};

const stopAnalysisEventStream = () => {
  if (analysisEventReconnectTimer) clearTimeout(analysisEventReconnectTimer);
  analysisEventReconnectTimer = 0;
  const socket = analysisEventSocket;
  analysisEventSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
};

const scheduleAnalysisEventReconnect = () => {
  if (analysisEventReconnectTimer) return;
  analysisEventReconnectTimer = setTimeout(() => {
    analysisEventReconnectTimer = 0;
    connectAnalysisEventStream();
  }, analysisEventReconnectDelay);
  analysisEventReconnectDelay = Math.min(analysisEventReconnectDelay * 2, 15000);
};

const startInitialReplayIfNeeded = () => withBacktestAdvanceLock(async () => {
  let stored;
  try {
    stored = await chrome.storage.local.get([
      "backtestMode",
      "backtestPending",
      "backtestFailureStreak"
    ]);
  } catch (error) {
    backtestError = `读取回测状态失败：${error.message}`;
    latest.error = backtestError;
    return { ok: false, error: backtestError };
  }
  if (!stored.backtestMode) return { ok: true };
  if (Number(stored.backtestFailureStreak || 0) >= 3) {
    latest.error = "连续 3 根 K 线分析失败，自动回测已暂停";
    latest.backtestStatus = "连续分析失败，自动回测已暂停";
    return { ok: true, paused: true };
  }
  const pending = stored.backtestPending;
  const recoverable = [
    "syncing",
    "sync_failed",
    "advance_ready",
    "advance_failed"
  ].includes(String(pending?.state || ""));
  if (pending && !recoverable) return { ok: true, waiting: true };
  try {
    if (pending?.state === "sync_failed" || pending?.state === "syncing") {
      const current = await queryActiveChart(pending.tabId || null);
      await sendReceivedSnapshot(current.tab, current.received);
    } else if (pending) {
      await resumeReplayFromPending(pending);
    } else {
      await advanceReplayAndSync();
    }
    backtestError = "";
    latest.backtestStatus = "等待 Agent 分析当前 K 线";
    return { ok: true };
  } catch (error) {
    backtestError = error.message;
    latest.error = error.message;
    latest.backtestStatus = "初始回放同步失败，请查看错误信息";
    return { ok: false, error: error.message };
  }
});

const handleAnalysisEventLocked = async (event) => {
  const analysisFailed = event.event === "failed";
  if (
    (!analysisFailed && event.event !== "completed")
    || !Number.isFinite(Number(event.eventId))
  ) return;
  const eventId = Number(event.eventId);
  // Flush every snapshot POST already queued before matching a completion.
  // Otherwise an older in-flight upload could replace the pending identity.
  await snapshotSyncQueue.catch(() => {});
  const stored = await chrome.storage.local.get([
    "backtestMode",
    "backtestPending",
    "lastAnalysisEventId",
    "backtestFailureStreak"
  ]);
  if (eventId <= Number(stored.lastAnalysisEventId || 0)) return;

  const pending = stored.backtestPending;
  const eventTime = Number(event.barTime) < 100000000000
    ? Number(event.barTime) * 1000
    : Number(event.barTime);
  const legacyTimeMatches = Math.abs(eventTime - Number(pending?.barTime)) < 1000
    && Number(event.createdAt || 0) >= Number(pending?.sentAt || pending?.createdAt || 0);
  const syncMatches = event.syncId && pending?.syncId
    ? String(event.syncId) === String(pending.syncId)
    : legacyTimeMatches;
  const sessionMatches = !event.backtestSessionId
    || !pending?.backtestSessionId
    || String(event.backtestSessionId) === String(pending.backtestSessionId);
  const matchesPending = !!stored.backtestMode
    && !!pending
    && normalizeEventSymbol(event.symbol) === normalizeEventSymbol(pending.symbol)
    && normalizeEventTimeframe(event.timeframe) === normalizeEventTimeframe(pending.timeframe)
    && syncMatches
    && sessionMatches;

  if (!matchesPending) {
    await chrome.storage.local.set({ lastAnalysisEventId: eventId });
    return;
  }
  const failureStreak = analysisFailed
    ? Number(stored.backtestFailureStreak || 0) + 1
    : 0;
  if (analysisFailed && failureStreak >= 3) {
    const error = String(event.error || "Agent 分析失败").slice(0, 500);
    backtestError = `连续 ${failureStreak} 根 K 线分析失败，自动回测已暂停：${error}`;
    latest.error = backtestError;
    latest.backtestStatus = "连续分析失败，自动回测已暂停；失败记录已保留";
    await chrome.storage.local.set({
      lastAnalysisEventId: eventId,
      backtestFailureStreak: failureStreak
    });
    return;
  }
  latest.backtestStatus = analysisFailed
    ? "当前 K 线分析失败（错误已记录），继续推进下一根"
    : "Agent 已完成当前 K 线分析，等待下一根有效 K 线同步";
  const completedPending = {
    ...pending,
    state: "advance_ready",
    completionEventId: eventId
  };
  try {
    await chrome.storage.local.set({
      lastAnalysisEventId: eventId,
      backtestPending: completedPending,
      backtestFailureStreak: failureStreak
    });
    await resumeReplayFromPending(completedPending);
    backtestError = "";
    if (analysisFailed) {
      const error = String(event.error || "Agent 分析失败").slice(0, 500);
      latest.error = `上一根分析失败，错误记录已保留并继续回测：${error}`;
      latest.backtestStatus = "失败棒已记录并跳过；等待分析下一根";
    } else {
      latest.backtestStatus = "等待 Agent 分析当前 K 线";
    }
  } catch (error) {
    backtestError = `Agent 分析完成，但自动推进失败：${error.message}`;
    latest.error = backtestError;
    latest.backtestStatus = "自动推进失败，请查看错误信息";
    await chrome.storage.local.set({
      backtestPending: { ...completedPending, state: "advance_failed" }
    });
  }
};

const handleAnalysisEvent = (event) => (
  withBacktestAdvanceLock(() => handleAnalysisEventLocked(event))
);

const enqueueAnalysisEvent = (event) => {
  analysisEventQueue = analysisEventQueue
    .then(() => handleAnalysisEvent(event))
    .catch((error) => {
      backtestError = `处理 Agent 完成消息失败：${error.message}`;
      latest.error = backtestError;
    });
  return analysisEventQueue;
};

const connectAnalysisEventStream = async () => {
  const stored = await chrome.storage.local.get(["backtestMode", "backtestPending"]);
  if (
    (!stored.backtestMode && liveStreamPorts.size === 0)
    || analysisEventSocket
    || analysisEventConnectInProgress
  ) return;
  analysisEventConnectInProgress = true;
  try {
    const bridgeStatus = await refreshBridgeToken();
    const cursorState = await chrome.storage.local.get([
      "lastAnalysisEventId",
      "analysisEventSessionId"
    ]);
    const eventSessionId = String(bridgeStatus.eventSessionId || "");
    let afterEventId = Number(cursorState.lastAnalysisEventId || 0);
    const eventSessionChanged = eventSessionId
      && cursorState.analysisEventSessionId !== eventSessionId;
    if (eventSessionChanged) {
      afterEventId = 0;
      liveEventFloor = 0;
      // A restarted Agent has an empty in-memory bridge. Re-send the current
      // replay snapshot so an old pending item cannot leave the new Agent idle.
      if (stored.backtestMode && stored.backtestPending) {
        const current = await queryActiveChart(stored.backtestPending?.tabId || null);
        await sendReceivedSnapshot(current.tab, current.received);
      }
      await chrome.storage.local.set({
        analysisEventSessionId: eventSessionId,
        lastAnalysisEventId: 0
      });
    }
    const socket = new WebSocket("ws://127.0.0.1:18765/api/v1/events");
    analysisEventSocket = socket;
    socket.onopen = async () => {
      analysisEventReconnectDelay = 1000;
      if (analysisEventSocket === socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
          type: "subscribe",
          token: bridgeToken,
          afterEventId
        }));
        await startInitialReplayIfNeeded();
      }
    };
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === "keepalive") return;
        if (event.type === "ready") {
          liveEventFloor = Math.max(liveEventFloor, Number(event.latestEventId || 0));
          for (const port of liveStreamPorts) {
            try {
              port.postMessage({ type: "PA_AGENT_LIVE_SNAPSHOT", state: event.liveState || {} });
            } catch {}
          }
          return;
        }
        if (event.type === "llm_stream") {
          if (event.event === "completed" || event.event === "failed") {
            enqueueAnalysisEvent(event);
          }
          if (Number(event.eventId || 0) <= liveEventFloor) return;
          for (const port of liveStreamPorts) {
            try { port.postMessage({ type: "PA_AGENT_LIVE_EVENT", event }); } catch {}
          }
          return;
        }
        if (event.type === "backtest_disabled") {
          socket.close();
          return;
        }
        enqueueAnalysisEvent(event);
      } catch (error) {
        backtestError = `Agent 完成消息格式无效：${error.message}`;
        latest.error = backtestError;
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (analysisEventSocket === socket) analysisEventSocket = null;
      chrome.storage.local.get(["backtestMode"], (values) => {
        if (values.backtestMode || liveStreamPorts.size > 0) scheduleAnalysisEventReconnect();
      });
    };
  } catch (error) {
    backtestError = `连接 Agent 完成消息通道失败：${error.message}`;
    latest.error = backtestError;
    scheduleAnalysisEventReconnect();
  } finally {
    analysisEventConnectInProgress = false;
  }
};

const queryActiveChart = async (tabId = null, timeoutMs = 5000) => {
  let tab;
  try {
    if (tabId !== null) {
      tab = await chrome.tabs.get(tabId);
    } else {
      [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    }
  } catch (error) {
    throw new Error(`无法读取回测使用的 TradingView 标签页：${error.message}`);
  }
  if (!tab?.id || !/^https:\/\/[^/]*tradingview\.com\/chart\//.test(tab.url || "")) {
    throw new Error("请先切换到 TradingView 图表标签页");
  }
  const deadline = Date.now() + timeoutMs;
  let lastReceived = null;
  let lastMessageError = "";
  while (Date.now() < deadline) {
    const remaining = deadline - Date.now();
    const reply = await new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(
        () => finish({ error: "TradingView 图表读取器没有响应" }),
        Math.min(700, remaining)
      );
      chrome.tabs.sendMessage(tab.id, { type: "PA_AGENT_POLL" }, (response) => {
        const error = chrome.runtime.lastError?.message;
        finish(error ? { error } : { response });
      });
    });
    if (reply.error) {
      lastMessageError = reply.error;
    } else if (reply.response?.type === "PA_AGENT_CHART_DATA") {
      lastReceived = reply.response.payload || null;
      if (lastReceived?.bars?.length) return { tab, received: lastReceived };
    }
    const pause = Math.min(150, Math.max(0, deadline - Date.now()));
    if (pause > 0) await new Promise((resolve) => setTimeout(resolve, pause));
  }
  if (lastReceived) {
    throw new Error("TradingView 图表读取器已响应，但尚未收到有效 K 线；请等待图表加载后重试");
  }
  if (lastMessageError) {
    throw new Error(`未收到 TradingView 图表数据；请确认扩展已加载且图表已打开（${lastMessageError}）`);
  }
  throw new Error("尚未收到 TradingView 图表数据；请确认图表已加载并重试");
};

const replayEdgeTime = (received) => {
  const candidate = Number(received?.cutoffTimeCandidate);
  if (Number.isFinite(candidate) && candidate > 0) {
    return candidate < 100000000000 ? candidate * 1000 : candidate;
  }
  const newest = (received?.bars || []).reduce(
    (best, bar) => Number(bar?.time) > Number(best?.time) ? bar : best,
    null
  );
  const time = Number(newest?.time);
  return Number.isFinite(time) && time > 0
    ? (time < 100000000000 ? time * 1000 : time)
    : 0;
};

const replayDataCutoff = (received) => {
  const edgeTime = replayEdgeTime(received);
  const bars = [...(received?.bars || [])]
    .map((bar) => ({
      ...bar,
      time: Number(bar?.time) < 100000000000
        ? Number(bar?.time) * 1000
        : Number(bar?.time)
    }))
    .filter((bar) => Number.isFinite(bar.time) && bar.time > 0)
    .sort((a, b) => a.time - b.time);
  const edgeBar = bars.find((bar) => bar.time === edgeTime)
    || bars.at(-1);
  if (!edgeBar) return { time: edgeTime, source: received?.cutoffSource || "unknown" };

  const prices = ["open", "high", "low", "close"].map((key) => Number(edgeBar[key]));
  const tolerance = Math.max(1e-8, Math.abs(prices[3]) * 1e-10);
  const isFlatPlaceholder = prices.every(Number.isFinite)
    && Math.abs(prices[1] - prices[2]) <= tolerance
    && Math.abs(prices[0] - prices[3]) <= tolerance
    && Math.abs(prices[0] - prices[1]) <= tolerance;
  if (isFlatPlaceholder) {
    const closedBar = [...bars].reverse().find((bar) => bar.time < edgeBar.time);
    if (closedBar) {
      return { time: closedBar.time, source: "backtest-closed-bar" };
    }
  }
  return { time: edgeBar.time, source: received?.cutoffSource || "unknown" };
};

const sendReplayShortcut = async (tabId) => {
  const target = { tabId };
  await new Promise((resolve, reject) => {
    chrome.debugger.attach(target, "1.3", () => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
  const sendKey = (type, key, code, virtualKeyCode, modifiers = 0) => new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
      type,
      key,
      code,
      windowsVirtualKeyCode: virtualKeyCode,
      nativeVirtualKeyCode: virtualKeyCode,
      modifiers
    }, () => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve();
    });
  });
  try {
    // Bar Replay's Shift+Right shortcut, dispatched as browser input so the
    // TradingView key handler receives a trusted event.
    // CDP's modifier bitmask describes the modifiers pressed for each event,
    // including the Shift keydown itself.
    await sendKey("rawKeyDown", "Shift", "ShiftLeft", 16, 8);
    await sendKey("rawKeyDown", "ArrowRight", "ArrowRight", 39, 8);
    await sendKey("keyUp", "ArrowRight", "ArrowRight", 39, 8);
    await sendKey("keyUp", "Shift", "ShiftLeft", 16);
  } catch (error) {
    throw new Error(`发送 TradingView 回放快捷键失败：${error.message}`);
  } finally {
    await new Promise((resolve) => {
      chrome.debugger.detach(target, () => resolve());
    });
  }
};

const clickReplayNext = async (tabId) => new Promise((resolve, reject) => {
  chrome.tabs.sendMessage(tabId, { type: "PA_AGENT_REPLAY_NEXT" }, (result) => {
    if (chrome.runtime.lastError) {
      reject(new Error("无法控制 TradingView 回放工具栏；请刷新图表页后重试"));
      return;
    }
    if (!result?.ok) {
      const controls = (result?.buttons || [])
        .map((item) => item.label)
        .filter(Boolean)
        .slice(0, 8);
      const detail = controls.length ? `；工具栏控件：${controls.join(" | ")}` : "";
      reject(new Error(`${result?.error || "点击 TradingView 下一根 K 线失败"}${detail}`));
      return;
    }
    if (result.method !== "keyboard") {
      resolve(result);
      return;
    }
    sendReplayShortcut(tabId)
      .then(() => resolve({ ...result, method: "trusted-keyboard" }))
      .catch(reject);
  });
});

const advanceReplayAndSync = async (knownChart = null, tabId = null) => {
  const { tab, received: before } = knownChart || await queryActiveChart(tabId);
  if (String(before.resolution) !== "5") {
    throw new Error("回测模式只支持 TradingView 5 分钟图表");
  }
  const previousEdge = replayEdgeTime(before);
  const previousCutoff = replayDataCutoff(before).time;
  if (!previousEdge || !previousCutoff) {
    throw new Error("未能读取当前回放 K 线位置");
  }
  const clickResult = await clickReplayNext(tab.id);

  let advanced = null;
  let observedEdge = previousEdge;
  let observedCutoff = previousCutoff;
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 300));
    try {
      const current = await queryActiveChart(tab.id, 400);
      observedEdge = replayEdgeTime(current.received);
      observedCutoff = replayDataCutoff(current.received).time;
      if (
        String(current.received.resolution) === "5"
        && observedCutoff > previousCutoff
      ) {
        advanced = current;
        break;
      }
    } catch (_) {
      // TradingView may briefly replace chart state while replay advances.
    }
  }
  if (!advanced) {
    const edgeText = (value) => value > 0
      ? new Date(value).toISOString()
      : "未读取到";
    const method = clickResult?.method || "未知方式";
    const button = String(clickResult?.button || "未返回控件名称").slice(0, 100);
    throw new Error(
      `下一根有效 K 线未同步（等待 10 秒）。点击方式：${method}；控件：${button}；`
      + `点击前图表边缘：${edgeText(previousEdge)}；当前图表边缘：${edgeText(observedEdge)}；`
      + `点击前有效截止棒：${edgeText(previousCutoff)}；当前有效截止棒：${edgeText(observedCutoff)}。`
    );
  }
  const result = await sendReceivedSnapshot(advanced.tab, advanced.received);
  backtestError = "";
  return result;
};

const resumeReplayFromPending = async (pending) => {
  const current = await queryActiveChart(pending.tabId || null);
  const currentCutoff = replayDataCutoff(current.received).time;
  const analyzedCutoff = Number(pending.barTime || 0);
  if (!currentCutoff || !analyzedCutoff) {
    throw new Error("无法恢复回放进度：缺少当前截止棒或已分析截止棒");
  }
  if (currentCutoff > analyzedCutoff) {
    // The chart transition completed before the service worker stopped or the
    // snapshot POST failed. Sync the visible candle instead of stepping twice.
    await sendReceivedSnapshot(current.tab, current.received);
    return;
  }
  if (currentCutoff < analyzedCutoff) {
    throw new Error("TradingView 回放位置已倒退；请先同步当前 K 线再继续");
  }
  await advanceReplayAndSync(current, pending.tabId || null);
};

const sendReceivedSnapshotNow = async (tab, received, explicitHistoryCutoff) => {
  latest = { ...received, receivedAt: Date.now(), error: "" };
  if (!received.bars?.length) throw new Error("尚未收到包含有效 K 线的图表数据包；悬停 OHLC 不会被当作历史数据");
  const stored = await chrome.storage.local.get(["symbolFilter", "timeframeFilter", "historyCutoff", "autoCutoff", "backtestMode"]);
  if (stored.backtestMode && String(received.resolution) !== "5") {
    throw new Error("回测模式只支持 TradingView 5 分钟图表");
  }
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
  let cutoffSource = received.cutoffSource || "unknown";
  if (stored.backtestMode) {
    const replayCutoff = replayDataCutoff(received);
    if (replayCutoff.time > 0) {
      cutoffTime = replayCutoff.time;
      cutoffSource = replayCutoff.source;
    }
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
  let backtestSessionId = null;
  if (stored.backtestMode) {
    await sendBridge("/api/v1/backtest", { enabled: true });
    const bridgeStatus = await refreshBridgeToken();
    backtestSessionId = String(bridgeStatus.backtestSessionId || "");
    if (!backtestSessionId) throw new Error("Agent 未返回有效的回测会话编号");
    if (!cutoffTime) throw new Error("回测快照缺少有效截止棒时间");
  }
  const storedPending = stored.backtestMode
    ? (await chrome.storage.local.get(["backtestPending"])).backtestPending
    : null;
  const sameUnfinishedBar = ["awaiting_analysis", "syncing", "sync_failed"]
    .includes(String(storedPending?.state || ""))
    && storedPending.backtestSessionId === backtestSessionId
    && storedPending.symbol === normalizeEventSymbol(received.symbol)
    && storedPending.timeframe === normalizeEventTimeframe(received.resolution)
    && Number(storedPending.barTime) === Number(cutoffTime);
  const syncId = sameUnfinishedBar ? storedPending.syncId : crypto.randomUUID();
  const pending = stored.backtestMode ? {
    syncId,
    backtestSessionId,
    symbol: normalizeEventSymbol(received.symbol),
    timeframe: normalizeEventTimeframe(received.resolution),
    barTime: cutoffTime,
    tabId: tab.id,
    state: "syncing",
    createdAt: Date.now()
  } : null;
  if (pending) {
    // Persist the correlation identity before the Agent can observe the
    // snapshot and start analysis.
    await chrome.storage.local.set({ backtestPending: pending });
  }
  try {
    const acknowledgement = await sendBridge("/api/v1/klines", {
      pageUrl: tab.url,
      symbol: received.symbol,
      resolution: received.resolution,
      source: "tradingview_edge_extension",
      bars: received.bars,
      cutoffTime,
      cutoffSource,
      diagnostics: received.diagnostics,
      syncId,
      backtestSessionId
    });
    if (acknowledgement?.syncId !== syncId) {
      throw new Error(
        acknowledgement?.syncId
          ? "Agent 确认了不同的图表快照编号"
          : "Agent 桥接协议版本过旧或未重启；请先重启 PA Agent"
      );
    }
    if (
      Number(acknowledgement.cutoffTime || 0) !== Number(cutoffTime || 0)
      || (pending && acknowledgement.backtestSessionId !== backtestSessionId)
    ) {
      throw new Error("Agent 确认的回测会话或截止棒与当前快照不一致");
    }
    if (pending) {
      const current = await chrome.storage.local.get(["backtestPending"]);
      if (
        current.backtestPending?.syncId === syncId
        && current.backtestPending?.state === "syncing"
      ) {
        await chrome.storage.local.set({
          backtestPending: {
            ...current.backtestPending,
            state: "awaiting_analysis",
            acknowledgedAt: Date.now()
          }
        });
      }
    }
  } catch (error) {
    if (pending) {
      const current = await chrome.storage.local.get(["backtestPending"]);
      if (
        current.backtestPending?.syncId === syncId
        && current.backtestPending?.state === "syncing"
      ) {
        await chrome.storage.local.set({
          backtestPending: {
            ...current.backtestPending,
            state: "sync_failed",
            syncError: String(error.message || error).slice(0, 500)
          }
        });
      }
    }
    throw error;
  }
  latest = {
    ...latest,
    cutoffTime,
    cutoffSource,
    backtestStatus: stored.backtestMode ? "等待 Agent 分析当前 K 线" : "",
    cutoffMode: stored.backtestMode
      ? "replay"
      : (autoCutoff ? "auto" : (manualCutoff ? "manual" : "latest"))
  };
  if (stored.backtestMode) backtestError = "";
  return latest;
};

const sendReceivedSnapshot = (tab, received, explicitHistoryCutoff) => {
  const task = snapshotSyncQueue
    .catch(() => {})
    .then(() => sendReceivedSnapshotNow(tab, received, explicitHistoryCutoff));
  snapshotSyncQueue = task.catch(() => {});
  return task;
};

const capture = async (explicitHistoryCutoff) => {
  const { tab, received } = await queryActiveChart();
  return sendReceivedSnapshot(tab, received, explicitHistoryCutoff);
};

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type === "PA_AGENT_POPUP_STATE") {
    Promise.all([
      chrome.storage.local.get(["enabled", "autoCutoff", "backtestMode"]),
      fetch(`${BRIDGE}/api/v1/status`, { cache: "no-store" }).then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const value = await response.json();
        bridgeToken = value.token || bridgeToken;
        return { status: value, error: "" };
      }).catch((error) => ({ status: null, error: error.message }))
    ]).then(([values, bridge]) => respond({
      enabled: !!values.enabled,
      backtestMode: !!values.backtestMode,
      autoCutoff: values.autoCutoff !== false,
      latest,
      bridge: bridge.status,
      bridgeError: bridge.error
    }));
    return true;
  }
  if (message?.type === "PA_AGENT_SET_ENABLED") {
    enabled = !!message.enabled;
    (async () => {
      const values = await chrome.storage.local.get(["backtestMode"]);
      const backtestMode = enabled && !!values.backtestMode;
      await chrome.storage.local.set({
        enabled,
        backtestMode,
        ...(backtestMode ? {} : { backtestPending: null })
      });
      if (values.backtestMode && !backtestMode) {
        await sendBridge("/api/v1/backtest", { enabled: false }).catch(() => {});
        stopAnalysisEventStream();
      }
      if (enabled) {
        if (backtestMode) {
          chrome.alarms.clear(ALARM);
          connectAnalysisEventStream();
        } else {
          chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
          capture().catch((error) => { latest.error = error.message; });
        }
      } else {
        chrome.alarms.clear(ALARM);
        stopAnalysisEventStream();
      }
      respond({ enabled, backtestMode });
    })();
    return true;
  }
  if (message?.type === "PA_AGENT_SET_BACKTEST") {
    const backtestMode = !!message.enabled;
    (async () => {
      try {
        if (backtestMode) {
          backtestError = "";
          await chrome.storage.local.set({
            backtestMode: true,
            enabled: true,
            backtestPending: null,
            backtestFailureStreak: 0
          });
          enabled = true;
          chrome.alarms.clear(ALARM);
          await sendBridge("/api/v1/backtest", { enabled: true });
          connectAnalysisEventStream();
          const initial = await startInitialReplayIfNeeded();
          respond({
            ok: true,
            backtestMode: true,
            ...(initial.ok ? {} : { waiting: true, error: initial.error })
          });
          return;
        }
        backtestError = "";
        latest.error = "";
        await chrome.storage.local.set({
          backtestMode: false,
          backtestPending: null,
          backtestFailureStreak: 0
        });
        stopAnalysisEventStream();
        await sendBridge("/api/v1/backtest", { enabled: backtestMode });
        const values = await chrome.storage.local.get(["enabled"]);
        enabled = !!values.enabled;
        if (enabled) {
          chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
        } else {
          chrome.alarms.clear(ALARM);
        }
        respond({ ok: true, backtestMode });
      } catch (error) {
        backtestError = error.message;
        latest.error = error.message;
        if (backtestMode) scheduleAnalysisEventReconnect();
        respond({ ok: false, error: error.message, backtestMode });
      }
    })();
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

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "pa-agent-live-stream") return;
  liveStreamPorts.add(port);
  connectAnalysisEventStream();
  port.onDisconnect.addListener(() => {
    liveStreamPorts.delete(port);
    chrome.storage.local.get(["backtestMode"], (values) => {
      if (!values.backtestMode && liveStreamPorts.size === 0) stopAnalysisEventStream();
    });
  });
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "PA_AGENT_PAGE_READER" && sender.tab?.url?.includes("tradingview.com/chart/")) {
    latest = {
      ...message.payload,
      receivedAt: Date.now(),
      error: backtestError || ""
    };
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
      chrome.storage.local.get(["enabled", "backtestMode"], (values) => {
        enabled = !!values.enabled;
        if (!enabled) return;
        // An automatic transition is already polled and synchronized by
        // advanceReplayAndSync(). Manual TradingView replay controls do not
        // use that path, so forward their changed edge directly to the Agent.
        if (values.backtestMode && backtestAdvanceInProgress) return;
        sendReceivedSnapshot({ id: sender.tab.id, url: sender.tab.url }, message.payload)
          .then(() => {
            if (values.backtestMode) {
              latest.backtestStatus = "手动推进的 K 线已同步，等待 Agent 分析";
            }
          })
          .catch((error) => {
            latest.error = error.message;
            if (values.backtestMode) backtestError = error.message;
          });
      });
    }
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== ALARM) return;
  chrome.storage.local.get(["backtestMode"], (values) => {
    if (values.backtestMode) {
      connectAnalysisEventStream();
    } else {
      capture().catch((error) => { latest.error = error.message; });
    }
  });
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

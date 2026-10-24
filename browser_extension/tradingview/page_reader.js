(() => {
  "use strict";

  // Read only parsed chart-series messages. Never cancel, rewrite, or forward a
  // TradingView frame; the extension emits validated candle rows to its relay.
  const EVENT = "pa-agent-tv-chart-snapshot";
  const MAX_BARS = 10000;
  const bars = new Map();
  const stats = { packetsSeen: 0, chartPackets: 0, rejected: 0, duplicates: 0 };
  let config = { symbol: "", resolution: "" };
  let lastSignature = "";
  let latestUpdateTime = 0;
  let cutoffSource = "none";
  let legendObserver = null;
  let observedLegend = null;
  let legendSyncTimer = 0;
  let pointerInsideChart = false;

  const symbolFromPage = () => {
    const label = [...document.querySelectorAll("[aria-label]")]
      .map((node) => node.getAttribute("aria-label") || "")
      .find((text) => /的图表/.test(text) && /!|:/.test(text));
    if (label) return label.split(/[，,]/)[0].trim();
    return document.querySelector("#header-toolbar-symbol-search")?.textContent?.trim() || "";
  };

  const resolutionFromPage = () => {
    const selected = document.querySelector('[role="radio"][aria-checked="true"], [role="radio"][aria-pressed="true"]')
      || [...document.querySelectorAll('[aria-checked="true"], [aria-pressed="true"]')]
        .find((node) => /分钟|小时|日|周|月/.test(node.getAttribute("aria-label") || node.textContent || ""));
    const label = selected?.getAttribute("aria-label") || selected?.textContent || "";
    const minute = label.match(/(\d+)\s*(分钟|分|min)/i);
    if (minute) return String(Number(minute[1]));
    const hour = label.match(/(\d+)\s*小时/);
    if (hour) return String(Number(hour[1]) * 60);
    if (/日/.test(label)) return "D";
    if (/周/.test(label)) return "W";
    if (/月/.test(label)) return "M";
    return "";
  };

  const refreshConfig = () => {
    const symbol = symbolFromPage();
    const resolution = resolutionFromPage();
    if (!symbol || !resolution) return false;
    if (config.symbol && (symbol !== config.symbol || resolution !== config.resolution)) {
      bars.clear();
      lastSignature = "";
      latestUpdateTime = 0;
      cutoffSource = "none";
    }
    config = { symbol, resolution };
    return true;
  };

  const parseFramedMessages = (raw) => {
    if (typeof raw !== "string") return [];
    const parsed = [];
    let rest = raw;
    while (rest.startsWith("~m~")) {
      const delimiter = rest.indexOf("~m~", 3);
      if (delimiter < 0) break;
      const length = Number(rest.slice(3, delimiter));
      if (!Number.isInteger(length) || length < 0) break;
      const start = delimiter + 3;
      const body = rest.slice(start, start + length);
      if (body.length !== length) break;
      rest = rest.slice(start + length);
      try {
        const value = JSON.parse(body);
        if (value && typeof value === "object") parsed.push(value);
      } catch (_) { /* heartbeat/control frames are not JSON candle data */ }
    }
    if (!parsed.length && raw.trim().startsWith("{")) {
      try { parsed.push(JSON.parse(raw)); } catch (_) { /* ignore non-JSON messages */ }
    }
    return parsed;
  };

  const isChartDataMessage = (message) => {
    const name = String(message?.m || message?.method || "").toLowerCase();
    return name === "timescale_update" || name === "du" || name === "series_update";
  };

  const collectSeriesRows = (node, output, depth = 0) => {
    if (!node || depth > 9) return;
    if (Array.isArray(node)) {
      for (let index = 0; index < node.length; index += 1) {
        if (/^sds_\d+$/.test(String(node[index])) && node[index + 1] && typeof node[index + 1] === "object") {
          collectSeriesRows({ [node[index]]: node[index + 1] }, output, depth + 1);
        }
        collectSeriesRows(node[index], output, depth + 1);
      }
      return;
    }
    if (typeof node !== "object") return;
    for (const [key, value] of Object.entries(node)) {
      if (/^sds_\d+$/.test(key) && value && typeof value === "object") {
        const rows = value.s || value.bars || value.data || [];
        if (Array.isArray(rows)) {
          for (const row of rows) {
            const raw = Array.isArray(row) ? row : row?.v ?? row?.value;
            if (Array.isArray(raw) && raw.length >= 6) output.push(raw.slice(0, 6));
          }
        }
      }
      if (value && typeof value === "object") collectSeriesRows(value, output, depth + 1);
    }
  };

  const acceptRows = (rows) => {
    if (!refreshConfig()) return 0;
    let packetMaxTime = 0;
    for (const row of rows) {
      const [rawTime, rawOpen, rawHigh, rawLow, rawClose, rawVolume] = row;
      let time = Number(rawTime);
      const [open, high, low, close, volume] = [rawOpen, rawHigh, rawLow, rawClose, rawVolume].map(Number);
      if (!Number.isFinite(time) || ![open, high, low, close, volume].every(Number.isFinite)) {
        stats.rejected += 1;
        continue;
      }
      if (time < 100000000000) time *= 1000;
      time = Math.trunc(time);
      if (time <= 0 || low <= 0 || high < low || open < low || open > high || close < low || close > high || volume < 0) {
        stats.rejected += 1;
        continue;
      }
      if (bars.has(time)) stats.duplicates += 1;
      bars.set(time, { time, open, high, low, close, volume });
      packetMaxTime = Math.max(packetMaxTime, time);
    }
    while (bars.size > MAX_BARS) bars.delete(bars.keys().next().value);
    return packetMaxTime;
  };

  const parseLegendNumber = (value) => {
    let text = String(value || "").trim();
    if (!text) return null;
    const multiplier = /K$|千$/i.test(text) ? 1_000
      : /M$|百万$/i.test(text) ? 1_000_000
      : /B$|十亿$/i.test(text) ? 1_000_000_000
      : 1;
    text = text.replace(/[,\s\u00a0\u202f]/g, "").replace(/[KMB千百万十亿]+$/i, "");
    const number = Number(text);
    return Number.isFinite(number) ? number * multiplier : null;
  };

  const findLegend = () => (
    document.querySelector('[data-qa-id="legend-series-item"]')
    || document.querySelector('[data-qa-id="legend"]')
  );

  const readLegendOhlcv = () => {
    const legend = findLegend();
    if (!legend) return null;
    const values = new Map();
    for (const item of legend.querySelectorAll('[data-test-id-value-title]')) {
      const title = String(item.getAttribute("data-test-id-value-title") || "").trim();
      const value = item.querySelector('[class*="valueValue"]')?.textContent || "";
      if (title) values.set(title, value);
    }
    const pick = (...titles) => {
      for (const title of titles) {
        if (values.has(title)) return parseLegendNumber(values.get(title));
      }
      return null;
    };
    const open = pick("开=", "開=", "O=", "O", "Open");
    const high = pick("高=", "H=", "H", "High");
    const low = pick("低=", "L=", "L", "Low");
    const close = pick("收=", "C=", "C", "Close", "Last");
    const volume = pick("成交量", "Vol", "Volume");
    if (![open, high, low, close].every(Number.isFinite)) return null;
    return { open, high, low, close, volume };
  };

  const findLegendBarMatch = (legend) => {
    const tolerance = Math.max(1e-6, Math.abs(legend.close) * 1e-6);
    const matches = [];
    for (const bar of bars.values()) {
      if (
        Math.abs(bar.open - legend.open) <= tolerance
        && Math.abs(bar.high - legend.high) <= tolerance
        && Math.abs(bar.low - legend.low) <= tolerance
        && Math.abs(bar.close - legend.close) <= tolerance
      ) {
        matches.push(bar);
      }
    }
    const closest = (candidates) => candidates.reduce((best, bar) => {
      const barDistance = Math.abs(bar.time - (latestUpdateTime || bar.time));
      const bestDistance = Math.abs(best.time - (latestUpdateTime || best.time));
      if (barDistance !== bestDistance) return barDistance < bestDistance ? bar : best;
      if (legend.volume !== null && Number.isFinite(legend.volume)) {
        const barVolumeError = Math.abs(bar.volume - legend.volume);
        const bestVolumeError = Math.abs(best.volume - legend.volume);
        return barVolumeError < bestVolumeError ? bar : best;
      }
      return bar;
    });
    if (matches.length) {
      return {
        bar: matches.length === 1 ? matches[0] : closest(matches),
        needsUpdate: false,
      };
    }

    // TradingView can create the newly active candle with a flat placeholder
    // (O=H=L=C) before the real OHLCV update arrives. Even after the first
    // backfill, later high/low/close changes may not arrive through our packet
    // parser. Match by the stable bar open and keep the active candle updated
    // from the legend.
    const openMatches = [];
    for (const bar of bars.values()) {
      if (Math.abs(bar.open - legend.open) <= tolerance) {
        openMatches.push(bar);
      }
    }
    if (!openMatches.length) return null;
    const eligible = latestUpdateTime
      ? openMatches.filter((bar) => bar.time >= latestUpdateTime)
      : openMatches;
    const candidates = eligible.length ? eligible : openMatches;
    const nearest = latestUpdateTime
      ? candidates.reduce((best, bar) => (
        Math.abs(bar.time - latestUpdateTime) < Math.abs(best.time - latestUpdateTime)
          ? bar
          : best
      ))
      : candidates.reduce((best, bar) => (bar.time > best.time ? bar : best));
    return { bar: nearest, needsUpdate: true };
  };

  const updateCutoffFromLegend = (force = false) => {
    refreshConfig();
    if (!config.symbol || (pointerInsideChart && !force)) return false;
    const legend = readLegendOhlcv();
    if (!legend) return false;
    const match = findLegendBarMatch(legend);
    if (!match) return false;
    const { bar, needsUpdate } = match;
    if (needsUpdate) {
      bars.set(bar.time, {
        time: bar.time,
        open: legend.open,
        high: legend.high,
        low: legend.low,
        close: legend.close,
        volume: legend.volume ?? bar.volume,
      });
    }
    if (bar.time === latestUpdateTime && !needsUpdate) return false;
    latestUpdateTime = bar.time;
    cutoffSource = "legend";
    return true;
  };

  const syncCutoffFromLegend = (force = false) => {
    if (!updateCutoffFromLegend(force)) return false;
    emitSnapshot();
    return true;
  };

  const scheduleLegendSync = () => {
    if (legendSyncTimer) return;
    legendSyncTimer = setTimeout(() => {
      legendSyncTimer = 0;
      syncCutoffFromLegend();
    }, 80);
  };

  const attachLegendObserver = () => {
    const legend = findLegend();
    if (!legend || legend === observedLegend) return;
    legendObserver?.disconnect();
    observedLegend = legend;
    legendObserver = new MutationObserver(scheduleLegendSync);
    legendObserver.observe(legend, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  };

  const emitSnapshot = () => {
    updateCutoffFromLegend();
    refreshConfig();
    const rows = [...bars.values()].sort((a, b) => a.time - b.time);
    const newest = rows.at(-1);
    if (!latestUpdateTime && newest) cutoffSource = "chart_latest";
    const cutoffBar = latestUpdateTime ? bars.get(latestUpdateTime) : newest;
    const signature = [
      config.symbol,
      config.resolution,
      rows.length,
      newest?.time || 0,
      newest?.close || 0,
      latestUpdateTime,
      cutoffBar?.open || 0,
      cutoffBar?.high || 0,
      cutoffBar?.low || 0,
      cutoffBar?.close || 0,
      cutoffBar?.volume || 0,
    ].join("|");
    if (signature === lastSignature) return;
    lastSignature = signature;
    window.dispatchEvent(new CustomEvent(EVENT, { detail: {
      symbol: config.symbol,
      resolution: config.resolution,
      bars: rows,
      cutoffTimeCandidate: latestUpdateTime || newest?.time || 0,
      cutoffSource,
      diagnostics: {
        packetsSeen: stats.packetsSeen,
        chartPackets: stats.chartPackets,
        accepted: rows.length,
        rejected: stats.rejected,
        duplicates: stats.duplicates,
        reader: rows.length ? "tradingview-chart-packets" : "waiting-for-chart-candles"
      }
    }}));
  };

  const readPacket = (data) => {
    stats.packetsSeen += 1;
    if (typeof Blob !== "undefined" && data instanceof Blob) {
      data.text().then(readPacket).catch(() => {});
      return;
    }
    if (data instanceof ArrayBuffer) data = new TextDecoder().decode(data);
    if (ArrayBuffer.isView(data)) data = new TextDecoder().decode(data);
    for (const message of parseFramedMessages(data)) {
      if (!isChartDataMessage(message)) continue;
      stats.chartPackets += 1;
      const rows = [];
      collectSeriesRows(message.p, rows);
      if (rows.length) {
        const packetMaxTime = acceptRows(rows);
        const name = String(message?.m || message?.method || "").toLowerCase();
        // `du`/`series_update` represent the active series edge and may move
        // backwards during Bar Replay. A timescale history batch may only
        // advance the edge; scrolling old history must not rewind the cutoff.
        if (packetMaxTime && (name === "du" || name === "series_update")) {
          latestUpdateTime = packetMaxTime;
          cutoffSource = name;
        } else if (packetMaxTime && packetMaxTime > latestUpdateTime) {
          latestUpdateTime = packetMaxTime;
          cutoffSource = name || "chart_packet";
        }
      }
    }
    emitSnapshot();
  };

  const NativeWebSocket = window.WebSocket;
  if (NativeWebSocket) {
    window.WebSocket = new Proxy(NativeWebSocket, {
      construct(target, args, newTarget) {
        const socket = Reflect.construct(target, args, newTarget);
        socket.addEventListener("message", (event) => readPacket(event.data));
        return socket;
      }
    });
  }

  const isChartTarget = (target) => (
    target instanceof Element
    && !!target.closest('[data-qa-id="pane-widget-chart-gui-wrapper"]')
  );
  document.addEventListener("pointerover", (event) => {
    if (isChartTarget(event.target)) pointerInsideChart = true;
  }, true);
  document.addEventListener("pointerout", (event) => {
    if (
      pointerInsideChart
      && (!event.relatedTarget || !isChartTarget(event.relatedTarget))
    ) {
      pointerInsideChart = false;
    }
  }, true);
  window.addEventListener("blur", () => {
    pointerInsideChart = false;
    scheduleLegendSync();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pointerInsideChart = false;
      scheduleLegendSync();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.shiftKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
      setTimeout(() => syncCutoffFromLegend(true), 160);
    }
  }, true);
  document.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest('[data-name="replay-bottom-toolbar"]')) {
      setTimeout(() => syncCutoffFromLegend(true), 160);
    }
  }, true);

  window.addEventListener("pa-agent-tv-reader-poll", emitSnapshot);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachLegendObserver, { once: true });
  } else {
    attachLegendObserver();
  }
  setInterval(() => {
    attachLegendObserver();
    refreshConfig();
    syncCutoffFromLegend();
    emitSnapshot();
  }, 1500);
})();

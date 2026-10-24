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
    if (!refreshConfig()) return;
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
    }
    while (bars.size > MAX_BARS) bars.delete(bars.keys().next().value);
  };

  const emitSnapshot = () => {
    refreshConfig();
    const rows = [...bars.values()].sort((a, b) => a.time - b.time);
    const newest = rows.at(-1);
    const signature = `${config.symbol}|${config.resolution}|${rows.length}|${newest?.time || 0}|${newest?.close || 0}`;
    if (signature === lastSignature) return;
    lastSignature = signature;
    window.dispatchEvent(new CustomEvent(EVENT, { detail: {
      symbol: config.symbol,
      resolution: config.resolution,
      bars: rows,
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
      if (rows.length) acceptRows(rows);
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

  window.addEventListener("pa-agent-tv-reader-poll", emitSnapshot);
  setInterval(() => { refreshConfig(); emitSnapshot(); }, 1500);
})();

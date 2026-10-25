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

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type !== "PA_AGENT_POLL") return;
  window.dispatchEvent(new Event("pa-agent-tv-reader-poll"));
  // The page reader handles the poll event in the main world and publishes
  // its current snapshot through the window event above. Keep the short
  // settling delay used by the previous relay, then return that snapshot
  // through the request's response channel for reliable retries.
  setTimeout(() => {
    respond({ type: "PA_AGENT_CHART_DATA", payload: latest });
  }, 80);
  return true;
});

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  if (message?.type !== "PA_AGENT_REPLAY_NEXT") return;
  const toolbar = document.querySelector('[data-name="replay-bottom-toolbar"]');
  if (!toolbar) {
    respond({ ok: false, error: "未找到 TradingView 回放工具栏，请先开启 K 线回放并选好起始 K 线" });
    return;
  }
  // TradingView has used plain divs and SVG-backed controls in this toolbar,
  // so the replay action is not always a <button> or role=button. Inspect
  // labeled/data-named descendants too, then click their nearest control.
  const labeled = [...toolbar.querySelectorAll(
    'button,[role="button"],[aria-label],[aria-labelledby],[title],[data-name],[data-qa-id],[data-tooltip],svg'
  )];
  const labeledOnPage = [...document.querySelectorAll(
    'button,[role="button"],[aria-label],[aria-labelledby],[title],[data-name],[data-qa-id],[data-tooltip],svg'
  )];
  const describe = (element) => [
    element.getAttribute("aria-label"),
    element.getAttribute("aria-labelledby"),
    element.getAttribute("title"),
    element.getAttribute("data-name"),
    element.getAttribute("data-qa-id"),
    element.getAttribute("data-tooltip"),
    element.querySelector("title")?.textContent,
    element.textContent
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const isNextStep = (element) => {
    // Include common TradingView internal names such as replay-forward and
    // replay-step-forward, which may be attached to an icon/wrapper element.
    const label = describe(element).toLowerCase();
    if (!label || /(previous|back|rewind|real.?time|pause|暂停|播放|上一|后退|实时)/i.test(label)) {
      return false;
    }
    return /(next|forward|step|replay-forward|下一|前进)/i.test(label);
  };
  const isVisible = (element) => {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    for (let current = element; current && current !== toolbar.parentElement; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) {
        return false;
      }
    }
    return true;
  };
  const isUsableNext = (element, requireNearToolbar = false) => {
    if (!isNextStep(element)) return false;
    const control = element.closest('button,[role="button"]') || element;
    if (
      control.getAttribute("aria-disabled") === "true"
      || control.disabled === true
      || !isVisible(control)
    ) return false;
    if (requireNearToolbar) {
      const toolbarRect = toolbar.getBoundingClientRect();
      const controlRect = control.getBoundingClientRect();
      if (Math.abs((controlRect.top + controlRect.bottom) / 2 - (toolbarRect.top + toolbarRect.bottom) / 2) > 180) {
        return false;
      }
    }
    return true;
  };
  const candidate = labeled.find((element) => isUsableNext(element))
    // Some TradingView layouts render the date selector/exit control in
    // replay-bottom-toolbar while the transport controls are its siblings.
    || labeledOnPage.find((element) => (
      !toolbar.contains(element) && isUsableNext(element, true)
    ));
  if (candidate) {
    const button = candidate.closest('button,[role="button"]') || candidate;
    button.click();
    respond({ ok: true, button: describe(candidate), method: "click" });
    return;
  }

  // Let the service worker send the documented shortcut through browser
  // input. Synthetic DOM keyboard events are untrusted and TradingView may
  // ignore them.
  respond({
    ok: true,
    button: "Shift+→",
    method: "keyboard",
    diagnostic: labeled.map((element) => ({
      label: describe(element),
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute("role"),
      disabled: element.getAttribute("aria-disabled") === "true" || element.disabled === true,
      visible: isVisible(element)
    })).filter((item) => item.label)
  });
});

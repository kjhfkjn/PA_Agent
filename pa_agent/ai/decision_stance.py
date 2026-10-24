"""Trading decision stance profiles for Stage 2 prompt injection."""
from __future__ import annotations

from typing import Literal

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]

STANCE_LABELS_ZH: dict[str, str] = {
    "conservative": "保守",
    "balanced": "均衡",
    "aggressive": "激进",
    "extreme_aggressive": "极度激进",
}

_STANCE_ALIASES: dict[str, DecisionStance] = {
    "conservative": "conservative",
    "保守": "conservative",
    "balanced": "balanced",
    "均衡": "balanced",
    "aggressive": "aggressive",
    "激进": "aggressive",
    "extreme_aggressive": "extreme_aggressive",
    "extreme": "extreme_aggressive",
    "极度激进": "extreme_aggressive",
}


def normalize_stance(value: str | None) -> DecisionStance:
    """Coerce settings/UI value to a known stance id."""
    if not value:
        return "conservative"
    key = str(value).strip().lower()
    if key in _STANCE_ALIASES:
        return _STANCE_ALIASES[key]
    raw = str(value).strip()
    if raw in _STANCE_ALIASES:
        return _STANCE_ALIASES[raw]
    return "conservative"


def stance_label_zh(stance: str | None) -> str:
    """Return Chinese label for UI."""
    return STANCE_LABELS_ZH.get(normalize_stance(stance), "保守")


def build_decision_stance_guidance(stance: str | None) -> str:
    """Return Stage-2-only guidance block for the current trading stance."""
    normalized = normalize_stance(stance)
    label = stance_label_zh(normalized)

    common_rules = (
        "通用约束（各档都必须遵守）：\n"
        "- 仍必须完整输出 decision_trace，按 §9–§11、§14 走适用节点，不得伪造 trace。\n"
        "- 节点 10.3 须基于已拟定的 entry/stop/target 做数值判断；禁止无止损、无目标。\n"
        "- 每笔入场至少要有两条彼此独立、同向且可由 K 线验证的理由，并在 reasoning 或 decision_trace 分别列明；同一结构换名或重复描述只算一条。不足两条则不下单。\n"
        "- 入场方式默认优先止损触发单（阶段二 JSON `order_type=\"突破单\"`），entry 设在有效信号/结构触发棒极点外1跳动。\n"
        "- 限价单仅为少数例外：趋势方向清晰，价格回到前一个仍有效、未回补的价格行为突破缺口，并同时满足两条独立理由、结构止损/目标明确、TP1 RR≥1:1；普通支撑/阻力、EMA、通道边界或一般回撤不授权限价。\n"
        "- 不使用市价单作为常规入场方式；若信号已运行、触发位不清或等待条件未满足，应观望，不改用市价追入。\n"
        "- Brooks 价格行为、可由 K 线直接确认的 ICT 结构（如 FVG、流动性扫取、MSS、位移、订单块）与价格行为突破缺口可作为证据来源；必须分清 FVG、突破缺口、opening gap 与均线 gap_bar，不得猜测数据中看不到的 ICT 背景。\n"
        "- **定价**：先定结构 entry/TP1/TP2，再定结构 stop；RR≥1.0。"
        "若 RR>1.0 程序会自动向外扩 stop（保持 TP 不变）。"
        "RR<1.0 时收紧 stop 或调整 entry，禁止向外扩 stop。禁止缩小 TP 凑 RR。\n"
        "- **§9.0 / §9.0P**：无合格信号棒时填写 §9.0P 检查是否满足上述唯一限价例外；可以判否/等待。止损触发方案不可用时，不得用普通结构位限价替代。\n"
        "- **direction=neutral 约束**：neutral 时 §9.0P **仅允许顺 §2.4**（AIL→只做多侧，"
        "AIS→只做空侧；§2.4=否→默认 wait）；禁止上下边界双向刮头皮。\n"
        "- **方案连续性**：若上一轮可执行方案**未失效**，默认 wait；"
        "同结构位 N 根内禁止反手（详见阶段二「方案连续性」块）。\n"
        "- 只有严格限价例外 setup 的 pending 订单才适用计划型限价 K1 对照：只看 entry 相对 K1.close 方向；影线触及 entry 不算失效。\n"
        "- 完成 10.3 后必须在 decision 中填写 estimated_win_rate（0–100）与 estimated_win_rate_reasoning；"
        "order_type=不下单 时 estimated_win_rate 必须为 null。\n"
        "- **decision 与 trace/terminal 必须一致**："
        "decision_trace 中 10.3=否、terminal.outcome 为 wait/reject、或 §14 判「是」时，"
        "decision.order_type **必须**为「不下单」，"
        "entry/stop/target/order_direction/entry_basis_* 全部为 null，"
        "**禁止**一边写突破单/限价单价格一边在 trace 里判方程不通过。\n"
        "- 有下单 → terminal.outcome=trade；不下单 → outcome=wait 或 reject。\n"
        "- 触犯 §14 硬性禁止项时，各档均须 order_type=不下单，"
        "并在 reasoning 明确写出触犯的条款。\n"
    )

    if normalized == "conservative":
        profile = (
            "【保守】= 当前系统默认裁定标准（与改版前一致）。\n"
            "- §9 入场：优先典型、清晰、收盘确认的一类信号；次优/模糊 setup 默认继续等待。\n"
            "- §9.0P 只可评估前一未回补突破缺口回撤例外；普通边界限价不符合用户风格。方向中性或缺少两条独立理由时默认 wait。\n"
            "- §10：止损必须明确且不过大；10.3 交易者方程边际情况倾向判「否」。"
            "RR 须 ≥1.0 且方程通过。\n"
            "- §14：从严扫描；有疑虑即不下单。\n"
            "- trade_confidence：40–59 或结构存在明显歧义时，优先 order_type=不下单。\n"
            "- 方向中性、独立理由不足两条或结构止损不清时默认观望；靠近支撑/阻力/通道边界本身不构成入场依据。\n"
        )
    elif normalized == "balanced":
        profile = (
            "【均衡】= 在遵守决策树与用户指定交易风格的前提下，比【保守】更愿意执行交易；不得降低入场理由、订单类型或风险回报的硬门槛。\n"
            "- §9.0=否 后填写 §9.0P 检查唯一的突破缺口回撤限价例外；不符合时判否/等待，不得尝试普通边界限价。\n"
            "- §9 入场：除典型信号外，若结构与阶段一 direction/cycle_position 一致，"
            "允许「次优但可执行」的二类 setup（须在 reason 中写明为何仍值得做）。\n"
            "- 宽通道/区间边界不自动授权入场；若信号质量弱或无信号棒，只能在严格限价例外成立时等待限价触发，否则观望。\n"
            "- §10：10.3 边际可通过时，若方程为正且结构清晰，可判「是」；"
            "须在 trade_confidence_reasoning 写明假设。\n"
            "- §14：仅明确触犯才不下单；不要因「不够完美」单独放弃。\n"
            "- trade_confidence：35–49 且入场逻辑完整时，可给出下单方案（在 reasoning 说明风险克制）。\n"
            "- 顺势通道/尖峰延续、区间边界反弹仍遵守止损触发单优先和两条独立理由要求；本档只影响边际情形的风险容忍度。\n"
        )
    elif normalized == "aggressive":
        profile = (
            "【激进】= 在遵守决策树的前提下，比【均衡】更愿意执行交易。\n"
            "- §9 入场：结构方向一致时，可接受更早、更不完美的入场触发；"
            "须在 reason 说明接受的瑕疵与失效条件。\n"
            "- §10：10.3 在 entry/stop/target 已明确时，若方程略偏边际但方向与周期位置一致，"
            "可判「是」；须在 reasoning 强调风险克制。\n"
            "- §14：仅硬性禁止项触发不下单；不要因为「理想目标位更远」而放弃可执行方案。\n"
            "- trade_confidence：30–44 且逻辑链完整时，仍可输出具体下单类型；"
            "用 watch_points / invalidation_condition 补足不确定性。\n"
            "- 趋势延续、突破回踩、区间边界：只要阶段一 gate_result=proceed，"
            "应主动寻找可下单方案，而不是先找理由观望。\n"
        )
    else:
        profile = (
            "【极度激进】= 在完整满足用户交易风格（两条独立理由、顺向触发方案、TP1 RR≥1:1）"
            "且 **10.3 可通过、未触犯 §14** 时，优先执行可行方案；不得因犹豫放弃合格 setup，"
            "但禁止为了强制交易而虚构方向、理由或价格。\n"
            "- **拟下单路径**（仅当 10.3 判「是」且 terminal.outcome=trade 时生效）："
            "order_type 只从「限价单」「突破单」中选择，"
            "order_direction 为「做多」或「做空」，"
            "entry/stop/target 为有效数值。\n"
            "- **不下单路径**（10.3=否、方程不达标、或 §14=是）："
            "order_type=「不下单」，所有价格字段为 null；"
            "terminal.outcome=reject 或 wait；"
            "若主因是方程不通过，terminal.node_id 建议为 10.3。\n"
            "- 仅交易阶段一/近期结构允许的方向；direction=neutral 且无法建立清晰方向或两条独立依据时，必须 wait，不强行二选一。\n"
            "- §9：信号不完美也可判「是」，但须在 reason 写明接受的瑕疵。\n"
            "- §10.3：突破单须先用依据 K 的极值±1跳动写出 entry，再写 stop/target，用这三价做检验；"
            "RR 须 ≥1.0 且胜率×回报>败率×风险，否则**必须**判「否」并走不下单路径，"
            "不得保留具体下单价格；trace 中的风险/回报数字须与 decision 三价一致。\n"
            "- 突破单不可行时，仅当严格限价例外的所有条件均成立才评估限价；否则等待或不下单。\n"
            "- 可在 10.3 通过时合理估算胜率（约 45–55%），"
            "边际通过须在 trade_confidence_reasoning 说明。\n"
            "- trade_confidence 可低至 25–40，但仅适用于 **10.3=是** 的下单方案。\n"
        )

    return (
        f"## 交易倾向（当前：{label} / {normalized}）\n\n"
        f"{common_rules}\n"
        f"{profile}\n"
        "请在 decision.reasoning 与 trade_confidence_reasoning 中体现本档位如何影响最终裁定。"
    )

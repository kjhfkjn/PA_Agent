"""Ordered, session-only history of completed agent decisions."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class DecisionHistoryPanel(QWidget):
    """Show completed decisions in arrival order; clearing affects this view only."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = 0
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("决策结果记录")
        title.setObjectName("toolbarTitle")
        header.addWidget(title)
        header.addStretch(1)
        self._clear_button = QPushButton("清除界面")
        self._clear_button.setToolTip("仅清除本窗口显示的决策卡片，不删除已保存的分析记录或日志")
        self._clear_button.clicked.connect(self.clear)
        header.addWidget(self._clear_button)
        layout.addLayout(header)

        hint = QLabel("按分析完成顺序显示。本列表仅保存在当前界面中。")
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(2, 2, 2, 2)
        self._cards.setSpacing(8)
        self._empty = QLabel("暂无决策结果")
        self._empty.setObjectName("mutedLabel")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cards.addWidget(self._empty)
        self._cards.addStretch(1)
        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll, 1)

    def add_decision(
        self,
        decision: dict[str, Any],
        *,
        symbol: str = "",
        timeframe: str = "",
    ) -> None:
        inner = decision.get("decision")
        inner = inner if isinstance(inner, dict) else {}
        self._count += 1
        if self._empty is not None:
            self._cards.removeWidget(self._empty)
            self._empty.deleteLater()
            self._empty = None

        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        title = QLabel(
            f"#{self._count}  {symbol or '未知品种'} {timeframe}  ·  {when}"
        )
        title.setObjectName("toolbarTitle")
        title.setWordWrap(True)
        card_layout.addWidget(title)

        order = str(inner.get("order_type") or "未给出")
        direction = str(inner.get("order_direction") or "")
        summary = QLabel(f"最终决策：{direction}{order}")
        summary.setStyleSheet("font-size: 15px; font-weight: 600;")
        summary.setWordWrap(True)
        card_layout.addWidget(summary)

        levels = []
        for label, key in (("入场", "entry_price"), ("止损", "stop_loss_price"), ("止盈", "take_profit_price")):
            value = inner.get(key)
            if value not in (None, ""):
                levels.append(f"{label} {value}")
        if levels:
            prices = QLabel("  ·  ".join(levels))
            prices.setObjectName("mutedLabel")
            prices.setWordWrap(True)
            card_layout.addWidget(prices)

        reasoning = str(inner.get("reasoning") or decision.get("reasoning") or "").strip()
        if reasoning:
            reason_label = QLabel(reasoning)
            reason_label.setWordWrap(True)
            card_layout.addWidget(reason_label)

        trace = decision.get("decision_trace") or []
        if isinstance(trace, list) and trace:
            steps = []
            for item in trace:
                if not isinstance(item, dict):
                    continue
                node = str(item.get("node_id") or "").strip()
                question = str(item.get("question") or "").strip()
                answer = str(item.get("answer") or "").strip()
                text = " ".join(part for part in (node, question, answer) if part)
                if text:
                    steps.append(text)
            if steps:
                trace_label = QLabel("决策路径：\n" + "\n".join(
                    f"{index}. {step}" for index, step in enumerate(steps, 1)
                ))
                trace_label.setObjectName("mutedLabel")
                trace_label.setWordWrap(True)
                card_layout.addWidget(trace_label)

        # Keep the stretch at the end; insert before it to preserve chronological order.
        self._cards.insertWidget(self._cards.count() - 1, card)
        self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        )

    def clear(self) -> None:
        """Remove cards from this widget without touching persisted records or logs."""
        while self._cards.count() > 0:
            item = self._cards.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._count = 0
        self._empty = QLabel("暂无决策结果")
        self._empty.setObjectName("mutedLabel")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cards.addWidget(self._empty)
        self._cards.addStretch(1)

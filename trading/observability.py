"""结构化运行观测日志。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from config.settings import TRADE_EVENTS_FILE


class EventRecorder:
    """JSONL 事件记录器。

    盘中运行需要可追溯的信号、风控和成交流水；JSONL 便于追加写入和后续分析。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or TRADE_EVENTS_FILE
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        """追加一条结构化事件。"""
        event = {
            "event_type": event_type,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "payload": payload,
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

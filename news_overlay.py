"""新闻事件研判覆盖层(容器侧)。

外部生成器每日产出 data/news_overlay/YYYYMMDD.json,盘中可能同路径覆盖更新。
本模块只负责读取与严格校验:文件缺失、JSON 损坏或结构不符时一律返回 None,
绝不抛异常,也不影响既有策略路径。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("news_overlay")

OVERLAY_DIR_NAME = "news_overlay"
VALID_ACTIONS: frozenset[str] = frozenset({"risk_sell", "boost", "watch"})
# watch 与置信度不足 0.7 的研判不产生任何交易动作。
MIN_ACTION_CONFIDENCE = 0.7


@dataclass(frozen=True)
class NewsVerdict:
    """单只标的的新闻研判结论。"""

    code: str
    name: str
    action: str
    confidence: float
    reason: str
    sources: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        """返回研判是否达到可产生交易动作的门槛。"""
        return self.action != "watch" and self.confidence >= MIN_ACTION_CONFIDENCE


@dataclass(frozen=True)
class NewsOverlay:
    """当日研判文件解析结果,已按 code 去重。"""

    generated_at: str
    verdicts: tuple[NewsVerdict, ...]

    @property
    def by_code(self) -> dict[str, NewsVerdict]:
        """返回按六位代码索引的研判表。"""
        return {verdict.code: verdict for verdict in self.verdicts}

    def actionable_codes(self, action: str) -> frozenset[str]:
        """返回指定动作且置信度达标的六位代码集合。"""
        return frozenset(
            verdict.code
            for verdict in self.verdicts
            if verdict.action == action and verdict.is_actionable
        )


def news_overlay_path(date_str: str, root_dir: Path | str) -> Path:
    """返回指定交易日的研判文件路径。"""
    return Path(root_dir).resolve() / "data" / OVERLAY_DIR_NAME / f"{date_str}.json"


def _parse_generated_at(value: Any) -> str:
    """校验 generated_at 为可解析的 ISO 时间字符串,非法时抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("generated_at 不能为空")
    text = value.strip()
    # Python 3.10 的 fromisoformat 不识别 Z 后缀,统一替换成 +00:00。
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    datetime.fromisoformat(normalized)
    return text


def _parse_verdict(entry: Any) -> NewsVerdict | None:
    """校验单条研判;字段缺失、类型不符或 code 非 6 位数字时丢弃该条。"""
    if not isinstance(entry, dict):
        LOGGER.debug("新闻研判条目不是对象,已丢弃: %r", entry)
        return None
    code = entry.get("code")
    if not isinstance(code, str):
        LOGGER.debug("新闻研判 code 不是字符串,已丢弃: %r", code)
        return None
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        LOGGER.debug("新闻研判 code 不是 6 位数字,已丢弃: %r", code)
        return None
    action = entry.get("action")
    if action not in VALID_ACTIONS:
        LOGGER.debug("新闻研判 action 非法,已丢弃: code=%s action=%r", code, action)
        return None
    confidence = entry.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        LOGGER.debug("新闻研判 confidence 不是数值,已丢弃: code=%s", code)
        return None
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        LOGGER.debug(
            "新闻研判 confidence 超出 [0,1],已丢弃: code=%s value=%s", code, confidence
        )
        return None
    name = entry.get("name")
    if not isinstance(name, str):
        LOGGER.debug("新闻研判 name 缺失或类型不符,已丢弃: code=%s", code)
        return None
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        LOGGER.debug("新闻研判 reason 缺失或为空,已丢弃: code=%s", code)
        return None
    sources_raw = entry.get("sources")
    if not isinstance(sources_raw, list):
        LOGGER.debug("新闻研判 sources 缺失或不是列表,已丢弃: code=%s", code)
        return None
    sources = tuple(
        source for source in sources_raw if isinstance(source, str) and source.strip()
    )
    return NewsVerdict(
        code=code,
        name=name.strip() or code,
        action=action,
        confidence=confidence,
        reason=reason.strip(),
        sources=sources,
    )


def load_news_overlay(date_str: str, root_dir: Path | str) -> NewsOverlay | None:
    """读取并严格校验当日研判文件;缺失或损坏时返回 None,不抛异常。

    同一 code 出现多条研判时保留 confidence 最高的一条。
    """
    if not isinstance(date_str, str) or len(date_str) != 8 or not date_str.isdigit():
        LOGGER.warning("新闻研判日期参数必须为 YYYYMMDD: %r", date_str)
        return None
    path = news_overlay_path(date_str, root_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # 每日文件可能尚未生成,属正常情况,不打扰 INFO 级日志。
        LOGGER.debug("新闻研判文件不存在,跳过: %s", path)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOGGER.warning("新闻研判文件不是合法 JSON,已忽略: %s error=%s", path, exc)
        return None
    if not isinstance(payload, dict):
        LOGGER.warning("新闻研判文件顶层不是对象,已忽略: %s", path)
        return None
    try:
        generated_at = _parse_generated_at(payload.get("generated_at"))
    except ValueError as exc:
        LOGGER.warning("新闻研判文件 generated_at 非法,已忽略: %s error=%s", path, exc)
        return None
    verdicts_raw = payload.get("verdicts")
    if not isinstance(verdicts_raw, list):
        LOGGER.warning("新闻研判文件缺少 verdicts 列表,已忽略: %s", path)
        return None
    best: dict[str, NewsVerdict] = {}
    dropped = 0
    for entry in verdicts_raw:
        verdict = _parse_verdict(entry)
        if verdict is None:
            dropped += 1
            continue
        existing = best.get(verdict.code)
        # 重复 code 只保留 confidence 最高的一条,并列时保留先出现的。
        if existing is None or verdict.confidence > existing.confidence:
            best[verdict.code] = verdict
    if dropped:
        LOGGER.warning("新闻研判文件丢弃 %d 条非法条目: %s", dropped, path)
    overlay = NewsOverlay(generated_at=generated_at, verdicts=tuple(best.values()))
    LOGGER.info(
        "新闻研判文件已加载: %s generated_at=%s verdicts=%d",
        path,
        overlay.generated_at,
        len(overlay.verdicts),
    )
    return overlay

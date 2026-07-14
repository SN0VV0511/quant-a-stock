"""A 股官方交易日历。

交易日判断是下单安全边界。远端日历不可用时只能使用此前成功落盘且覆盖目标
日期的日历，不能把普通工作日直接当成交易日，否则法定节假日会误触发交易。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

_CALENDAR_PATH = Path(
    os.getenv(
        "TRADING_CALENDAR_CACHE_PATH",
        os.path.join(BASE_DIR, "data", "cache", "trading_calendar.json"),
    )
).expanduser()


class TradingCalendarUnavailableError(RuntimeError):
    """目标日期没有可验证的交易所日历数据。"""


@dataclass(frozen=True)
class TradingCalendarSnapshot:
    """一次已验证的交易日历快照。"""

    trading_days: frozenset[str]
    first_date: str
    last_date: str
    source: str
    generated_at: str

    def covers(self, date_text: str) -> bool:
        """判断快照覆盖目标自然日。"""
        return self.first_date <= date_text <= self.last_date


_calendar_cache: TradingCalendarSnapshot | None = None


def _normalize_date(date_str: str) -> str:
    """校验并归一化日期为 ``YYYYMMDD``。"""
    normalized = date_str.replace("-", "").strip()
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError(f"日期必须为 YYYYMMDD: {date_str}")
    datetime.strptime(normalized, "%Y%m%d")
    return normalized


def _build_snapshot(
    days: set[str], *, source: str, generated_at: str
) -> TradingCalendarSnapshot:
    """从日期集合构建不可变快照。"""
    if not days:
        raise TradingCalendarUnavailableError("交易日历为空")
    ordered = sorted(days)
    return TradingCalendarSnapshot(
        trading_days=frozenset(ordered),
        first_date=ordered[0],
        last_date=ordered[-1],
        source=source,
        generated_at=generated_at,
    )


def _read_local_calendar() -> TradingCalendarSnapshot | None:
    """读取此前成功写入的交易日历，损坏文件会明确记录并忽略。"""
    if not _CALENDAR_PATH.exists():
        return None
    try:
        payload = json.loads(_CALENDAR_PATH.read_text(encoding="utf-8"))
        raw_days = payload.get("trading_days", [])
        if not isinstance(raw_days, list):
            raise ValueError("trading_days 必须是数组")
        days = {_normalize_date(str(value)) for value in raw_days}
        return _build_snapshot(
            days,
            source=str(payload.get("source") or "local-cache"),
            generated_at=str(payload.get("generated_at") or "unknown"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("本地交易日历损坏: %s", exc)
        return None


def _write_local_calendar(snapshot: TradingCalendarSnapshot) -> None:
    """原子保存远端交易日历，避免进程中断留下半截 JSON。"""
    _CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source": snapshot.source,
        "generated_at": snapshot.generated_at,
        "first_date": snapshot.first_date,
        "last_date": snapshot.last_date,
        "trading_days": sorted(snapshot.trading_days),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=_CALENDAR_PATH.parent,
            delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(_CALENDAR_PATH)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _load_remote_calendar() -> TradingCalendarSnapshot:
    """从 AKShare 官方交易日接口加载完整可用区间。"""
    import akshare as ak
    import pandas as pd

    frame = ak.tool_trade_date_hist_sina()
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        raise TradingCalendarUnavailableError("AKShare 返回空交易日历")
    values = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    days = set(values.dt.strftime("%Y%m%d").tolist())
    return _build_snapshot(
        days,
        source="AKShare.tool_trade_date_hist_sina",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def _load_trading_calendar(*, force_refresh: bool = False) -> TradingCalendarSnapshot:
    """加载交易日历；远端失败时仅回退到覆盖目标日期的本地快照。"""
    global _calendar_cache
    if _calendar_cache is not None and not force_refresh:
        return _calendar_cache

    try:
        snapshot = _load_remote_calendar()
        _calendar_cache = snapshot
        try:
            _write_local_calendar(snapshot)
        except OSError as exc:
            logger.warning("交易日历落盘失败，但本次内存快照仍可使用: %s", exc)
        logger.info(
            "交易日历加载完成: %d 个交易日，覆盖 %s-%s",
            len(snapshot.trading_days),
            snapshot.first_date,
            snapshot.last_date,
        )
        return snapshot
    except Exception as exc:
        local = _read_local_calendar()
        if local is not None:
            _calendar_cache = local
            logger.warning("远端交易日历加载失败，使用本地快照: %s", exc)
            return local
        raise TradingCalendarUnavailableError(
            "交易日历不可用且没有可验证的本地快照，已停止交易判断"
        ) from exc


def reset_calendar_cache() -> None:
    """清空进程内缓存，供运维刷新和单元测试使用。"""
    global _calendar_cache
    _calendar_cache = None


def is_trading_day(date_str: str) -> bool:
    """判断目标日期是否为 A 股交易日。

    周末可直接确定为休市；工作日必须由交易日历快照覆盖，否则抛出异常并让
    上层停止交易，而不是猜测。
    """
    date_text = _normalize_date(date_str)
    current = datetime.strptime(date_text, "%Y%m%d")
    if current.weekday() >= 5:
        return False
    snapshot = _load_trading_calendar()
    if not snapshot.covers(date_text):
        raise TradingCalendarUnavailableError(
            f"交易日历仅覆盖 {snapshot.first_date}-{snapshot.last_date}，"
            f"无法验证 {date_text}"
        )
    return date_text in snapshot.trading_days


def is_a_stock_holiday(date_str: str) -> bool:
    """兼容旧调用：返回目标日期是否休市。"""
    return not is_trading_day(date_str)


def previous_trading_day(date_str: str, *, max_lookback_days: int = 20) -> str:
    """返回严格早于目标日期的最近交易日。"""
    if max_lookback_days <= 0:
        raise ValueError("交易日回看天数必须大于 0")
    cursor = datetime.strptime(_normalize_date(date_str), "%Y%m%d") - timedelta(days=1)
    for _ in range(max_lookback_days):
        candidate = cursor.strftime("%Y%m%d")
        if is_trading_day(candidate):
            return candidate
        cursor -= timedelta(days=1)
    raise TradingCalendarUnavailableError(
        f"{date_str} 前 {max_lookback_days} 天内未找到可验证交易日"
    )


def next_trading_day(date_str: str, *, max_lookahead_days: int = 20) -> str:
    """返回严格晚于目标日期的下一个交易日。"""
    if max_lookahead_days <= 0:
        raise ValueError("交易日前瞻天数必须大于 0")
    cursor = datetime.strptime(_normalize_date(date_str), "%Y%m%d") + timedelta(days=1)
    for _ in range(max_lookahead_days):
        candidate = cursor.strftime("%Y%m%d")
        if is_trading_day(candidate):
            return candidate
        cursor += timedelta(days=1)
    raise TradingCalendarUnavailableError(
        f"{date_str} 后 {max_lookahead_days} 天内未找到可验证交易日"
    )

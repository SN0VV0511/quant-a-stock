"""A 股交易日历失败关闭测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data import holidays


def test_calendar_uses_covering_local_snapshot_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """远端故障时允许使用已验证且覆盖目标日期的本地快照。"""
    cache_path = tmp_path / "calendar.json"
    cache_path.write_text(
        json.dumps(
            {
                "source": "test",
                "generated_at": "2026-01-01 00:00:00",
                "trading_days": ["20260713", "20260714", "20260715"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(holidays, "_CALENDAR_PATH", cache_path)
    monkeypatch.setattr(
        holidays,
        "_load_remote_calendar",
        lambda: (_ for _ in ()).throw(ConnectionError("模拟断网")),
    )
    holidays.reset_calendar_cache()

    assert holidays.is_trading_day("20260714") is True
    assert holidays.is_trading_day("20260718") is False
    assert holidays.previous_trading_day("20260715") == "20260714"


def test_calendar_fails_closed_without_covering_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """工作日超出本地快照覆盖范围时必须报错，不能按星期猜测。"""
    cache_path = tmp_path / "calendar.json"
    cache_path.write_text(
        json.dumps(
            {
                "source": "test",
                "generated_at": "2026-01-01 00:00:00",
                "trading_days": ["20260713", "20260714"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(holidays, "_CALENDAR_PATH", cache_path)
    monkeypatch.setattr(
        holidays,
        "_load_remote_calendar",
        lambda: (_ for _ in ()).throw(ConnectionError("模拟断网")),
    )
    holidays.reset_calendar_cache()

    with pytest.raises(holidays.TradingCalendarUnavailableError, match="无法验证"):
        holidays.is_trading_day("20260715")


def test_calendar_fails_closed_when_no_source_is_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """远端和本地均不可用时必须阻断交易日判断。"""
    monkeypatch.setattr(holidays, "_CALENDAR_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(
        holidays,
        "_load_remote_calendar",
        lambda: (_ for _ in ()).throw(ConnectionError("模拟断网")),
    )
    holidays.reset_calendar_cache()

    with pytest.raises(
        holidays.TradingCalendarUnavailableError,
        match="已停止交易判断",
    ):
        holidays.is_trading_day("20260714")

"""实时盯盘启动节奏测试。"""

from datetime import datetime

from live_runner import _initial_scan_time


def test_initial_scan_after_continuous_auction_open() -> None:
    """首次全市场扫描默认应避开 9:00 未开盘行情。"""
    now = datetime(2026, 6, 2, 8, 50, 0)
    assert _initial_scan_time(now, delay_minutes=5) == datetime(2026, 6, 2, 9, 35, 0)


def test_initial_scan_delay_clamped_to_market_open() -> None:
    """负数延迟不应把扫描提前到开盘前。"""
    now = datetime(2026, 6, 2, 10, 0, 0)
    assert _initial_scan_time(now, delay_minutes=-3) == datetime(2026, 6, 2, 9, 30, 0)

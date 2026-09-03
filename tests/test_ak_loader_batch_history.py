"""BaoStock 批量历史行情加载测试。"""

from __future__ import annotations

import json
from pathlib import Path
import time

import pandas as pd
import pytest

from data import bs_worker
from data.ak_loader import AKDataLoader, _history_batch_timeout


def _rows(close: str) -> list[list[str]]:
    """构造 BaoStock 历史行情返回行。"""
    return [["2026-06-12", "10", "11", "9", close, "1000", "10000", "9.8", "1.2"]]


def _ext_rows(close: str) -> list[list[str]]:
    """构造 BaoStock 扩展历史行情返回行。"""
    return [
        [
            "2026-06-12",
            "10",
            "11",
            "9",
            close,
            "1000000",
            "10000000",
            "1.5",
            "10",
            "1.2",
            "0",
            "1",
            "1.2",
        ]
    ]


def test_batch_history_reuses_worker_login_and_skips_cached_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """缓存缺失项应合并到一个批量 worker，不应逐股启动子进程。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "preclose": [9.8],
            "pctChg": [1.2],
        }
    )
    loader._write_cache("hist_600000_260", cached)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _run(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((args, kwargs))
        return {
            "results": {
                "sh.600001": {"error_code": "0", "rows": _rows("12")},
                "sh.600002": {"error_code": "0", "rows": _rows("13")},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _run)
    monkeypatch.setattr("data.ak_loader.BAOSTOCK_HISTORY_BATCH_SIZE", 25)
    monkeypatch.setattr("data.ak_loader.BAOSTOCK_HISTORY_BATCH_WORKERS", 8)

    result = loader.get_batch_history(["600000", "600001", "600002"], days=260)

    assert set(result) == {"600000", "600001", "600002"}
    assert len(calls) == 1
    assert calls[0][0] == (
        "query_history_batch",
        calls[0][0][1],
        calls[0][0][2],
        "sh.600001",
        "sh.600002",
    )
    assert calls[0][1]["timeout"] == 30


def test_batch_history_timeout_has_global_tail_latency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大批次不能按逐股超时线性放大到数分钟。"""
    monkeypatch.setattr(
        "data.ak_loader._BS_HISTORY_BATCH_TIMEOUT_CAP_SECONDS",
        60,
    )

    assert _history_batch_timeout(2, 8) == 30
    assert _history_batch_timeout(25, 8) == 60


def test_batch_extended_history_reuses_one_worker_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """估值与停牌字段也必须批量拉取，不能为每只股票启动进程。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _run(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((args, kwargs))
        return {
            "results": {
                "sh.600001": {"error_code": "0", "rows": _ext_rows("12")},
                "sh.600002": {"error_code": "0", "rows": _ext_rows("13")},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _run)
    monkeypatch.setattr("data.ak_loader.BAOSTOCK_HISTORY_BATCH_SIZE", 25)

    result = loader.get_batch_history_ext(["600001", "600002"], days=260)

    assert set(result) == {"600001", "600002"}
    assert len(calls) == 1
    assert calls[0][0][0] == "query_history_ext_batch"
    assert calls[0][0][3:] == ("sh.600001", "sh.600002")
    assert result["600001"]["pb"].iloc[-1] == pytest.approx(1.2)


def test_batch_history_splits_work_and_keeps_partial_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """批次失败时应保留其他批次结果并完成返回。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    # 本测试只关注 BaoStock 批次语义，屏蔽 新浪→腾讯→东财 备用日K源。
    monkeypatch.setattr(loader, "fetch_daily_kline", lambda *_a, **_k: None)

    def _run(*args: object, **_kwargs: object) -> dict[str, object] | None:
        bs_codes = args[3:]
        if "sh.600002" in bs_codes:
            return None
        return {
            "results": {
                code: {"error_code": "0", "rows": _rows("10")} for code in bs_codes
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _run)
    monkeypatch.setattr("data.ak_loader.BAOSTOCK_HISTORY_BATCH_SIZE", 2)
    monkeypatch.setattr("data.ak_loader.BAOSTOCK_HISTORY_BATCH_WORKERS", 2)

    result = loader.get_batch_history(
        ["600000", "600001", "600002", "600003"],
        days=260,
    )

    assert set(result) == {"600000", "600001"}


def test_worker_batch_logs_in_once_for_multiple_stocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 批量查询多只股票时应仅建立一次 BaoStock 会话。"""

    class FakeResult:
        """模拟 BaoStock 查询结果游标。"""

        error_code = "0"
        error_msg = "success"

        def __init__(self) -> None:
            self._returned = False

        def next(self) -> bool:
            if self._returned:
                return False
            self._returned = True
            return True

        def get_row_data(self) -> list[str]:
            return _rows("10")[0]

    class FakeBaoStock:
        """记录登录与查询次数的 BaoStock 替身。"""

        def __init__(self) -> None:
            self.login_calls = 0
            self.query_calls: list[str] = []

        def login(self) -> FakeResult:
            self.login_calls += 1
            return FakeResult()

        def query_history_k_data_plus(self, code: str, *_args, **_kwargs) -> FakeResult:
            self.query_calls.append(code)
            return FakeResult()

    fake = FakeBaoStock()
    monkeypatch.setattr(bs_worker, "bs", fake)

    result = bs_worker.cmd_query_history_batch(
        "2026-01-01",
        "2026-06-15",
        "sh.600000",
        "sh.600001",
        "sh.600002",
    )

    assert fake.login_calls == 1
    assert fake.query_calls == ["sh.600000", "sh.600001", "sh.600002"]
    assert set(result["results"]) == {"sh.600000", "sh.600001", "sh.600002"}


def test_batch_history_refresh_failure_falls_back_to_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """基础历史旧缓存应触发刷新，并只在远端失败时作为回退。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    # 屏蔽备用日K源，聚焦 BaoStock 刷新失败时的旧缓存回退。
    monkeypatch.setattr(loader, "fetch_daily_kline", lambda *_a, **_k: None)
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "preclose": [9.8],
            "pctChg": [1.2],
        }
    )
    loader._write_cache("hist_600000_260", cached)
    meta_path = tmp_path / "hist_600000_260.pkl.meta"
    meta_path.write_text(
        json.dumps({"ts": time.time() - 86_400}),
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    def _fail_remote(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {
            "results": {
                "sh.600000": {"error_code": "1", "rows": []},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _fail_remote)

    result = loader.get_batch_history(["600000"], days=260)

    assert set(result) == {"600000"}
    assert len(calls) == 1
    assert calls[0][0] == "query_history_batch"
    assert result["600000"]["close"].iloc[-1] == pytest.approx(10.5)
    persisted = pd.read_pickle(tmp_path / "hist_600000_260.pkl")
    assert persisted["close"].iloc[-1] == pytest.approx(10.5)


def test_batch_history_refresh_success_replaces_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """基础历史远端刷新成功时应返回并持久化新行情。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "preclose": [9.8],
            "pctChg": [1.2],
        }
    )
    loader._write_cache("hist_600000_260", cached)
    meta_path = tmp_path / "hist_600000_260.pkl.meta"
    meta_path.write_text(
        json.dumps({"ts": time.time() - 86_400}),
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    def _refresh_remote(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {
            "results": {
                "sh.600000": {"error_code": "0", "rows": _rows("12")},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _refresh_remote)

    result = loader.get_batch_history(["600000"], days=260)

    assert len(calls) == 1
    assert result["600000"]["close"].iloc[-1] == pytest.approx(12.0)
    persisted = pd.read_pickle(tmp_path / "hist_600000_260.pkl")
    assert persisted["close"].iloc[-1] == pytest.approx(12.0)


def test_batch_extended_history_refresh_failure_falls_back_to_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """扩展历史旧缓存也应刷新，失败时保留估值等旧字段。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    # 屏蔽备用日K源，聚焦 BaoStock 刷新失败时的旧缓存回退。
    monkeypatch.setattr(loader, "fetch_daily_kline", lambda *_a, **_k: None)
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "close": [10.5],
            "pb": [1.2],
        }
    )
    loader._write_cache("histext_600000_260", cached)
    meta_path = tmp_path / "histext_600000_260.pkl.meta"
    meta_path.write_text(
        json.dumps({"ts": time.time() - 86_400}),
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    def _fail_remote(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {
            "results": {
                "sh.600000": {"error_code": "1", "rows": []},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _fail_remote)

    result = loader.get_batch_history_ext(["600000"], days=260)

    assert len(calls) == 1
    assert calls[0][0] == "query_history_ext_batch"
    assert result["600000"]["pb"].iloc[-1] == pytest.approx(1.2)
    persisted = pd.read_pickle(tmp_path / "histext_600000_260.pkl")
    assert persisted["pb"].iloc[-1] == pytest.approx(1.2)


def test_batch_extended_history_refresh_success_replaces_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """扩展历史远端刷新成功时应返回并持久化新数据。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "close": [10.5],
            "pb": [1.2],
        }
    )
    loader._write_cache("histext_600000_260", cached)
    meta_path = tmp_path / "histext_600000_260.pkl.meta"
    meta_path.write_text(
        json.dumps({"ts": time.time() - 86_400}),
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    def _refresh_remote(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {
            "results": {
                "sh.600000": {"error_code": "0", "rows": _ext_rows("13")},
            }
        }

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _refresh_remote)

    result = loader.get_batch_history_ext(["600000"], days=260)

    assert len(calls) == 1
    assert result["600000"]["close"].iloc[-1] == pytest.approx(13.0)
    persisted = pd.read_pickle(tmp_path / "histext_600000_260.pkl")
    assert persisted["close"].iloc[-1] == pytest.approx(13.0)


def test_batch_history_reuses_longer_compatible_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """请求 260 天时应直接复用同代码 300 天缓存。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    cached = pd.DataFrame(
        {
            "date": ["2026-06-12"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "preclose": [9.8],
            "pctChg": [1.2],
        }
    )
    loader._write_cache("hist_600000_300", cached)

    def _fail_remote(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("更长周期缓存不应触发 BaoStock 下载")

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _fail_remote)

    result = loader.get_batch_history(["600000"], days=260)

    assert set(result) == {"600000"}

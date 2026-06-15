"""股票池缓存优先与 BaoStock 超时隔离测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from data.ak_loader import AKDataLoader, _run_bs_with_subprocess


def _touch(path: Path) -> None:
    """创建空缓存文件；股票池恢复只依赖文件名。"""
    path.touch()


def test_get_all_stocks_prefers_cache_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """存在普通或扩展历史缓存时，不应调用 BaoStock。"""
    _touch(tmp_path / "hist_600519_60.pkl")
    _touch(tmp_path / "hist_600519_120.pkl")
    _touch(tmp_path / "histext_000001_40.pkl")
    _touch(tmp_path / "hist_invalid_120.pkl")

    def _fail_remote(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("存在本地缓存时不应访问 BaoStock")

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _fail_remote)

    stocks = AKDataLoader(cache_dir=str(tmp_path)).get_all_stocks()

    assert [stock["code"] for stock in stocks] == ["000001", "600519"]
    assert stocks[0]["bs_code"] == "sz.000001"
    assert stocks[1]["bs_code"] == "sh.600519"


def test_get_all_stocks_stops_after_first_remote_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """无缓存且远端超时时，应立即熔断而不是循环尝试六个日期。"""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _timeout(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _timeout)
    loader = AKDataLoader(cache_dir=str(tmp_path))

    assert loader.get_all_stocks() == []
    assert len(calls) == 1
    assert calls[0][0][0] == "query_all_stock"
    assert calls[0][1]["timeout"] == 5
    assert loader._bs_available is False


def test_subprocess_timeout_does_not_communicate_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 超时后不得再次同步 communicate，否则扫描线程仍可能永久阻塞。"""

    class FakePipe:
        """记录 stdout 管道是否被关闭。"""

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        """模拟无法正常返回的 BaoStock worker。"""

        def __init__(self) -> None:
            self.pid = 12345
            self.stdout = FakePipe()
            self.returncode: int | None = None
            self.communicate_calls = 0
            self.wait_calls = 0

        def communicate(self, timeout: float) -> tuple[bytes, None]:
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired(cmd="bs_worker", timeout=timeout)

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self) -> int:
            self.wait_calls += 1
            self.returncode = -9
            return self.returncode

    process = FakeProcess()

    def _popen(*_args: object, **_kwargs: object) -> Any:
        return process

    monkeypatch.setattr("data.ak_loader.subprocess.Popen", _popen)
    monkeypatch.setattr("data.ak_loader.os.killpg", lambda *_args: None)

    result = _run_bs_with_subprocess("query_all_stock", "2026-06-15", timeout=0.01)

    assert result is None
    assert process.communicate_calls == 1
    assert process.stdout.closed is True

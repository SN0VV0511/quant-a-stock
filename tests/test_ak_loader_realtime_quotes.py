"""腾讯实时行情并发加载测试。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from data.ak_loader import AKDataLoader


def _quote_payload(code: str) -> bytes:
    """构造腾讯行情接口的最小合法响应。"""
    fields = [""] * 40
    fields[1] = "测试股票"
    fields[2] = code
    fields[3] = "10.00"
    fields[4] = "9.90"
    fields[5] = "9.95"
    fields[6] = "1000"
    fields[32] = "1.01"
    fields[33] = "10.20"
    fields[34] = "9.80"
    return f'v_{code}="{"~".join(fields)}";'.encode("gbk")


def test_realtime_quotes_fetches_batches_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """全市场批次应有限并发执行，而不是逐批串行等待。"""
    lock = threading.Lock()
    active = 0
    max_active = 0

    class FakeResponse:
        """模拟腾讯 HTTP 响应。"""

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            return None

    def _urlopen(request, timeout):
        nonlocal active, max_active
        assert timeout == 5
        code = request.full_url.split("q=")[1].split(",")[0][-6:]
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return FakeResponse(_quote_payload(code))

    monkeypatch.setattr("data.ak_loader.urllib.request.urlopen", _urlopen)
    codes = [f"60{index:04d}" for index in range(700)]

    quotes = AKDataLoader(cache_dir=str(tmp_path)).get_realtime_quotes(codes)

    assert len(quotes) == 7
    assert max_active > 1
    assert max_active <= 6


def test_realtime_quotes_keeps_successful_batches_when_one_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """单批网络失败不应吞掉其他批次结果或中止整个扫描。"""
    calls = 0

    class FakeResponse:
        """模拟腾讯 HTTP 响应。"""

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            return None

    def _urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("模拟超时")
        code = request.full_url.split("q=")[1].split(",")[0][-6:]
        return FakeResponse(_quote_payload(code))

    monkeypatch.setattr("data.ak_loader.urllib.request.urlopen", _urlopen)
    codes = [f"60{index:04d}" for index in range(200)]

    quotes = AKDataLoader(cache_dir=str(tmp_path)).get_realtime_quotes(codes)

    assert len(quotes) == 1
    assert calls == 2


def test_realtime_quotes_raises_when_all_batches_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """全部批次失败必须显式报错，不能伪装成扫描到零只股票。"""

    def _urlopen(_request, _timeout):
        raise TimeoutError("模拟全量超时")

    monkeypatch.setattr("data.ak_loader.urllib.request.urlopen", _urlopen)

    with pytest.raises(ConnectionError, match="全部 2 批请求失败"):
        AKDataLoader(cache_dir=str(tmp_path)).get_realtime_quotes(
            [f"60{index:04d}" for index in range(200)]
        )

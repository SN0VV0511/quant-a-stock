"""日K多源故障转移(新浪→腾讯→东财)测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from data import ak_loader
from data.ak_loader import AKDataLoader


class _FakeResponse:
    """记录 URL/参数/超时并按域名返回固定行情的 requests 替身。"""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        """模拟 2xx 响应。"""

    def json(self) -> object:
        """返回预设行情负载。"""
        return self._payload


class _FakeRequests:
    """按域名路由的 requests.get 替身，记录每次调用参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], object]] = []

    def get(self, url: str, params: dict[str, object] | None = None, timeout=None):
        """记录调用并返回对应数据源的样例响应。"""
        self.calls.append((url, dict(params or {}), timeout))
        if "sina" in url:
            return _FakeResponse(
                [
                    {
                        "day": "2026-09-01",
                        "open": "10.0",
                        "high": "10.5",
                        "low": "9.8",
                        "close": "10.2",
                        "volume": "1000000",
                    },
                    {
                        "day": "2026-09-02",
                        "open": "10.3",
                        "high": "11.0",
                        "low": "10.1",
                        "close": "10.8",
                        "volume": "1200000",
                    },
                ]
            )
        if "gtimg.cn" in url:
            return _FakeResponse(
                {
                    "data": {
                        "sh600000": {
                            "qfqday": [
                                ["2026-09-01", "10.0", "10.2", "10.5", "9.8", "1000000"],
                                ["2026-09-02", "10.3", "10.8", "11.0", "10.1", "1200000"],
                            ]
                        }
                    }
                }
            )
        if "eastmoney" in url:
            return _FakeResponse(
                {
                    "data": {
                        "klines": [
                            "2026-09-01,10.0,10.2,10.5,9.8,1000000",
                            "2026-09-02,10.3,10.8,11.0,10.1,1200000",
                        ]
                    }
                }
            )
        raise AssertionError(f"未预期的请求地址: {url}")


def test_each_kline_source_uses_three_second_timeout_and_shared_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个日K源都应 3 秒超时，并归一化为与新浪一致的列格式。"""
    fake = _FakeRequests()
    monkeypatch.setattr(ak_loader, "_requests_lib", fake)

    sina = ak_loader._fetch_kline_sina("600000", datalen=120)
    tencent = ak_loader._fetch_kline_tencent("600000", datalen=120)
    eastmoney = ak_loader._fetch_kline_eastmoney("600000", datalen=120)

    for name, frame in (("sina", sina), ("tencent", tencent), ("eastmoney", eastmoney)):
        assert frame is not None, name
        assert frame["date"].tolist() == ["20260901", "20260902"]
        assert frame["close"].tolist() == [10.2, 10.8]
        assert frame["open"].tolist() == [10.0, 10.3]
        assert frame["volume"].tolist() == [1_000_000.0, 1_200_000.0]

    sina_url, sina_params, sina_timeout = fake.calls[0]
    assert "CN_MarketData.getKLineData" in sina_url
    assert sina_params["symbol"] == "sh600000"
    assert sina_timeout == 3

    tencent_url, tencent_params, tencent_timeout = fake.calls[1]
    assert "fqkline/get" in tencent_url
    assert tencent_params["param"] == "sh600000,day,,,120,qfq"
    assert tencent_timeout == 3

    em_url, em_params, em_timeout = fake.calls[2]
    assert "push2his.eastmoney.com" in em_url
    assert em_params["secid"] == "1.600000"
    assert em_params["fqt"] == "1"
    assert em_timeout == 3


def test_stock_history_falls_over_sina_456_to_tencent_and_caches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """新浪 456 反爬后应按顺序转到腾讯，成功结果写入现有缓存目录复用。"""
    calls: list[str] = []

    def _sina(code: str, datalen: int):
        calls.append("sina")
        raise RuntimeError("456 Client Error: Too Many Requests")

    def _tencent(code: str, datalen: int):
        calls.append("tencent")
        return pd.DataFrame(
            {
                "date": ["20260901", "20260902"],
                "open": [10.0, 10.3],
                "close": [10.2, 10.8],
                "high": [10.5, 11.0],
                "low": [9.8, 10.1],
                "volume": [1_000_000.0, 1_200_000.0],
                "code": code,
                "name": code,
                "amount": 0.0,
                "pctChg": [float("nan"), float("nan")],
            }
        )

    def _eastmoney(code: str, datalen: int):
        calls.append("eastmoney")
        return None

    monkeypatch.setattr(
        ak_loader,
        "_KLINE_FETCHERS",
        {"sina": _sina, "tencent": _tencent, "eastmoney": _eastmoney},
    )
    loader = AKDataLoader(cache_dir=str(tmp_path))
    # BaoStock 熔断中，直接进入备用日K链路。
    loader._bs_available = False
    loader._bs_unavailable_since = None

    df = loader.get_stock_history("600000", days=120)

    assert df is not None
    assert df["close"].tolist() == [10.2, 10.8]
    assert calls == ["sina", "tencent"]
    assert (tmp_path / "hist_600000_120.pkl").exists()

    # 缓存命中后不再触网。
    calls.clear()
    cached = loader.get_stock_history("600000", days=120)
    assert cached is not None
    assert cached["close"].tolist() == [10.2, 10.8]
    assert calls == []


def test_kline_source_backoff_doubles_and_caps_at_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同源连续失败应按 2 的幂退避，封顶 60 秒。"""
    circuit = ak_loader._KlineSourceCircuit()
    now = 1_000.0
    monkeypatch.setattr(ak_loader.time, "monotonic", lambda: now)

    circuit.record_failure("sina", "456 Client Error", now)
    assert circuit.blocked_remaining("sina", now) == 2
    circuit.record_failure("sina", "456 Client Error", now)
    assert circuit.blocked_remaining("sina", now) == 4
    circuit.record_failure("sina", "456 Client Error", now)
    assert circuit.blocked_remaining("sina", now) == 8

    for _ in range(7):
        circuit.record_failure("sina", "456 Client Error", now)
    assert circuit.blocked_remaining("sina", now) == 60

    # 成功后清零退避。
    circuit.record_success("sina")
    assert circuit.blocked_remaining("sina", now) == 0


def test_failover_skips_source_in_backoff_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """退避窗口内的数据源应被跳过，直接尝试下一个源。"""
    calls: list[str] = []

    def _sina(code: str, datalen: int):
        calls.append("sina")
        raise RuntimeError("456 Client Error")

    def _tencent(code: str, datalen: int):
        calls.append("tencent")
        return None

    def _eastmoney(code: str, datalen: int):
        calls.append("eastmoney")
        return pd.DataFrame(
            {
                "date": ["20260901"],
                "open": [10.0],
                "close": [10.2],
                "high": [10.5],
                "low": [9.8],
                "volume": [1_000_000.0],
                "code": code,
                "name": code,
                "amount": 0.0,
                "pctChg": [float("nan")],
            }
        )

    monkeypatch.setattr(
        ak_loader,
        "_KLINE_FETCHERS",
        {"sina": _sina, "tencent": _tencent, "eastmoney": _eastmoney},
    )
    loader = AKDataLoader(cache_dir=str(tmp_path))
    now = 1_000.0
    monkeypatch.setattr(ak_loader.time, "monotonic", lambda: now)
    # 第一轮:新浪失败进入退避，腾讯失败，东财成功。
    first = loader.fetch_daily_kline("600000", days=120)
    assert first is not None
    assert calls == ["sina", "tencent", "eastmoney"]

    # 第二轮(同一时刻):新浪与腾讯都在退避窗口内，只应请求东财。
    calls.clear()
    second = loader.fetch_daily_kline("600001", days=120)
    assert second is not None
    assert calls == ["eastmoney"]


def test_all_kline_sources_failed_returns_none_and_reports_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """全部源失败时返回 None，并按顺序列出各源失败原因。"""
    monkeypatch.setattr(
        ak_loader,
        "_KLINE_FETCHERS",
        {
            "sina": lambda code, datalen: (_ for _ in ()).throw(
                RuntimeError("456 Client Error")
            ),
            "tencent": lambda code, datalen: (_ for _ in ()).throw(
                RuntimeError("ReadTimeout")
            ),
            "eastmoney": lambda code, datalen: None,
        },
    )
    loader = AKDataLoader(cache_dir=str(tmp_path))
    monkeypatch.setattr(ak_loader.time, "monotonic", lambda: 1_000.0)

    assert loader.fetch_daily_kline("600000", days=120) is None

    summary = loader.kline_source_failure_summary()
    assert "sina: 456 Client Error" in summary
    assert "tencent: ReadTimeout" in summary
    assert "eastmoney: 返回空数据" in summary
    assert summary.index("sina") < summary.index("tencent") < summary.index("eastmoney")

"""沪深 A 股标的范围测试。"""

from __future__ import annotations

import pytest

from config.settings import (
    get_a_share_market,
    is_a_share_stock,
    is_chinext,
    normalize_a_share_code,
    to_baostock_code,
    to_tencent_code,
)
from data.ak_loader import AKDataLoader
from risk.control import RiskController
from strategies.market_scanner import MarketScanner
from trading.brokers import PaperBrokerAdapter
from trading.models import OrderIntent


def test_a_share_code_helpers_accept_hu_shen_stocks() -> None:
    """代码工具应只接受当前虚拟盘支持的沪深 A 股股票。"""
    assert normalize_a_share_code("sh.600519") == "600519"
    assert get_a_share_market("600519") == "sh"
    assert get_a_share_market("000001") == "sz"
    assert is_a_share_stock("sh601988") is True
    assert is_a_share_stock("sz300750") is True
    assert is_chinext("sz301308") is True
    assert to_tencent_code("600519") == "sh600519"
    assert to_tencent_code("000001") == "sz000001"
    assert to_baostock_code("sh601988") == "sh.601988"


@pytest.mark.parametrize(
    "code",
    [
        "sh000001",  # 上证指数
        "sz399001",  # 深证成指
        "159915",  # ETF
        "sh510300",  # ETF
        "hk00700",  # 港股
        "usAAPL",  # 美股
        "bj430047",  # 北交所，本轮暂不纳入
        "sh000651",  # 前缀和实际深市股票归属不一致
    ],
)
def test_a_share_code_helpers_reject_non_supported_targets(code: str) -> None:
    """指数、ETF、港美股、北交所和错误前缀不能进入实时交易范围。"""
    assert is_a_share_stock(code) is False
    with pytest.raises(ValueError):
        to_tencent_code(code)
    with pytest.raises(ValueError):
        to_baostock_code(code)


def test_realtime_loader_ignores_non_a_share_codes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """实时行情请求应在联网前过滤掉非沪深 A 股股票。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))

    def _fail_urlopen(*_args, **_kwargs):
        raise AssertionError("非沪深 A 股代码不应触发外部行情请求")

    monkeypatch.setattr("urllib.request.urlopen", _fail_urlopen)

    quotes = loader.get_realtime_quotes(["hk00700", "sh000001", "159915"])

    assert quotes == {}


def test_market_scanner_filters_input_to_hu_shen_a_shares() -> None:
    """扫描器应在请求实时行情前过滤非沪深 A 股股票。"""

    class FakeLoader:
        """固定数据源，用于验证扫描器入参过滤。"""

        def __init__(self) -> None:
            self.realtime_codes: list[str] | None = None

        def get_all_stocks(self) -> list[dict[str, str]]:
            return [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "000001", "name": "平安银行"},
                {"code": "sh000001", "name": "上证指数"},
                {"code": "159915", "name": "创业板 ETF"},
            ]

        def get_realtime_quotes(self, codes: list[str]) -> dict[str, dict[str, float]]:
            self.realtime_codes = codes
            return {}

        def get_batch_history(self, codes: list[str], days: int) -> dict[str, object]:
            return {}

    loader = FakeLoader()
    scanner = MarketScanner(loader=loader)

    assert scanner.scan(top_n=5) == []
    assert loader.realtime_codes == ["600519", "000001"]


def test_risk_and_paper_broker_reject_non_a_share_order(tmp_path) -> None:
    """风控和虚拟 Broker 都应拒绝非沪深 A 股股票订单。"""
    broker = PaperBrokerAdapter(
        state_file=str(tmp_path / "state.json"),
        trade_log_file=str(tmp_path / "trade_log.json"),
        snapshot_log_file=str(tmp_path / "snapshots.jsonl"),
    )
    broker.connect()
    risk = RiskController()
    order = OrderIntent(
        code="hk00700",
        action="buy",
        price=300.0,
        shares=100,
        strategy="全市场扫描+组合策略",
        date="20260528",
    )

    approved, rejected = risk.filter_order_intents(
        [order],
        broker.portfolio,
        {"hk00700": {"current_price": 300.0, "prev_close": 299.0, "is_st": False, "is_suspended": False}},
    )
    report = broker.place_order(order)

    assert approved == []
    assert len(rejected) == 1
    assert "不是沪深 A 股股票" in rejected[0].reason
    assert report.status == "rejected"
    assert "仅支持沪深 A 股股票代码" in report.message

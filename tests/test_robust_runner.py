"""robust_v2 收盘信号与次日执行链路测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from robust_runner import RobustV2Runner, SignalData, load_runtime_config
from strategies.robust_v2 import RobustV2Config, build_market_data_hash
from trading.brokers import SQLitePaperBrokerAdapter
from trading.models import MarketSnapshot


def _etf_history(code: str, slope: float) -> pd.DataFrame:
    """生成截至 2026-07-09 的 ETF 历史。"""
    dates = pd.bdate_range(end="2026-07-09", periods=280)
    close = [4 + slope * index for index in range(len(dates))]
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y%m%d"),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [30_000_000] * len(dates),
            "amount": [200_000_000] * len(dates),
            "name": [code] * len(dates),
        }
    )


class FakeLoader:
    """仅满足 runner 测试所需接口。"""

    def close(self) -> None:
        """兼容真实加载器。"""


class FakeDataService:
    """返回固定的收盘和执行行情。"""

    def __init__(self) -> None:
        self.history = {
            "510300": _etf_history("510300", 0.01),
            "510500": _etf_history("510500", 0.008),
        }
        self.snapshot = MarketSnapshot(
            trade_date="20260709",
            previous_trade_date="20260708",
            source="test",
            adjustment="qfq",
            data_hash=build_market_data_hash(self.history, "20260709"),
            freshness_seconds=0,
        )

    def load_signal_data(self, trade_date: str, account_value: float) -> SignalData:
        """返回固定收盘数据。"""
        assert trade_date == "20260709"
        assert account_value == 50_000
        return SignalData(self.snapshot, self.history, {}, {}, {})

    def load_execution_data(self, codes: set[str], signal_date: str):
        """返回与信号日前收盘一致的次日实时行情。"""
        assert signal_date == "20260709"
        quotes = {
            code: {
                "price": float(self.history[code]["close"].iloc[-1]),
                "prev_close": float(self.history[code]["close"].iloc[-1]),
            }
            for code in codes
        }
        return self.snapshot, {code: self.history[code] for code in codes}, quotes


def test_close_signal_does_not_trade_until_next_day(tmp_path: Path) -> None:
    """T 日只写目标，T+1 才产生订单和持仓。"""
    broker = SQLitePaperBrokerAdapter(
        ledger_path=str(tmp_path / "paper_v2.db"),
        initial_cash=50_000,
    )
    broker.connect()
    runner = RobustV2Runner(
        broker,
        FakeLoader(),  # type: ignore[arg-type]
        RobustV2Config(enable_stock_enhancement=False, etf_min_avg_amount=0),
        tmp_path,
    )
    runner.data = FakeDataService()  # type: ignore[assignment]

    target = runner.generate_close_target("20260709", force=True)
    same_day = runner.execute_pending_target("20260709")
    next_day = runner.execute_pending_target("20260710")

    assert target is not None
    assert broker.query_orders() == list(next_day.reports)
    assert same_day.reports == ()
    assert next_day.reports
    assert all(report.status == "filled" for report in next_day.reports)
    assert broker.query_positions()
    assert all(
        position["sellable_qty"] == 0 for position in broker.query_positions().values()
    )
    broker.close()


def test_runtime_loads_walk_forward_selection_and_etf_fallback(tmp_path: Path) -> None:
    """守护入口应使用样本外结果，并在失败标记下关闭个股增强。"""
    path = tmp_path / "selected.json"
    path.write_text(
        """{
  "selected_params": {
    "enable_etf_trend_filter": true,
    "stock_reversal_days": 20,
    "rebalance_days": 10,
    "stock_stop_pct": 0.09,
    "max_total_position": 0.60
  },
  "fallback_to_etf": true
}
""",
        encoding="utf-8",
    )

    config = load_runtime_config(path)

    assert config.max_total_position == 0.60
    assert config.rebalance_days == 10
    assert config.enable_stock_enhancement is False

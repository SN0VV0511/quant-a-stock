"""AKShare ETF/行业指数数据源测试。"""

from __future__ import annotations

import pandas as pd

from data.ak_loader import AKDataLoader


class FakeAKShare:
    """模拟 AKShare 返回,避免单元测试依赖外部网络。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fund_etf_hist_em(
        self,
        symbol: str,
        period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """返回 ETF 日线样例。"""
        self.calls.append(("fund_etf_hist_em", {
            "symbol": symbol,
            "period": period,
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        }))
        return pd.DataFrame({
            "日期": ["2026-01-02", "2026-01-05"],
            "开盘": [4.0, 4.1],
            "最高": [4.2, 4.3],
            "最低": [3.9, 4.0],
            "收盘": [4.1, 4.2],
            "成交量": [1_000_000, 1_100_000],
            "成交额": [4_100_000, 4_620_000],
            "涨跌幅": [1.0, 2.0],
        })

    def stock_board_industry_hist_em(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        """返回行业指数日线样例。"""
        self.calls.append(("stock_board_industry_hist_em", {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "period": period,
            "adjust": adjust,
        }))
        return pd.DataFrame({
            "日期": ["2026-01-02", "2026-01-05"],
            "开盘": [1000.0, 1010.0],
            "最高": [1020.0, 1030.0],
            "最低": [990.0, 1000.0],
            "收盘": [1010.0, 1025.0],
            "成交量": [800_000, 900_000],
            "成交额": [8_000_000, 9_000_000],
            "涨跌幅": [0.8, 1.5],
        })


def test_get_etf_history_normalizes_akshare_columns(monkeypatch, tmp_path) -> None:
    """ETF 历史行情应标准化为策略统一字段并写入缓存。"""
    fake = FakeAKShare()
    monkeypatch.setattr("data.ak_loader.ak", fake)
    loader = AKDataLoader(cache_dir=str(tmp_path))

    df = loader.get_etf_history("sh510300", start_date="20260101", end_date="20260131")

    assert df is not None
    assert df["date"].tolist() == ["20260102", "20260105"]
    assert df["code"].tolist() == ["510300", "510300"]
    assert df["close"].tolist() == [4.1, 4.2]
    assert fake.calls[0][0] == "fund_etf_hist_em"
    assert fake.calls[0][1]["symbol"] == "510300"


def test_get_industry_index_history_normalizes_akshare_columns(monkeypatch, tmp_path) -> None:
    """行业指数历史行情应标准化为策略统一字段。"""
    fake = FakeAKShare()
    monkeypatch.setattr("data.ak_loader.ak", fake)
    loader = AKDataLoader(cache_dir=str(tmp_path))

    df = loader.get_industry_index_history("证券", start_date="20260101", end_date="20260131")

    assert df is not None
    assert df["date"].tolist() == ["20260102", "20260105"]
    assert df["code"].tolist() == ["证券", "证券"]
    assert df["close"].tolist() == [1010.0, 1025.0]
    assert fake.calls[0][0] == "stock_board_industry_hist_em"
    assert fake.calls[0][1]["symbol"] == "证券"

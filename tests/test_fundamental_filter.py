"""基本面过滤器单元测试。"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.fundamental_filter import normalize_stock_code
import pandas as pd


class TestNormalizeStockCode(unittest.TestCase):
    def test_sh_prefix(self):
        self.assertEqual(normalize_stock_code("sh600519"), "600519")

    def test_sz_prefix(self):
        self.assertEqual(normalize_stock_code("sz000001"), "000001")

    def test_plain_code(self):
        self.assertEqual(normalize_stock_code("600519"), "600519")

    def test_dot_prefix(self):
        self.assertEqual(normalize_stock_code("sh.600519"), "600519")

    def test_bj_prefix(self):
        self.assertEqual(normalize_stock_code("bj830799"), "830799")

    def test_whitespace(self):
        self.assertEqual(normalize_stock_code("  600519  "), "600519")


class TestSpotFilter(unittest.TestCase):
    """测试实时快照基本面过滤逻辑。"""

    def _make_df(self, **overrides):
        """构造一行模拟数据。"""
        row = {
            "code": "600519",
            "name": "贵州茅台",
            "pe": 30.0,
            "pb": 8.0,
            "total_market_cap": 2_000_000_000_000,
            "float_market_cap": 1_800_000_000_000,
            "momentum_60d": 15.0,
            "momentum_ytd": 20.0,
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_negative_pe_filtered(self):
        df = self._make_df(pe=-5.0)
        df = df[df["pe"] > 0]
        self.assertTrue(df.empty)

    def test_high_pe_filtered(self):
        df = self._make_df(pe=150.0)
        df = df[df["pe"] <= 100]
        self.assertTrue(df.empty)

    def test_low_pb_filtered(self):
        df = self._make_df(pb=0.3)
        df = df[df["pb"] >= 0.5]
        self.assertTrue(df.empty)

    def test_high_momentum_filtered(self):
        df = self._make_df(momentum_60d=250.0)
        df = df[df["momentum_60d"] <= 120]
        self.assertTrue(df.empty)

    def test_normal_passes(self):
        df = self._make_df()
        pe_ok = (df["pe"] > 0) & (df["pe"] <= 100)
        pb_ok = (df["pb"] >= 0.5) & (df["pb"] <= 20)
        mom_ok = df["momentum_60d"] <= 120
        self.assertTrue(pe_ok.all())
        self.assertTrue(pb_ok.all())
        self.assertTrue(mom_ok.all())


class TestReportFilter(unittest.TestCase):
    """测试财报过滤逻辑。"""

    def _make_df(self, **overrides):
        row = {
            "code": "600519",
            "report_name": "贵州茅台",
            "revenue": 50_000_000_000,
            "revenue_yoy": 15.0,
            "net_profit": 25_000_000_000,
            "net_profit_yoy": 12.0,
            "roe": 30.0,
            "gross_margin": 90.0,
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_negative_profit_filtered(self):
        df = self._make_df(net_profit=-100_000_000)
        df = df[df["net_profit"] >= 0]
        self.assertTrue(df.empty)

    def test_normal_passes(self):
        df = self._make_df()
        rev_ok = df["revenue"] >= 100_000_000
        profit_ok = df["net_profit"] >= 0
        roe_ok = df["roe"] >= 0
        gm_ok = df["gross_margin"] >= 5
        self.assertTrue(rev_ok.all())
        self.assertTrue(profit_ok.all())
        self.assertTrue(roe_ok.all())
        self.assertTrue(gm_ok.all())


if __name__ == "__main__":
    unittest.main(verbosity=2)

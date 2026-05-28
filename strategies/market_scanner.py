"""
全市场扫描模块
使用 AKDataLoader（腾讯实时 + BaoStock 历史），自动筛选动量排名靠前的股票
"""

import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class MarketScanner:
    """全市场扫描器（基于 AKDataLoader）"""

    def __init__(self, loader=None):
        """
        Args:
            loader: AKDataLoader 实例，None 则自动创建
        """
        if loader is None:
            from data.ak_loader import AKDataLoader
            self.loader = AKDataLoader()
            self._owns_loader = True
        else:
            self.loader = loader
            self._owns_loader = False

    def close(self):
        if self._owns_loader:
            self.loader.close()

    def get_all_a_stocks(self):
        """获取全 A 股列表（腾讯数据源）

        Returns:
            list of dict: [{code, name}]
        """
        return self.loader.get_all_stocks()

    def scan(self, stock_list=None, top_n=20, momentum_period=60):
        """扫描股票池，按动量排名

        Args:
            stock_list: 股票列表 [{code, name}]，None 则扫全 A 股
            top_n: 返回前 N 名
            momentum_period: 动量计算周期

        Returns:
            list of dict: 排名后的股票信息
        """
        if stock_list is None:
            stock_list = self.loader.get_all_stocks()

        logger.info(f"开始扫描 {len(stock_list)} 只股票...")

        # 批量获取实时行情（腾讯接口，含最新价、涨跌幅、成交量）
        codes = [s["code"] for s in stock_list]
        quotes = self.loader.get_realtime_quotes(codes)
        logger.info(f"获取到 {len(quotes)} 只实时行情")

        # 第一轮粗筛：用实时行情快速过滤
        pre_filtered = []
        for stock in stock_list:
            code = stock["code"]
            name = stock["name"]

            if code not in quotes:
                continue

            q = quotes[code]
            price = q["price"]
            pct = q["pct_change"]
            volume = q["volume"]

            # 基本过滤
            if price <= 0:
                continue
            if price < 5:               # 排除低价股（5 元以下）
                continue
            if abs(pct) >= 9.8:         # 排除涨跌停
                continue
            if volume < 1000000:         # 排除成交量过低（100 万股）
                continue
            # 排除涨跌幅太小的（停牌/僵尸股）
            if abs(pct) < 0.1:
                continue

            pre_filtered.append({
                "code": code,
                "name": name,
                "price": price,
                "pct_change": pct,
                "volume": volume,
            })

        logger.info(f"实时行情粗筛后: {len(pre_filtered)} 只")

        # 按成交量降序，优先扫描活跃股，限制最多 2000 只
        pre_filtered.sort(key=lambda x: x["volume"], reverse=True)
        MAX_HIST_FETCH = 2000
        if len(pre_filtered) > MAX_HIST_FETCH:
            logger.info(f"粗筛后 {len(pre_filtered)} 只，取成交量前 {MAX_HIST_FETCH} 只")
            pre_filtered = pre_filtered[:MAX_HIST_FETCH]

        # 第二轮细筛：批量并发拉历史数据，再计算动量、均线、波动率
        fetch_codes = [s["code"] for s in pre_filtered]
        logger.info(f"开始批量加载 {len(fetch_codes)} 只股票历史数据...")
        history_map = self.loader.get_batch_history(fetch_codes, days=momentum_period + 30)

        results = []
        scanned = 0
        total_prefilter = len(pre_filtered)

        for i, stock in enumerate(pre_filtered):
            code = stock["code"]
            name = stock["name"]

            if (i + 1) % 200 == 0:
                logger.info(f"筛选进度: {i + 1}/{total_prefilter}，已有效: {len(results)}")

            try:
                df = history_map.get(code)
                if df is None or len(df) < momentum_period + 1:
                    continue

                close = pd.to_numeric(df["close"], errors="coerce").dropna()
                if len(close) < momentum_period + 1:
                    continue

                close_now = stock["price"]  # 用腾讯实时价

                # 20 日均量（历史数据）
                volume_col = pd.to_numeric(df["volume"], errors="coerce").dropna()
                avg_vol = volume_col.tail(20).mean() if len(volume_col) >= 20 else 0
                if avg_vol < 500000:
                    continue

                # 60 日动量（用历史收盘价，避免 BaoStock 延迟影响）
                close_N_ago = close.iloc[-(momentum_period)]
                momentum = (close_now - close_N_ago) / close_N_ago if close_N_ago > 0 else 0

                # 20 日动量
                close_20_ago = close.iloc[-20] if len(close) >= 20 else close.iloc[0]
                momentum_20 = (close_now - close_20_ago) / close_20_ago if close_20_ago > 0 else 0

                # 均线（用历史数据 + 实时价拼接）
                recent_closes = close.tail(19).tolist() + [close_now]
                ma20 = sum(recent_closes) / len(recent_closes)
                ma60 = close.tail(60).mean() if len(close) >= 60 else close.mean()
                ma_passed = close_now > ma20 > ma60

                # 波动率（年化，20 日）
                returns = close.pct_change().dropna().tail(20)
                volatility = returns.std() * np.sqrt(252) if len(returns) > 0 else 999

                results.append({
                    "code": code,
                    "name": name,
                    "price": round(close_now, 2),
                    "momentum": round(momentum, 4),
                    "momentum_20": round(momentum_20, 4),
                    "ma20": round(ma20, 2),
                    "ma60": round(ma60, 2),
                    "ma_passed": ma_passed,
                    "volatility": round(volatility, 4),
                    "avg_volume": int(avg_vol),
                    "last_pct": round(stock["pct_change"], 2),
                })

                scanned += 1

            except Exception:
                continue

        # 综合得分 = 动量*0.5 + 短期动量*0.3 - 波动率*0.2
        for r in results:
            r["score"] = (
                r["momentum"] * 0.5 +
                r["momentum_20"] * 0.3 -
                r["volatility"] * 0.2
            )

        # 均线多头优先
        ma_passed = [r for r in results if r["ma_passed"]]
        ma_failed = [r for r in results if not r["ma_passed"]]

        ma_passed.sort(key=lambda x: x["score"], reverse=True)
        ma_failed.sort(key=lambda x: x["score"], reverse=True)

        final = (ma_passed + ma_failed)[:top_n]

        for i, r in enumerate(final):
            r["rank"] = i + 1

        logger.info(f"扫描完成: {scanned} 只，筛选出 {len(final)} 只")
        return final


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scanner = MarketScanner()

    print("=== 全 A 股动量扫描 Top10 ===")
    results = scanner.scan(top_n=10)
    print()
    for r in results:
        ma = "✓" if r["ma_passed"] else "✗"
        print(f"  #{r['rank']:2d} {r['name']:8s}({r['code']}) "
              f"价格={r['price']:8.2f} "
              f"动量={r['momentum']:+.2%} "
              f"均线={ma} "
              f"波动={r['volatility']:.2%} "
              f"得分={r['score']:.4f}")

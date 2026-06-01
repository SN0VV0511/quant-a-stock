"""
沪深 A 股扫描模块
使用 AKDataLoader（腾讯实时 + BaoStock 历史），自动筛选动量排名靠前的沪深 A 股股票

打分核心 ``score_candidates`` 抽成纯函数,实时扫描与历史回测共用同一套逻辑,
保证回测与线上一致;同时统一价格口径并对因子做横截面标准化。
"""

import logging

import pandas as pd
import numpy as np

from config.settings import (
    is_a_share_stock,
    SCAN_MIN_PRICE,
    SCAN_MIN_VOLUME,
    SCAN_MIN_AVG_VOLUME,
    SCAN_LIMIT_PCT,
    SCAN_MAX_HIST_FETCH,
    SCORE_WEIGHT_MOMENTUM,
    SCORE_WEIGHT_MOMENTUM_20,
    SCORE_WEIGHT_VOLATILITY,
)

logger = logging.getLogger(__name__)


def _zscore(values: np.ndarray) -> np.ndarray:
    """横截面 z-score 标准化。

    标准差为 0(全部相同)时返回全 0,避免除零。
    """
    if len(values) == 0:
        return values
    mean = values.mean()
    std = values.std()
    if std <= 1e-12:
        return np.zeros_like(values)
    return (values - mean) / std


def score_candidates(
    history_map,
    top_n=20,
    momentum_period=60,
    name_map=None,
    min_avg_volume=SCAN_MIN_AVG_VOLUME,
    weights=None,
):
    """对给定历史数据做横截面动量打分并排序(纯函数,无网络/IO)。

    所有因子(动量、均线、波动率)统一使用 ``history_map`` 中的收盘价口径,
    避免实时价(不复权)与 BaoStock 历史价(前复权)混用导致的动量失真。
    打分前对各因子做横截面 z-score 标准化,消除量纲差异——这是修复旧版
    "波动率项长期压制动量项"问题的关键。

    Args:
        history_map: ``{code: DataFrame}``,DataFrame 需含 ``close``、``volume`` 列,
            且收盘价口径需统一(建议前复权)。回测时传入截至某交易日的历史切片即可
            天然避免未来函数。
        top_n: 返回前 N 名。
        momentum_period: 长动量周期(交易日)。
        name_map: ``{code: name}`` 仅用于展示,缺失时回退为代码本身。
        min_avg_volume: 20 日均量下限(股)。
        weights: ``(w_momentum, w_momentum_20, w_volatility)``,None 时取 config 默认。

    Returns:
        list[dict]: 每项含 code/name/price/momentum/momentum_20/ma20/ma60/
            ma_passed/volatility/avg_volume/score/rank,按 score 降序(均线多头优先),
            长度 <= top_n。
    """
    name_map = name_map or {}
    if weights is None:
        weights = (SCORE_WEIGHT_MOMENTUM, SCORE_WEIGHT_MOMENTUM_20, SCORE_WEIGHT_VOLATILITY)
    w_mom, w_mom20, w_vol = weights

    raw = []
    for code, df in history_map.items():
        if df is None or len(df) < momentum_period + 1:
            continue

        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) < momentum_period + 1:
            continue

        volume_col = pd.to_numeric(df["volume"], errors="coerce").dropna()
        avg_vol = volume_col.tail(20).mean() if len(volume_col) >= 20 else 0
        if avg_vol < min_avg_volume:
            continue

        # 统一价格口径:全部使用历史收盘序列,不混入实时价
        close_now = float(close.iloc[-1])

        # N 日动量:用 close[-1] 与 close[-(N+1)],两端同口径
        close_n_ago = float(close.iloc[-(momentum_period + 1)])
        momentum = (close_now - close_n_ago) / close_n_ago if close_n_ago > 0 else 0.0

        close_20_ago = float(close.iloc[-21]) if len(close) >= 21 else float(close.iloc[0])
        momentum_20 = (close_now - close_20_ago) / close_20_ago if close_20_ago > 0 else 0.0

        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float(close.mean())
        ma_passed = close_now > ma20 > ma60

        returns = close.pct_change().dropna().tail(20)
        volatility = float(returns.std() * np.sqrt(252)) if len(returns) > 0 else 999.0

        raw.append({
            "code": code,
            "name": name_map.get(code, code),
            "price": round(close_now, 2),
            "momentum": round(momentum, 4),
            "momentum_20": round(momentum_20, 4),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma_passed": ma_passed,
            "volatility": round(volatility, 4),
            "avg_volume": int(avg_vol),
        })

    if not raw:
        return []

    # 横截面 z-score 标准化后再加权,消除量纲差异
    mom_z = _zscore(np.array([r["momentum"] for r in raw], dtype=float))
    mom20_z = _zscore(np.array([r["momentum_20"] for r in raw], dtype=float))
    vol_z = _zscore(np.array([r["volatility"] for r in raw], dtype=float))

    for i, r in enumerate(raw):
        r["score"] = round(
            float(mom_z[i] * w_mom + mom20_z[i] * w_mom20 - vol_z[i] * w_vol), 4
        )

    # 均线多头优先,组内按得分降序
    ma_ok = sorted([r for r in raw if r["ma_passed"]], key=lambda x: x["score"], reverse=True)
    ma_no = sorted([r for r in raw if not r["ma_passed"]], key=lambda x: x["score"], reverse=True)

    final = (ma_ok + ma_no)[:top_n]
    for i, r in enumerate(final):
        r["rank"] = i + 1
    return final


class MarketScanner:
    """沪深 A 股股票扫描器（基于 AKDataLoader）。"""

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
        """获取沪深 A 股股票列表。

        Returns:
            list of dict: [{code, name}]
        """
        return self.loader.get_all_stocks()

    def scan(self, stock_list=None, top_n=20, momentum_period=60):
        """扫描股票池，按动量排名

        Args:
            stock_list: 股票列表 [{code, name}]，None 则扫沪深 A 股股票
            top_n: 返回前 N 名
            momentum_period: 动量计算周期

        Returns:
            list of dict: 排名后的股票信息
        """
        if stock_list is None:
            stock_list = self.loader.get_all_stocks()

        stock_list = [
            stock for stock in stock_list
            if is_a_share_stock(str(stock.get("code", "")))
        ]
        logger.info(f"开始扫描 {len(stock_list)} 只沪深 A 股股票...")

        # 批量获取实时行情(腾讯接口)。实时行情仅用于"粗筛"以减少历史数据拉取量,
        # 不参与最终动量打分——打分统一用历史复权口径,避免量纲/复权不一致。
        codes = [s["code"] for s in stock_list]
        quotes = self.loader.get_realtime_quotes(codes)
        logger.info(f"获取到 {len(quotes)} 只实时行情")

        pre_filtered = []
        for stock in stock_list:
            code = stock["code"]
            if code not in quotes:
                continue

            q = quotes[code]
            price = q["price"]
            pct = q["pct_change"]
            volume = q["volume"]

            if price <= 0:
                continue
            if price < SCAN_MIN_PRICE:          # 排除低价股
                continue
            if abs(pct) >= SCAN_LIMIT_PCT:      # 排除涨跌停
                continue
            if volume < SCAN_MIN_VOLUME:        # 排除成交量过低
                continue
            if abs(pct) < 0.1:                  # 排除停牌/僵尸股
                continue

            pre_filtered.append({"code": code, "name": stock["name"], "volume": volume})

        logger.info(f"实时行情粗筛后: {len(pre_filtered)} 只")

        # 按成交量降序优先扫描活跃股,限制历史拉取数量上限
        pre_filtered.sort(key=lambda x: x["volume"], reverse=True)
        if len(pre_filtered) > SCAN_MAX_HIST_FETCH:
            logger.info(f"粗筛后 {len(pre_filtered)} 只,取成交量前 {SCAN_MAX_HIST_FETCH} 只")
            pre_filtered = pre_filtered[:SCAN_MAX_HIST_FETCH]

        fetch_codes = [s["code"] for s in pre_filtered]
        name_map = {s["code"]: s["name"] for s in pre_filtered}
        logger.info(f"开始批量加载 {len(fetch_codes)} 只股票历史数据...")
        history_map = self.loader.get_batch_history(fetch_codes, days=momentum_period + 30)

        final = score_candidates(
            history_map,
            top_n=top_n,
            momentum_period=momentum_period,
            name_map=name_map,
        )

        logger.info(f"扫描完成: 有效 {len(history_map)} 只,筛选出 {len(final)} 只")
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

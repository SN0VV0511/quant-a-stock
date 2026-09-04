"""robust_v2 参数放宽前后的 A/B/C 对照回测(2026-09-04)。

背景:2026 年 8 月底生产参数被放宽(stock_max_market_cap 30B→50B、
stock_max_5d_gain 0.08→0.15、stock_max_price_ma20 1.08→1.15、
stock_target 0.32→0.50、max_stock_count 2→None),但没有任何回测证据。
本脚本用 BaoStock 真实数据 + backtest/robust_v2.py 无未来函数引擎,在同一份
数据上跑三组配置并输出对照报告:

    A 原参数(放宽前)      rebalance_days=20
    B 现生产参数(放宽后)  rebalance_days=20
    C 现生产参数 + 周频    rebalance_days=5

数据落盘 data/robust_research/:
    etf/<code>.pkl  宽基 ETF 池日 K(东财 em 前复权,故障时降级腾讯 qfq;
                    BaoStock 的 ETF 日 K 仅保留约近 8 个月,不满足 200 日
                    均线预载需求)
    stock/<code>.pkl 主板样本日 K(BaoStock 前复权,含 peTTM/pb/流通市值近似/
                    is_st/tradestatus)
    universe/robust_v2_YYYYMMDD.json  月末 point-in-time 主板可交易池快照,
    与 scripts/robust_walk_forward.py 的目录/格式约定兼容
已存在的 pkl/快照自动跳过(断点续传)。

用法: python3 scripts/param_ab_20260904.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import baostock as bs  # noqa: E402

from backtest.robust_v2 import (  # noqa: E402
    RobustParameterSet,
    RobustPortfolioBacktester,
)
from config.settings import ROBUST_V2_BROAD_ETF_CODES  # noqa: E402
from strategies.robust_v2 import RobustV2Config  # noqa: E402

LOGGER = logging.getLogger("param_ab")

RESEARCH_ROOT = ROOT_DIR / "data" / "robust_research"
ETF_DIR = RESEARCH_ROOT / "etf"
STOCK_DIR = RESEARCH_ROOT / "stock"
UNIVERSE_DIR = RESEARCH_ROOT / "universe"
BENCHMARK_DIR = RESEARCH_ROOT / "benchmark"
REPORT_PATH = ROOT_DIR / "reports" / "param_ab_20260904.json"

BACKTEST_START = "20240101"
BACKTEST_END = datetime.now().strftime("%Y%m%d")  # 滚动到当天,支持定期重跑
# 预载历史起点。任务建议"预载多取 180 天",但策略要求 stock_min_history_days=250
# 根 K 线与 MA200/ETF 200 日均线,180 个自然日只有约 120 个交易日,会让 2024
# 上半年个股与 ETF sleeve 因"历史不足"系统性空仓、扭曲对照,因此取约 500 个
# 自然日(2024-01 首个信号日已有约 340 根 K 线)。
PRELOAD_START = "20220820"
SAMPLE_SIZE = 300
INITIAL_CAPITAL = 50_000.0
# pkl 末根 K 线距 end 不超过该天数即视为新鲜,跳过重复下载(容忍长期停牌股)。
FRESH_TOLERANCE_DAYS = 10
# 已落盘 universe 快照距 end 超过该天数才重刷(月末快照通常不需要更新)。
SNAPSHOT_REFRESH_DAYS = 60

K_FIELDS = "date,open,high,low,close,volume,amount,turn,tradestatus,isST,peTTM,pbMRQ"
# AKShare 标准化后的 ETF 日 K 列(无 turn/tradestatus;引擎对缺失 tradestatus
# 默认视为可交易,ETF 流动性过滤只依赖 amount)。
ETF_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

# 沪深主板前缀(与 config.settings.get_stock_board 的 mainboard 判定一致:
# 创业板 300/301、科创板 688/689 不入池)。
MAINBOARD_PREFIXES = {
    "sh": ("600", "601", "603", "605"),
    "sz": ("000", "001", "002", "003"),
}

LIMITATIONS = [
    "固定样本按当前上市清单等步长抽样 300 只,期间退市股未包含,存在幸存者偏差,"
    "整体收益预期被系统性高估;本报告结论仅用于三组参数的相对比较,不构成绝对收益预期。",
    "BaoStock 无总股本字段,mktcap 用流通市值近似(close×volume/(turn×1%),即"
    " close×流通股本)。流通市值≤总市值,30B/50B 阈值对应的真实总市值边界整体"
    "下移(偏紧);三组共用同一口径,组间相对比较方向不受影响,但边界附近股票"
    "的归组与真实总市值口径可能有出入。",
    "净利润字段(net_profit)缺失;策略中 net_profit>0 门槛与 peTTM>0 门槛在"
    "过滤上等价,不影响候选结果。",
    "单一区间(2024-01→2026-09)单一路径,未覆盖完整牛熊周期,也未做参数再寻优;"
    "三组的 ETF sleeve、成本、风控(止损/涨跌停/T+1)完全同源,差异仅来自研究维度。",
]

# 与对照无关的维度全部显式钉住在 settings 默认值,防止部署环境变量(例如
# ROBUST_V2_REBALANCE_DAYS=5 的周频覆盖)渗入对照组。
PINNED_CONFIG = {
    "etf_target": 0.48,
    "min_cash": 0.20,
    "max_single_etf": 0.24,
    "max_single_stock": 0.16,
    "max_etf_count": 2,
    "etf_stop_pct": 0.10,
}


@dataclass(frozen=True)
class ParamABSet(RobustParameterSet):
    """在受限参数集之上叠加本次要对照的放宽维度。

    引擎只通过 ``params.to_config()`` 读取配置,因此覆写该方法即可注入
    stock_max_market_cap 等研究维度,不需要改动 backtest/robust_v2.py。
    """

    label: str
    max_stock_count: int | None
    stock_max_market_cap: float
    stock_max_5d_gain: float
    stock_max_price_ma20: float
    stock_target: float

    def to_config(self, *, enable_stock_enhancement: bool = True) -> RobustV2Config:
        return RobustV2Config(
            etf_min_20d_return=self.etf_min_20d_return,
            stock_min_earnings_yield=self.stock_min_earnings_yield,
            rebalance_days=self.rebalance_days,
            stock_stop_pct=self.stock_stop_pct,
            max_total_position=self.max_total_position,
            enable_stock_enhancement=enable_stock_enhancement,
            **PINNED_CONFIG,
            max_stock_count=self.max_stock_count,
            stock_max_market_cap=self.stock_max_market_cap,
            stock_max_5d_gain=self.stock_max_5d_gain,
            stock_max_price_ma20=self.stock_max_price_ma20,
            stock_target=self.stock_target,
        )

    def dims(self) -> dict[str, Any]:
        """返回本次对照的研究维度,用于报告与控制台。"""
        return {
            "rebalance_days": self.rebalance_days,
            "max_stock_count": self.max_stock_count,
            "stock_max_market_cap": self.stock_max_market_cap,
            "stock_max_5d_gain": self.stock_max_5d_gain,
            "stock_max_price_ma20": self.stock_max_price_ma20,
            "stock_target": self.stock_target,
        }


def build_groups() -> tuple[ParamABSet, ...]:
    """返回 A/B/C 三组配置,基础维度与生产默认一致。"""
    base = {
        "etf_min_20d_return": -0.05,
        "stock_min_earnings_yield": 0.02,
        "stock_stop_pct": 0.07,
        "max_total_position": 0.80,
    }
    relaxed = {
        "max_stock_count": None,
        "stock_max_market_cap": 5e10,
        "stock_max_5d_gain": 0.15,
        "stock_max_price_ma20": 1.15,
        "stock_target": 0.50,
    }
    return (
        ParamABSet(
            label="A",
            rebalance_days=20,
            max_stock_count=2,
            stock_max_market_cap=3e10,
            stock_max_5d_gain=0.08,
            stock_max_price_ma20=1.08,
            stock_target=0.32,
            **base,
        ),
        ParamABSet(label="B", rebalance_days=20, **relaxed, **base),
        ParamABSet(label="C", rebalance_days=5, **relaxed, **base),
    )


# ==================== BaoStock 取数 ====================


class BaostockSession:
    """登录一次、失效自动重登的上下文管理器。

    BaoStock 长连接会被服务端断开(报"用户未登录",并发登录也会互踢),所有
    查询统一走 _run_query,会话类错误自动重登并重试,单次失败不拖垮整批下载。
    """

    def __enter__(self) -> "BaostockSession":
        self._login()
        global _SESSION
        _SESSION = self
        return self

    @staticmethod
    def _login() -> None:
        last_error = "unknown"
        for _ in range(5):
            result = bs.login()
            if result.error_code == "0":
                return
            last_error = f"{result.error_code}: {result.error_msg}"
            time.sleep(2)
        raise ConnectionError(f"BaoStock 登录失败: {last_error}")

    @staticmethod
    def relogin_on_error(error_msg: str) -> bool:
        """会话类错误重登后返回 True,调用方应重试查询。"""
        if "登录" not in str(error_msg):
            return False
        LOGGER.warning("BaoStock 会话失效(%s),重新登录", error_msg)
        BaostockSession._login()
        return True

    def __exit__(self, *_exc: object) -> None:
        global _SESSION
        bs.logout()
        _SESSION = None


_SESSION: BaostockSession | None = None


def _run_query(describe: str, query, *args: Any, **kwargs: Any) -> list[list[str]]:
    """执行 BaoStock 查询并展开全部行,处理会话失效与瞬时错误。"""
    rows: list[list[str]] = []
    error_msg = "unknown"
    for attempt in range(4):
        rs = query(*args, **kwargs)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code == "0":
            return rows
        error_msg = rs.error_msg
        if _SESSION is not None and _SESSION.relogin_on_error(error_msg):
            continue
        time.sleep(1 + attempt)
    LOGGER.warning("%s 查询失败: %s", describe, error_msg)
    return []


def _norm_date(value: Any) -> str:
    return str(value).replace("-", "")[:8]


def _fmt_date(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _bs_code(code: str) -> str:
    # 沪市:股票 6 开头、ETF 51/56/58 开头;深市:股票 000/001/002/003、ETF 15 开头。
    if code.startswith(("6", "51", "56", "58")):
        return "sh." + code
    return "sz." + code


def _six_code(bs_code: str) -> str:
    return bs_code.rsplit(".", 1)[-1]


def _is_mainboard(bs_code: str) -> bool:
    market, _, six = bs_code.partition(".")
    return six[:3] in MAINBOARD_PREFIXES.get(market, ())


def _fetch_kline(bs_code: str, start: str, end: str) -> pd.DataFrame:
    """下载日 K(前复权),数值化并去重排序。"""
    rows = _run_query(
        f"K线{bs_code}",
        bs.query_history_k_data_plus,
        bs_code,
        K_FIELDS,
        start_date=_fmt_date(start),
        end_date=_fmt_date(end),
        frequency="d",
        adjustflag="2",
    )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows, columns=K_FIELDS.split(","))
    frame["date"] = frame["date"].map(_norm_date)
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turn",
        "tradestatus",
        "peTTM",
        "pbMRQ",
    ):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # isST 为 point-in-time 的 ST 标志,映射为策略读取的 is_st 布尔列。
    frame["is_st"] = frame.get("isST", "0").astype(str).isin({"1", "true", "True"})
    frame = frame.drop(columns=["isST"])
    return (
        frame.sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def _attach_fundamentals(frame: pd.DataFrame) -> pd.DataFrame:
    """附加策略扫描需要的 pb 与 mktcap(流通市值近似)列。"""
    data = frame.copy()
    data["pb"] = data.get("pbMRQ")
    turn = data["turn"].where(data["turn"] > 0)
    data["mktcap"] = data["close"] * data["volume"] / (turn / 100.0)
    return data


def _pkl_is_fresh(path: Path, end: str, *, min_start: str | None = None) -> bool:
    """判断已落盘 pkl 是否覆盖到 end 附近(且不早于 min_start),支持断点续传。"""
    if not path.exists():
        return False
    try:
        frame = pd.read_pickle(path)
        if frame is None or frame.empty or "date" not in frame.columns:
            return False
        last = _norm_date(frame["date"].iloc[-1])
        first = _norm_date(frame["date"].iloc[0])
    except Exception:  # noqa: BLE001 - 损坏的 pkl 直接重下
        return False
    age_days = (
        datetime.strptime(end, "%Y%m%d") - datetime.strptime(last, "%Y%m%d")
    ).days
    if not 0 <= age_days <= FRESH_TOLERANCE_DAYS:
        return False
    # 覆盖起点校验:防止把 BaoStock 只含近 8 个月的 ETF 短历史误当完整数据复用。
    return min_start is None or first <= min_start


def _download_stocks(
    codes: Sequence[str], directory: Path, end: str
) -> dict[str, pd.DataFrame]:
    """下载(或复用)主板个股日 K 并落盘 pkl。"""
    directory.mkdir(parents=True, exist_ok=True)
    history: dict[str, pd.DataFrame] = {}
    for index, code in enumerate(codes, 1):
        path = directory / f"{code}.pkl"
        if _pkl_is_fresh(path, end):
            history[code] = pd.read_pickle(path)
            continue
        try:
            frame = _fetch_kline(_bs_code(code), PRELOAD_START, end)
        except Exception as exc:  # noqa: BLE001 - 单只失败不终止整体
            LOGGER.warning("下载 %s 异常: %s", code, exc)
            continue
        if frame.empty:
            LOGGER.warning("%s 在 %s-%s 无 K 线数据", code, PRELOAD_START, end)
            continue
        frame = _attach_fundamentals(frame)
        frame.to_pickle(path)
        history[code] = frame
        if index % 20 == 0 or index == len(codes):
            LOGGER.info("K 线进度 %d/%d (%s)", index, len(codes), directory.name)
    return history


def _common_last_date(stock_history: Mapping[str, pd.DataFrame]) -> str:
    """取多数个股共有的最后交易日(80% 分位,容忍少数长期停牌股)。"""
    last_dates = sorted(
        _norm_date(frame["date"].iloc[-1]) for frame in stock_history.values()
    )
    if not last_dates:
        raise RuntimeError("个股行情为空,无法确定数据截止日")
    return last_dates[int(len(last_dates) * 0.8)]


def _etf_from_tencent(code: str, start: str, end: str) -> pd.DataFrame | None:
    """腾讯日 K(前复权)作为 ETF 长历史的降级源。

    东财 em 接口故障时使用;腾讯返回的 amount 列实为成交量(手),换算为
    volume(股) 后用 close×volume 近似成交额(元),供 ETF 流动性过滤使用。
    """
    import akshare as ak

    symbol = ("sh" if code.startswith(("51", "56", "58")) else "sz") + code
    try:
        raw = ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=_fmt_date(start),
            end_date=_fmt_date(end),
            adjust="qfq",
        )
    except Exception as exc:  # noqa: BLE001 - 网络错误由调用方降级处理
        LOGGER.warning("腾讯 ETF %s 获取失败: %s", code, exc)
        return None
    if raw is None or raw.empty:
        return None
    frame = raw.copy()
    frame["date"] = frame["date"].map(_norm_date)
    frame = frame.loc[frame["date"] <= end]
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    lots = pd.to_numeric(frame["amount"], errors="coerce")
    frame["volume"] = lots * 100.0
    frame["amount"] = frame["volume"] * frame["close"]
    return (
        frame.loc[:, ETF_COLUMNS]
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .dropna(subset=["close"])
        .reset_index(drop=True)
    )


def _download_etf_history(
    codes: Sequence[str], end: str
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """ETF 日 K 下载并落盘:东财 em(前复权)优先,腾讯 qfq 降级。

    BaoStock 的 ETF 日 K 只保留最近约 8 个月,不满足 ETF MA200/120 日回看的
    预载需求。em 接口偶发拒绝连接,且 AKDataLoader 内置备用源只给约 120 天,
    因此这里显式校验覆盖起点,不足则改用腾讯 qfq;两个来源都失败则终止,
    避免残缺的 ETF 池悄悄改变对照口径。
    """
    from data.ak_loader import AKDataLoader

    ETF_DIR.mkdir(parents=True, exist_ok=True)
    min_start = (
        datetime.strptime(PRELOAD_START, "%Y%m%d") + timedelta(days=45)
    ).strftime("%Y%m%d")
    loader = AKDataLoader()
    history: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    try:
        for code in codes:
            path = ETF_DIR / f"{code}.pkl"
            if _pkl_is_fresh(path, end, min_start=min_start):
                history[code] = pd.read_pickle(path)
                sources[code] = "cached"
                continue
            frame = None
            source = "akshare-em-qfq"
            try:
                frame = loader.get_etf_history(
                    code, start_date=PRELOAD_START, end_date=end, adjust="qfq"
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("em ETF %s 异常: %s", code, exc)
            if (
                frame is None
                or frame.empty
                or _norm_date(frame["date"].iloc[0]) > min_start
            ):
                LOGGER.warning(
                    "ETF %s 的 em 来源不可用或覆盖不足,降级腾讯 qfq", code
                )
                frame = _etf_from_tencent(code, PRELOAD_START, end)
                source = "tencent-qfq"
            if frame is None or frame.empty:
                raise RuntimeError(f"ETF {code} 两个来源均不可用,终止")
            data = frame.copy()
            data["date"] = data["date"].map(_norm_date)
            data = (
                data.loc[data["date"] <= end, ETF_COLUMNS]
                .sort_values("date")
                .drop_duplicates("date", keep="last")
                .reset_index(drop=True)
            )
            if _norm_date(data["date"].iloc[0]) > min_start:
                raise RuntimeError(
                    f"ETF {code} 历史仅从 {data['date'].iloc[0]} 开始,"
                    f"不满足预载起点 {min_start}"
                )
            data.to_pickle(path)
            history[code] = data
            sources[code] = source
            LOGGER.info(
                "ETF %s(%s) 落盘 %d 行(%s→%s)",
                code,
                source,
                len(data),
                data["date"].iloc[0],
                data["date"].iloc[-1],
            )
    finally:
        loader.close()
    return history, sources


def _download_benchmark(end: str) -> pd.DataFrame | None:
    """下载沪深 300 指数作为基准,并写成 robust_walk_forward 兼容的 CSV。"""
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    rows = _run_query(
        "沪深300",
        bs.query_history_k_data_plus,
        "sh.000300",
        "date,close",
        start_date=_fmt_date(PRELOAD_START),
        end_date=_fmt_date(end),
        frequency="d",
        adjustflag="3",
    )
    if not rows:
        LOGGER.warning("沪深 300 基准下载失败")
        return None
    frame = pd.DataFrame(rows, columns=["date", "close"])
    frame["date"] = frame["date"].map(_norm_date)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"]).reset_index(drop=True)
    frame.to_csv(BENCHMARK_DIR / "000300.csv", index=False)
    return frame


def _query_all_stocks(day: str) -> list[list[str]]:
    """获取指定交易日的全部证券清单(code, tradeStatus, code_name)。"""
    return _run_query(f"all_stock({day})", bs.query_all_stock, day=_fmt_date(day))


def _latest_all_stock(day: str) -> tuple[str, list[list[str]]]:
    """从 day 起向前找最近一个有清单的交易日。"""
    current = datetime.strptime(day, "%Y%m%d")
    for offset in range(10):
        candidate = (current - timedelta(days=offset)).strftime("%Y%m%d")
        rows = _query_all_stocks(candidate)
        if rows:
            return candidate, rows
    raise RuntimeError(f"截至 {day} 连续 10 天都无法获取 query_all_stock 清单")


def _stride_sample(codes: Sequence[str], size: int) -> list[str]:
    """按代码等步长抽 size 只,保持全市场覆盖面。"""
    ordered = sorted(codes)
    if len(ordered) <= size:
        return list(ordered)
    step = len(ordered) // size
    return ordered[::step][:size]


def _month_end_dates(trading_dates: Sequence[str], start: str, end: str) -> list[str]:
    """取区间内每个自然月的最后一个交易日(末月为最后可用交易日)。"""
    by_month: dict[str, str] = {}
    for date in trading_dates:
        if start <= date <= end:
            by_month[date[:6]] = date
    return sorted(by_month.values())


def _snapshot_stocks(rows: Sequence[Sequence[str]]) -> list[dict[str, str]]:
    """月末可交易主板池:剔除非主板、停牌、ST/退市风险。"""
    stocks: list[dict[str, str]] = []
    for bs_code, trade_status, name in rows:
        if not _is_mainboard(bs_code):
            continue
        if str(trade_status) != "1":
            continue
        name_text = str(name)
        if "ST" in name_text.upper() or "退" in name_text:
            continue
        stocks.append({"code": _six_code(bs_code), "name": name_text})
    stocks.sort(key=lambda item: item["code"])
    return stocks


def _write_universe_snapshots(
    trading_dates: Sequence[str], start: str, end: str
) -> int:
    """生成 robust_v2_YYYYMMDD.json 月度快照(已存在且不陈旧则跳过)。"""
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    threshold = datetime.strptime(end, "%Y%m%d") - timedelta(days=SNAPSHOT_REFRESH_DAYS)
    for date in _month_end_dates(trading_dates, start, end):
        path = UNIVERSE_DIR / f"robust_v2_{date}.json"
        if path.exists() and datetime.strptime(date, "%Y%m%d") <= threshold:
            continue
        rows = _query_all_stocks(date)
        if not rows:
            LOGGER.warning("月末快照 %s 无清单数据,跳过(回测将沿用更早版本)", date)
            continue
        stocks = _snapshot_stocks(rows)
        payload = {
            "snapshot": {
                "trade_date": date,
                "source": "baostock.query_all_stock",
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "script": "scripts/param_ab_20260904.py",
                "note": "沪深主板可交易池(剔除停牌/ST/退市风险)",
            },
            "count": len(stocks),
            "stocks": stocks,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        written += 1
        LOGGER.info("月末快照 %s: %d 只主板可交易", date, len(stocks))
    return written


def _load_universe_snapshots() -> dict[str, set[str]]:
    """从磁盘读取全部快照,结构与 robust_walk_forward 保持一致。"""
    result: dict[str, set[str]] = {}
    for path in sorted(UNIVERSE_DIR.glob("robust_v2_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        date = str(
            (payload.get("snapshot") or {}).get("trade_date")
            or path.stem.rsplit("_", 1)[-1]
        )
        result[date] = {
            str(stock["code"])
            for stock in payload.get("stocks", [])
            if stock.get("code")
        }
    if not result:
        raise RuntimeError("universe 快照为空,无法执行 point-in-time 回测")
    return result


# ==================== 回测与报告 ====================


def _run_group(
    backtester: RobustPortfolioBacktester,
    params: ParamABSet,
    start: str,
    end: str,
    stock_history: Mapping[str, pd.DataFrame],
) -> dict[str, Any]:
    result = backtester.run(params, start, end)
    last = result.daily_values[-1]
    total_value = max(float(last["total_value"]), 1e-9)
    stock_trades = sum(1 for t in result.trades if t["code"] in stock_history)
    metrics = dict(result.metrics)
    metrics["final_cash_ratio"] = round(float(last["cash"]) / total_value, 4)
    metrics["stock_trades"] = stock_trades
    metrics["etf_trades"] = len(result.trades) - stock_trades
    metrics["unique_stock_bought"] = len(
        {t["code"] for t in result.trades if t["action"] == "buy" and t["code"] in stock_history}
    )
    return {
        "label": params.label,
        "params": params.dims(),
        "base_params": {
            "etf_min_20d_return": params.etf_min_20d_return,
            "stock_min_earnings_yield": params.stock_min_earnings_yield,
            "stock_stop_pct": params.stock_stop_pct,
            "max_total_position": params.max_total_position,
            **PINNED_CONFIG,
        },
        "metrics": metrics,
        "trading_days": len(result.daily_values),
        "benchmark_return": result.benchmark_return,
        "equity": [
            {"date": row["date"], "value": row["total_value"]}
            for row in result.daily_values
        ],
    }


def _pct(value: Any) -> str:
    return f"{float(value) * 100:+.2f}%"


def _print_table(results: Mapping[str, dict[str, Any]]) -> None:
    print(f"\n=== 参数 A/B/C 对照({BACKTEST_START}→{BACKTEST_END},"
          f"初始资金 {INITIAL_CAPITAL:,.0f} 元,BaoStock 真实数据)===")
    header = (
        f"{'组别':<14}{'总收益':>10}{'年化':>10}{'最大回撤':>10}{'夏普':>8}"
        f"{'卡尔玛':>8}{'交易次数':>10}{'年换手':>8}{'总成本(元)':>12}{'期末现金占比':>14}"
    )
    print(header)
    print("-" * len(header.expandtabs()))
    names = {"A": "A 原参数", "B": "B 现生产", "C": "C 周频"}
    for label in ("A", "B", "C"):
        metrics = results[label]["metrics"]
        print(
            f"{names[label]:<14}"
            f"{float(metrics['total_return']):>10.2%}"
            f"{float(metrics['annual_return']):>10.2%}"
            f"{float(metrics['max_drawdown']):>10.2%}"
            f"{float(metrics['sharpe_ratio']):>8.2f}"
            f"{float(metrics['calmar_ratio']):>8.2f}"
            f"{int(metrics['total_trades']):>10}"
            f"{float(metrics.get('annual_turnover', 0)):>8.1f}"
            f"{float(metrics['total_cost']):>12,.0f}"
            f"{float(metrics['final_cash_ratio']):>14.2%}"
        )


def _print_dims(groups: Sequence[ParamABSet]) -> None:
    print("\n研究维度(其余维度三组完全一致):")
    rows = {
        "rebalance_days": "rebalance_days",
        "max_stock_count": "max_stock_count",
        "stock_max_market_cap": "stock_max_market_cap",
        "stock_max_5d_gain": "stock_max_5d_gain",
        "stock_max_price_ma20": "stock_max_price_ma20",
        "stock_target": "stock_target",
    }
    header = f"{'参数':<24}" + "".join(f"{g.label:>16}" for g in groups)
    print(header)
    for name, key in rows.items():
        cells = "".join(
            f"{_dim_text(g.dims()[key]):>16}" for g in groups
        )
        print(f"{name:<24}{cells}")


def _dim_text(value: Any) -> str:
    if value is None:
        return "None(不限制)"
    if isinstance(value, float) and value >= 1e9:
        return f"{value / 1e9:.0f}B"
    return str(value)


def _build_conclusion(results: Mapping[str, dict[str, Any]]) -> list[str]:
    """基于三组指标生成 3-5 句结论文案。"""
    a, b, c = results["A"]["metrics"], results["B"]["metrics"], results["C"]["metrics"]
    sentences: list[str] = []
    ret_gap = float(b["total_return"]) - float(a["total_return"])
    sharpe_gap = float(b["sharpe_ratio"]) - float(a["sharpe_ratio"])
    dd_gap = float(b["max_drawdown"]) - float(a["max_drawdown"])
    sentences.append(
        f"B(现生产放宽参数)对比 A(原参数):总收益 {_pct(a['total_return'])}→"
        f"{_pct(b['total_return'])}({ret_gap * 100:+.2f}pp),最大回撤 "
        f"{float(a['max_drawdown']):.2%}→{float(b['max_drawdown']):.2%}"
        f"({dd_gap * 100:+.2f}pp),夏普 {float(a['sharpe_ratio']):.2f}→"
        f"{float(b['sharpe_ratio']):.2f},个股买入标的数 "
        f"{a['unique_stock_bought']}→{b['unique_stock_bought']}。"
    )
    if ret_gap > 0 and sharpe_gap >= -0.05 and dd_gap <= 0.03:
        verdict = "放宽参数在收益端占优且风险指标未显著恶化,回测支持保留放宽参数"
    elif ret_gap > 0 and (sharpe_gap < -0.05 or dd_gap > 0.03):
        verdict = "放宽参数收益更高但伴随更差的风险调整表现,是否保留取决于对回撤/波动的容忍度"
    else:
        verdict = "放宽参数未带来收益改善,回测不支持保留放宽参数,建议回滚或缩短观察期后再评估"
    sentences.append(f"判断:{verdict}(收益差 {ret_gap * 100:+.2f}pp、夏普差 {sharpe_gap:+.2f}、回撤差 {dd_gap * 100:+.2f}pp)。")
    trade_ratio = max(float(c["total_trades"]), 1) / max(float(b["total_trades"]), 1)
    cost_gap = float(c["total_cost"]) - float(b["total_cost"])
    c_ret_gap = float(c["total_return"]) - float(b["total_return"])
    sentences.append(
        f"C(周频)对比 B:交易次数 {int(b['total_trades'])}→{int(c['total_trades'])}"
        f"({trade_ratio:.1f}×),总成本 {float(b['total_cost']):,.0f}→"
        f"{float(c['total_cost']):,.0f} 元(增加 {cost_gap:,.0f} 元,"
        f"占初始资金 {abs(cost_gap) / INITIAL_CAPITAL:.2%}),总收益变化 "
        f"{c_ret_gap * 100:+.2f}pp,最大回撤 "
        f"{float(b['max_drawdown']):.2%}→{float(c['max_drawdown']):.2%}。"
    )
    if c_ret_gap > abs(cost_gap) / INITIAL_CAPITAL:
        speed = "周频的额外成本被收益改善覆盖"
    else:
        speed = "周频的收益改善不足以覆盖其额外成本"
    sentences.append(
        f"周频的成本代价:约 {abs(cost_gap):,.0f} 元额外交易成本(交易次数约 "
        f"{trade_ratio:.1f} 倍),{speed}。"
    )
    sentences.append(
        "注意:以上为固定 300 只主板样本上的相对比较,样本存在幸存者偏差"
        "(退市股未包含),市值用流通市值近似,结论仅用于参数取舍,不代表绝对收益预期。"
    )
    return sentences


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="robust_v2 参数放宽 A/B/C 对照回测")
    parser.add_argument("--start", default=BACKTEST_START, help="回测起始日 YYYYMMDD")
    parser.add_argument("--end", default=BACKTEST_END, help="回测截止日 YYYYMMDD")
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE, help="主板抽样数量")
    parser.add_argument("--report", default=str(REPORT_PATH), help="输出报告 JSON 路径")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = _parse_args()
    start, end = args.start, args.end
    for directory in (ETF_DIR, STOCK_DIR, UNIVERSE_DIR, BENCHMARK_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    with BaostockSession():
        list_date, all_rows = _latest_all_stock(end)
        mainboard = [_six_code(row[0]) for row in all_rows if _is_mainboard(row[0])]
        sample = _stride_sample(mainboard, args.sample_size)
        name_map = {_six_code(row[0]): str(row[2]) for row in all_rows}
        LOGGER.info(
            "主板全市场 %d 只(清单日期 %s),等步长抽样 %d 只",
            len(mainboard),
            list_date,
            len(sample),
        )
        stock_history = _download_stocks(sample, STOCK_DIR, end)
        benchmark = _download_benchmark(end)
    if len(stock_history) < max(1, int(len(sample) * 0.8)):
        raise RuntimeError(
            f"个股下载仅 {len(stock_history)}/{len(sample)} 成功,"
            "疑似数据源会话异常,终止以避免生成样本残缺的误导性报告"
        )
    data_end = _common_last_date(stock_history)
    etf_history, etf_sources = _download_etf_history(
        sorted(ROBUST_V2_BROAD_ETF_CODES), data_end
    )
    if not etf_history:
        raise RuntimeError("ETF 行情为空,无法构建回测交易日历")
    with BaostockSession():
        calendar = sorted(
            {date for frame in etf_history.values() for date in frame["date"]}
        )
        snapshots_written = _write_universe_snapshots(calendar, PRELOAD_START, data_end)

    LOGGER.info(
        "数据就绪(截止 %s): ETF %d/%d(来源 %s),个股 %d/%d(BaoStock),"
        "基准 %s,新增快照 %d 份",
        data_end,
        len(etf_history),
        len(ROBUST_V2_BROAD_ETF_CODES),
        ",".join(sorted(set(etf_sources.values()))),
        len(stock_history),
        len(sample),
        "有" if benchmark is not None else "缺失",
        snapshots_written,
    )

    universe = _load_universe_snapshots()
    backtester = RobustPortfolioBacktester(
        etf_history,
        stock_history,
        universe,
        initial_capital=INITIAL_CAPITAL,
        benchmark_history=benchmark,
        name_map=name_map,
    )
    groups = build_groups()
    results: dict[str, dict[str, Any]] = {}
    for params in groups:
        LOGGER.info("回测组别 %s: %s", params.label, params.dims())
        results[params.label] = _run_group(
            backtester, params, start, end, stock_history
        )

    _print_dims(groups)
    _print_table(results)
    conclusion = _build_conclusion(results)
    print("\n结论:")
    for sentence in conclusion:
        print(f"  - {sentence}")
    print("\n局限:")
    for item in LIMITATIONS:
        print(f"  - {item}")

    data_dates = sorted(
        {date for frame in stock_history.values() for date in frame["date"]}
    )
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": "scripts/param_ab_20260904.py",
        "question": "2026-08 放宽后的 robust_v2 生产参数是否优于原参数",
        "window": {
            "backtest_start": start,
            "backtest_end": end,
            "preload_start": PRELOAD_START,
            "initial_capital": INITIAL_CAPITAL,
        },
        "data": {
            "source": "个股/基准/月末快照: BaoStock;ETF 长历史: 东财 em(前复权,"
            "故障时降级腾讯 qfq)。BaoStock 的 ETF 日 K 仅保留约近 8 个月,"
            "不满足 MA200 预载,故不用作 ETF 来源",
            "data_end": data_end,
            "etf_sources": etf_sources,
            "mainboard_total": len(mainboard),
            "sample_size": len(stock_history),
            "sample_rule": f"按代码等步长抽 {args.sample_size} 只(清单日期 {list_date})",
            "etf_codes": sorted(etf_history),
            "universe_snapshots": len(universe),
            "stock_date_range": [data_dates[0], data_dates[-1]] if data_dates else None,
            "mktcap_proxy": "流通市值近似 close×volume/(turn×1%);BaoStock 无总股本字段",
            "net_profit_note": "净利润字段缺失;net_profit>0 门槛与 peTTM>0 门槛过滤等价",
        },
        "configs": {label: results[label]["params"] | results[label]["base_params"] for label in results},
        "results": {label: results[label] for label in ("A", "B", "C")},
        "conclusion": conclusion,
        "limitations": LIMITATIONS,
    }
    report_path = Path(args.report)
    _atomic_write_json(report_path, payload)
    print(f"\n报告已写入: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


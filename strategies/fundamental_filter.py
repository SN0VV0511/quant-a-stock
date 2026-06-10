"""基本面过滤器 — 在选股雷达粗筛阶段补充 PE/PB/市值/财报过滤。

所有 akshare 数据批量拉取，通过 code merge 与候选股列表关联。
支持缓存和降级。
"""

from __future__ import annotations

import logging
import os
import re

import pandas as pd

import config.settings as settings

logger = logging.getLogger(__name__)


def normalize_stock_code(code: str) -> str:
    """统一股票代码为 6 位纯数字。sh600519 -> 600519"""
    code = str(code).strip()
    code = re.sub(r"^(sh|sz|bj)\.?", "", code, flags=re.IGNORECASE)
    return code


def _cache_dir() -> str:
    d = os.path.join(settings.BASE_DIR, settings.SCAN_FUNDAMENTAL_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _save_cache(df: pd.DataFrame, filename: str) -> None:
    path = os.path.join(_cache_dir(), filename)
    df.to_csv(path, index=False)
    logger.info(f"[fundamental] cache saved: {path} ({len(df)} rows)")


def _load_cache(filename: str) -> pd.DataFrame | None:
    path = os.path.join(_cache_dir(), filename)
    if os.path.exists(path):
        df = pd.read_csv(path, dtype=str)
        logger.info(f"[fundamental] cache loaded: {path} ({len(df)} rows)")
        return df
    return None


def fetch_spot_fundamentals() -> pd.DataFrame:
    """批量拉取全市场实时快照基本面数据（akshare）。"""
    import akshare as ak

    raw = ak.stock_zh_a_spot_em()
    df = raw.rename(columns={
        "代码": "code",
        "名称": "name",
        "市盈率-动态": "pe",
        "市净率": "pb",
        "总市值": "total_market_cap",
        "流通市值": "float_market_cap",
        "60日涨跌幅": "momentum_60d",
        "年初至今涨跌幅": "momentum_ytd",
    })
    df["code"] = df["code"].apply(normalize_stock_code)
    for col in ["pe", "pb", "total_market_cap", "float_market_cap", "momentum_60d", "momentum_ytd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["code", "name", "pe", "pb", "total_market_cap", "float_market_cap", "momentum_60d", "momentum_ytd"]]


def fetch_report_fundamentals(report_date: str) -> pd.DataFrame:
    """批量拉取财报数据（akshare 季报业绩快报）。"""
    import akshare as ak

    raw = ak.stock_yjbb_em(date=report_date)
    df = raw.rename(columns={
        "股票代码": "code",
        "股票简称": "report_name",
        "营业总收入-营业总收入": "revenue",
        "营业总收入-同比增长": "revenue_yoy",
        "净利润-净利润": "net_profit",
        "净利润-同比增长": "net_profit_yoy",
        "净资产收益率": "roe",
        "销售毛利率": "gross_margin",
        "最新公告日期": "report_announce_date",
    })
    df["code"] = df["code"].apply(normalize_stock_code)
    for col in ["revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "gross_margin"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = ["code", "report_name", "revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "gross_margin", "report_announce_date"]
    return df[[c for c in keep if c in df.columns]]


def load_or_fetch_spot_fundamentals() -> pd.DataFrame | None:
    """拉取实时快照基本面，支持缓存和降级。"""
    try:
        df = fetch_spot_fundamentals()
        if settings.SCAN_USE_FUNDAMENTAL_CACHE:
            _save_cache(df, "spot_latest.csv")
        return df
    except Exception as e:
        logger.warning(f"[warning] akshare spot fundamentals unavailable: {e}")
        if settings.SCAN_USE_FUNDAMENTAL_CACHE:
            cached = _load_cache("spot_latest.csv")
            if cached is not None:
                logger.warning("[warning] akshare spot fundamentals unavailable, use cache")
                for col in ["pe", "pb", "total_market_cap", "float_market_cap", "momentum_60d", "momentum_ytd"]:
                    if col in cached.columns:
                        cached[col] = pd.to_numeric(cached[col], errors="coerce")
                return cached
        if settings.SCAN_ALLOW_FUNDAMENTAL_FALLBACK:
            logger.warning("[warning] akshare spot fundamentals unavailable, skip fundamental filter")
            return None
        raise


def load_or_fetch_report_fundamentals(report_date: str) -> pd.DataFrame | None:
    """拉取财报数据，支持缓存和降级。"""
    cache_file = f"report_{report_date}.csv"
    try:
        df = fetch_report_fundamentals(report_date)
        if settings.SCAN_USE_FUNDAMENTAL_CACHE:
            _save_cache(df, cache_file)
        return df
    except Exception as e:
        logger.warning(f"[warning] akshare report fundamentals unavailable: {e}")
        if settings.SCAN_USE_FUNDAMENTAL_CACHE:
            cached = _load_cache(cache_file)
            if cached is not None:
                logger.warning("[warning] akshare report fundamentals unavailable, use cache")
                for col in ["revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "gross_margin"]:
                    if col in cached.columns:
                        cached[col] = pd.to_numeric(cached[col], errors="coerce")
                return cached
        if settings.SCAN_ALLOW_FUNDAMENTAL_FALLBACK:
            logger.warning("[warning] akshare report fundamentals unavailable, skip report filter")
            return None
        raise


def apply_spot_fundamental_filter(df: pd.DataFrame) -> pd.DataFrame:
    """对合并了实时快照基本面的 DataFrame 进行过滤。"""
    n0 = len(df)
    # 名称过滤
    if "name" in df.columns:
        mask = ~df["name"].astype(str).str.contains(r"ST|\*ST|退市|^N|^C", na=False, regex=True)
        df = df[mask]
        logger.info(f"[fundamental] name filter: {n0} -> {len(df)}")

    # PE
    n = len(df)
    if "pe" in df.columns:
        df = df[df["pe"].notna() & (df["pe"] > settings.SCAN_MIN_PE) & (df["pe"] <= settings.SCAN_MAX_PE)]
    logger.info(f"[fundamental] pe filter: {n} -> {len(df)}")

    # PB
    n = len(df)
    if "pb" in df.columns:
        df = df[df["pb"].notna() & (df["pb"] >= settings.SCAN_MIN_PB) & (df["pb"] <= settings.SCAN_MAX_PB)]
    logger.info(f"[fundamental] pb filter: {n} -> {len(df)}")

    # 总市值
    n = len(df)
    if "total_market_cap" in df.columns:
        df = df[df["total_market_cap"].notna() & (df["total_market_cap"] >= settings.SCAN_MIN_TOTAL_MARKET_CAP) & (df["total_market_cap"] <= settings.SCAN_MAX_TOTAL_MARKET_CAP)]
    logger.info(f"[fundamental] market_cap filter: {n} -> {len(df)}")

    # 60日动量
    n = len(df)
    if "momentum_60d" in df.columns:
        df = df[df["momentum_60d"].notna() & (df["momentum_60d"] <= settings.SCAN_MAX_MOMENTUM_60D)]
    logger.info(f"[fundamental] momentum_60d filter: {n} -> {len(df)}")

    # 年初至今动量
    n = len(df)
    if "momentum_ytd" in df.columns:
        df = df[df["momentum_ytd"].notna() & (df["momentum_ytd"] <= settings.SCAN_MAX_YTD_MOMENTUM)]
    logger.info(f"[fundamental] momentum_ytd filter: {n} -> {len(df)}")

    return df


def apply_report_fundamental_filter(df: pd.DataFrame) -> pd.DataFrame:
    """对合并了财报数据的 DataFrame 进行过滤。"""
    n = len(df)
    if "revenue" in df.columns:
        df = df[df["revenue"].notna() & (df["revenue"] >= settings.SCAN_MIN_REVENUE)]
    logger.info(f"[fundamental] revenue filter: {n} -> {len(df)}")

    n = len(df)
    if "revenue_yoy" in df.columns:
        df = df[df["revenue_yoy"].notna() & (df["revenue_yoy"] >= settings.SCAN_MIN_REVENUE_YOY)]
    logger.info(f"[fundamental] revenue_yoy filter: {n} -> {len(df)}")

    n = len(df)
    if "net_profit" in df.columns:
        df = df[df["net_profit"].notna() & (df["net_profit"] >= settings.SCAN_MIN_NET_PROFIT)]
    logger.info(f"[fundamental] net_profit filter: {n} -> {len(df)}")

    n = len(df)
    if "roe" in df.columns:
        df = df[df["roe"].notna() & (df["roe"] >= settings.SCAN_MIN_ROE)]
    logger.info(f"[fundamental] roe filter: {n} -> {len(df)}")

    n = len(df)
    if "gross_margin" in df.columns:
        df = df[df["gross_margin"].notna() & (df["gross_margin"] >= settings.SCAN_MIN_GROSS_MARGIN)]
    logger.info(f"[fundamental] gross_margin filter: {n} -> {len(df)}")

    return df


def apply_fundamental_filters(pre_filtered: list[dict]) -> list[dict]:
    """对 pre_filtered（list[dict]）进行基本面过滤。

    在 market_scanner.py 的粗筛阶段调用，拉历史 K 线之前。
    输入输出均为 list[dict]，每个 dict 至少含 code 和 name。
    """
    if not pre_filtered:
        return pre_filtered

    # 转 DataFrame 便于 merge
    pf_df = pd.DataFrame(pre_filtered)
    pf_df["code"] = pf_df["code"].apply(normalize_stock_code)
    n_before = len(pf_df)

    # 1. 拉取并合并实时快照基本面
    spot_df = load_or_fetch_spot_fundamentals()
    if spot_df is None:
        # 降级：跳过基本面过滤
        return pre_filtered

    pf_df = pf_df.merge(spot_df, on="code", how="left", suffixes=("", "_spot"))
    logger.info(f"[fundamental] spot merge: {n_before} -> {len(pf_df)}")

    # 2. 实时快照过滤
    n_before_spot = len(pf_df)
    pf_df = apply_spot_fundamental_filter(pf_df)
    logger.info(f"[fundamental] spot filter: {n_before_spot} -> {len(pf_df)}")

    if pf_df.empty:
        logger.warning("[fundamental] all candidates filtered out by spot filter")
        return []

    # 3. 拉取并合并财报数据
    if settings.SCAN_ENABLE_REPORT_FILTER:
        report_df = load_or_fetch_report_fundamentals(settings.SCAN_REPORT_DATE)
        if report_df is not None:
            n_before_report = len(pf_df)
            pf_df = pf_df.merge(report_df, on="code", how="left", suffixes=("", "_report"))
            logger.info(f"[fundamental] report merge: {n_before_report} -> {len(pf_df)}")

            n_before_report_filter = len(pf_df)
            pf_df = apply_report_fundamental_filter(pf_df)
            logger.info(f"[fundamental] report filter: {n_before_report_filter} -> {len(pf_df)}")

    # 转回 list[dict]
    result = pf_df.to_dict("records")
    logger.info(f"[fundamental] final: {n_before} -> {len(result)} candidates")
    return result

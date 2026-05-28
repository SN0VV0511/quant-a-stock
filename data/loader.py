"""
数据加载器
使用 BaoStock 获取 A 股/ETF 行情数据，支持缓存
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta

import baostock as bs
import pandas as pd

from config.settings import CACHE_DIR, is_etf, is_chinext

logger = logging.getLogger(__name__)


class DataLoader:
    """BaoStock 数据加载器，支持缓存"""

    def __init__(self, cache_dir=None, cache_expire_hours=4):
        self.cache_dir = cache_dir or CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_expire_hours = cache_expire_hours
        self._bs_logged_in = False

    def _login(self):
        """登录 BaoStock"""
        if not self._bs_logged_in:
            result = bs.login()
            if result.error_code != "0":
                raise ConnectionError(f"BaoStock 登录失败: {result.error_msg}")
            self._bs_logged_in = True

    def _logout(self):
        """登出 BaoStock"""
        if self._bs_logged_in:
            bs.logout()
            self._bs_logged_in = False

    def __enter__(self):
        self._login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._logout()

    def _cache_path(self, prefix, code, **kwargs):
        """生成缓存文件路径"""
        parts = [prefix, code]
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}_{v}")
        return os.path.join(self.cache_dir, "_".join(parts) + ".csv")

    def _cache_meta_path(self, prefix, code, **kwargs):
        """缓存元信息路径"""
        parts = [prefix, code]
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}_{v}")
        return os.path.join(self.cache_dir, "_".join(parts) + ".meta.json")

    def _is_cache_valid(self, prefix, code, **kwargs):
        """检查缓存是否有效"""
        meta_path = self._cache_meta_path(prefix, code, **kwargs)
        data_path = self._cache_path(prefix, code, **kwargs)

        if not os.path.exists(meta_path) or not os.path.exists(data_path):
            return False

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            cached_time = meta.get("cached_at", 0)
            # 缓存过期检查
            expire_seconds = self.cache_expire_hours * 3600
            return (time.time() - cached_time) < expire_seconds
        except (json.JSONDecodeError, KeyError):
            return False

    def _save_cache(self, df, prefix, code, **kwargs):
        """保存数据到缓存"""
        if df is None or df.empty:
            return
        data_path = self._cache_path(prefix, code, **kwargs).replace(".parquet", ".csv")
        meta_path = self._cache_meta_path(prefix, code, **kwargs)
        df.to_csv(data_path, index=False, encoding="utf-8-sig")
        with open(meta_path, "w") as f:
            json.dump({"cached_at": time.time(), "rows": len(df)}, f)

    def _load_cache(self, prefix, code, **kwargs):
        """从缓存加载数据"""
        data_path = self._cache_path(prefix, code, **kwargs).replace(".parquet", ".csv")
        if os.path.exists(data_path):
            return pd.read_csv(data_path)
        return None

    def get_daily_data(self, code, start_date, end_date, adjust_flag="2",
                       fields=None, use_cache=True):
        """获取股票/ETF 日线数据

        Args:
            code: BaoStock 代码，如 sh.601988, sz.159915
            start_date: 开始日期 YYYY-MM-DD 或 YYYYMMDD
            end_date: 结束日期
            adjust_flag: 复权类型 "2"=前复权, "1"=后复权, "3"=不复权
            fields: 字段列表，None 为全部
            use_cache: 是否使用缓存

        Returns:
            pd.DataFrame
        """
        # 标准化日期格式
        start_date = start_date.replace("-", "")
        end_date = end_date.replace("-", "")
        # BaoStock 需要 YYYY-MM-DD 格式
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code

        cache_key = {"start": start_date, "end": end_date, "adj": adjust_flag}

        if use_cache and self._is_cache_valid("daily", code_bs, **cache_key):
            return self._load_cache("daily", code_bs, **cache_key)

        self._login()

        if fields is None:
            fields_list = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg"
        elif isinstance(fields, list):
            fields_list = ",".join(fields)
        else:
            fields_list = fields

        rs = bs.query_history_k_data_plus(
            code_bs, fields_list,
            start_date=start_fmt, end_date=end_fmt,
            frequency="d", adjustflag=adjust_flag,
        )

        if rs is None:
            logger.warning(f"查询返回 None: {code_bs} {start_fmt}-{end_fmt}")
            return pd.DataFrame()

        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            logger.warning(f"无数据: {code_bs} {start_date}-{end_date}")
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)

        # 转换数值列
        numeric_cols = ["open", "high", "low", "close", "preclose", "volume",
                        "amount", "turn", "pctChg"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if use_cache:
            self._save_cache(df, "daily", code_bs, **cache_key)

        return df

    def get_latest_price(self, code):
        """获取最新收盘价

        Returns:
            float or None
        """
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

        df = self.get_daily_data(code, start_date, end_date, adjust_flag="3",
                                 fields="date,close", use_cache=False)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            return float(last["close"]) if pd.notna(last["close"]) else None
        return None

    def get_batch_latest_prices(self, code_list):
        """批量获取最新价格

        Args:
            code_list: 代码列表

        Returns:
            dict {code: price}
        """
        result = {}
        for code in code_list:
            price = self.get_latest_price(code)
            if price is not None:
                result[code] = price
        return result

    def get_trading_calendar(self, start_date, end_date):
        """获取交易日历

        Args:
            start_date: YYYYMMDD
            end_date: YYYYMMDD

        Returns:
            list of str: 交易日期列表
        """
        start_date = start_date.replace("-", "")
        end_date = end_date.replace("-", "")
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

        cache_key = {"start": start_date, "end": end_date}
        cache_prefix = "trade_cal"

        if self._is_cache_valid(cache_prefix, "all", **cache_key):
            df = self._load_cache(cache_prefix, "all", **cache_key)
            if df is not None and not df.empty:
                return df[df["is_trading_day"] == "1"]["calendar_date"].str.replace("-", "").tolist()

        self._login()
        rs = bs.query_trade_dates(start_date=start_fmt, end_date=end_fmt)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return []

        df = pd.DataFrame(rows, columns=["calendar_date", "is_trading_day"])
        self._save_cache(df, cache_prefix, "all", **cache_key)
        return df[df["is_trading_day"] == "1"]["calendar_date"].str.replace("-", "").tolist()

    def is_trading_day(self, date_str):
        """判断是否为交易日

        基于 AKShare 交易日历，自动获取、覆盖全年。

        Args:
            date_str: YYYYMMDD

        Returns:
            bool
        """
        from data.holidays import is_trading_day as _check
        return _check(date_str)

    def get_stock_basic(self, code):
        """获取股票基本信息

        Args:
            code: BaoStock 代码

        Returns:
            dict: {code, code_name, ipoDate, outDate, type, status}
        """
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code

        cache_path = os.path.join(self.cache_dir, f"basic_{code_bs}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    meta = json.load(f)
                if time.time() - meta.get("cached_at", 0) < 86400:  # 1天缓存
                    return meta.get("data")
            except (json.JSONDecodeError, KeyError):
                pass

        self._login()
        rs = bs.query_stock_basic(code=code_bs)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return {}

        info = dict(zip(rs.fields, rows[0]))

        with open(cache_path, "w") as f:
            json.dump({"cached_at": time.time(), "data": info}, f, ensure_ascii=False)

        return info

    def get_stock_industry(self, code):
        """获取股票所属行业

        Args:
            code: BaoStock 代码

        Returns:
            dict
        """
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code
        self._login()

        rs = bs.query_stock_industry(code=code_bs)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return {}
        return dict(zip(rs.fields, rows[0]))

    def get_profit_data(self, code, year, quarter):
        """获取盈利能力数据

        Args:
            code: BaoStock 代码
            year: 年份
            quarter: 季度 1-4

        Returns:
            pd.DataFrame
        """
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code
        self._login()

        rs = bs.query_profit_data(code=code_bs, year=year, quarter=quarter)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        numeric_cols = ["roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM",
                        "MBRevenue", "totalShare", "liqaShare"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_cashflow_data(self, code, year, quarter):
        """获取现金流数据

        Returns:
            pd.DataFrame
        """
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code
        self._login()

        rs = bs.query_cash_flow_data(code=code_bs, year=year, quarter=quarter)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        numeric_cols = ["CAToAsset", "NCAToAsset", "tangibleAssetToAsset",
                        "ocfToProfit", "OCFToOR"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_growth_data(self, code, year, quarter):
        """获取成长能力数据

        Returns:
            pd.DataFrame
        """
        code_bs = code.replace("sh", "sh.").replace("sz", "sz.") if "." not in code else code
        self._login()

        rs = bs.query_growth_data(code=code_bs, year=year, quarter=quarter)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        numeric_cols = ["YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def preload_data(self, code_list, start_date, end_date, adjust_flag="2"):
        """批量预加载数据

        Args:
            code_list: 代码列表
            start_date: 开始日期
            end_date: 结束日期
            adjust_flag: 复权方式
        """
        self._login()
        success = 0
        failed = []
        for code in code_list:
            try:
                df = self.get_daily_data(code, start_date, end_date,
                                         adjust_flag=adjust_flag, use_cache=True)
                if df is not None and not df.empty:
                    success += 1
                else:
                    failed.append(code)
            except Exception as e:
                logger.error(f"预加载失败 {code}: {e}")
                failed.append(code)

        logger.info(f"预加载完成: 成功 {success}, 失败 {len(failed)}")
        if failed:
            logger.warning(f"失败代码: {failed}")
        return success, failed

    def close(self):
        """关闭连接"""
        self._logout()

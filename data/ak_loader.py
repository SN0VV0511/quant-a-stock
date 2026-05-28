"""
沪深 A 股股票数据加载器
- 股票列表: BaoStock
- 实时行情: 腾讯接口（2026 年实时数据）
- 历史数据: BaoStock
"""

import os
import sys
import json
import time
import socket
import threading
import logging
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np

from config.settings import (
    is_a_share_stock,
    normalize_a_share_code,
    to_baostock_code,
    to_tencent_code,
)

try:
    import baostock as bs
except ImportError:  # pragma: no cover - 运行环境可能未安装行情依赖
    bs = None

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(30)


def _require_baostock():
    """确保 BaoStock 依赖已安装。"""
    if bs is None:
        raise ImportError("缺少 baostock 依赖，请先执行: pip install -r requirements.txt")


class AKDataLoader:
    """沪深 A 股股票数据加载器。"""

    def __init__(self, cache_dir=None, cache_ttl=300):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_ttl = cache_ttl
        self._stock_list_cache = None
        self._stock_list_cache_time = 0
        self._bs_logged_in = False
        self._bs_lock = threading.Lock()

    def _login(self):
        _require_baostock()
        if not self._bs_logged_in:
            try:
                bs.login()
                self._bs_logged_in = True
            except Exception:
                self._bs_logged_in = False
                raise

    def _ensure_login(self):
        """确保 BaoStock 连接有效，已登录时用轻量查询验证，失败则重新登录，共重试 3 次。"""
        _require_baostock()
        for attempt in range(3):
            if self._bs_logged_in:
                try:
                    rs = bs.query_stock_basic("sh.600000")
                    if rs.error_code == "0":
                        return
                    # 查询返回错误码，连接已失效
                    logger.warning(f"BaoStock 连接已失效 (error_code={rs.error_code})，重新登录")
                    self._bs_logged_in = False
                except Exception as e:
                    logger.warning(f"BaoStock 连接验证失败: {e}，重新登录")
                    self._bs_logged_in = False

            try:
                bs.login()
                self._bs_logged_in = True
                return
            except Exception as e:
                wait = 2 * (attempt + 1)
                logger.warning(f"BaoStock 登录失败 (第{attempt+1}次): {e}, {wait}s 后重试")
                self._bs_logged_in = False
                if attempt < 2:
                    time.sleep(wait)
        raise ConnectionError("BaoStock 登录重试 3 次均失败")

    def _logout(self):
        if self._bs_logged_in and bs is not None:
            bs.logout()
            self._bs_logged_in = False

    def close(self):
        self._logout()

    def get_all_stocks(self):
        """获取沪深 A 股股票列表（BaoStock）。

        自动回退到最近有数据的日期（盘中/节假日场景）
        """
        now = time.time()
        if self._stock_list_cache and (now - self._stock_list_cache_time) < 3600:
            return self._stock_list_cache

        self._ensure_login()

        # 尝试今天及前 5 天，BaoStock 盘中可能没数据
        rows = []
        for offset in range(6):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            rs = bs.query_all_stock(day=day)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                logger.info(f"股票列表使用日期: {day} ({len(rows)} 条)")
                break

        stocks = []
        for row in rows:
            code = row[0]
            stock_type = row[1] if len(row) > 1 else ""
            name = row[2] if len(row) > 2 else ""

            if stock_type != "1":
                continue

            if "ST" in name.upper() or "退" in name:
                continue
            if not is_a_share_stock(code):
                continue

            stocks.append({
                "code": normalize_a_share_code(code),
                "bs_code": to_baostock_code(code),
                "name": name,
            })

        self._stock_list_cache = stocks
        self._stock_list_cache_time = now
        logger.info(f"获取沪深 A 股股票列表: {len(stocks)} 只")
        return stocks

    def get_realtime_quotes(self, codes=None):
        """获取沪深 A 股实时行情（腾讯接口）。

        Args:
            codes: 股票代码列表，None 则获取沪深 A 股股票全市场。

        Returns:
            dict: code -> {price, open, high, low, prev_close, volume, amount, pct_change}
        """
        if codes is None:
            stocks = self.get_all_stocks()
            codes = [s["code"] for s in stocks]

        tencent_codes = []
        seen_codes = set()
        invalid_codes = []
        for code in codes:
            try:
                raw_code = normalize_a_share_code(str(code))
                tencent_code = to_tencent_code(str(code))
            except ValueError:
                invalid_codes.append(str(code))
                continue
            if raw_code in seen_codes:
                continue
            seen_codes.add(raw_code)
            tencent_codes.append(tencent_code)

        if invalid_codes:
            preview = ", ".join(invalid_codes[:20])
            if len(invalid_codes) > 20:
                preview = f"{preview}, ..."
            logger.warning("实时行情忽略非沪深 A 股股票代码: %s", preview)

        if not tencent_codes:
            logger.info("没有可请求的沪深 A 股实时行情代码")
            return {}

        quotes = {}

        # 批量查询（每批 100 个）
        batch_size = 100
        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i:i + batch_size]
            batch_str = ",".join(batch)

            try:
                url = f"https://qt.gtimg.cn/q={batch_str}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=15)
                data = resp.read().decode("gbk")

                for line in data.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue

                    try:
                        parts = line.split('"')[1].split("~")
                        if len(parts) < 40:
                            continue

                        code = parts[2]
                        name = parts[1] if len(parts) > 1 else ""
                        price = float(parts[3]) if parts[3] else 0
                        if price <= 0:
                            continue

                        prev_close = float(parts[4]) if parts[4] else price
                        open_price = float(parts[5]) if parts[5] else price
                        volume = float(parts[6]) if parts[6] else 0  # 手
                        high = float(parts[33]) if parts[33] else price
                        low = float(parts[34]) if parts[34] else price
                        change_pct = float(parts[32]) if parts[32] else 0

                        quotes[code] = {
                            "name": name,
                            "price": price,
                            "open": open_price,
                            "high": high,
                            "low": low,
                            "prev_close": prev_close,
                            "volume": volume * 100,  # 转为股
                            "pct_change": change_pct,
                        }
                    except (IndexError, ValueError):
                        continue

            except Exception as e:
                logger.warning(f"腾讯行情批次 {i//batch_size} 失败: {e}")
                time.sleep(1)
                continue

            if i > 0 and i % 500 == 0:
                logger.info(f"已获取 {i}/{len(tencent_codes)} 行情")

        logger.info(f"获取实时行情: {len(quotes)} 只")
        return quotes

    def get_stock_history(self, code, days=120):
        """获取沪深 A 股个股历史数据（BaoStock），捕获连接异常后重连再重试。"""
        try:
            raw_code = normalize_a_share_code(str(code))
            bs_code = to_baostock_code(str(code))
        except ValueError as exc:
            logger.warning("历史行情忽略非沪深 A 股股票代码: %s", exc)
            return None

        cache_key = f"hist_{raw_code}_{days}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

        with self._bs_lock:
            self._ensure_login()

            for attempt in range(3):
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount,preclose,pctChg",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="2",
                    )

                    rows = []
                    while rs.error_code == "0" and rs.next():
                        rows.append(rs.get_row_data())

                    if not rows:
                        return None

                    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"])
                    for col in ["open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"]:
                        df[col] = pd.to_numeric(df[col], errors="coerce")

                    if len(df) == 0 or df["close"].iloc[-1] <= 0:
                        return None

                    self._write_cache(cache_key, df)
                    return df

                except (ConnectionError, OSError, socket.timeout, socket.error) as e:
                    logger.warning(f"get_stock_history({code}) 连接异常 (第{attempt+1}次): {e}")
                    self._bs_logged_in = False
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        try:
                            bs.login()
                            self._bs_logged_in = True
                        except Exception:
                            pass
                        continue
                    return None
                except Exception as e:
                    logger.warning(f"get_stock_history({code}) 异常: {e}")
                    return None

            return None

    def get_batch_history(self, codes, days=120, max_batch=5000):
        """批量获取历史数据（ThreadPoolExecutor 并发加载）"""
        result = {}
        total = min(len(codes), max_batch)
        target_codes = codes[:max_batch]

        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(self.get_stock_history, code, days): code
                for code in target_codes
            }
            done_count = 0
            for future in as_completed(future_map):
                code = future_map[future]
                done_count += 1
                try:
                    df = future.result()
                    if df is not None and not df.empty:
                        result[code] = df
                except Exception as e:
                    logger.debug(f"get_batch_history({code}) 失败: {e}")
                if done_count % 100 == 0:
                    logger.info(f"历史数据进度: {done_count}/{total}")

        logger.info(f"批量历史数据完成: {len(result)}/{total} 只成功")
        return result

    def get_realtime_batch(self, codes):
        """批量获取实时价格（返回 code->price 字典）"""
        quotes = self.get_realtime_quotes(codes)
        return {code: q["price"] for code, q in quotes.items()}

    def get_stock_data(self, code, days=60):
        """获取个股历史数据（兼容别名）"""
        return self.get_stock_history(code, days=days)

    def _cache_path(self, key):
        return os.path.join(self.cache_dir, f"{key}.pkl")

    def _read_cache(self, key):
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            meta_path = path + ".meta"
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                if time.time() - meta.get("ts", 0) > self.cache_ttl:
                    return None
            return pd.read_pickle(path)
        except Exception:
            return None

    def _write_cache(self, key, df):
        try:
            path = self._cache_path(key)
            df.to_pickle(path)
            with open(path + ".meta", "w") as f:
                json.dump({"ts": time.time()}, f)
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    loader = AKDataLoader()

    stocks = loader.get_all_stocks()
    print(f"沪深 A 股股票: {len(stocks)} 只")

    # 测试实时行情（前 10 只）
    codes = [s["code"] for s in stocks[:10]]
    quotes = loader.get_realtime_quotes(codes)
    print(f"实时行情: {len(quotes)} 只")
    for code, q in quotes.items():
        print(f"  {code}: 现价={q['price']:.2f} 涨跌={q['pct_change']:+.2f}%")

    loader.close()

"""
沪深 A 股股票数据加载器
- 股票列表: BaoStock（不可用时回退到缓存文件）
- 实时行情: 腾讯接口（2026 年实时数据）
- 历史数据: BaoStock
"""

import os
import sys
import json
import time
import socket
import subprocess
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


_BS_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bs_worker.py")


def _run_bs_with_subprocess(command, *args, timeout=30):
    """用 subprocess 执行 BaoStock 查询，绕过 GIL 导致的 threading 超时失效。

    Returns:
        dict: 子进程返回的 JSON（含 error_code / rows），异常或超时返回 None。
    """
    cmd = [sys.executable, _BS_WORKER, command, *args]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.warning("bs_worker %s 失败 (rc=%s): %s", command, proc.returncode, err_msg)
            return None
        return json.loads(stdout.decode("utf-8"))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.warning("bs_worker %s 超时 (%ds)，已 kill", command, timeout)
        return None
    except Exception as e:
        logger.warning("bs_worker %s 异常: %s", command, e)
        try:
            proc.kill()
            proc.communicate()
        except Exception:
            pass
        return None


def _require_baostock():
    """确保 BaoStock 依赖已安装。"""
    if bs is None:
        raise ImportError("缺少 baostock 依赖，请先执行: pip install -r requirements.txt")


class AKDataLoader:
    """沪深 A 股股票数据加载器。"""

    def __init__(self, cache_dir=None, cache_ttl=3600):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_ttl = cache_ttl
        self._stock_list_cache = None
        self._stock_list_cache_time = 0
        self._bs_logged_in = False
        self._bs_available = True
        self._bs_lock = threading.Lock()
        self._bs_last_verified = 0  # timestamp of last successful verification
        self._bs_verify_interval = 300  # re-verify at most every 5 minutes

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
        """确保 BaoStock 连接有效，通过子进程验证，失败则重新登录，共重试 3 次。"""
        if not self._bs_available:
            raise ConnectionError("BaoStock 已标记为不可用，跳过登录")
        _require_baostock()
        # Skip re-verification if recently verified
        now = time.time()
        if self._bs_logged_in and (now - self._bs_last_verified) < self._bs_verify_interval:
            return
        for attempt in range(3):
            if self._bs_logged_in:
                result = _run_bs_with_subprocess("query_stock_basic", "sh.600000")
                if result is not None and result.get("error_code") == "0":
                    self._bs_last_verified = time.time()
                    return
                logger.warning("BaoStock 连接已失效，重新登录")
                self._bs_logged_in = False

            result = _run_bs_with_subprocess("login")
            if result is not None and result.get("error_code") == "0":
                self._bs_logged_in = True
                self._bs_last_verified = time.time()
                return
            wait = 2 * (attempt + 1)
            logger.warning("BaoStock 登录失败 (第%d次), %ds 后重试", attempt + 1, wait)
            self._bs_logged_in = False
            if attempt < 2:
                time.sleep(wait)
        self._bs_available = False
        logger.error("BaoStock 登录重试 3 次均失败，标记为不可用，后续调用将跳过 BaoStock")
        raise ConnectionError("BaoStock 登录重试 3 次均失败")

    def _logout(self):
        if self._bs_logged_in and bs is not None:
            bs.logout()
            self._bs_logged_in = False

    def close(self):
        self._logout()

    def _get_stocks_from_cache(self):
        """从 data/cache/hist_*.pkl 文件中提取股票代码作为备用列表。"""
        stocks = []
        try:
            for fname in os.listdir(self.cache_dir):
                if not fname.startswith("hist_") or not fname.endswith(".pkl"):
                    continue
                # 文件名格式: hist_{code}_{days}.pkl
                parts = fname[len("hist_"):-len(".pkl")].rsplit("_", 1)
                if not parts:
                    continue
                raw_code = parts[0]
                if is_a_share_stock(raw_code):
                    stocks.append({
                        "code": normalize_a_share_code(raw_code),
                        "bs_code": to_baostock_code(raw_code),
                        "name": "",
                    })
        except Exception as e:
            logger.warning(f"从缓存目录读取股票列表失败: {e}")
        stocks.sort(key=lambda s: s["code"])
        logger.info(f"从缓存文件中恢复股票列表: {len(stocks)} 只")
        return stocks

    def get_all_stocks(self):
        """获取沪深 A 股股票列表（BaoStock）。

        BaoStock 不可用时自动回退到缓存文件中的股票列表。
        """
        now = time.time()
        if self._stock_list_cache and (now - self._stock_list_cache_time) < 3600:
            return self._stock_list_cache

        # BaoStock 已标记不可用，直接走缓存
        if not self._bs_available:
            stocks = self._get_stocks_from_cache()
            self._stock_list_cache = stocks
            self._stock_list_cache_time = now
            return stocks

        try:
            self._ensure_login()
        except ConnectionError:
            stocks = self._get_stocks_from_cache()
            self._stock_list_cache = stocks
            self._stock_list_cache_time = now
            return stocks

        # 尝试今天及前 5 天，BaoStock 盘中可能没数据
        rows = []
        for offset in range(6):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            result = _run_bs_with_subprocess("query_all_stock", day)
            if result is None:
                logger.warning("BaoStock query_all_stock(%s) 失败或超时", day)
                continue
            if result.get("error_code") != "0":
                logger.warning("BaoStock query_all_stock(%s) 错误: %s", day, result.get("error_code"))
                continue
            rows = result.get("rows", [])
            if rows:
                logger.info("股票列表使用日期: %s (%d 条)", day, len(rows))
                break

        # BaoStock 返回 0 条，回退到缓存
        if not rows:
            logger.warning("BaoStock 返回 0 条记录，回退到缓存文件中的股票列表")
            stocks = self._get_stocks_from_cache()
            self._stock_list_cache = stocks
            self._stock_list_cache_time = now
            return stocks

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

        if not self._bs_available:
            return None

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

        self._ensure_login()

        for attempt in range(3):
            result = _run_bs_with_subprocess("query_history", bs_code, start, end)

            if result is None:
                logger.warning("get_stock_history(%s) 超时或异常 (第%d次)", code, attempt + 1)
                self._bs_logged_in = False
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

            if result.get("error_code") != "0":
                logger.warning("get_stock_history(%s) BaoStock 错误: %s", code, result.get("error_code"))
                return None

            rows = result.get("rows", [])
            if not rows:
                return None

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"])
            for col in ["open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            if len(df) == 0 or df["close"].iloc[-1] <= 0:
                return None

            self._write_cache(cache_key, df)
            return df

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

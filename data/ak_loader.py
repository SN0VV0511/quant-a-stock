"""
沪深 A 股股票数据加载器
- 股票列表: BaoStock（不可用时回退到缓存文件）
- 实时行情: 腾讯接口（2026 年实时数据）
- 历史数据: BaoStock
"""

from __future__ import annotations

import os
import sys
import json
import time
import socket
import signal
import subprocess
import threading
import logging
import urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np

from config.settings import (
    BAOSTOCK_HISTORY_BATCH_SIZE,
    BAOSTOCK_HISTORY_BATCH_WORKERS,
    BAOSTOCK_HISTORY_CACHE_TTL_SECONDS,
    BAOSTOCK_HISTORY_STALE_MAX_AGE_SECONDS,
    BAOSTOCK_HISTORY_TIMEOUT_PER_STOCK_SECONDS,
    is_a_share_stock,
    is_etf,
    normalize_a_share_code,
    to_baostock_code,
    to_tencent_security_code,
    INDUSTRY_TENCENT_MAP,
)

try:
    import baostock as bs
except ImportError:  # pragma: no cover - 运行环境可能未安装行情依赖
    bs = None

try:
    import akshare as ak
except ImportError:  # pragma: no cover - 运行环境可能未安装行情依赖
    ak = None

try:
    import requests as _requests_lib
except ImportError:  # pragma: no cover
    _requests_lib = None

logger = logging.getLogger(__name__)

socket.setdefaulttimeout(30)


_BS_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bs_worker.py")
_BS_SUBPROCESS_TIMEOUT_SECONDS = 15
_BS_UNIVERSE_TIMEOUT_SECONDS = 5
_TENCENT_BATCH_SIZE = 100
_TENCENT_MAX_WORKERS = 6
_TENCENT_REQUEST_TIMEOUT_SECONDS = 5


def _reap_process(proc: subprocess.Popen[bytes], command: str) -> None:
    """在守护线程中回收已终止的子进程，避免阻塞业务线程。"""
    try:
        proc.wait()
    except Exception as exc:  # pragma: no cover - 仅用于尽力回收异常进程
        logger.debug("回收 bs_worker %s 失败: %s", command, exc)


def _terminate_bs_worker(proc: subprocess.Popen[bytes], command: str) -> None:
    """终止 BaoStock worker，但绝不在调用线程中等待其退出。"""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows 兼容分支
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.debug("终止 bs_worker %s 失败: %s", command, exc)
        try:
            proc.kill()
        except Exception:
            pass

    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except Exception:
            pass

    threading.Thread(
        target=_reap_process,
        args=(proc, command),
        name=f"bs-worker-reaper-{command}",
        daemon=True,
    ).start()


def _run_bs_with_subprocess(
    command: str,
    *args: str,
    timeout: float = _BS_SUBPROCESS_TIMEOUT_SECONDS,
) -> dict[str, object] | None:
    """用 subprocess 执行 BaoStock 查询，绕过 GIL 导致的 threading 超时失效。

    超时后只负责发送终止信号和关闭管道，进程回收交给守护线程。这样即使
    worker 卡在不可中断的系统调用中，也不会再次阻塞扫描线程。

    Args:
        command: worker 命令名称。
        *args: 传递给 worker 的字符串参数。
        timeout: 最大等待秒数。

    Returns:
        子进程返回的 JSON；异常或超时返回 ``None``。
    """
    cmd = [sys.executable, _BS_WORKER, command, *args]
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout, _ = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            logger.warning("bs_worker %s 失败 (rc=%s)", command, proc.returncode)
            return None
        return json.loads(stdout.decode("utf-8"))
    except subprocess.TimeoutExpired:
        if proc is not None:
            _terminate_bs_worker(proc, command)
        logger.warning("bs_worker %s 超时 (%.1fs)，已请求终止", command, timeout)
        return None
    except Exception as e:
        logger.warning("bs_worker %s 异常: %s", command, e)
        if proc is not None and proc.poll() is None:
            _terminate_bs_worker(proc, command)
        return None


def _require_baostock():
    """确保 BaoStock 依赖已安装。"""
    if bs is None:
        raise ImportError("缺少 baostock 依赖，请先执行: pip install -r requirements.txt")


def _require_akshare():
    """确保 AKShare 依赖已安装。"""
    if ak is None:
        raise ImportError("缺少 akshare 依赖，请先执行: pip install -r requirements.txt")


def _fetch_tencent_quote_batch(
    tencent_codes: list[str],
    timeout: float = _TENCENT_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, dict[str, float | str]]:
    """请求并解析一批腾讯实时行情。

    Args:
        tencent_codes: 腾讯格式证券代码列表，例如 ``sh600519``。
        timeout: 单次 HTTP 请求超时秒数。

    Returns:
        以六位证券代码为键的实时行情字典。

    Raises:
        OSError: 网络连接或读取失败。
        UnicodeError: 响应编码异常。
    """
    batch_str = ",".join(tencent_codes)
    url = f"https://qt.gtimg.cn/q={batch_str}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(request, timeout=timeout)
    try:
        data = response.read().decode("gbk")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    quotes: dict[str, dict[str, float | str]] = {}
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
            volume = float(parts[6]) if parts[6] else 0
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
                "volume": volume * 100,
                "pct_change": change_pct,
            }
        except (IndexError, ValueError):
            logger.debug("腾讯行情响应行解析失败: %r", line[:200])
    return quotes


def _history_rows_to_dataframe(rows: list[list[str]]) -> pd.DataFrame | None:
    """将 BaoStock 历史行情行转换为统一 DataFrame。"""
    if not rows:
        return None

    columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "preclose",
        "pctChg",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    for column in columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.empty or frame["close"].iloc[-1] <= 0:
        return None
    return frame


def _normalize_compact_date(value: object) -> str:
    """将常见日期值归一化为 YYYYMMDD。"""
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text.replace("-", "").replace("/", "")[:8]
    return parsed.strftime("%Y%m%d")


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否为网络类错误，用于决定是否重试。"""
    msg = str(exc).lower()
    network_keywords = (
        "remote end closed connection",
        "connection aborted",
        "remotedisconnected",
        "connection reset",
        "timed out",
        "timeout",
        "max retries exceeded",
        "too many requests",
        "service unavailable",
    )
    return any(kw in msg for kw in network_keywords)


def _safe_cache_fragment(value: object) -> str:
    """生成可用于缓存文件名的短字符串。"""
    raw = str(value).strip()
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw)
    return safe[:80] or "empty"


def _pick_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """按候选名称查找 DataFrame 列。"""
    for column in candidates:
        if column in df.columns:
            return column
    return None


def _normalize_market_history(
    raw_df: pd.DataFrame,
    code: str,
    name: str | None = None,
) -> pd.DataFrame:
    """将 AKShare ETF/行业指数历史行情标准化为策略统一字段。

    Args:
        raw_df: AKShare 原始返回数据。
        code: 标的代码或行业名称。
        name: 展示名称。

    Returns:
        含 ``date/open/high/low/close/volume/amount/pctChg`` 的 DataFrame。

    Raises:
        ValueError: 当缺少日期或收盘价字段时抛出。
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    columns = {
        "date": ("日期", "date", "时间"),
        "open": ("开盘", "open", "开盘价"),
        "high": ("最高", "high", "最高价"),
        "low": ("最低", "low", "最低价"),
        "close": ("收盘", "close", "收盘价"),
        "volume": ("成交量", "volume", "成交量(股)"),
        "amount": ("成交额", "amount", "成交额(元)"),
        "pctChg": ("涨跌幅", "pctChg", "涨幅"),
    }
    picked = {target: _pick_column(raw_df, candidates) for target, candidates in columns.items()}
    if picked["date"] is None or picked["close"] is None:
        raise ValueError(f"行情数据缺少必要字段: columns={list(raw_df.columns)}")

    df = pd.DataFrame()
    df["date"] = raw_df[picked["date"]].map(_normalize_compact_date)
    df["code"] = code
    df["name"] = name or code
    for target in ("open", "high", "low", "close", "volume", "amount", "pctChg"):
        source = picked[target]
        if source is None:
            df[target] = np.nan
        else:
            df[target] = pd.to_numeric(raw_df[source], errors="coerce")

    df = df.dropna(subset=["date", "close"])
    df = df[df["date"].astype(str).str.len() == 8]
    df = df[df["close"] > 0]
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 新浪/腾讯 fallback – AKShare 失败时的备用数据源
# ---------------------------------------------------------------------------

def _sina_symbol_for_etf(code: str) -> str:
    """ETF 代码转换为新浪 symbol (sh510300 / sz159915)。"""
    raw = code.strip().lower().replace(".", "")
    if raw.startswith(("sh", "sz")):
        raw = raw[2:]
    # 5/6 开头→上交所, 1/2/3/4 开头→深交所
    prefix = "sh" if raw[:1] in ("5", "6") else "sz"
    return f"{prefix}{raw}"


def _fetch_etf_history_sina(
    code: str,
    datalen: int = 120,
) -> pd.DataFrame | None:
    """通过新浪财经获取 ETF 日K线历史行情。"""
    if _requests_lib is None:
        return None
    symbol = _sina_symbol_for_etf(code)
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)}
    for attempt in range(2):
        try:
            r = _requests_lib.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            df = pd.DataFrame(data)
            # 新浪返回: day, open, high, low, close, volume (全是字符串)
            df = df.rename(columns={"day": "date"})
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = df["date"].map(_normalize_compact_date)
            df["code"] = code
            df["name"] = code
            df["amount"] = 0
            df["pctChg"] = np.nan
            df = df.dropna(subset=["date", "close"])
            df = df[df["close"] > 0]
            df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            return df
        except Exception as exc:
            if attempt < 1:
                time.sleep(1)
                continue
            logger.warning("新浪 ETF 历史行情获取失败 %s: %s", code, exc)
            return None
    return None


def _fetch_industry_history_tencent(
    industry_name: str,
    datalen: int = 120,
) -> pd.DataFrame | None:
    """通过腾讯财经获取行业指数日K线历史行情。

    使用 INDUSTRY_TENCENT_MAP 映射行业名到指数代码。
    """
    if _requests_lib is None:
        return None
    tencent_code = INDUSTRY_TENCENT_MAP.get(industry_name)
    if not tencent_code:
        logger.debug("行业 '%s' 无腾讯指数映射，跳过 fallback", industry_name)
        return None
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    # param 格式: code,day,start,end,count,fq
    param_str = f"{tencent_code},day,,,{datalen},qfq"
    for attempt in range(2):
        try:
            r = _requests_lib.get(url, params={"param": param_str}, timeout=15)
            r.raise_for_status()
            resp = r.json()
            # 腾讯返回结构: {"data": {code: {"qfqday": [[...], ...]}}}
            code_data = resp.get("data", {}).get(tencent_code, {})
            rows = code_data.get("qfqday") or code_data.get("day") or []
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = df["date"].map(_normalize_compact_date)
            df["code"] = industry_name
            df["name"] = industry_name
            df["amount"] = 0
            df["pctChg"] = np.nan
            df = df.dropna(subset=["date", "close"])
            df = df[df["close"] > 0]
            df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            return df
        except Exception as exc:
            if attempt < 1:
                time.sleep(1)
                continue
            logger.warning("腾讯行业指数历史行情获取失败 %s: %s", industry_name, exc)
            return None
    return None


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
            # 走子进程登录，避免 baostock C 扩展直接写 fd 2 污染日志
            result = _run_bs_with_subprocess("login")
            if result and result.get("ok"):
                self._bs_logged_in = True
            else:
                self._bs_logged_in = False
                raise ConnectionError("BaoStock 登录失败")

    def _ensure_login(self):
        """确保 BaoStock 连接有效,通过子进程验证,失败则重新登录,共重试 3 次。

        盯盘线程与扫描线程共享同一 loader,会并发触达本方法,因此用
        ``self._bs_lock`` 串行化"登录态校验 + 重登录",避免对
        ``_bs_logged_in`` / ``_bs_available`` / ``_bs_last_verified`` 的竞态。
        注意:锁只保护登录态本身,不覆盖 ``get_stock_history`` 里的历史数据
        子进程查询,从而保留 ``get_batch_history`` 的并发能力。
        """
        with self._bs_lock:
            if not self._bs_available:
                raise ConnectionError("BaoStock 已标记为不可用,跳过登录")
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
                    logger.warning("BaoStock 连接已失效,重新登录")
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
            logger.error("BaoStock 登录重试 3 次均失败,标记为不可用,后续调用将跳过 BaoStock")
            raise ConnectionError("BaoStock 登录重试 3 次均失败")

    def _logout(self):
        if self._bs_logged_in and bs is not None:
            # 走子进程登出，避免 baostock C 扩展污染日志
            _run_bs_with_subprocess("logout")
            self._bs_logged_in = False

    def close(self):
        self._logout()

    _stock_name_map_cache: dict[str, str] | None = None
    _stock_name_map_cache_time: float = 0

    def get_stock_name_map(self) -> dict[str, str]:
        """获取 A 股代码→名称映射，缓存 1 小时。"""
        now = time.time()
        if self._stock_name_map_cache and (now - self._stock_name_map_cache_time) < 3600:
            return self._stock_name_map_cache
        try:
            import akshare as ak
            df = ak.stock_info_a_code_name()
            name_map = dict(zip(df["code"].astype(str), df["name"].astype(str)))
            self._stock_name_map_cache = name_map
            self._stock_name_map_cache_time = now
            logger.info("加载 A 股名称映射: %d 只", len(name_map))
            return name_map
        except Exception as e:
            logger.warning("加载 A 股名称映射失败: %s", e)
            return {}

    def _get_stocks_from_cache(self) -> list[dict[str, str]]:
        """从历史行情缓存文件名中提取并去重股票代码。"""
        stocks_by_code: dict[str, dict[str, str]] = {}
        try:
            for entry in os.scandir(self.cache_dir):
                if not entry.is_file() or not entry.name.endswith(".pkl"):
                    continue

                prefix = next(
                    (
                        value
                        for value in ("hist_", "histext_")
                        if entry.name.startswith(value)
                    ),
                    None,
                )
                if prefix is None:
                    continue

                # 文件名格式: hist_{code}_{days}.pkl / histext_{code}_{days}.pkl
                cache_name = entry.name[len(prefix) : -len(".pkl")]
                raw_code, separator, _days = cache_name.rpartition("_")
                if not separator:
                    continue
                if is_a_share_stock(raw_code):
                    code = normalize_a_share_code(raw_code)
                    stocks_by_code[code] = {
                        "code": code,
                        "bs_code": to_baostock_code(raw_code),
                        "name": "",
                    }
        except Exception as e:
            logger.warning("从缓存目录读取股票列表失败: %s", e)
        # 用 AKShare 名称映射补全空名称
        name_map = self.get_stock_name_map()
        if name_map:
            for stock in stocks_by_code.values():
                if not stock["name"]:
                    stock["name"] = name_map.get(stock["code"], stock["code"])
        stocks = sorted(stocks_by_code.values(), key=lambda stock: stock["code"])
        logger.info("从缓存文件中恢复股票列表: %d 只", len(stocks))
        return stocks

    def get_all_stocks(self) -> list[dict[str, str]]:
        """获取沪深 A 股股票列表，优先使用本地历史缓存。

        扫描链路不能依赖 BaoStock 的可用性：只要磁盘上存在历史缓存，就直接
        构建股票池。仅在本地完全无缓存时才尝试远端查询。
        """
        now = time.time()
        if self._stock_list_cache and (now - self._stock_list_cache_time) < 3600:
            return self._stock_list_cache

        cached_stocks = self._get_stocks_from_cache()
        if cached_stocks:
            self._stock_list_cache = cached_stocks
            self._stock_list_cache_time = now
            return cached_stocks

        if not self._bs_available:
            return []

        # 尝试今天及前 5 天，BaoStock 盘中可能没数据
        rows = []
        for offset in range(6):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            result = _run_bs_with_subprocess(
                "query_all_stock",
                day,
                timeout=_BS_UNIVERSE_TIMEOUT_SECONDS,
            )
            if result is None:
                logger.error("BaoStock query_all_stock(%s) 超时或异常，立即熔断", day)
                self._bs_available = False
                break
            if result.get("error_code") != "0":
                logger.warning(
                    "BaoStock query_all_stock(%s) 错误: %s",
                    day,
                    result.get("error_code"),
                )
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
            # 去掉 sh/sz 前缀做去重 key（兼容 A 股和 ETF）
            raw_code = str(code).replace("sh", "").replace("sz", "")
            try:
                tencent_code = to_tencent_security_code(str(code))
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

        batches = [
            tencent_codes[index : index + _TENCENT_BATCH_SIZE]
            for index in range(0, len(tencent_codes), _TENCENT_BATCH_SIZE)
        ]
        worker_count = min(_TENCENT_MAX_WORKERS, len(batches))
        started_at = time.monotonic()
        logger.info(
            "开始获取腾讯实时行情: %d 只，%d 批，%d 路并发，单批超时 %ds",
            len(tencent_codes),
            len(batches),
            worker_count,
            _TENCENT_REQUEST_TIMEOUT_SECONDS,
        )

        quotes: dict[str, dict[str, float | str]] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="tencent-quotes",
        ) as executor:
            future_map = {
                executor.submit(
                    _fetch_tencent_quote_batch,
                    batch,
                    _TENCENT_REQUEST_TIMEOUT_SECONDS,
                ): (batch_index, batch)
                for batch_index, batch in enumerate(batches, start=1)
            }
            completed = 0
            for future in as_completed(future_map):
                batch_index, batch = future_map[future]
                completed += 1
                try:
                    quotes.update(future.result())
                except Exception as exc:
                    failures.append(f"第{batch_index}批({len(batch)}只): {exc}")
                    logger.warning(
                        "腾讯行情第 %d/%d 批失败: %s",
                        batch_index,
                        len(batches),
                        exc,
                        exc_info=len(failures) == 1,
                    )

                if completed == len(batches) or completed % 5 == 0:
                    logger.info(
                        "腾讯行情进度: %d/%d 批，已解析 %d 只，失败 %d 批",
                        completed,
                        len(batches),
                        len(quotes),
                        len(failures),
                    )

        elapsed = time.monotonic() - started_at
        if failures:
            logger.warning(
                "腾讯实时行情部分失败: %d/%d 批，耗时 %.2fs；首个错误: %s",
                len(failures),
                len(batches),
                elapsed,
                failures[0],
            )
        if len(failures) == len(batches):
            raise ConnectionError(
                f"腾讯实时行情全部 {len(batches)} 批请求失败；首个错误: {failures[0]}"
            )
        if not quotes:
            raise RuntimeError(
                f"腾讯实时行情响应未解析出有效数据: {len(tencent_codes)} 只，"
                f"{len(batches)} 批，失败 {len(failures)} 批"
            )
        logger.info("获取实时行情: %d 只，耗时 %.2fs", len(quotes), elapsed)
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
        cached = self._read_cache(
            cache_key,
            max_age=BAOSTOCK_HISTORY_CACHE_TTL_SECONDS,
        )
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
                with self._bs_lock:
                    self._bs_logged_in = False
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return None

            if result.get("error_code") != "0":
                logger.warning("get_stock_history(%s) BaoStock 错误: %s", code, result.get("error_code"))
                return None

            df = _history_rows_to_dataframe(result.get("rows", []))
            if df is None:
                return None

            self._write_cache(cache_key, df)
            return df

        return None

    def get_batch_history(
        self,
        codes: list[str],
        days: int = 120,
        max_batch: int = 5000,
        timeout_per_stock: int = BAOSTOCK_HISTORY_TIMEOUT_PER_STOCK_SECONDS,
    ) -> dict[str, pd.DataFrame]:
        """批量加载历史数据，缓存命中后按组复用 BaoStock 登录会话。

        每个子进程只登录一次并连续查询一组股票，避免旧实现为每只股票重复
        启动 Python、登录 BaoStock。主进程只并发少量批次，并在每批完成后
        输出进度，便于区分慢查询与真正卡死。
        """
        started_at = time.monotonic()
        target_codes: list[str] = []
        seen_codes: set[str] = set()
        for code in codes[:max_batch]:
            try:
                normalized = normalize_a_share_code(str(code))
            except ValueError:
                continue
            if normalized not in seen_codes:
                seen_codes.add(normalized)
                target_codes.append(normalized)

        result: dict[str, pd.DataFrame] = {}
        missing: list[tuple[str, str]] = []
        stale_cache_count = 0
        compatible_cache_count = 0
        compatible_cache_keys: dict[str, list[tuple[int, str]]] = {}
        try:
            for entry in os.scandir(self.cache_dir):
                if not entry.is_file() or not entry.name.startswith("hist_"):
                    continue
                if not entry.name.endswith(".pkl"):
                    continue
                cache_name = entry.name[len("hist_") : -len(".pkl")]
                raw_code, separator, cached_days_text = cache_name.rpartition("_")
                if not separator or not cached_days_text.isdigit():
                    continue
                try:
                    normalized = normalize_a_share_code(raw_code)
                except ValueError:
                    continue
                cached_days = int(cached_days_text)
                if cached_days >= days:
                    compatible_cache_keys.setdefault(normalized, []).append(
                        (cached_days, f"hist_{raw_code}_{cached_days}")
                    )
        except OSError as exc:
            logger.warning("扫描历史缓存目录失败: %s", exc)

        for index, code in enumerate(target_codes, start=1):
            cache_key = f"hist_{code}_{days}"
            cached = self._read_cache(
                cache_key,
                max_age=BAOSTOCK_HISTORY_CACHE_TTL_SECONDS,
            )
            if cached is None:
                cached = self._read_cache(
                    cache_key,
                    max_age=BAOSTOCK_HISTORY_STALE_MAX_AGE_SECONDS,
                )
                if cached is not None:
                    stale_cache_count += 1
            if cached is None:
                candidates = sorted(
                    compatible_cache_keys.get(code, []),
                    reverse=True,
                )
                for _cached_days, candidate_key in candidates:
                    if candidate_key == cache_key:
                        continue
                    candidate = self._read_cache(
                        candidate_key,
                        max_age=BAOSTOCK_HISTORY_STALE_MAX_AGE_SECONDS,
                    )
                    if candidate is not None and not candidate.empty:
                        cached = candidate
                        compatible_cache_count += 1
                        break
            if cached is not None and not cached.empty:
                result[code] = cached
            else:
                missing.append((code, to_baostock_code(code)))
            if index % 500 == 0:
                logger.info(
                    "历史缓存检查进度: %d/%d，命中 %d 只",
                    index,
                    len(target_codes),
                    len(result),
                )

        logger.info(
            "历史缓存检查完成: 总计 %d 只，命中 %d 只（旧缓存 %d，兼容长周期缓存 %d），待下载 %d 只，耗时 %.2fs",
            len(target_codes),
            len(result),
            stale_cache_count,
            compatible_cache_count,
            len(missing),
            time.monotonic() - started_at,
        )
        if not missing or not self._bs_available:
            return result

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
        batch_size = max(1, BAOSTOCK_HISTORY_BATCH_SIZE)
        batches = [
            missing[index : index + batch_size]
            for index in range(0, len(missing), batch_size)
        ]
        worker_count = min(max(1, BAOSTOCK_HISTORY_BATCH_WORKERS), len(batches))
        logger.info(
            "开始批量下载历史行情: %d 只，%d 批，每批 %d 只，%d 路并发",
            len(missing),
            len(batches),
            batch_size,
            worker_count,
        )

        failed_codes: list[str] = []
        processed = 0
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="baostock-history",
        ) as executor:
            future_map = {}
            for batch_index, batch in enumerate(batches, start=1):
                bs_codes = [bs_code for _code, bs_code in batch]
                batch_timeout = max(30, len(batch) * max(1, timeout_per_stock))
                future = executor.submit(
                    _run_bs_with_subprocess,
                    "query_history_batch",
                    start,
                    end,
                    *bs_codes,
                    timeout=batch_timeout,
                )
                future_map[future] = (batch_index, batch)

            for future in as_completed(future_map):
                batch_index, batch = future_map[future]
                processed += len(batch)
                try:
                    payload = future.result()
                except Exception as exc:
                    payload = None
                    logger.warning(
                        "历史行情第 %d/%d 批执行异常: %s",
                        batch_index,
                        len(batches),
                        exc,
                        exc_info=True,
                    )

                batch_results = payload.get("results", {}) if payload else {}
                for code, bs_code in batch:
                    item = batch_results.get(bs_code)
                    if not item or item.get("error_code") != "0":
                        failed_codes.append(code)
                        continue
                    frame = _history_rows_to_dataframe(item.get("rows", []))
                    if frame is None:
                        failed_codes.append(code)
                        continue
                    result[code] = frame
                    self._write_cache(f"hist_{code}_{days}", frame)

                logger.info(
                    "历史数据进度: %d/%d 只，成功 %d 只，失败 %d 只，耗时 %.1fs",
                    processed,
                    len(missing),
                    len(result),
                    len(failed_codes),
                    time.monotonic() - started_at,
                )

        logger.info(
            "批量历史数据完成: %d/%d 只成功，远端失败 %d 只，总耗时 %.2fs",
            len(result),
            len(target_codes),
            len(failed_codes),
            time.monotonic() - started_at,
        )
        return result

    def get_realtime_batch(self, codes):
        """批量获取实时价格（返回 code->price 字典）"""
        quotes = self.get_realtime_quotes(codes)
        return {code: q["price"] for code, q in quotes.items()}

    def get_stock_data(self, code, days=60):
        """获取个股/ETF 历史数据（兼容别名）。ETF 走 AKShare，个股走 BaoStock。"""
        from config.settings import is_etf as _is_etf
        if _is_etf(str(code)):
            return self.get_etf_history(str(code), days=days)
        return self.get_stock_history(code, days=days)

    def get_stock_history_ext(self, code, days=40):
        """获取个股扩展字段历史(含换手率/PB/ST/停牌),并附加 pb/mktcap/is_st/is_suspended 列。

        供小市值价值选股使用。流通市值 ≈ close * volume / (turn/100)。
        """
        try:
            raw_code = normalize_a_share_code(str(code))
            bs_code = to_baostock_code(str(code))
        except ValueError:
            return None

        cache_key = f"histext_{raw_code}_{days}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        if not self._bs_available:
            return None

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days + 40)).strftime("%Y-%m-%d")
        self._ensure_login()

        for attempt in range(2):
            result = _run_bs_with_subprocess("query_history_ext", bs_code, start, end)
            if result is None:
                with self._bs_lock:
                    self._bs_logged_in = False
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None
            if result.get("error_code") != "0":
                return None
            rows = result.get("rows", [])
            if not rows:
                return None

            cols = ["date", "open", "high", "low", "close", "volume", "amount",
                    "turn", "peTTM", "pbMRQ", "isST", "tradestatus", "pctChg"]
            df = pd.DataFrame(rows, columns=cols)
            for c in ["open", "high", "low", "close", "volume", "amount",
                      "turn", "peTTM", "pbMRQ", "isST", "tradestatus", "pctChg"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            if len(df) == 0 or df["close"].iloc[-1] <= 0:
                return None

            df["pb"] = df["pbMRQ"]
            df["is_st"] = df["isST"].fillna(0)
            df["is_suspended"] = (df["tradestatus"] == 0)
            turn = df["turn"].where(df["turn"] > 0)
            df["mktcap"] = df["close"] * df["volume"] / (turn / 100.0)

            self._write_cache(cache_key, df)
            return df
        return None

    def get_batch_history_ext(self, codes, days=40, max_batch=5000, timeout_per_stock=30):
        """并发批量获取扩展字段历史。"""
        result = {}
        target = codes[:max_batch]
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {executor.submit(self.get_stock_history_ext, c, days): c for c in target}
            done = 0
            for future in as_completed(future_map):
                code = future_map[future]
                done += 1
                try:
                    df = future.result(timeout=timeout_per_stock)
                    if df is not None and not df.empty:
                        result[code] = df
                except Exception:
                    pass
                if done % 200 == 0:
                    logger.info("扩展历史进度: %d/%d", done, len(target))
        logger.info("扩展历史完成: %d/%d 只", len(result), len(target))
        return result

    def get_index_history(self, index_code="sh000300", days=120, start_date=None, end_date=None):
        """获取指数历史日线(用于大盘择时)。

        指数代码(如 ``sh000300``)不是 A 股个股,不能走个股归一化路径,
        因此这里直接构造 BaoStock 代码并通过子进程查询。

        Args:
            index_code: 指数代码,支持 ``sh000300`` / ``sh.000300`` / ``000300``。
            days: 回溯自然日数(``start_date``/``end_date`` 均为 None 时生效)。
            start_date: 起始日 YYYYMMDD(可选,显式区间,回测用)。
            end_date: 结束日 YYYYMMDD(可选)。

        Returns:
            pd.DataFrame(含 date/close 等列)或 None。
        """
        raw = str(index_code).strip().lower().replace(".", "")
        if raw.startswith(("sh", "sz")):
            market, digits = raw[:2], raw[2:]
        else:
            # 缺省按上交所指数处理(沪深300、上证指数等)
            market, digits = "sh", raw
        bs_code = f"{market}.{digits}"

        if start_date and end_date:
            start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            cache_key = f"index_{market}{digits}_{start_date}_{end_date}"
        else:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
            cache_key = f"index_{market}{digits}_{days}"

        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        if not self._bs_available:
            return None

        try:
            self._ensure_login()
        except ConnectionError:
            return None

        result = _run_bs_with_subprocess("query_history", bs_code, start, end)
        if result is None or result.get("error_code") != "0":
            logger.warning("get_index_history(%s) 获取失败", index_code)
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

    def get_etf_history(
        self,
        code: str,
        days: int = 120,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame | None:
        """通过 AKShare 获取 ETF 日线历史行情。

        Args:
            code: ETF 代码,支持 ``510300`` / ``sh510300`` / ``sz159915``。
            days: 未显式传入日期区间时的回溯自然日数。
            start_date: 起始日期,支持 YYYYMMDD 或 YYYY-MM-DD。
            end_date: 结束日期,支持 YYYYMMDD 或 YYYY-MM-DD。
            adjust: 复权方式,AKShare 支持 ``""`` / ``qfq`` / ``hfq``。

        Returns:
            标准化后的日线 DataFrame,失败时返回 None。
        """
        raw_code = str(code).strip().lower().replace(".", "")
        if raw_code.startswith(("sh", "sz")):
            raw_code = raw_code[2:]
        if not is_etf(raw_code):
            logger.warning("ETF 历史行情忽略非 ETF 代码: %s", code)
            return None

        start, end = self._resolve_date_range(days, start_date, end_date)
        cache_key = f"ak_etf_{raw_code}_{start}_{end}_{adjust or 'none'}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        for attempt in range(3):
            try:
                _require_akshare()
                raw_df = ak.fund_etf_hist_em(
                    symbol=raw_code,
                    period="daily",
                    start_date=start,
                    end_date=end,
                    adjust=adjust,
                )
                df = _normalize_market_history(raw_df, code=raw_code)
                break
            except Exception as exc:
                if _is_network_error(exc) and attempt < 2:
                    delay = (attempt + 1) * 2
                    logger.warning(
                        "AKShare ETF %s 网络错误，%ds后重试(%d/3): %s",
                        code, delay, attempt + 2, exc,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("AKShare ETF 历史行情获取失败 %s: %s", code, exc)
                # AKShare 失败，fallback 到新浪
                logger.info("ETF %s 尝试新浪 fallback", code)
                df = _fetch_etf_history_sina(raw_code, datalen=days)
                if df is not None and not df.empty:
                    logger.info("ETF %s 新浪 fallback 成功，%d 条数据", code, len(df))
                    self._write_cache(cache_key, df)
                    return df
                return None

        if df.empty:
            logger.warning("AKShare ETF 历史行情为空: %s %s-%s", code, start, end)
            return None
        self._write_cache(cache_key, df)
        return df

    def get_batch_etf_history(
        self,
        codes: list[str],
        days: int = 120,
        max_batch: int = 50,
        adjust: str = "qfq",
    ) -> dict[str, pd.DataFrame]:
        """批量获取 ETF 日线历史行情。

        Args:
            codes: ETF 代码列表。
            days: 回溯自然日数。
            max_batch: 单次最多请求数量,避免小服务器被外部接口拖慢。
            adjust: 复权方式。

        Returns:
            ``{code: DataFrame}``。
        """
        result: dict[str, pd.DataFrame] = {}
        target_codes = codes[:max_batch]
        workers = min(4, max(1, len(target_codes)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(self.get_etf_history, code, days, None, None, adjust): code
                for code in target_codes
            }
            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    df = future.result(timeout=30)
                    if df is not None and not df.empty:
                        result[code] = df
                except Exception as exc:
                    logger.warning("批量 ETF 历史行情失败 %s: %s", code, exc)
        logger.info("ETF 历史行情完成: %d/%d 只", len(result), len(target_codes))
        return result

    def get_industry_index_history(
        self,
        industry_name: str,
        days: int = 120,
        start_date: str | None = None,
        end_date: str | None = None,
        provider: str = "em",
    ) -> pd.DataFrame | None:
        """通过 AKShare 获取行业指数日线历史行情。

        Args:
            industry_name: 行业板块名称,例如 ``证券``、``半导体``。
            days: 未显式传入日期区间时的回溯自然日数。
            start_date: 起始日期,支持 YYYYMMDD 或 YYYY-MM-DD。
            end_date: 结束日期,支持 YYYYMMDD 或 YYYY-MM-DD。
            provider: ``em`` 使用东方财富行业板块,``ths`` 使用同花顺行业指数。

        Returns:
            标准化后的行业指数日线 DataFrame,失败时返回 None。
        """
        name = str(industry_name).strip()
        if not name:
            logger.warning("行业指数名称为空")
            return None
        if provider not in ("em", "ths"):
            raise ValueError(f"不支持的行业指数数据源: {provider}")

        start, end = self._resolve_date_range(days, start_date, end_date)
        cache_name = _safe_cache_fragment(name)
        cache_key = f"ak_industry_{provider}_{cache_name}_{start}_{end}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        for attempt in range(3):
            try:
                _require_akshare()
                if provider == "em":
                    raw_df = ak.stock_board_industry_hist_em(
                        symbol=name,
                        start_date=start,
                        end_date=end,
                        period="日k",
                        adjust="",
                    )
                else:
                    raw_df = ak.stock_board_industry_index_ths(
                        symbol=name,
                        start_date=start,
                        end_date=end,
                    )
                df = _normalize_market_history(raw_df, code=name, name=name)
                break
            except Exception as exc:
                if _is_network_error(exc) and attempt < 2:
                    delay = (attempt + 1) * 2
                    logger.warning(
                        "AKShare 行业指数 %s 网络错误，%ds后重试(%d/3): %s",
                        name, delay, attempt + 2, exc,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("AKShare 行业指数历史行情获取失败 %s(%s): %s", name, provider, exc)
                # AKShare 失败，fallback 到腾讯
                logger.info("行业指数 %s 尝试腾讯 fallback", name)
                df = _fetch_industry_history_tencent(name, datalen=days)
                if df is not None and not df.empty:
                    logger.info("行业指数 %s 腾讯 fallback 成功，%d 条数据", name, len(df))
                    self._write_cache(cache_key, df)
                    return df
                return None

        if df.empty:
            logger.warning("AKShare 行业指数历史行情为空: %s %s-%s", name, start, end)
            return None
        self._write_cache(cache_key, df)
        return df

    def get_batch_industry_index_history(
        self,
        industry_names: list[str],
        days: int = 120,
        max_batch: int = 50,
        provider: str = "em",
    ) -> dict[str, pd.DataFrame]:
        """批量获取行业指数日线历史行情。

        Args:
            industry_names: 行业名称列表。
            days: 回溯自然日数。
            max_batch: 单次最多请求数量。
            provider: 行业指数数据源。

        Returns:
            ``{industry_name: DataFrame}``。
        """
        result: dict[str, pd.DataFrame] = {}
        targets = industry_names[:max_batch]
        workers = min(4, max(1, len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    self.get_industry_index_history,
                    name,
                    days,
                    None,
                    None,
                    provider,
                ): name
                for name in targets
            }
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    df = future.result(timeout=30)
                    if df is not None and not df.empty:
                        result[name] = df
                except Exception as exc:
                    logger.warning("批量行业指数历史行情失败 %s: %s", name, exc)
        logger.info("行业指数历史行情完成: %d/%d 个", len(result), len(targets))
        return result

    def get_industry_index_history_tencent(
        self,
        industry_names: list[str],
        days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        """通过腾讯接口获取行业指数日线历史行情（AKShare 不稳定时的兜底）。

        腾讯历史K线接口:
            https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq

        Args:
            industry_names: 行业名称列表（如 证券、半导体 等）。
            days: 回溯自然日数。

        Returns:
            ``{industry_name: DataFrame}``,列与 get_industry_index_history 一致。
        """
        from config.settings import INDUSTRY_TENCENT_MAP
        import requests as _requests

        result: dict[str, pd.DataFrame] = {}
        for name in industry_names:
            tencent_code = INDUSTRY_TENCENT_MAP.get(name)
            if not tencent_code:
                continue
            try:
                url = (
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    f"?param={tencent_code},day,,,{days},qfq"
                )
                resp = _requests.get(
                    url,
                    headers={"Referer": "https://stock.qq.com"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                klines = (
                    data.get("data", {})
                    .get(tencent_code, {})
                    .get("day", [])
                    or data.get("data", {})
                    .get(tencent_code, {})
                    .get("qfqday", [])
                )
                if not klines:
                    continue
                rows = []
                for k in klines:
                    rows.append({
                        "date": str(k[0]),
                        "open": float(k[1]),
                        "close": float(k[2]),
                        "high": float(k[3]),
                        "low": float(k[4]),
                        "volume": float(k[5]) if len(k) > 5 else 0,
                    })
                df = pd.DataFrame(rows)
                if not df.empty:
                    result[name] = df
            except Exception as exc:
                logger.warning("腾讯行业指数 %s 获取失败: %s", name, exc)
        logger.info("腾讯行业指数完成: %d/%d 个", len(result), len(industry_names))
        return result

    @staticmethod
    def _resolve_date_range(
        days: int,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[str, str]:
        """解析历史行情查询日期区间为 AKShare 需要的 YYYYMMDD。"""
        if days <= 0:
            raise ValueError(f"days 必须为正整数: {days}")
        if start_date and end_date:
            start = str(start_date).replace("-", "")[:8]
            end = str(end_date).replace("-", "")[:8]
            return start, end
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
        return start, end

    def _cache_path(self, key):
        return os.path.join(self.cache_dir, f"{key}.pkl")

    def _read_cache(self, key, max_age=None):
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            meta_path = path + ".meta"
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                effective_max_age = self.cache_ttl if max_age is None else max_age
                if time.time() - meta.get("ts", 0) > effective_max_age:
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

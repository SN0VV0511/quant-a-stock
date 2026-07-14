"""
量化交易系统 - Web 仪表盘 v2
双线程实时监控 + 日志 + 全记录
"""

from __future__ import annotations

import os
import sys
import argparse
import hashlib
import hmac
import json
import secrets
import time
import logging
import threading
import mimetypes
import sqlite3
import subprocess
from dataclasses import asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    INITIAL_CAPITAL,
    STATE_FILE,
    TRADE_LOG_FILE,
    REPORT_DIR,
    LOG_DIR,
    DATA_DIR,
    SNAPSHOT_LOG_FILE,
    TRADE_EVENTS_FILE,
    RPS_STATE_FILE,
    normalize_a_share_code,
    ROBUST_V2_ACCOUNT_ID,
    ROBUST_V2_LEDGER_PATH,
)
from config.time_utils import format_local
from data.ak_loader import AKDataLoader
from data.scan_store import StockScanStore
from scripts.backtest_cache import ensure_backtest_cache
from scripts.paper_status import build_status

logger = logging.getLogger(__name__)
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")

# 日志文件路径
LIVE_LOG = os.path.join(LOG_DIR, "robust_v2.log")
LIVE_TODAY_LOG = LIVE_LOG
ROOT_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN_DIR = ROOT_DIR / "data" / "scans"
_PREVIEW_SCAN_LOCK = threading.Lock()
_PREVIEW_SCAN_PROCESS: subprocess.Popen[bytes] | None = None


def _scan_store() -> StockScanStore:
    """返回共享运行目录中的扫描快照存储。"""
    return StockScanStore(SCAN_DIR)


def _load_latest_scan() -> dict[str, Any]:
    """读取最新结构化扫描，损坏时返回可展示的失败状态。"""
    try:
        payload = _scan_store().load_latest()
    except RuntimeError as exc:
        logger.error("读取个股扫描快照失败: %s", exc)
        return {
            "status": "failed",
            "error": str(exc),
            "candidates": [],
            "generated_at": "",
        }
    if payload is None:
        return {
            "status": "never_run",
            "error": "",
            "candidates": [],
            "generated_at": "",
        }
    return payload


def _preview_scan_running() -> bool:
    """返回由当前 Web 实例启动的安全预览进程是否仍在运行。"""
    with _PREVIEW_SCAN_LOCK:
        return (
            _PREVIEW_SCAN_PROCESS is not None and _PREVIEW_SCAN_PROCESS.poll() is None
        )


def _v2_connection():
    """以只读模式打开 paper_v2 账本，缺失或损坏时返回 None。"""
    path = Path(ROBUST_V2_LEDGER_PATH).expanduser().resolve()
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2
        )
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        logger.warning("只读打开 paper_v2 账本失败: %s", exc)
        return None


def _load_v2_state():
    """从 SQLite 读取与旧 Web API 兼容的账户状态。"""
    connection = _v2_connection()
    if connection is None:
        return None
    try:
        account = connection.execute(
            "SELECT cash, updated_at FROM accounts WHERE account_id = ?",
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchone()
        if account is None:
            return None
        rows = connection.execute(
            "SELECT * FROM positions WHERE account_id = ? ORDER BY code",
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchall()
        positions = {
            str(row["code"]): {
                "name": str(row["name"]),
                "shares": int(row["shares"]),
                "avg_cost": float(row["avg_cost"]),
                "current_price": float(row["last_price"]),
                "strategy_tag": "robust_v2",
                "strategy_version": str(row["strategy_version"]),
            }
            for row in rows
        }
        return {
            "cash": float(account["cash"]),
            "positions": positions,
            "trades": [],
            "updated_at": str(account["updated_at"]),
            "account_id": ROBUST_V2_ACCOUNT_ID,
            "source": "paper_v2",
        }
    except sqlite3.Error as exc:
        logger.warning("读取 paper_v2 账户状态失败: %s", exc)
        return None
    finally:
        connection.close()


def _load_v2_orders():
    """从幂等订单表读取成交和拒单，供交易页展示。"""
    connection = _v2_connection()
    if connection is None:
        return None
    try:
        rows = connection.execute(
            "SELECT * FROM orders WHERE account_id = ? ORDER BY trade_date, created_at, rowid",
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchall()
        return [
            {
                "date": str(row["trade_date"]),
                "time": str(row["filled_at"] or row["created_at"])[-8:],
                "code": str(row["code"]),
                "name": str(row["name"]),
                "action": str(row["action"]),
                "direction": str(row["action"]),
                "price": float(row["requested_price"]),
                "actual_price": float(row["actual_price"]),
                "shares": int(row["filled_shares"] or row["requested_shares"]),
                "amount": float(row["amount"]),
                "cost": float(row["total_cost"]),
                "profit": float(row["profit"]) if row["profit"] is not None else None,
                "strategy": str(row["strategy"]),
                "strategy_tag": str(row["strategy_tag"]),
                "reason": str(row["reason"]),
                "sell_reason": str(row["reason"]) if row["action"] == "sell" else "",
                "status": str(row["status"]),
                "reject_reason": str(row["message"])
                if row["status"] == "rejected"
                else "",
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        logger.warning("读取 paper_v2 订单失败: %s", exc)
        return None
    finally:
        connection.close()


def _load_v2_equity():
    """读取单账本净值和回撤序列。"""
    connection = _v2_connection()
    if connection is None:
        return None
    try:
        rows = connection.execute(
            """
            SELECT snapshot_date, captured_at, total_value FROM nav_snapshots
            WHERE account_id = ? ORDER BY snapshot_date, captured_at
            """,
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchall()
        # 兼容升级前可能遗留的同日多条快照，只采用当天最后一次收盘记录。
        latest_by_date = {str(row["snapshot_date"]): row for row in rows}
        points = []
        peak = INITIAL_CAPITAL
        for snapshot_date in sorted(latest_by_date):
            row = latest_by_date[snapshot_date]
            value = float(row["total_value"])
            peak = max(peak, value)
            points.append(
                {
                    "t": str(row["captured_at"] or row["snapshot_date"]),
                    "value": value,
                    "drawdown": round((peak - value) / peak, 6) if peak > 0 else 0.0,
                }
            )
        return points
    except sqlite3.Error as exc:
        logger.warning("读取 paper_v2 净值失败: %s", exc)
        return None
    finally:
        connection.close()


def _load_v2_profit_ranking() -> list[dict[str, Any]] | None:
    """按 FIFO 成交成本统计已实现收益，部分卖出不会扣除全部历史买入。"""
    connection = _v2_connection()
    if connection is None:
        return None
    try:
        rows = connection.execute(
            """
            SELECT code,
                   MAX(name) AS name,
                   SUM(CASE WHEN action = 'buy' THEN amount ELSE 0 END) AS buy_amount,
                   SUM(CASE WHEN action = 'sell' THEN amount ELSE 0 END) AS sell_amount,
                   SUM(CASE WHEN action = 'sell' THEN shares ELSE 0 END) AS shares_sold,
                   SUM(CASE WHEN action = 'sell' THEN COALESCE(net_pnl, 0) ELSE 0 END) AS net_pnl,
                   SUM(CASE WHEN action = 'sell'
                            THEN amount - COALESCE(gross_pnl, 0)
                            ELSE 0 END) AS realized_cost_basis
            FROM trades
            WHERE account_id = ?
            GROUP BY code
            HAVING SUM(CASE WHEN action = 'sell' THEN shares ELSE 0 END) > 0
            """,
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchall()
        ranking: list[dict[str, Any]] = []
        for row in rows:
            net_profit = float(row["net_pnl"] or 0)
            cost_basis = float(row["realized_cost_basis"] or 0)
            ranking.append(
                {
                    "code": str(row["code"]),
                    "name": str(row["name"] or row["code"]),
                    "net_profit": round(net_profit, 2),
                    "roi": round(net_profit / cost_basis, 4) if cost_basis > 0 else 0.0,
                    "buy_amount": round(float(row["buy_amount"] or 0), 2),
                    "sell_amount": round(float(row["sell_amount"] or 0), 2),
                    "shares_traded": int(row["shares_sold"] or 0),
                }
            )
        ranking.sort(key=lambda item: float(item["roi"]), reverse=True)
        return ranking
    except sqlite3.Error as exc:
        logger.warning("读取 paper_v2 已实现收益榜失败: %s", exc)
        return None
    finally:
        connection.close()


def load_state():
    v2_state = _load_v2_state()
    if v2_state is not None:
        return v2_state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8", errors="replace") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取账户状态失败: %s", exc)
    return {"cash": INITIAL_CAPITAL, "positions": {}, "updated_at": ""}


def load_trade_log():
    v2_orders = _load_v2_orders()
    if v2_orders is not None:
        return v2_orders
    trades = []
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取交易流水失败: %s", exc)
    state = load_state()
    state_trades = state.get("trades", [])
    if state_trades:
        # 去重 key 只用 date+code+action+shares，不用 time/price
        # 同一笔交易在 trade_log.json 和 portfolio_state.json 中时间和价格可能略有差异
        known = {
            (
                t.get("date"),
                t.get("code"),
                t.get("action") or t.get("direction"),
                t.get("shares"),
            )
            for t in trades
        }
        for trade in state_trades:
            key = (
                trade.get("date"),
                trade.get("code"),
                trade.get("action") or trade.get("direction"),
                trade.get("shares"),
            )
            if key not in known:
                trades.append(trade)
    # 按 (date, time) 升序排序，确保前端 reverse() 后最新在前
    trades.sort(key=lambda t: (str(t.get("date", "")), str(t.get("time", ""))))
    return trades


def load_rps_state():
    """读取 ETF/RPS 日频轮动状态。"""
    if _load_v2_state() is not None:
        return {
            "available": False,
            "status": "research_only",
            "message": "paper_v2 不运行旧 ETF/RPS 日内账户策略",
            "etf_signals": [],
            "industry_signals": [],
            "orders": [],
        }
    if not os.path.exists(RPS_STATE_FILE):
        return {
            "available": False,
            "status": "missing",
            "message": "尚未生成 ETF/RPS 状态",
            "etf_signals": [],
            "industry_signals": [],
            "orders": [],
        }
    try:
        with open(RPS_STATE_FILE, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        data["available"] = True
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 RPS 状态失败: %s", exc)
        return {
            "available": False,
            "status": "error",
            "message": str(exc),
            "etf_signals": [],
            "industry_signals": [],
            "orders": [],
        }


def normalize_code(code):
    try:
        return normalize_a_share_code(code)
    except ValueError:
        logger.warning("仪表盘忽略无法归一化的非沪深 A 股代码: %s", code)
        return str(code).strip().lower()


def get_realtime_prices(codes):
    try:
        loader = AKDataLoader()
        raw_codes = [normalize_code(c) for c in codes]
        prices_raw = loader.get_realtime_batch(raw_codes)
        return {c: prices_raw.get(normalize_code(c), 0) for c in codes}
    except Exception:
        logger.warning("获取实时价格失败", exc_info=True)
        return {}


def load_reports():
    reports = []
    if os.path.isdir(REPORT_DIR):
        for fname in sorted(os.listdir(REPORT_DIR), reverse=True)[:30]:
            if fname.endswith(".txt"):
                fpath = os.path.join(REPORT_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    reports.append(
                        {
                            "date": fname.replace(".txt", "").replace("daily_", ""),
                            "content": content,
                        }
                    )
                except Exception:
                    pass
    return reports


def _load_v2_lease() -> dict[str, Any]:
    """读取 robust_v2 守护实例租约，作为跨容器真实心跳。"""
    connection = _v2_connection()
    if connection is None:
        return {}
    try:
        row = connection.execute(
            """
            SELECT holder_id, heartbeat_at, expires_at
            FROM leases WHERE account_id = ?
            """,
            (ROBUST_V2_ACCOUNT_ID,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "holder_id": str(row["holder_id"]),
            "heartbeat_at": str(row["heartbeat_at"]),
            "expires_at": float(row["expires_at"]),
            "active": float(row["expires_at"]) > time.time(),
        }
    except sqlite3.Error as exc:
        logger.warning("读取 robust_v2 租约失败: %s", exc)
        return {}
    finally:
        connection.close()


def is_process_running(name: str) -> bool:
    """检查进程状态；robust_v2 优先使用跨容器 SQLite 租约。"""
    if name == "robust_runner.py":
        lease = _load_v2_lease()
        if lease:
            return bool(lease.get("active"))
    try:
        result = subprocess.run(
            ["pgrep", "-f", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ==================== 登录验证 ====================

# 密码 hash 存储路径（不上传 git）
_PASSWORD_FILE = os.path.join(DATA_DIR, ".password_hash")
_PBKDF2_ITERATIONS = 600_000
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 5


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    """使用带随机盐的 PBKDF2 保存密码，避免离线彩虹表攻击。"""
    if not password:
        raise ValueError("密码不能为空")
    selected_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        selected_salt,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${selected_salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """校验密码，并标记旧 SHA-256 格式是否需要在登录后迁移。"""
    parts = stored_hash.split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
        except (ValueError, TypeError):
            return False, False
        if iterations < 100_000 or not salt or not expected:
            return False, False
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual_digest, expected), False

    # 仅为现有部署提供一次平滑迁移；新密码不会再写入无盐 SHA-256。
    if len(stored_hash) == 64:
        legacy_digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        accepted = hmac.compare_digest(legacy_digest, stored_hash)
        return accepted, accepted
    return False, False


def _load_password_hash() -> str:
    """加载密码 hash：环境变量 > 本地文件 > 自动生成。"""
    env_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
    if env_hash:
        return env_hash
    if os.path.exists(_PASSWORD_FILE):
        with open(_PASSWORD_FILE) as f:
            return f.read().strip()
    # 首次运行：生成随机密码并保存
    pwd = secrets.token_hex(8)
    h = _hash_password(pwd)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(h)
    os.chmod(_PASSWORD_FILE, 0o600)
    logger.warning("首次运行，已生成随机密码: %s（请尽快修改）", pwd)
    return h


def _save_password_hash(hash_val: str) -> None:
    """持久化密码 hash 到本地文件。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary = f"{_PASSWORD_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(hash_val)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, _PASSWORD_FILE)


_DASHBOARD_PASSWORD_HASH = _load_password_hash()
# 会话 token → 过期时间
_sessions: dict[str, float] = {}
_SESSION_TTL = 86400  # 24 小时
_AUTH_LOCK = threading.RLock()
_login_failures: dict[str, list[float]] = {}


def _cookie_token(handler: Any) -> str:
    """从 Cookie 中提取当前会话令牌。"""
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("quant_token="):
            return part[len("quant_token=") :]
    return ""


def _client_identity(handler: Any) -> str:
    """返回限速使用的客户端标识，不信任可伪造的转发头。"""
    address = getattr(handler, "client_address", ("unknown", 0))
    return str(address[0]) if address else "unknown"


def _login_rate_limited(identity: str, *, now: float | None = None) -> bool:
    """判断同一来源在时间窗内是否超过密码失败上限。"""
    current = time.time() if now is None else now
    with _AUTH_LOCK:
        recent = [
            value
            for value in _login_failures.get(identity, [])
            if current - value < _LOGIN_WINDOW_SECONDS
        ]
        _login_failures[identity] = recent
        return len(recent) >= _LOGIN_MAX_FAILURES


def _record_login_failure(identity: str) -> None:
    """记录一次登录失败。"""
    with _AUTH_LOCK:
        _login_failures.setdefault(identity, []).append(time.time())


def _clear_login_failures(identity: str) -> None:
    """登录成功后清理当前来源的失败计数。"""
    with _AUTH_LOCK:
        _login_failures.pop(identity, None)


def _check_auth(handler) -> bool:
    """检查请求是否已认证。返回 True 表示已登录。"""
    token = _cookie_token(handler)
    if token:
        with _AUTH_LOCK:
            if token in _sessions:
                if time.time() - _sessions[token] < _SESSION_TTL:
                    _sessions[token] = time.time()  # 续期
                    return True
                del _sessions[token]
    return False


def _set_auth_cookie(handler):
    """设置认证 cookie。"""
    token = secrets.token_hex(32)
    with _AUTH_LOCK:
        _sessions[token] = time.time()
    secure = os.getenv("DASHBOARD_COOKIE_SECURE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    secure_attribute = "; Secure" if secure else ""
    handler.send_header(
        "Set-Cookie",
        f"quant_token={token}; Path=/; HttpOnly; SameSite=Lax; "
        f"Max-Age={_SESSION_TTL}{secure_attribute}",
    )


class QuantHandler(SimpleHTTPRequestHandler):
    def _require_auth(self) -> bool:
        """页面鉴权：未登录时重定向到登录页。"""
        if _check_auth(self):
            return True
        self.send_response(302)
        self.send_header("Location", "login")
        self.end_headers()
        return False

    def _require_api_auth(self) -> bool:
        """API 鉴权：未登录时返回结构化 401。"""
        if _check_auth(self):
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "未登录", "needLogin": True}).encode())
        return False

    def _require_auth_or_redirect(self) -> bool:
        """页面路由专用:未登录时返回登录页而非 JSON,让 React 能加载。"""
        if _check_auth(self):
            return True
        self._serve_spa("login.html")
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # 兼容反向代理的 /quantify 前缀:Vite base="/quantify/" 构建的产物引用
        # 的是 /quantify/assets/...,本地直接访问时后端路由(/assets/、/api/)无该前缀,
        # 这里统一剥离,使本地直连和反向代理部署都能命中路由。
        if path.startswith("/quantify/"):
            path = path[len("/quantify") :]

        # 登录页不需要认证（优先 React SPA）
        if path == "/login":
            return self._serve_spa("login.html")

        if path == "/healthz":
            return self._json_response({"ok": True, "service": "quant-dashboard"})

        # React/Vite 构建产物，登录页也需要加载 JS/CSS，因此静态资源不做鉴权。
        if path.startswith("/assets/"):
            return self._serve_static_asset(path)

        # API 路由
        api_routes = {
            "/api/portfolio": lambda: self._json_response(self._api_portfolio()),
            "/api/trades": lambda: self._json_response(self._api_trades()),
            "/api/profit-ranking": lambda: self._json_response(
                self._api_profit_ranking()
            ),
            "/api/scan": lambda: self._json_response(self._api_scan()),
            "/api/reports": lambda: self._json_response(self._api_reports()),
            "/api/logs": lambda: self._json_response(self._api_logs(params)),
            "/api/status": lambda: self._json_response(self._api_status()),
            "/api/observation": lambda: self._json_response(self._api_observation()),
            "/api/rps": lambda: self._json_response(self._api_rps()),
            "/api/candidates": lambda: self._json_response(self._api_candidates()),
            "/api/equity": lambda: self._json_response(self._api_equity()),
            "/api/backtest": lambda: self._json_response(self._api_backtest()),
        }

        if path in api_routes:
            if not self._require_api_auth():
                return
            api_routes[path]()
            return

        # 静态文件 / 主页
        if path in ("/", "/index.html"):
            if not self._require_auth_or_redirect():
                return
            return self._serve_spa("index.html")

        self.send_error(404)

    def do_HEAD(self):
        """支持静态资源与 SPA 的 HEAD 检查，避免默认文件服务绕过自定义路由。"""
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/quantify/"):
            path = path[len("/quantify") :]

        if path.startswith("/assets/"):
            return self._serve_static_asset(path, write_body=False)

        if path == "/login":
            return self._serve_spa("login.html", write_body=False)

        if path in ("/", "/index.html"):
            if not self._require_auth_or_redirect():
                return
            return self._serve_spa("index.html", write_body=False)

        self.send_error(404)

    def do_OPTIONS(self):
        """CORS 预检请求支持。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        global _DASHBOARD_PASSWORD_HASH
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/quantify/"):
            path = path[len("/quantify") :]
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._json_response({"error": "Content-Length 非法"}, status=400)
            return
        if content_len < 0 or content_len > _MAX_REQUEST_BODY_BYTES:
            self._json_response({"error": "请求体过大"}, status=413)
            return
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response({"error": "请求体不是有效 JSON"}, status=400)
            return
        if not isinstance(data, dict):
            self._json_response({"error": "请求体必须是 JSON 对象"}, status=400)
            return

        if path == "/api/login":
            identity = _client_identity(self)
            if _login_rate_limited(identity):
                self._json_response(
                    {"success": False, "error": "尝试次数过多，请稍后再试"},
                    status=429,
                )
                return
            pwd = str(data.get("password", ""))
            accepted, needs_upgrade = _verify_password(pwd, _DASHBOARD_PASSWORD_HASH)
            if accepted:
                if needs_upgrade:
                    _DASHBOARD_PASSWORD_HASH = _hash_password(pwd)
                    _save_password_hash(_DASHBOARD_PASSWORD_HASH)
                    logger.info("仪表盘密码散列已迁移到 PBKDF2")
                _clear_login_failures(identity)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                _set_auth_cookie(self)
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode())
            else:
                _record_login_failure(identity)
                self._json_response({"success": False, "error": "密码错误"}, status=401)
            return

        if path == "/api/logout":
            token = _cookie_token(self)
            if token:
                with _AUTH_LOCK:
                    _sessions.pop(token, None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Set-Cookie",
                "quant_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode())
            return

        if path == "/api/scan/trigger":
            if not self._require_api_auth():
                return
            payload, status = self._api_scan_trigger()
            self._json_response(payload, status=status)
            return

        if path == "/api/change-password":
            identity = _client_identity(self)
            if _login_rate_limited(identity):
                self._json_response(
                    {"success": False, "error": "尝试次数过多，请稍后再试"},
                    status=429,
                )
                return
            old_pwd = str(data.get("old_password", ""))
            new_pwd = str(data.get("new_password", ""))
            if len(new_pwd) < 12:
                self._json_response(
                    {"success": False, "error": "新密码至少12位"}, status=400
                )
                return
            accepted, _needs_upgrade = _verify_password(
                old_pwd, _DASHBOARD_PASSWORD_HASH
            )
            if not accepted:
                _record_login_failure(identity)
                self._json_response(
                    {"success": False, "error": "旧密码错误"}, status=401
                )
                return
            new_hash = _hash_password(new_pwd)
            _DASHBOARD_PASSWORD_HASH = new_hash
            _save_password_hash(new_hash)
            _clear_login_failures(identity)
            with _AUTH_LOCK:
                _sessions.clear()
            self._json_response({"success": True})
            return

        self.send_error(404)

    # ==================== API ====================

    def _api_portfolio(self):
        state = load_state()
        cash = state.get("cash", INITIAL_CAPITAL)
        positions = state.get("positions", {})
        codes = list(positions.keys())
        quotes: dict[str, dict[str, Any]] = {}
        try:
            if codes:
                loader = AKDataLoader()
                raw_codes = [normalize_code(c) for c in codes]
                quotes = loader.get_realtime_quotes(raw_codes)
        except Exception as exc:
            logger.warning("持仓实时行情加载失败，回退账本最后价格: %s", exc)

        prices = {
            code: float(quotes.get(normalize_code(code), {}).get("price", 0) or 0)
            for code in codes
        }
        name_map = {
            code: str(quotes.get(normalize_code(code), {}).get("name", ""))
            for code in codes
        }

        def get_name(code):
            return name_map.get(code) or positions.get(code, {}).get("name") or code

        positions_value = 0
        position_list = []
        for code, pos in positions.items():
            current = prices.get(code, pos.get("current_price", pos.get("cost", 0)))
            shares = pos.get("shares", 0)
            avg_cost = pos.get("avg_cost", pos.get("cost", 0))
            value = shares * current
            positions_value += value
            pnl = (current - avg_cost) / avg_cost if avg_cost > 0 else 0
            position_list.append(
                {
                    "code": code,
                    "name": get_name(code),
                    "shares": shares,
                    "avg_cost": round(avg_cost, 3),
                    "current_price": round(current, 3),
                    "value": round(value, 2),
                    "profit": round((current - avg_cost) * shares, 2),
                    "profit_pct": round(pnl, 4),
                }
            )

        total_value = cash + positions_value
        pnl = total_value - INITIAL_CAPITAL

        return {
            "total_value": round(total_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "position_ratio": round(positions_value / total_value, 4)
            if total_value > 0
            else 0,
            "position_count": len(positions),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / INITIAL_CAPITAL, 4),
            "positions": position_list,
            "updated_at": state.get("updated_at", ""),
        }

    def _api_trades(self):
        trades = load_trade_log()
        # 支持 ?date=YYYYMMDD 参数
        query_date = None
        if "?date=" in self.path:
            query_date = self.path.split("?date=")[-1].split("&")[0]
            if len(query_date) == 8:
                query_date = query_date[:8]
        # 合并 trade_events.jsonl 中被风控拒绝的订单
        events_file = os.path.join(DATA_DIR, "trade_events.jsonl")
        if _load_v2_state() is None and os.path.exists(events_file):
            try:
                with open(events_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        evt = json.loads(line)
                        if evt.get("event_type") != "risk_rejected":
                            continue
                        order = evt.get("payload", {}).get("order", {})
                        ts = evt.get("timestamp", "")
                        # 拆分 timestamp 为 date + time
                        date_part = ts[:10].replace("-", "") if len(ts) >= 10 else ""
                        time_part = ts[11:19] if len(ts) >= 19 else ""
                        # 去重：同一天同一代码同一操作只保留一条
                        dup = any(
                            t.get("date") == date_part
                            and t.get("code") == order.get("code")
                            and t.get("action") == order.get("action")
                            for t in trades
                        )
                        if dup:
                            continue
                        trades.append(
                            {
                                "date": date_part,
                                "time": time_part,
                                "code": order.get("code", ""),
                                "name": order.get("name", ""),
                                "action": order.get("action", ""),
                                "shares": order.get("shares", 0),
                                "actual_price": order.get("price", 0),
                                "strategy": order.get("strategy", ""),
                                "reason": order.get("reason", ""),
                                "status": "rejected",
                                "reject_reason": evt.get("payload", {}).get(
                                    "reason", ""
                                ),
                            }
                        )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取风控拒绝事件失败: %s", exc)
        # 获取股票名称映射
        name_map = {}
        try:
            codes = list({t.get("code", "") for t in trades if t.get("code")})
            if codes:
                loader = AKDataLoader()
                raw_codes = [normalize_code(c) for c in codes]
                quotes = loader.get_realtime_quotes(raw_codes)
                name_map = {k: v.get("name", "") for k, v in quotes.items()}
        except Exception:
            pass
        # 添加名称到交易记录（已有名称的跳过）
        for t in trades:
            if not t.get("name"):
                code = t.get("code", "")
                raw = normalize_code(code)
                t["name"] = name_map.get(raw, "")
        # 提取所有日期列表
        all_dates = sorted(
            set(t.get("date", "") for t in trades if t.get("date")), reverse=True
        )
        # 按 (date, time) 升序排序，确保前端 reverse() 后最新在前
        trades.sort(key=lambda t: (str(t.get("date", "")), str(t.get("time", ""))))
        # 按日期过滤
        if query_date:
            trades = [t for t in trades if t.get("date") == query_date]
        return {"trades": trades[-200:], "dates": all_dates}

    def _api_profit_ranking(self):
        """历史战绩榜：按已平仓股票累计收益率排名。"""
        v2_ranking = _load_v2_profit_ranking()
        if v2_ranking is not None:
            return {"ranking": v2_ranking}
        trades = load_trade_log()
        from collections import defaultdict

        stock = defaultdict(
            lambda: {
                "name": "",
                "buy_amount": 0,
                "sell_amount": 0,
                "buy_fee": 0,
                "sell_fee": 0,
                "shares_bought": 0,
                "shares_sold": 0,
            }
        )
        for t in trades:
            code = t["code"]
            stock[code]["name"] = t.get("name", code)
            if t["action"] == "buy":
                stock[code]["buy_amount"] += t["amount"]
                stock[code]["buy_fee"] += t.get("cost", 0)
                stock[code]["shares_bought"] += t["shares"]
            elif t["action"] == "sell":
                stock[code]["sell_amount"] += t["amount"]
                stock[code]["sell_fee"] += t.get("cost", 0)
                stock[code]["shares_sold"] += t["shares"]

        ranking = []
        for code, s in stock.items():
            if s["shares_sold"] <= 0:
                continue
            total_cost = s["buy_amount"] + s["buy_fee"]
            total_revenue = s["sell_amount"] - s["sell_fee"]
            net = total_revenue - total_cost
            roi = net / total_cost if total_cost > 0 else 0
            ranking.append(
                {
                    "code": code,
                    "name": s["name"],
                    "net_profit": round(net, 2),
                    "roi": round(roi, 4),
                    "buy_amount": round(s["buy_amount"], 2),
                    "sell_amount": round(s["sell_amount"], 2),
                    "shares_traded": s["shares_sold"],
                }
            )

        ranking.sort(key=lambda x: x["roi"], reverse=True)
        return {"ranking": ranking}

    def _api_scan(self) -> dict[str, Any]:
        """返回 robust_v2 最新结构化个股扫描。"""
        snapshot = _load_latest_scan()
        return {
            **snapshot,
            "stocks": snapshot.get("candidates", []),
            "updated_at": snapshot.get("generated_at", ""),
            "scan_running": _preview_scan_running(),
        }

    def _api_candidates(self) -> dict[str, Any]:
        """返回最新候选、过滤统计和实时价格。"""
        scan = self._api_scan()
        stocks = scan.get("stocks", [])[:20]

        codes = [s["code"] for s in stocks]
        prices = get_realtime_prices(codes) if codes else {}

        candidates = []
        for s in stocks:
            code = s["code"]
            price = prices.get(code, 0)
            candidates.append(
                {
                    **s,
                    "current_price": round(price, 2)
                    if price
                    else s.get("current_price", s.get("price", 0)),
                }
            )

        return {
            "candidates": candidates,
            "updated_at": scan.get("updated_at", ""),
            "trade_date": scan.get("trade_date", ""),
            "status": scan.get("status", "never_run"),
            "mode": scan.get("mode", ""),
            "scan_running": scan.get("scan_running", False),
            "input_count": scan.get("input_count", 0),
            "eligible_count": scan.get("eligible_count", 0),
            "universe": scan.get("universe", {}),
            "prefilter_counts": scan.get("prefilter_counts", {}),
            "prefilter_labels": scan.get("prefilter_labels", {}),
            "filter_counts": scan.get("filter_counts", {}),
            "filter_labels": scan.get("filter_labels", {}),
            "selected_codes": scan.get("selected_codes", []),
            "next_scheduled_scan_at": scan.get("next_scheduled_scan_at", ""),
            "error": scan.get("error", ""),
            "schedule": "每个交易日 15:05 更新候选；周度调仓日才生成正式信号",
        }

    def _api_logs(self, params):
        """读取日志文件（支持 tail）"""
        lines_count = int(params.get("lines", [100])[0])
        log_file = params.get("file", ["live_today"])[0]

        if log_file == "live":
            path = LIVE_LOG
        else:
            path = LIVE_TODAY_LOG

        if not os.path.exists(path):
            return {"logs": [], "file": log_file}

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            # 返回最后 N 行
            tail_lines = all_lines[-lines_count:]
            # 解析日志格式
            parsed = []
            for line in tail_lines:
                line = line.rstrip()
                if not line:
                    continue
                parsed.append(line)
            return {"logs": parsed, "file": log_file, "total": len(all_lines)}
        except Exception as e:
            return {"logs": [f"读取失败: {e}"], "file": log_file}

    def _api_status(self) -> dict[str, Any]:
        """系统状态"""
        live_running = is_process_running("robust_runner.py")
        web_running = True  # 自己在跑
        scan = _load_latest_scan()
        lease = _load_v2_lease()

        # 读取最新日志时间
        last_log_time = ""
        if os.path.exists(LIVE_TODAY_LOG):
            try:
                with open(LIVE_TODAY_LOG, "r", encoding="utf-8", errors="replace") as f:
                    for line in reversed(f.readlines()):
                        if "[INFO]" in line:
                            parts = line.split(" [INFO] ")
                            if parts:
                                last_log_time = parts[0].strip()
                            break
            except Exception:
                pass

        return {
            "live_runner": live_running,
            "web_server": web_running,
            "strategy_mode": "weekly_close_target",
            "scan_running": _preview_scan_running(),
            "scan_status": scan.get("status", "never_run"),
            "latest_scan_at": scan.get("generated_at", ""),
            "latest_scan_trade_date": scan.get("trade_date", ""),
            "next_scan_at": scan.get("next_scheduled_scan_at", ""),
            "scan_schedule": "每日 15:05 候选观察；周度调仓生成正式信号",
            "daemon_heartbeat_at": lease.get("heartbeat_at", ""),
            "last_log_time": last_log_time,
            "now": format_local(),
        }

    def _api_observation(self):
        """虚拟盘观察期统一状态。"""
        return asdict(build_status(ROOT_DIR, log_lines=30))

    def _api_rps(self):
        """ETF/RPS 日频轮动状态。"""
        return load_rps_state()

    def _api_equity(self):
        """净值曲线与回撤序列。

        合并三个数据源:
        1. portfolio_snapshots.jsonl — 收盘快照(含回撤)
        2. trade_events.jsonl — 盘中快照(portfolio_snapshot 事件)
        3. state daily_snapshots — 兜底(仅日期与总市值)
        """
        v2_points = _load_v2_equity()
        if v2_points is not None:
            return {
                "points": v2_points,
                "initial": INITIAL_CAPITAL,
                "source": "paper_v2",
            }

        points: list[dict] = []
        seen_timestamps: set[str] = set()

        # 1. 收盘快照
        if os.path.exists(SNAPSHOT_LOG_FILE):
            try:
                with open(
                    SNAPSHOT_LOG_FILE, "r", encoding="utf-8", errors="replace"
                ) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        s = obj.get("summary", {})
                        t = obj.get("timestamp") or obj.get("date", "")
                        points.append(
                            {
                                "t": t,
                                "value": s.get("total_value"),
                                "drawdown": s.get("drawdown", 0),
                            }
                        )
                        seen_timestamps.add(t)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取快照流水失败: %s", exc)

        # 2. 盘中快照(合并，去重)
        if os.path.exists(TRADE_EVENTS_FILE):
            try:
                peak = INITIAL_CAPITAL
                with open(
                    TRADE_EVENTS_FILE, "r", encoding="utf-8", errors="replace"
                ) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if obj.get("event_type") != "portfolio_snapshot":
                            continue
                        p = obj.get("payload", {})
                        val = p.get("total_value")
                        if val is None:
                            continue
                        t = obj.get("timestamp", "")
                        if t in seen_timestamps:
                            continue
                        seen_timestamps.add(t)
                        peak = max(peak, val)
                        points.append(
                            {
                                "t": t,
                                "value": val,
                                "drawdown": p.get(
                                    "drawdown",
                                    round((peak - val) / peak, 4) if peak > 0 else 0,
                                ),
                            }
                        )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取交易事件流水失败: %s", exc)

        # 3. 兜底: daily_snapshots
        if not points:
            state = load_state()
            daily = state.get("daily_snapshots", {})
            peak = INITIAL_CAPITAL
            for date in sorted(daily.keys()):
                val = daily[date].get("total_value")
                if val is None:
                    continue
                peak = max(peak, val)
                points.append(
                    {
                        "t": date,
                        "value": val,
                        "drawdown": round((peak - val) / peak, 4) if peak > 0 else 0,
                    }
                )

        # 按时间排序
        points.sort(key=lambda p: p["t"])

        return {"points": points, "initial": INITIAL_CAPITAL}

    def _api_backtest(self):
        """读取最近一次回测结果；缺失或过期时自动后台生成。"""
        status = ensure_backtest_cache(ROOT_DIR, async_run=True)
        path = os.path.join(REPORT_DIR, "backtest_latest.json")
        if not os.path.exists(path):
            return {**status.to_dict(), "series": []}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            data.update(status.to_dict())
            data["available"] = True
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取回测结果失败: %s", exc)
            return {
                **status.to_dict(),
                "available": False,
                "error": str(exc),
                "series": [],
            }

    def _api_reports(self):
        return {"reports": load_reports()}

    def _api_scan_trigger(self) -> tuple[dict[str, str], int]:
        """异步启动不会写入交易信号和订单的安全预览扫描。"""
        global _PREVIEW_SCAN_PROCESS
        runner_path = ROOT_DIR / "robust_runner.py"
        ledger_path = Path(ROBUST_V2_LEDGER_PATH).expanduser()
        if not runner_path.exists():
            return {"status": "error", "message": "未找到 robust_runner.py"}, 500
        if not ledger_path.exists():
            return {"status": "error", "message": "paper_v2 账本尚未初始化"}, 503

        with _PREVIEW_SCAN_LOCK:
            if (
                _PREVIEW_SCAN_PROCESS is not None
                and _PREVIEW_SCAN_PROCESS.poll() is None
            ):
                return {"status": "running", "message": "安全预览扫描正在运行"}, 409
            try:
                _PREVIEW_SCAN_PROCESS = subprocess.Popen(
                    [
                        sys.executable,
                        str(runner_path),
                        "preview-scan",
                        "--ledger",
                        str(ledger_path),
                    ],
                    cwd=ROOT_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                logger.exception("启动安全预览扫描失败")
                return {"status": "error", "message": str(exc)}, 500
        return {
            "status": "started",
            "message": "安全预览扫描已启动，不会生成交易信号或订单",
        }, 202

    # ==================== 辅助 ====================

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_template(self, name):
        path = os.path.join(TEMPLATE_DIR, name)
        if not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_spa(self, fallback_template: str, write_body: bool = True):
        """优先服务 React 构建产物；缺失时回退到旧模板，便于未构建环境运行。"""
        index_path = os.path.join(DIST_DIR, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))  # type: ignore[arg-type]
            # 入口文件引用带内容哈希的资源，必须每次校验以便及时发现新构建。
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if write_body:
                self.wfile.write(body)
            return
        self._serve_template(fallback_template)

    def _serve_static_asset(self, request_path: str, write_body: bool = True):
        """服务 Vite assets，并防止通过路径穿越读取 dist 外文件。"""
        relative_path = request_path.lstrip("/")
        asset_path = os.path.abspath(os.path.join(DIST_DIR, relative_path))
        dist_root = os.path.abspath(DIST_DIR)
        if not asset_path.startswith(dist_root + os.sep) or not os.path.exists(
            asset_path
        ):
            self.send_error(404)
            return
        mime_type, _ = mimetypes.guess_type(asset_path)
        with open(asset_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", len(body))  # type: ignore[arg-type]
        # Vite 构建资源带内容哈希，可安全长期缓存且无需重复校验。
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _parse_args(argv=None):
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="A 股虚拟盘 Web 仪表盘")
    parser.add_argument(
        "port", nargs="?", type=int, default=8888, help="监听端口，默认 8888"
    )
    return parser.parse_args(argv)


def main(argv=None):
    """启动 Web 仪表盘。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = _parse_args(argv)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), QuantHandler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    logger.info("量化系统仪表盘 v2: http://0.0.0.0:%s", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，关闭 Web 仪表盘")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

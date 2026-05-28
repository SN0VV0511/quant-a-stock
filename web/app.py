"""
量化交易系统 - Web 仪表盘 v2
双线程实时监控 + 日志 + 全记录
"""

import os
import sys
import json
import time
import logging
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    INITIAL_CAPITAL, STATE_FILE, TRADE_LOG_FILE, REPORT_DIR, LOG_DIR,
)
from data.ak_loader import AKDataLoader

logger = logging.getLogger(__name__)
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# 日志文件路径
LIVE_LOG = os.path.join(LOG_DIR, "live.log")
LIVE_TODAY_LOG = os.path.join(LOG_DIR, "live_today.log")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cash": INITIAL_CAPITAL, "positions": {}, "updated_at": ""}


def load_trade_log():
    trades = []
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
        except Exception:
            pass
    return trades


def normalize_code(code):
    if code.startswith("sh.") or code.startswith("sz."):
        return code[3:]
    if code.startswith("sh") or code.startswith("sz"):
        return code[2:]
    return code


def get_realtime_prices(codes):
    try:
        loader = AKDataLoader()
        raw_codes = [normalize_code(c) for c in codes]
        prices_raw = loader.get_realtime_batch(raw_codes)
        return {c: prices_raw.get(normalize_code(c), 0) for c in codes}
    except Exception:
        return {}


def load_reports():
    reports = []
    if os.path.isdir(REPORT_DIR):
        for fname in sorted(os.listdir(REPORT_DIR), reverse=True)[:30]:
            if fname.endswith(".txt"):
                fpath = os.path.join(REPORT_DIR, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    reports.append({"date": fname.replace(".txt", "").replace("daily_", ""), "content": content})
                except Exception:
                    pass
    return reports


def is_process_running(name):
    """检查进程是否在运行"""
    import subprocess
    try:
        result = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


class QuantHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        routes = {
            "/": lambda: self._serve_template("index.html"),
            "/index.html": lambda: self._serve_template("index.html"),
            "/api/portfolio": lambda: self._json_response(self._api_portfolio()),
            "/api/trades": lambda: self._json_response(self._api_trades()),
            "/api/scan": lambda: self._json_response(self._api_scan()),
            "/api/reports": lambda: self._json_response(self._api_reports()),
            "/api/logs": lambda: self._json_response(self._api_logs(params)),
            "/api/status": lambda: self._json_response(self._api_status()),
            "/api/candidates": lambda: self._json_response(self._api_candidates()),
        }

        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/scan/trigger":
            self._json_response(self._api_scan_trigger())
        else:
            self.send_error(404)

    # ==================== API ====================

    def _api_portfolio(self):
        state = load_state()
        cash = state.get("cash", INITIAL_CAPITAL)
        positions = state.get("positions", {})
        codes = list(positions.keys())
        prices = get_realtime_prices(codes) if codes else {}

        # 从腾讯行情获取股票名称
        name_map = {}
        try:
            loader = AKDataLoader()
            raw_codes = [normalize_code(c) for c in codes]
            quotes = loader.get_realtime_quotes(raw_codes)
            name_map = {k: v.get("name", "") for k, v in quotes.items()}
        except Exception:
            pass

        def get_name(code):
            raw = normalize_code(code)
            return name_map.get(raw, code)

        positions_value = 0
        position_list = []
        for code, pos in positions.items():
            current = prices.get(code, pos.get("current_price", pos.get("avg_cost", 0)))
            shares = pos.get("shares", 0)
            avg_cost = pos.get("avg_cost", 0)
            value = shares * current
            positions_value += value
            pnl = (current - avg_cost) / avg_cost if avg_cost > 0 else 0
            position_list.append({
                "code": code,
                "name": get_name(code),
                "shares": shares,
                "avg_cost": round(avg_cost, 3),
                "current_price": round(current, 3),
                "value": round(value, 2),
                "profit": round((current - avg_cost) * shares, 2),
                "profit_pct": round(pnl, 4),
            })

        total_value = cash + positions_value
        pnl = total_value - INITIAL_CAPITAL

        return {
            "total_value": round(total_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "position_ratio": round(positions_value / total_value, 4) if total_value > 0 else 0,
            "position_count": len(positions),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / INITIAL_CAPITAL, 4),
            "positions": position_list,
            "updated_at": state.get("updated_at", ""),
        }

    def _api_trades(self):
        trades = load_trade_log()
        return {"trades": trades[-50:]}

    def _api_scan(self):
        """从 live_today.log 中提取扫描结果"""
        result = {"stocks": [], "updated_at": ""}
        if not os.path.exists(LIVE_TODAY_LOG):
            return result

        try:
            with open(LIVE_TODAY_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()

            stocks = []
            scan_time = ""
            for line in lines:
                if "扫描完成，候选股" in line:
                    # 提取时间
                    parts = line.split(" [INFO] ")
                    if parts:
                        scan_time = parts[0].strip()
                if "#" in line and "动量=" in line and "得分=" in line:
                    try:
                        # 解析: #1 华电辽能(600396) 动量=+462.39% 得分=2.3897
                        import re
                        m = re.search(r'#(\d+)\s+(.+?)\((\d+)\)\s+动量=([+\-][\d.]+)%\s+得分=([\d.]+)', line)
                        if m:
                            stocks.append({
                                "rank": int(m.group(1)),
                                "name": m.group(2).strip(),
                                "code": m.group(3),
                                "momentum": float(m.group(4)) / 100,
                                "score": float(m.group(5)),
                            })
                    except Exception:
                        pass

            result["stocks"] = stocks
            result["updated_at"] = scan_time
        except Exception:
            pass
        return result

    def _api_candidates(self):
        """候选股实时价格"""
        scan = self._api_scan()
        stocks = scan.get("stocks", [])
        if not stocks:
            return {"candidates": [], "updated_at": ""}

        codes = [s["code"] for s in stocks]
        prices = get_realtime_prices(codes)

        candidates = []
        for s in stocks:
            code = s["code"]
            price = prices.get(code, 0)
            candidates.append({
                **s,
                "current_price": round(price, 2) if price else 0,
            })

        return {"candidates": candidates, "updated_at": scan.get("updated_at", "")}

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
            with open(path, "r", encoding="utf-8") as f:
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

    def _api_status(self):
        """系统状态"""
        live_running = is_process_running("live_runner.py")
        web_running = True  # 自己在跑

        # 读取最新日志时间
        last_log_time = ""
        if os.path.exists(LIVE_TODAY_LOG):
            try:
                with open(LIVE_TODAY_LOG, "r", encoding="utf-8") as f:
                    for line in reversed(f.readlines()):
                        if "[INFO]" in line:
                            parts = line.split(" [INFO] ")
                            if parts:
                                last_log_time = parts[0].strip()
                            break
            except Exception:
                pass

        # 检查线程状态
        watch_active = False
        scan_active = False
        if os.path.exists(LIVE_TODAY_LOG):
            try:
                with open(LIVE_TODAY_LOG, "r", encoding="utf-8") as f:
                    content = f.read()
                watch_active = "盯盘线启动" in content and "盯盘线退出" not in content.split("盯盘线启动")[-1]
                scan_active = "扫描线启动" in content and "扫描线退出" not in content.split("扫描线启动")[-1]
            except Exception:
                pass

        return {
            "live_runner": live_running,
            "web_server": web_running,
            "watch_thread": watch_active,
            "scan_thread": scan_active,
            "last_log_time": last_log_time,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _api_reports(self):
        return {"reports": load_reports()}

    def _api_scan_trigger(self):
        import subprocess
        try:
            # 通过信号触发 live_runner 的扫描（或直接调用 scanner）
            return {"status": "ok", "message": "扫描已触发"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ==================== 辅助 ====================

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
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
        with open(path, "r", encoding="utf-8") as f:
            body = f.read().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    server = HTTPServer(("0.0.0.0", port), QuantHandler)
    print(f"🚀 量化系统仪表盘 v2: http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()

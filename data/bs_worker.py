"""BaoStock 子进程 worker —— 由 ak_loader.py 通过 subprocess 调用。

用法:
    python bs_worker.py <command> [args...]

命令:
    login
    query_all_stock <date>
    query_stock_basic <code>
    query_history <bs_code> <start> <end>
    query_history_batch <start> <end> <bs_code> [bs_code...]

输出: JSON 到 stdout
"""

import contextlib
import io
import json
import os
import socket
import sys

# 必须在导入 baostock 前设置，确保其底层 socket 继承有限超时。
socket.setdefaulttimeout(float(os.getenv("BAOSTOCK_SOCKET_TIMEOUT_SECONDS", "8")))

import baostock as bs  # noqa: E402 - socket 超时必须先于第三方库导入设置


HISTORY_FIELDS = "date,open,high,low,close,volume,amount,preclose,pctChg"


@contextlib.contextmanager
def _suppress_stdout():
    """Suppress baostock's internal print() calls that pollute stdout/stderr."""
    old_out = sys.stdout
    old_err = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old_out
        sys.stderr = old_err


def _ensure_login():
    """登录 BaoStock，失败时显式抛错。"""
    with _suppress_stdout():
        result = bs.login()
    if result.error_code != "0":
        raise ConnectionError(
            f"BaoStock 登录失败: {result.error_code} {result.error_msg}"
        )


def cmd_login():
    with _suppress_stdout():
        rs = bs.login()
    if rs.error_code != "0":
        return {"ok": False, "error": rs.error_msg, "error_code": rs.error_code}
    return {"ok": True, "error_code": rs.error_code, "error_msg": rs.error_msg}


def cmd_query_all_stock(date):
    _ensure_login()
    with _suppress_stdout():
        rs = bs.query_all_stock(day=date)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    return {"error_code": rs.error_code, "rows": rows}


def cmd_query_stock_basic(code):
    _ensure_login()
    with _suppress_stdout():
        rs = bs.query_stock_basic(code)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    return {"error_code": rs.error_code, "rows": rows}


def _query_history(bs_code, start, end):
    """在当前登录会话中查询一只股票历史行情。"""
    with _suppress_stdout():
        rs = bs.query_history_k_data_plus(
            bs_code,
            HISTORY_FIELDS,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    return {
        "error_code": rs.error_code,
        "error_msg": getattr(rs, "error_msg", ""),
        "rows": rows,
    }


def cmd_query_history(bs_code, start, end):
    """登录后查询一只股票历史行情。"""
    _ensure_login()
    return _query_history(bs_code, start, end)


def cmd_query_history_batch(start, end, *bs_codes):
    """一次登录连续查询多只股票，消除逐股进程启动与登录开销。"""
    if not bs_codes:
        return {"results": {}}

    _ensure_login()
    results = {}
    for bs_code in bs_codes:
        try:
            results[bs_code] = _query_history(bs_code, start, end)
        except Exception as exc:
            results[bs_code] = {
                "error_code": "WORKER_ERROR",
                "error_msg": str(exc),
                "rows": [],
            }
    return {"results": results}


# 扩展字段:含换手率/估值/ST/停牌,供小市值价值选股使用
EXT_FIELDS = (
    "date,open,high,low,close,volume,amount,turn,peTTM,pbMRQ,isST,tradestatus,pctChg"
)


def cmd_query_history_ext(bs_code, start, end):
    """查询扩展字段历史(含 turn/peTTM/pbMRQ/isST/tradestatus)。"""
    _ensure_login()
    with _suppress_stdout():
        rs = bs.query_history_k_data_plus(
            bs_code,
            EXT_FIELDS,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    return {"error_code": rs.error_code, "rows": rows, "fields": EXT_FIELDS}


def cmd_query_history_ext_batch(start, end, *bs_codes):
    """一次登录连续查询一批扩展历史，避免逐股启动进程。"""
    if not bs_codes:
        return {"results": {}}
    _ensure_login()
    results = {}
    for bs_code in bs_codes:
        try:
            with _suppress_stdout():
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    EXT_FIELDS,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="2",
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            results[bs_code] = {
                "error_code": rs.error_code,
                "error_msg": getattr(rs, "error_msg", ""),
                "rows": rows,
            }
        except Exception as exc:
            results[bs_code] = {
                "error_code": "WORKER_ERROR",
                "error_msg": str(exc),
                "rows": [],
            }
    return {"results": results}


def cmd_logout():
    """登出 BaoStock（子进程内执行，避免污染主进程日志）。"""
    with _suppress_stdout():
        bs.logout()
    return {"ok": True}


COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "query_all_stock": cmd_query_all_stock,
    "query_stock_basic": cmd_query_stock_basic,
    "query_history": cmd_query_history,
    "query_history_batch": cmd_query_history_batch,
    "query_history_ext": cmd_query_history_ext,
    "query_history_ext_batch": cmd_query_history_ext_batch,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(
            json.dumps(
                {"ok": False, "error": f"unknown command: {sys.argv[1:]}"},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    cmd_name = sys.argv[1]
    args = sys.argv[2:]

    try:
        result = COMMANDS[cmd_name](*args)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

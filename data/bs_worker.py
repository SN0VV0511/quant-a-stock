"""BaoStock 子进程 worker —— 由 ak_loader.py 通过 subprocess 调用。

用法:
    python bs_worker.py <command> [args...]

命令:
    login
    query_all_stock <date>
    query_stock_basic <code>
    query_history <bs_code> <start> <end>

输出: JSON 到 stdout
"""

import contextlib
import io
import json
import sys

import baostock as bs


@contextlib.contextmanager
def _suppress_stdout():
    """Suppress baostock's internal print() calls that pollute stdout."""
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old


def _ensure_login():
    """Login to BaoStock (suppressing its stdout noise)."""
    with _suppress_stdout():
        bs.login()


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


def cmd_query_history(bs_code, start, end):
    _ensure_login()
    with _suppress_stdout():
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
    return {"error_code": rs.error_code, "rows": rows}


COMMANDS = {
    "login": cmd_login,
    "query_all_stock": cmd_query_all_stock,
    "query_stock_basic": cmd_query_stock_basic,
    "query_history": cmd_query_history,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "error": f"unknown command: {sys.argv[1:]}"}, ensure_ascii=False))
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

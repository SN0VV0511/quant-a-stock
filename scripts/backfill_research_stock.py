"""补齐 robust_research/stock 目录缺失的历史行情(供 robust_walk_forward 全池校验)。

用法(容器内): python -m scripts.backfill_research_stock [--limit N] [--end 20260909]
数据源: baostock 日K(前复权),与 param_ab_20260904 同口径,输出 CSV 至 data/robust_research/stock/。
已存在的代码自动跳过,可反复执行(断点续传)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.param_ab_20260904 import (  # noqa: E402
    PRELOAD_START,
    _attach_fundamentals,
    _bs_code,
    _fetch_kline,
)

DATA = ROOT / "data" / "robust_research"
STOCK = DATA / "stock"
UNIVERSE = DATA / "universe"


def missing_codes(end: str) -> list[str]:
    codes: set[str] = set()
    for path in sorted(UNIVERSE.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        codes.update(payload.get("codes") or [])
    existing = {path.stem for path in STOCK.glob("*.csv")}
    return sorted(codes - existing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        default=str(DATA),
        help="研究数据目录(默认脚本相对路径;stdin运行时必须显式指定)",
    )
    parser.add_argument("--end", default="20260909", help="截止日期 YYYYMMDD")
    parser.add_argument("--limit", type=int, default=0, help="最多下载数量(0=全部)")
    parser.add_argument("--sleep", type=float, default=0.2, help="单只间隔秒")
    args = parser.parse_args()

    stock_dir = Path(args.data_root) / "stock"
    universe_dir = Path(args.data_root) / "universe"
    todo_codes: set[str] = set()
    for path in sorted(universe_dir.glob("robust_v2_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for stock in payload.get("stocks") or []:
            if isinstance(stock, dict) and stock.get("code"):
                code = str(stock["code"]).rsplit(".", 1)[-1]
                todo_codes.add(code)
    existing = {path.stem for path in stock_dir.glob("*.csv")}
    todo = sorted(todo_codes - existing)
    if args.limit > 0:
        todo = todo[: args.limit]
    print(f"待补 {len(todo)} 只", flush=True)
    if not todo:
        return 0

    login = bs.login()
    if login.error_code != "0":
        print(f"baostock 登录失败: {login.error_msg}")
        return 1

    ok, failed = 0, []
    try:
        for index, code in enumerate(todo, 1):
            try:
                frame = _fetch_kline(_bs_code(code), PRELOAD_START, args.end)
                if frame.empty:
                    failed.append(code)
                    continue
                frame = _attach_fundamentals(frame)
                frame.to_csv(stock_dir / f"{code}.csv", index=False)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(code)
                print(f"[{index}] {code} 异常: {exc}", flush=True)
            if index % 25 == 0 or index == len(todo):
                print(f"进度 {index}/{len(todo)} ok={ok} fail={len(failed)}", flush=True)
            time.sleep(args.sleep)
    finally:
        bs.logout()

    print(f"完成: ok={ok} fail={len(failed)}", flush=True)
    if failed:
        print("失败清单:", ",".join(failed), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

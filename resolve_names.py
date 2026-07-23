#!/usr/bin/env python3
"""解析股票/ETF名称，优先本地缓存，回退 akshare。用于收盘日报推送，避免 baostock 卡死。"""

import io
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "data", "name_cache.json")
REPORT = os.path.join(
    BASE,
    "reports",
    "daily_v2_"
    + subprocess.check_output(["date", "+%Y%m%d"]).decode().strip()
    + ".txt",
)

cache = {}
if os.path.exists(CACHE):
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}


def get_codes():
    try:
        raw = open(REPORT, encoding="utf-8", errors="ignore").read()
        # 只保留 A 股/ETF 代码（以 0/3/5/6 开头），过滤日期、随机数等噪声
        found = set(re.findall(r"[0-9]{6}", raw))
        return sorted(c for c in found if c[0] in "0356")
    except Exception:
        return []


codes = sys.argv[1:] or get_codes()
codes = [c for c in codes if c[0] in "0356"]
names = dict(cache)
missing = [c for c in codes if c not in names]

if missing:
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()  # 屏蔽 akshare 噪声
    try:
        import akshare as ak

        try:
            fdf = ak.fund_name_em()
            for _, r in fdf.iterrows():
                names[r["基金代码"]] = r["基金简称"]
        except Exception:
            pass
        try:
            sdf = ak.stock_info_a_code_name()
            for _, r in sdf.iterrows():
                names[r["code"]] = r["name"]
        except Exception:
            pass
    except Exception:
        pass
    sys.stdout = old_stdout
    try:
        json.dump(names, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

for c in codes:
    print(f"{c}:{names.get(c, '未知')}")

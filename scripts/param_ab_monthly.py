"""逐月分解参数A/B回测:看B的优势是稳定还是运气。"""
import json
from collections import defaultdict

import pandas as pd

REPORT = "/opt/quant-a-stock/reports/param_ab_20260904.json"
OUT = "/opt/quant-a-stock/reports/param_ab_monthly_20260904.json"

d = json.load(open(REPORT))
series = {}
for label, cfg in d["results"].items():
    eq = pd.DataFrame(cfg["equity"])
    eq["date"] = pd.to_datetime(eq["date"], format="%Y%m%d")
    s = eq.set_index("date")["value"].sort_index()
    series[label] = s

# 月末净值 → 月度收益率
monthly = {}
for label, s in series.items():
    month_end = s.resample("ME").last().dropna()
    rets = month_end.pct_change().dropna()
    monthly[label] = rets

months = sorted(set().union(*[set(m.index) for m in monthly.values()]))
table = []
for m in months:
    row = {"month": m.strftime("%Y-%m")}
    for label in ("A", "B", "C"):
        v = monthly[label].get(m)
        row[label] = round(float(v) * 100, 2) if v is not None else None
    table.append(row)

def stats(r):
    r = r.dropna()
    return {
        "正收益月数": int((r > 0).sum()),
        "总月数": int(len(r)),
        "月均收益%": round(float(r.mean()) * 100, 2),
        "月收益标准差%": round(float(r.std()) * 100, 2),
        "最好月%": round(float(r.max()) * 100, 2),
        "最差月%": round(float(r.min()) * 100, 2),
    }

stats_out = {label: stats(monthly[label]) for label in ("A", "B", "C")}

# 两两对决(双方都有数据的月份)
def duel(x, y):
    common = monthly[x].dropna().index.intersection(monthly[y].dropna().index)
    win = int((monthly[x][common] > monthly[y][common]).sum())
    return {"x>y月数": win, "共同样本": int(len(common)),
            "胜率%": round(win / len(common) * 100, 1) if len(common) else None}

duels = {"B_vs_A": duel("B", "A"), "B_vs_C": duel("B", "C"), "C_vs_A": duel("C", "A")}

# 年度小计
yearly = {}
for label, m in monthly.items():
    by_year = m.groupby(m.index.year).apply(lambda x: float((1 + x).prod() - 1) * 100)
    yearly[label] = {str(k): round(v, 2) for k, v in by_year.items()}

out = {"monthly_table": table, "monthly_stats": stats_out,
       "head_to_head": duels, "yearly_subtotal": yearly}
json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)

print("=== 逐月收益% (A原参数/B放宽月频/C放宽周频) ===")
print(f"{'月份':<9}{'A':>8}{'B':>8}{'C':>8}")
for row in table:
    fmt = lambda v: f"{v:>8.2f}" if v is not None else f"{'—':>8}"
    print(f"{row['month']:<9}{fmt(row['A'])}{fmt(row['B'])}{fmt(row['C'])}")
print()
for label, s in stats_out.items():
    print(label, s)
print()
for k, v in duels.items():
    print(k, v)
print()
print("年度小计:", json.dumps(yearly, ensure_ascii=False))

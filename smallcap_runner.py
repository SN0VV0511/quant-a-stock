"""小市值价值策略 - 虚拟盘日频执行器(周度调仓)。

依据回测结论(scripts/strategy_ab.py):A股价格动量长期负 IC,而"小市值 + 低PB +
短期反转 + 国九条风控"显著占优。本 runner 把该策略接入虚拟盘观察期:

流程(收盘后运行一次):
    1. 交易日判断;
    2. 盯市:用实时价更新持仓、保存账户快照(供观察期复盘);
    3. 调仓日(每周首个交易日)才换仓:
       a. 全市场股票列表 → 实时行情粗筛(价格/流动性)→ 按成交量截断;
       b. 拉取扩展字段历史(close/PB/流通市值/ST/停牌)→ 因子打分 → Top-N;
       c. 目标组合 vs 当前持仓求差额,先卖后买,经五层风控,走虚拟盘成交;
    4. 写结构化事件 + 收盘日报。

与回测行为一致:非调仓日不交易,只盯市。默认 BROKER_MODE=paper,绝不真实下单。

用法:
    python smallcap_runner.py --dry-run            # 预览选股与调仓,不成交
    python smallcap_runner.py                       # 调仓日执行虚拟成交
    python smallcap_runner.py --force-rebalance     # 强制当日调仓(联调用)
    python smallcap_runner.py --ignore-calendar     # 忽略交易日历(联调用)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.logging_setup import setup_logger  # noqa: E402
from config.settings import (  # noqa: E402
    INITIAL_CAPITAL,
    MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION,
    REPORT_DIR,
    SMALLCAP_TOP_N,
    SMALLCAP_REBALANCE_DAYS,
    SMALLCAP_REVERSAL_DAYS,
    SMALLCAP_MIN_PRICE,
    SCAN_MIN_VOLUME,
    SCAN_MAX_HIST_FETCH,
    normalize_a_share_code,
)
from data.ak_loader import AKDataLoader  # noqa: E402
from data.holidays import is_trading_day as is_calendar_trading_day  # noqa: E402
from risk.control import RiskController  # noqa: E402
from strategies.small_cap_value import build_factor_rows, score_small_cap_value  # noqa: E402
from trading.brokers import PaperBrokerAdapter, create_broker  # noqa: E402
from trading.models import OrderIntent, RiskDecision  # noqa: E402
from trading.observability import EventRecorder  # noqa: E402

logger = setup_logger("smallcap_runner", rotating_files=("smallcap_runner.log",),
                      plain_files=("smallcap_today.log",))


def _norm(code: str) -> str:
    """归一化为 6 位代码,失败返回原值小写。"""
    try:
        return normalize_a_share_code(code)
    except ValueError:
        return str(code).strip().lower()


def is_trading_day(date_str: str) -> bool:
    """判断交易日,日历失败回退周末判断。"""
    try:
        return is_calendar_trading_day(date_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历判断失败,回退周末判断: %s", exc)
        return datetime.strptime(date_str, "%Y%m%d").weekday() < 5


def is_rebalance_day(date_str: str) -> bool:
    """是否为调仓日:本周(ISO 周)的第一个交易日。

    向前回溯到上一个交易日,若其不在同一 ISO 周,则今天是本周首个交易日。
    这样即使周一停牌,也能在本周第一个开市日调仓。
    """
    today = datetime.strptime(date_str, "%Y%m%d")
    probe = today
    for _ in range(10):
        probe = probe - timedelta(days=1)
        ds = probe.strftime("%Y%m%d")
        if is_trading_day(ds):
            return probe.isocalendar()[:2] != today.isocalendar()[:2]
    return True


def _build_universe(loader: AKDataLoader, max_universe: int) -> tuple[list[str], dict[str, dict]]:
    """全市场列表 → 实时行情粗筛(价格/流动性)→ 按成交量截断。

    Returns:
        (codes, quotes): 候选代码列表(6 位)与其实时行情字典。
    """
    stocks = loader.get_all_stocks()
    all_codes = [s["code"] for s in stocks]
    name_map = {s["code"]: s.get("name", "") for s in stocks}
    logger.info("全市场股票 %d 只,获取实时行情粗筛...", len(all_codes))
    quotes = loader.get_realtime_quotes(all_codes)

    pre = []
    for code in all_codes:
        q = quotes.get(code)
        if not q:
            continue
        price = q.get("price", 0) or 0
        vol = q.get("volume", 0) or 0
        if price < SMALLCAP_MIN_PRICE or vol < SCAN_MIN_VOLUME:
            continue
        name = str(q.get("name", name_map.get(code, "")))
        if "ST" in name.upper() or "退" in name:  # 国九条:剔除 ST/退市风险
            continue
        pre.append((code, vol))

    pre.sort(key=lambda x: x[1], reverse=True)
    if len(pre) > max_universe:
        pre = pre[:max_universe]
    codes = [c for c, _ in pre]
    logger.info("粗筛后候选 %d 只(上限 %d)", len(codes), max_universe)
    return codes, quotes


def _select_targets(loader: AKDataLoader, codes: list[str], quotes: dict,
                    top_n: int) -> list[dict[str, Any]]:
    """拉扩展历史 → 因子打分 → 返回 Top-N 目标(含 code/name/price/score/rank)。"""
    logger.info("拉取 %d 只扩展历史(估值/市值/ST)...", len(codes))
    hist = loader.get_batch_history_ext(codes, days=SMALLCAP_REVERSAL_DAYS + 15)
    name_map = {c: str(quotes.get(c, {}).get("name", c)) for c in codes}
    rows = build_factor_rows(hist, reversal_days=SMALLCAP_REVERSAL_DAYS, name_map=name_map)
    targets = score_small_cap_value(rows, top_n=top_n)
    logger.info("因子打分后选出 Top%d", len(targets))
    return targets


def _submit(intent: OrderIntent, broker: PaperBrokerAdapter, risk: RiskController,
            market_data: dict, recorder: EventRecorder, dry_run: bool) -> bool:
    """提交单笔订单:记录信号 → 风控 → 虚拟成交。返回是否成交。"""
    recorder.record("signal", intent.to_dict())
    if dry_run:
        logger.info("[预览] %s %s %s股 @ %.3f - %s",
                    intent.action, intent.code, intent.shares, intent.price, intent.reason)
        return False

    approved, rejected = risk.filter_order_intents([intent], broker.portfolio, market_data)
    for dec in rejected:
        recorder.record("risk_rejected", dec.to_dict())
        logger.info("风控拒绝: %s %s - %s", intent.code, intent.action, dec.reason)
    if not approved:
        return False

    recorder.record("risk_approved", RiskDecision(order=intent, approved=True, reason="通过").to_dict())
    report = broker.place_order(intent)
    recorder.record("execution", report.to_dict())
    if report.is_success:
        logger.info("[成交] %s %s %s股 @ %.3f", intent.action, intent.code,
                    report.shares, report.actual_price)
        return True
    logger.warning("[拒单] %s %s %s", intent.action, intent.code, report.message)
    return False


def _market_data(codes: list[str], quotes: dict) -> dict[str, dict]:
    """构建风控行情数据。"""
    md = {}
    for code in codes:
        q = quotes.get(code) or {}
        name = str(q.get("name", ""))
        price = float(q.get("price", 0) or 0)
        md[code] = {
            "current_price": price,
            "prev_close": float(q.get("prev_close", 0) or 0),
            "is_st": "ST" in name.upper() or "退" in name,
            "is_suspended": price <= 0,
            "name": name,
        }
    return md


def rebalance(broker: PaperBrokerAdapter, loader: AKDataLoader, risk: RiskController,
              recorder: EventRecorder, date_str: str, top_n: int, max_universe: int,
              dry_run: bool) -> None:
    """执行一次周度调仓。"""
    codes, quotes = _build_universe(loader, max_universe)
    if len(codes) < top_n:
        logger.error("候选不足(%d < %d),放弃本次调仓以免基于坏数据交易", len(codes), top_n)
        recorder.record("rebalance_aborted", {"reason": "universe_too_small", "size": len(codes)})
        return

    targets = _select_targets(loader, codes, quotes, top_n)
    if not targets:
        logger.error("因子选股为空,放弃本次调仓")
        recorder.record("rebalance_aborted", {"reason": "no_targets"})
        return

    target_codes = {t["code"] for t in targets}
    holdings = broker.query_positions()
    held = set(holdings.keys())

    sells = held - target_codes
    buys = [t for t in targets if t["code"] not in held]

    logger.info("调仓: 持有 %d → 目标 %d | 卖出 %d 只, 买入 %d 只",
                len(held), len(target_codes), len(sells), len(buys))
    recorder.record("rebalance_plan", {
        "date": date_str, "targets": [t["code"] for t in targets],
        "sell": list(sells), "buy": [t["code"] for t in buys],
    })

    # 执行价/风控行情:对卖出持仓单独取实时价(可能已跌出粗筛池)
    sell_quotes = loader.get_realtime_quotes(list(sells)) if sells else {}
    exec_quotes = {**quotes, **sell_quotes}
    md = _market_data(list(sells) + [t["code"] for t in targets], exec_quotes)

    # 1) 先卖(释放现金)
    for code in sells:
        pos = holdings[code]
        price = float(exec_quotes.get(code, {}).get("price", 0) or pos.get("current_price", 0) or 0)
        if price <= 0:
            logger.info("跳过卖出 %s:无有效价格(可能停牌)", code)
            continue
        intent = OrderIntent(
            code=code, name=str(pos.get("name", code)), action="sell",
            price=price, shares=int(pos.get("shares", 0)), date=date_str,
            strategy="小市值调仓", reason="调出目标池", source="smallcap_runner",
            strategy_tag="smallcap_value",
        )
        _submit(intent, broker, risk, md, recorder, dry_run)

    # 2) 再买:等权目标受单票和总仓位上限约束,避免计划仓位被风控整体拒绝。
    max_ratio = min(MAX_SINGLE_STOCK, MAX_TOTAL_POSITION / max(1, top_n))
    for t in buys:
        code = t["code"]
        price = float(quotes.get(code, {}).get("price", 0) or t.get("price", 0) or 0)
        if price <= 0:
            continue
        total_value = broker.portfolio.get_total_value()
        shares = broker.portfolio.rules.calc_lot_size(
            price, broker.query_cash(), max_ratio=max_ratio, total_value=total_value)
        if shares <= 0:
            recorder.record("signal_skipped", {"code": code, "reason": "可买不足一手", "price": price})
            continue
        intent = OrderIntent(
            code=code, name=str(t.get("name", code)), action="buy",
            price=price, shares=shares, date=date_str,
            strategy="全市场扫描+小市值价值", reason=f"小市值价值 rank{t['rank']}",
            strategy_tag="smallcap_value",
            source="smallcap_runner", metadata={"score": t.get("score")},
        )
        _submit(intent, broker, risk, md, recorder, dry_run)


def _mark_and_snapshot(broker: PaperBrokerAdapter, loader: AKDataLoader,
                       recorder: EventRecorder, date_str: str) -> dict:
    """用实时价盯市并保存账户快照。返回价格字典。"""
    positions = broker.query_positions()
    prices: dict[str, float] = {}
    if positions:
        quotes = loader.get_realtime_quotes(list(positions.keys()))
        prices = {c: float(quotes.get(_norm(c), {}).get("price", 0) or 0) for c in positions}
        prices = {c: p for c, p in prices.items() if p > 0}
        broker.portfolio.update_prices(prices)
    broker.portfolio.save_snapshot(date_str, prices)
    snap = broker.query_snapshot(prices)
    recorder.record("portfolio_snapshot_close", snap.to_dict())
    return prices


def _write_report(broker: PaperBrokerAdapter, prices: dict, date_str: str,
                  rebalanced: bool) -> None:
    """生成收盘日报。"""
    snap = broker.query_snapshot(prices)
    trades = broker.portfolio.get_trades_by_date(date_str)
    lines = [
        "=" * 50,
        f"小市值价值策略日报 - {datetime.now().strftime('%Y-%m-%d')}(虚拟盘)",
        "=" * 50,
        f"调仓日: {'是' if rebalanced else '否(仅盯市)'}",
        f"初始资金: ¥{INITIAL_CAPITAL:,.2f}",
        f"当前总值: ¥{snap.total_value:,.2f}",
        f"总收益率: {snap.pnl_pct:+.2%}",
        f"今日交易: {len(trades)} 笔",
        "",
        "持仓明细:",
    ]
    if snap.positions:
        for p in snap.positions:
            lines.append(f"  {p['code']}: {p['shares']}股 成本={p['avg_cost']:.3f} "
                         f"现价={p['current_price']:.3f} 盈亏={p['profit_pct']:+.2%}")
    else:
        lines.append("  (空仓)")
    lines.append("=" * 50)
    text = "\n".join(lines)
    logger.info("\n%s", text)
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, f"daily_{date_str}.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def run(date_str: str | None = None, *, dry_run: bool = False, force_rebalance: bool = False,
        ignore_calendar: bool = False, top_n: int = SMALLCAP_TOP_N,
        max_universe: int = SCAN_MAX_HIST_FETCH) -> dict:
    """每日执行入口。"""
    date_str = (date_str or datetime.now().strftime("%Y%m%d")).replace("-", "")

    if not ignore_calendar and not is_trading_day(date_str):
        logger.info("%s 不是交易日,跳过", date_str)
        return {"date": date_str, "is_trading_day": False}

    broker = create_broker("paper")
    broker.connect()
    if not isinstance(broker, PaperBrokerAdapter):
        raise RuntimeError("小市值 runner 仅允许 paper 虚拟盘")
    loader = AKDataLoader()
    risk = RiskController()
    recorder = EventRecorder()
    risk.set_daily_start(broker.portfolio)

    rebalanced = force_rebalance or is_rebalance_day(date_str)
    logger.info("=" * 60)
    logger.info("小市值价值虚拟盘 %s | 调仓日=%s dry_run=%s top_n=%d",
                date_str, rebalanced, dry_run, top_n)

    try:
        if rebalanced:
            rebalance(broker, loader, risk, recorder, date_str, top_n, max_universe, dry_run)
        else:
            logger.info("非调仓日,仅盯市")
        # dry-run 全程只读,不写快照/状态/日报
        if not dry_run:
            prices = _mark_and_snapshot(broker, loader, recorder, date_str)
            _write_report(broker, prices, date_str, rebalanced)
    finally:
        loader.close()
        broker.close()

    snap = broker.query_snapshot()
    logger.info("完成: 总值=%.2f 收益=%.2f%% 持仓=%d",
                snap.total_value, snap.pnl_pct * 100, snap.position_count)
    return {"date": date_str, "is_trading_day": True, "rebalanced": rebalanced,
            "total_value": snap.total_value}


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="小市值价值策略虚拟盘日频执行")
    p.add_argument("--date", default=None, help="指定日期 YYYYMMDD")
    p.add_argument("--dry-run", action="store_true", help="预览选股与调仓,不成交")
    p.add_argument("--force-rebalance", action="store_true", help="强制当日调仓")
    p.add_argument("--ignore-calendar", action="store_true", help="忽略交易日历")
    p.add_argument("--top-n", type=int, default=SMALLCAP_TOP_N, help="持仓数量")
    p.add_argument("--max-universe", type=int, default=SCAN_MAX_HIST_FETCH, help="候选上限")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    run(args.date, dry_run=args.dry_run, force_rebalance=args.force_rebalance,
        ignore_calendar=args.ignore_calendar, top_n=args.top_n, max_universe=args.max_universe)


if __name__ == "__main__":
    main()

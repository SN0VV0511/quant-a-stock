"""虚拟盘运行健康检查。

用于每日开盘前、盘中巡检或收盘后验证本地状态文件、交易流水和快照流水是否可读，
并检查负现金、非法持仓、损坏 JSONL 等会影响一个月观察期可信度的问题。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (
    LOT_SIZE,
    MAX_SINGLE_ETF,
    MAX_SINGLE_STOCK,
    MAX_TOTAL_POSITION,
    is_etf,
    is_supported_trading_target,
)
from config.time_utils import now_local


LOGGER = logging.getLogger("paper_healthcheck")
RATIO_TOLERANCE = 0.01


@dataclass
class HealthcheckResult:
    """健康检查结果。"""

    ok: bool = True
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str) -> None:
        """记录失败项。"""
        self.ok = False
        self.failures.append(message)

    def warn(self, message: str) -> None:
        """记录警告项。"""
        self.warnings.append(message)


def _parse_timestamp(value: str | None) -> datetime | None:
    """解析项目里常见的时间格式。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _load_json(path: Path, result: HealthcheckResult, required: bool) -> dict[str, Any]:
    """读取 JSON 文件。"""
    if not path.exists():
        if required:
            result.fail(f"缺少必要文件: {path}")
        else:
            result.warn(f"文件不存在: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result.fail(f"读取 JSON 失败: {path} - {exc}")
        return {}


def _load_jsonl(path: Path, result: HealthcheckResult, required: bool) -> list[dict[str, Any]]:
    """读取 JSONL 文件并报告损坏行。"""
    if not path.exists():
        if required:
            result.fail(f"缺少必要文件: {path}")
        else:
            result.warn(f"文件不存在: {path}")
        return []

    rows: list[dict[str, Any]] = []
    try:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                result.fail(f"JSONL 损坏: {path}:{line_no} - {exc}")
    except OSError as exc:
        result.fail(f"读取 JSONL 失败: {path} - {exc}")
    return rows


def _check_state(state: dict[str, Any], result: HealthcheckResult) -> None:
    """检查持仓状态。"""
    cash = state.get("cash")
    if not isinstance(cash, (int, float)):
        result.fail("账户现金 cash 缺失或不是数字")
        cash = 0.0
    elif cash < 0:
        result.fail(f"账户现金为负数: {cash}")

    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        result.fail("positions 必须是字典")
        positions = {}

    position_value = 0.0
    position_values: dict[str, float] = {}
    for code, pos in positions.items():
        if not is_supported_trading_target(str(code)):
            result.fail(f"持仓标的不是沪深 A 股股票或 ETF: {code}")
        if not isinstance(pos, dict):
            result.fail(f"持仓结构非法: {code}")
            continue
        shares = pos.get("shares")
        avg_cost = pos.get("avg_cost")
        if not isinstance(shares, int) or shares <= 0:
            result.fail(f"持仓股数非法: {code} shares={shares}")
        elif shares % LOT_SIZE != 0:
            result.fail(f"持仓股数不是整手: {code} shares={shares}")
        if not isinstance(avg_cost, (int, float)) or avg_cost <= 0:
            result.fail(f"持仓成本价非法: {code} avg_cost={avg_cost}")
            avg_cost = 0.0
        current_price = pos.get("current_price", avg_cost)
        if not isinstance(current_price, (int, float)) or current_price <= 0:
            result.warn(f"持仓当前价缺失或非法，将按成本估值: {code}")
            current_price = avg_cost
        if isinstance(shares, int) and shares > 0:
            value = float(current_price) * shares
            position_values[str(code)] = value
            position_value += value

    total_value = float(cash or 0.0) + position_value
    if total_value > 0:
        total_position_ratio = position_value / total_value
        if total_position_ratio > MAX_TOTAL_POSITION + RATIO_TOLERANCE:
            result.fail(
                f"总仓位超限: {total_position_ratio:.2%} > {MAX_TOTAL_POSITION:.2%}"
            )

        for code, value in position_values.items():
            ratio = value / total_value
            max_ratio = MAX_SINGLE_ETF if is_etf(code) else MAX_SINGLE_STOCK
            shares = positions.get(code, {}).get("shares", 0)
            if ratio > max_ratio + RATIO_TOLERANCE:
                if shares == LOT_SIZE:
                    result.warn(
                        f"单票仓位超过上限但为最低一手: {code} {ratio:.2%} > {max_ratio:.2%}"
                    )
                else:
                    result.fail(f"单票仓位超限: {code} {ratio:.2%} > {max_ratio:.2%}")

    result.metrics.update({
        "cash": round(float(cash or 0.0), 2),
        "position_count": len(positions),
        "position_ratio": round(position_value / total_value, 4) if total_value > 0 else 0.0,
        "estimated_total_value": round(total_value, 2),
    })


def _check_snapshots(
    snapshots: list[dict[str, Any]],
    result: HealthcheckResult,
    max_snapshot_age_minutes: int,
    strict_snapshot: bool,
) -> None:
    """检查账户快照流水。"""
    if not snapshots:
        if strict_snapshot:
            result.fail("账户快照流水为空")
        else:
            result.warn("账户快照流水为空，建议盘中或收盘后生成快照")
        return

    latest = snapshots[-1]
    timestamp = _parse_timestamp(str(latest.get("timestamp", "")))
    if timestamp:
        age_minutes = (now_local() - timestamp).total_seconds() / 60
        result.metrics["latest_snapshot_age_minutes"] = round(age_minutes, 1)
        if age_minutes > max_snapshot_age_minutes:
            result.warn(f"最近快照已超过 {max_snapshot_age_minutes} 分钟: {latest.get('timestamp')}")
    else:
        result.warn("最近快照缺少可解析 timestamp")

    summary = latest.get("summary", {})
    if not isinstance(summary, dict):
        result.fail("最近快照 summary 结构非法")
        return
    total_value = summary.get("total_value")
    cash = summary.get("cash")
    if isinstance(total_value, (int, float)) and total_value < 0:
        result.fail(f"最近快照总资产为负数: {total_value}")
    if isinstance(cash, (int, float)) and cash < 0:
        result.fail(f"最近快照现金为负数: {cash}")


def _check_trades(trades: list[dict[str, Any]], result: HealthcheckResult) -> None:
    """检查交易流水。"""
    valid_actions = {"buy", "sell"}
    for idx, trade in enumerate(trades, start=1):
        code = str(trade.get("code", ""))
        if not is_supported_trading_target(code):
            result.fail(f"交易流水第 {idx} 行标的不是沪深 A 股股票或 ETF: {code}")
        action = trade.get("action") or trade.get("direction")
        if action not in valid_actions:
            result.fail(f"交易流水第 {idx} 行方向非法: {action}")
        shares = trade.get("shares")
        if not isinstance(shares, int) or shares <= 0:
            result.fail(f"交易流水第 {idx} 行股数非法: {shares}")
        elif shares % LOT_SIZE != 0:
            result.fail(f"交易流水第 {idx} 行股数不是整手: {shares}")
        price = trade.get("actual_price", trade.get("price"))
        if not isinstance(price, (int, float)) or price <= 0:
            result.fail(f"交易流水第 {idx} 行价格非法: {price}")
    result.metrics["trade_count"] = len(trades)


def _event_order_key(payload: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """提取信号、风控、成交事件中的订单追踪键。"""
    order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    code = order.get("code")
    action = order.get("action")
    if not code or not action:
        return None
    return str(code), str(action), order.get("date")


def _check_events(
    events: list[dict[str, Any]],
    result: HealthcheckResult,
    strict_events: bool,
    trade_count: int,
) -> None:
    """检查结构化事件是否能串起信号、风控和成交。"""
    if not events:
        if strict_events:
            result.fail("结构化事件流水为空")
        return

    event_counts: dict[str, int] = {}
    signal_keys = set()
    approved_keys = set()

    for idx, event in enumerate(events, start=1):
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            result.fail(f"事件第 {idx} 行缺少 event_type")
            continue
        event_counts[event_type] = event_counts.get(event_type, 0) + 1

        payload = event.get("payload")
        if not isinstance(payload, dict):
            result.fail(f"事件第 {idx} 行 payload 不是字典")
            continue

        if event_type == "signal":
            key = _event_order_key(payload)
            if key:
                signal_keys.add(key)
        elif event_type == "risk_approved":
            key = _event_order_key(payload)
            if key:
                approved_keys.add(key)
        elif event_type == "execution":
            key = _event_order_key(payload)
            code = str(payload.get("code", ""))
            if not is_supported_trading_target(code):
                result.fail(f"成交事件第 {idx} 行标的不是沪深 A 股股票或 ETF: {code}")
            if payload.get("status") == "filled":
                shares = payload.get("shares")
                actual_price = payload.get("actual_price")
                if not isinstance(shares, int) or shares <= 0:
                    result.fail(f"成交事件第 {idx} 行成交股数非法: {shares}")
                elif shares % LOT_SIZE != 0:
                    result.fail(f"成交事件第 {idx} 行成交股数不是整手: {shares}")
                if not isinstance(actual_price, (int, float)) or actual_price <= 0:
                    result.fail(f"成交事件第 {idx} 行成交价非法: {actual_price}")
                if key and key not in signal_keys:
                    result.fail(f"成交事件缺少对应信号记录: {key}")
                if key and key not in approved_keys:
                    result.fail(f"成交事件缺少对应风控通过记录: {key}")

    result.metrics["event_types"] = event_counts
    if strict_events and trade_count > 0 and event_counts.get("execution", 0) < trade_count:
        result.fail(
            f"成交事件数量少于交易流水: execution={event_counts.get('execution', 0)} trades={trade_count}"
        )


def _check_report_consistency(
    root_dir: Path,
    snapshots: list[dict[str, Any]],
    result: HealthcheckResult,
    strict_report: bool,
) -> None:
    """检查最新日报是否与最新账户快照一致。"""
    if not snapshots:
        return

    latest = snapshots[-1]
    date = str(latest.get("date", ""))
    summary = latest.get("summary", {})
    total_value = summary.get("total_value") if isinstance(summary, dict) else None
    if not date or not isinstance(total_value, (int, float)):
        return

    report_path = root_dir / "reports" / f"daily_{date}.txt"
    if not report_path.exists():
        if strict_report:
            result.fail(f"缺少最新快照对应日报: {report_path}")
        return

    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.fail(f"读取日报失败: {report_path} - {exc}")
        return

    expected = f"当前总值: ¥{float(total_value):,.2f}"
    if expected not in text:
        result.fail(f"日报总资产与快照不一致: 期望包含 {expected}")


def _dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去重交易流水。

    持仓状态和 trade_log.json 都会记录成交，同一笔交易不能在巡检指标里重复计数。
    """
    seen = set()
    deduped: list[dict[str, Any]] = []
    for trade in trades:
        key = (
            trade.get("date"),
            trade.get("time"),
            trade.get("code"),
            trade.get("action") or trade.get("direction"),
            trade.get("shares"),
            trade.get("price"),
            trade.get("actual_price"),
            trade.get("amount"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trade)
    return deduped


def run_healthcheck(
    root_dir: Path,
    max_snapshot_age_minutes: int = 1440,
    strict_snapshot: bool = False,
    strict_events: bool = False,
    strict_report: bool = False,
) -> HealthcheckResult:
    """运行虚拟盘健康检查。"""
    result = HealthcheckResult()
    data_dir = root_dir / "data"
    state = _load_json(data_dir / "portfolio_state.json", result, required=True)
    trade_log = _load_jsonl(data_dir / "trade_log.json", result, required=False)
    events = _load_jsonl(data_dir / "trade_events.jsonl", result, required=strict_events)
    snapshots = _load_jsonl(data_dir / "portfolio_snapshots.jsonl", result, required=strict_snapshot)
    # 回退: 从 trade_events.jsonl 读取盘中快照
    if not snapshots:
        events_for_snapshot = _load_jsonl(data_dir / "trade_events.jsonl", result, required=False)
        snapshots = [
            {"timestamp": e.get("timestamp", ""), "summary": e.get("payload", {})}
            for e in events_for_snapshot
            if e.get("event_type") == "portfolio_snapshot"
        ]

    if state:
        _check_state(state, result)
        state_trades = state.get("trades", [])
        if isinstance(state_trades, list):
            trade_log.extend([row for row in state_trades if isinstance(row, dict)])
        else:
            result.warn("状态文件 trades 字段不是列表")

    deduped_trades = _dedupe_trades(trade_log)
    _check_trades(deduped_trades, result)
    _check_snapshots(snapshots, result, max_snapshot_age_minutes, strict_snapshot)
    _check_events(events, result, strict_events, len(deduped_trades))
    _check_report_consistency(root_dir, snapshots, result, strict_report)
    result.metrics["event_count"] = len(events)
    result.metrics["snapshot_count"] = len(snapshots)
    return result


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="检查 A 股虚拟盘运行状态")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="项目根目录")
    parser.add_argument("--max-snapshot-age-minutes", type=int, default=1440, help="最近快照允许的最大分钟数")
    parser.add_argument("--strict-snapshot", action="store_true", help="快照缺失时直接失败")
    parser.add_argument("--strict-events", action="store_true", help="事件流水缺失或不可追溯时直接失败")
    parser.add_argument("--strict-report", action="store_true", help="最新日报缺失或与快照不一致时直接失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    result = run_healthcheck(
        Path(args.root),
        max_snapshot_age_minutes=args.max_snapshot_age_minutes,
        strict_snapshot=args.strict_snapshot,
        strict_events=args.strict_events,
        strict_report=args.strict_report,
    )

    payload = {
        "ok": result.ok,
        "failures": result.failures,
        "warnings": result.warnings,
        "metrics": result.metrics,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        if result.ok:
            LOGGER.info("虚拟盘健康检查通过: %s", result.metrics)
        else:
            LOGGER.error("虚拟盘健康检查失败: %s", result.failures)
        for warning in result.warnings:
            LOGGER.warning(warning)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""运行观察期脚本测试。"""

import json
from pathlib import Path

from scripts.monthly_review import build_review
from scripts.paper_acceptance import run_acceptance
from scripts.paper_daemon import DaemonConfig, build_live_command, decide_next_action
from scripts.paper_healthcheck import run_healthcheck
from scripts.paper_reset import reset_paper_state
from scripts.paper_service import ServiceConfig, get_status, start_service, stop_service
from scripts.paper_smoke_run import run_smoke
from scripts.paper_status import build_status
from datetime import datetime, timedelta, time as dt_time


def _write_jsonl(path, rows) -> None:
    """写入测试用 JSONL。"""
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _trace_events(code: str, action: str, date: str, price: float) -> list[dict[str, object]]:
    """构造可追溯的信号、风控、成交事件。"""
    order = {"code": code, "action": action, "date": date, "shares": 100, "price": price}
    return [
        {"event_type": "signal", "timestamp": "2026-05-28 10:00:00", "payload": order},
        {
            "event_type": "risk_approved",
            "timestamp": "2026-05-28 10:00:01",
            "payload": {"order": order, "approved": True, "reason": "通过"},
        },
        {
            "event_type": "execution",
            "timestamp": "2026-05-28 10:00:02",
            "payload": {
                **order,
                "status": "filled",
                "actual_price": price,
                "amount": round(price * 100, 2),
            },
        },
    ]


def test_paper_healthcheck_passes_valid_state(tmp_path) -> None:
    """健康检查应接受合法虚拟盘状态。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 9000.0,
        "positions": {
            "sh601988": {
                "name": "中国银行",
                "shares": 100,
                "avg_cost": 5.0,
                "current_price": 5.2,
                "buy_date": "20260527",
                "strategy": "测试策略",
            }
        },
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "trade_log.json", [{
        "date": "20260527",
        "action": "buy",
        "code": "sh601988",
        "shares": 100,
        "price": 5.0,
    }])
    _write_jsonl(data_dir / "portfolio_snapshots.jsonl", [{
        "date": "20260528",
        "timestamp": "2026-05-28 15:00:00",
        "summary": {"cash": 9000.0, "total_value": 9520.0},
    }])

    result = run_healthcheck(tmp_path, max_snapshot_age_minutes=10_000_000, strict_snapshot=True)

    assert result.ok is True
    assert result.metrics["position_count"] == 1
    assert result.metrics["trade_count"] == 1


def test_paper_healthcheck_fails_negative_cash(tmp_path) -> None:
    """健康检查应拒绝负现金。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": -1.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")

    result = run_healthcheck(tmp_path)

    assert result.ok is False
    assert any("现金为负数" in item for item in result.failures)


def test_paper_healthcheck_rejects_invalid_position_scope(tmp_path) -> None:
    """健康检查应拒绝非 A 股持仓和非整手持仓。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {
            "hk00700": {"shares": 50, "avg_cost": 300.0, "current_price": 300.0}
        },
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")

    result = run_healthcheck(tmp_path)

    assert result.ok is False
    assert any("不是沪深 A 股股票" in item for item in result.failures)
    assert any("不是整手" in item for item in result.failures)


def test_paper_healthcheck_fails_total_position_limit(tmp_path) -> None:
    """健康检查应拒绝总仓位超限状态。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 500.0,
        "positions": {
            "sh601988": {
                "shares": 2000,
                "avg_cost": 5.0,
                "current_price": 5.0,
            }
        },
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")

    result = run_healthcheck(tmp_path)

    assert result.ok is False
    assert any("总仓位超限" in item for item in result.failures)


def test_paper_healthcheck_requires_trace_events_when_strict(tmp_path) -> None:
    """严格事件模式应要求结构化事件流水存在。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")

    result = run_healthcheck(tmp_path, strict_events=True)

    assert result.ok is False
    assert any("trade_events.jsonl" in item or "结构化事件流水为空" in item for item in result.failures)


def test_paper_healthcheck_requires_execution_trace(tmp_path) -> None:
    """严格事件模式应验证成交事件能追溯到信号和风控通过。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "trade_events.jsonl", [{
        "event_type": "execution",
        "timestamp": "2026-05-28 10:00:00",
        "payload": {
            "status": "filled",
            "code": "sh601988",
            "action": "buy",
            "date": "20260528",
            "shares": 100,
            "actual_price": 5.0,
        },
    }])

    result = run_healthcheck(tmp_path, strict_events=True)

    assert result.ok is False
    assert any("缺少对应信号记录" in item for item in result.failures)
    assert any("缺少对应风控通过记录" in item for item in result.failures)


def test_paper_healthcheck_strict_report_matches_snapshot(tmp_path) -> None:
    """严格日报模式应验证最新日报和账户快照总资产一致。"""
    data_dir = tmp_path / "data"
    report_dir = tmp_path / "reports"
    data_dir.mkdir()
    report_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "portfolio_snapshots.jsonl", [{
        "date": "20260528",
        "timestamp": "2026-05-28 15:00:00",
        "summary": {"cash": 10000.0, "total_value": 10000.0},
    }])
    (report_dir / "daily_20260528.txt").write_text("当前总值: ¥10,000.00\n", encoding="utf-8")

    result = run_healthcheck(
        tmp_path,
        max_snapshot_age_minutes=10_000_000,
        strict_snapshot=True,
        strict_report=True,
    )

    assert result.ok is True


def test_monthly_review_uses_snapshots_and_trades(tmp_path) -> None:
    """月度复盘应基于快照和卖出盈亏计算指标。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 11000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "portfolio_snapshots.jsonl", [
        {"date": "20260501", "summary": {"total_value": 10000.0}},
        {"date": "20260515", "summary": {"total_value": 9600.0}},
        {"date": "20260528", "summary": {"total_value": 11000.0}},
    ])
    _write_jsonl(data_dir / "trade_log.json", [
        {"date": "20260502", "action": "buy", "code": "sh601988", "shares": 100, "price": 5.0},
        {"date": "20260520", "action": "sell", "code": "sh601988", "shares": 100, "price": 5.5, "profit": 45.0},
    ])

    summary = build_review(tmp_path, days=60)

    assert summary.initial_value == 10000.0
    assert summary.final_value == 11000.0
    assert summary.total_return == 0.1
    assert summary.max_drawdown == 0.04
    assert summary.trade_count == 2
    assert summary.win_rate == 1.0


def test_paper_smoke_run_completes_end_to_end(tmp_path) -> None:
    """烟测应完整生成交易、事件、快照，并通过健康检查。"""
    result = run_smoke(tmp_path)

    assert result.ok is True
    assert result.buy_report["status"] == "filled"
    assert result.sell_report["status"] == "filled"
    assert result.review["trade_count"] == 2
    assert (tmp_path / "data" / "trade_events.jsonl").exists()
    assert (tmp_path / "data" / "portfolio_snapshots.jsonl").exists()


def test_paper_daemon_decides_session_states(tmp_path) -> None:
    """守护脚本应根据交易日时间窗做出正确调度决策。"""
    pre_market = dt_time(9, 0)
    market_close = dt_time(15, 0)

    before = decide_next_action(datetime(2026, 5, 28, 8, 30), True, pre_market, market_close, 300)
    during = decide_next_action(datetime(2026, 5, 28, 10, 0), True, pre_market, market_close, 300)
    after = decide_next_action(datetime(2026, 5, 28, 15, 1), True, pre_market, market_close, 300)
    closed = decide_next_action(datetime(2026, 5, 30, 10, 0), False, pre_market, market_close, 300)

    assert before.state == "before_session"
    assert before.should_run is False
    assert during.state == "in_session"
    assert during.should_run is True
    assert after.state == "after_session"
    assert closed.state == "non_trading_day"


def test_paper_daemon_builds_live_command(tmp_path) -> None:
    """守护脚本应构建虚拟盘 live_runner 命令。"""
    config = DaemonConfig(
        root_dir=tmp_path,
        watch_interval=7,
        scan_interval=900,
        top_n=5,
        ignore_calendar=True,
    )

    command = build_live_command(config)

    assert str(tmp_path / "live_runner.py") in command
    assert "--broker" in command
    assert "paper" in command
    assert "--watch-interval" in command
    assert "7" in command
    assert "--scan-interval" in command
    assert "900" in command
    assert "--ignore-calendar" in command


def test_paper_service_status_without_pid(tmp_path) -> None:
    """后台服务无 PID 文件时应返回未运行。"""
    status = get_status(tmp_path)

    assert status.running is False
    assert status.pid is None
    assert status.stale_pid_file is False


def test_paper_service_start_writes_pid_metadata(tmp_path) -> None:
    """后台服务启动应写入 PID 元数据和启动命令。"""

    class FakeProcess:
        """模拟 Popen 返回对象。"""

        pid = 12345

    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    config = ServiceConfig(
        root_dir=tmp_path,
        watch_interval=6,
        scan_interval=900,
        top_n=8,
        ignore_calendar=True,
    )

    result = start_service(config, popen_factory=fake_popen, pid_checker=lambda _pid: False)
    status = get_status(tmp_path, pid_checker=lambda pid: pid == 12345)

    assert result.ok is True
    assert status.running is True
    assert status.pid == 12345
    assert "--watch-interval" in status.command
    assert "6" in status.command
    assert "--ignore-calendar" in status.command
    assert calls


def test_paper_service_start_refuses_duplicate_running_process(tmp_path) -> None:
    """已有运行中 PID 时不应重复启动。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "paper_daemon.pid").write_text(json.dumps({"pid": 12345}), encoding="utf-8")

    result = start_service(
        ServiceConfig(root_dir=tmp_path),
        popen_factory=lambda *args, **kwargs: None,
        pid_checker=lambda pid: pid == 12345,
    )

    assert result.ok is False
    assert "已在运行" in result.message


def test_paper_service_stop_cleans_stale_pid(tmp_path) -> None:
    """停止服务应清理过期 PID 文件。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pid_file = data_dir / "paper_daemon.pid"
    pid_file.write_text(json.dumps({"pid": 12345}), encoding="utf-8")

    result = stop_service(tmp_path, pid_checker=lambda _pid: False)

    assert result.ok is True
    assert "过期 PID" in result.message
    assert pid_file.exists() is False


def test_paper_status_combines_service_health_review_and_logs(tmp_path) -> None:
    """统一状态脚本应汇总服务、健康检查、复盘、验收和日志。"""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    (logs_dir / "live_today.log").write_text("line1\nline2\n", encoding="utf-8")

    status = build_status(tmp_path, log_lines=1)

    assert status.root_dir == str(tmp_path.resolve())
    assert status.service["running"] is False
    assert status.health["ok"] is True
    assert status.review["final_value"] == 10000.0
    assert status.acceptance["ready_for_qmt_dry_run"] is False
    assert status.logs["live_today"] == ["line2"]
    assert status.errors == []


def test_paper_acceptance_passes_after_enough_snapshots(tmp_path) -> None:
    """观察期验收应在快照、日志、回撤均满足时通过。"""
    data_dir = tmp_path / "data"
    report_dir = tmp_path / "reports"
    data_dir.mkdir()
    report_dir.mkdir()
    today = datetime.now()
    snapshot_rows = []
    for i in range(20):
        day = (today - timedelta(days=19 - i)).strftime("%Y%m%d")
        snapshot_rows.append({
            "date": day,
            "timestamp": f"{day[:4]}-{day[4:6]}-{day[6:]} 15:00:00",
            "summary": {"cash": 10000.0 + i * 10, "total_value": 10000.0 + i * 10},
        })
    latest = snapshot_rows[-1]
    latest_date = latest["date"]
    latest_total = latest["summary"]["total_value"]

    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": latest_total,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "portfolio_snapshots.jsonl", snapshot_rows)
    _write_jsonl(data_dir / "trade_log.json", [
        {"date": snapshot_rows[0]["date"], "action": "buy", "code": "sh601988", "shares": 100, "price": 5.0},
        {
            "date": latest_date,
            "action": "sell",
            "code": "sh601988",
            "shares": 100,
            "price": 5.5,
            "profit": 45.0,
        },
    ])
    _write_jsonl(
        data_dir / "trade_events.jsonl",
        _trace_events("sh601988", "buy", snapshot_rows[0]["date"], 5.0)
        + _trace_events("sh601988", "sell", latest_date, 5.5),
    )
    (report_dir / f"daily_{latest_date}.txt").write_text(
        f"当前总值: ¥{latest_total:,.2f}\n",
        encoding="utf-8",
    )

    result = run_acceptance(tmp_path, days=30, min_snapshot_days=20)

    assert result.ready_for_qmt_dry_run is True
    assert result.snapshot_days == 20
    assert result.failures == []


def test_paper_acceptance_fails_without_enough_snapshot_days(tmp_path) -> None:
    """观察期验收应拒绝快照天数不足的样本。"""
    data_dir = tmp_path / "data"
    report_dir = tmp_path / "reports"
    data_dir.mkdir()
    report_dir.mkdir()
    today = datetime.now().strftime("%Y%m%d")
    (data_dir / "portfolio_state.json").write_text(json.dumps({
        "cash": 10000.0,
        "positions": {},
        "trades": [],
    }, ensure_ascii=False), encoding="utf-8")
    _write_jsonl(data_dir / "portfolio_snapshots.jsonl", [{
        "date": today,
        "timestamp": datetime.now().strftime("%Y-%m-%d 15:00:00"),
        "summary": {"cash": 10000.0, "total_value": 10000.0},
    }])
    _write_jsonl(data_dir / "trade_events.jsonl", [{
        "event_type": "heartbeat",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "payload": {"ok": True},
    }])
    (report_dir / f"daily_{today}.txt").write_text("当前总值: ¥10,000.00\n", encoding="utf-8")

    result = run_acceptance(tmp_path, days=30, min_snapshot_days=20)

    assert result.ready_for_qmt_dry_run is False
    assert any("快照天数不足" in item for item in result.failures)


def test_paper_reset_preview_does_not_modify_files(tmp_path) -> None:
    """虚拟盘初始化默认只预览，不应修改旧状态。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_path = data_dir / "portfolio_state.json"
    state_path.write_text(json.dumps({"cash": 1.0, "positions": {}}, ensure_ascii=False), encoding="utf-8")

    result = reset_paper_state(tmp_path, cash=10000.0, confirm=False)

    assert result.changed is False
    assert "data/portfolio_state.json" in result.backed_up_files
    assert json.loads(state_path.read_text(encoding="utf-8"))["cash"] == 1.0


def test_paper_reset_confirm_backs_up_and_initializes(tmp_path) -> None:
    """确认初始化应备份旧文件并写入新账户状态。"""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    (data_dir / "portfolio_state.json").write_text(
        json.dumps({"cash": 1.0, "positions": {"sh601988": {"shares": 100, "avg_cost": 5.0}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "trade_log.json").write_text("old\n", encoding="utf-8")
    (logs_dir / "live.log").write_text("old log\n", encoding="utf-8")

    result = reset_paper_state(tmp_path, cash=12345.0, confirm=True)
    state = json.loads((data_dir / "portfolio_state.json").read_text(encoding="utf-8"))

    assert result.changed is True
    assert result.backup_dir is not None
    assert Path(result.backup_dir).exists()
    assert state["cash"] == 12345.0
    assert state["positions"] == {}
    assert (data_dir / "trade_log.json").read_text(encoding="utf-8") == ""
    assert (logs_dir / "live.log").read_text(encoding="utf-8") == ""
    assert (data_dir / "backups").exists()

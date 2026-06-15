"""虚拟 Broker 与风控集成测试。"""

import pytest

from config.settings import DRAWDOWN_RECOVERY_DAYS, EXIT_LIMIT_DOWN_COOLDOWN_SECONDS
from live_runner import SharedState, _handle_position_exits, _submit_exit_order
from risk.control import RiskController

from trading.brokers import PaperBrokerAdapter, QmtBrokerAdapter
from trading.models import OrderIntent
from trading.observability import EventRecorder


def _paper_broker(tmp_path) -> PaperBrokerAdapter:
    """创建使用临时文件的虚拟 Broker。"""
    broker = PaperBrokerAdapter(
        state_file=str(tmp_path / "state.json"),
        trade_log_file=str(tmp_path / "trade_log.json"),
        snapshot_log_file=str(tmp_path / "snapshots.jsonl"),
    )
    broker.connect()
    return broker


def test_risk_rejects_non_whitelist_order(tmp_path) -> None:
    """非白名单标的应被风控拒绝。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    order = OrderIntent(
        code="sh600000",
        action="buy",
        price=10.0,
        shares=100,
        strategy="普通策略",
        date="20260528",
    )

    approved, rejected = risk.filter_order_intents(
        [order],
        broker.portfolio,
        {"sh600000": {"current_price": 10.0, "prev_close": 9.9, "is_st": False, "is_suspended": False}},
    )

    assert approved == []
    assert len(rejected) == 1
    assert "不在白名单" in rejected[0].reason


def test_full_market_order_can_pass_risk_and_fill(tmp_path) -> None:
    """全市场扫描订单可绕过固定白名单并在虚拟盘成交。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    order = OrderIntent(
        code="600000",
        action="buy",
        price=10.0,
        shares=100,
        name="浦发银行",
        strategy="全市场扫描+组合策略",
        reason="固定样本信号",
        date="20260528",
    )
    market_data = {
        "600000": {"current_price": 10.0, "prev_close": 9.9, "is_st": False, "is_suspended": False}
    }

    approved, rejected = risk.filter_order_intents([order], broker.portfolio, market_data)
    report = broker.place_order(approved[0])

    assert rejected == []
    assert len(approved) == 1
    assert report.is_success is True
    assert broker.query_positions()["600000"]["shares"] == 100


def test_risk_allows_non_whitelist_position_sell(tmp_path) -> None:
    """非白名单持仓卖出不应被买入白名单拦截。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    buy_report = broker.place_order(
        OrderIntent(
            code="603773",
            action="buy",
            price=100.0,
            shares=100,
            name="沃格光电",
            strategy="全市场扫描+组合策略",
            date="20260528",
        )
    )
    sell_order = OrderIntent(
        code="603773",
        action="sell",
        price=101.0,
        shares=100,
        name="沃格光电",
        strategy="止损",
        date="20260529",
    )

    approved, rejected = risk.filter_order_intents(
        [sell_order],
        broker.portfolio,
        {
            "603773": {
                "current_price": 101.0,
                "prev_close": 100.0,
                "is_st": False,
                "is_suspended": False,
            }
        },
    )

    assert buy_report.is_success is True
    assert rejected == []
    assert len(approved) == 1
    assert approved[0].code == "603773"
    assert approved[0].action == "sell"


def test_same_day_exit_rejection_enters_cooldown(tmp_path) -> None:
    """同日卖出因 T+1 被拒后，不应在盯盘循环里重复刷风控日志。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    shared = SharedState()
    recorder = EventRecorder(path=str(tmp_path / "events.jsonl"))
    buy_report = broker.place_order(
        OrderIntent(
            code="603773",
            action="buy",
            price=100.0,
            shares=100,
            name="沃格光电",
            strategy="全市场扫描+组合策略",
            date="20260529",
        )
    )
    positions = broker.query_positions()
    prices = {"603773": 90.0}
    market_data = {
        "603773": {
            "current_price": 90.0,
            "prev_close": 100.0,
            "is_st": False,
            "is_suspended": False,
        }
    }

    _handle_position_exits(
        broker, positions, prices, object(), risk, market_data, recorder, shared=shared
    )
    first_event_count = len(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )
    _handle_position_exits(
        broker, positions, prices, object(), risk, market_data, recorder, shared=shared
    )
    second_event_count = len(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )

    assert buy_report.is_success is True
    assert shared.is_exit_cooling_down("603773") is True
    assert first_event_count == 2
    assert second_event_count == first_event_count


def test_paper_broker_buy_and_next_day_sell(tmp_path) -> None:
    """虚拟 Broker 应能完成买入和次日卖出，并保留订单回报。"""
    broker = _paper_broker(tmp_path)
    buy_order = OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260527",
    )
    sell_order = OrderIntent(
        code="sh601988",
        action="sell",
        price=5.2,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260528",
    )

    buy_report = broker.place_order(buy_order)
    sell_report = broker.place_order(sell_order)

    assert buy_report.status == "filled"
    assert sell_report.status == "filled"
    assert sell_report.profit is not None
    assert broker.query_positions() == {}
    assert len(broker.query_orders()) == 2


def test_risk_adjusts_sell_shares_to_position_size(tmp_path) -> None:
    """风控应把超出持仓的卖出数量下调到可卖数量。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    broker.place_order(OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        name="中国银行",
        strategy="测试策略",
        date="20260527",
    ))
    sell_order = OrderIntent(
        code="sh601988",
        action="sell",
        price=5.2,
        shares=300,
        name="中国银行",
        strategy="测试策略",
        date="20260528",
    )

    approved, rejected = risk.filter_order_intents(
        [sell_order],
        broker.portfolio,
        {"sh601988": {"current_price": 5.2, "prev_close": 5.1, "is_st": False, "is_suspended": False}},
    )

    assert rejected == []
    assert len(approved) == 1
    assert approved[0].shares == 100


def test_qmt_adapter_defaults_to_dry_run_and_blocks_live() -> None:
    """QMT 适配器默认只做 dry-run，真实通道必须被显式阻断。"""
    dry_run = QmtBrokerAdapter(live_enabled=False)
    dry_run.connect()
    report = dry_run.place_order(OrderIntent(
        code="sh601988",
        action="buy",
        price=5.0,
        shares=100,
        strategy="QMT联调",
        date="20260528",
    ))

    assert report.status == "submitted"
    assert "dry-run" in report.message

    live = QmtBrokerAdapter(live_enabled=True)
    with pytest.raises(NotImplementedError):
        live.connect()


def test_min_lot_exceeding_single_position_limit_is_rejected(tmp_path) -> None:
    """高价股一手金额超过单票上限时不得放宽买入。"""
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    order = OrderIntent(
        code="600000",
        action="buy",
        price=100.0,
        shares=100,
        name="高价样本",
        strategy="全市场扫描+组合策略",
        strategy_tag="combo_trend",
        date="20260528",
    )
    approved, rejected = risk.filter_order_intents(
        [order],
        broker.portfolio,
        {"600000": {"current_price": 100, "prev_close": 99, "is_st": False, "is_suspended": False}},
    )
    assert approved == []
    assert "MIN_LOT_EXCEEDS_POSITION_LIMIT" in rejected[0].reason


def test_stock_with_etf_strategy_tag_keeps_stock_position_limit(tmp_path) -> None:
    """股票即使被标 ETF 轮动标签,单票上限仍按股票 15% 判定,不得放宽到 30%。

    回归:旧 check_capital_allocation 用 `is_etf(code) or strategy_tag in
    {etf_rotation, rps_rotation}` 判定上限,若股票误带 rps_rotation 标签会
    被按 ETF 30% 放行,与 PositionManager 现价口径不一致。
    """
    broker = _paper_broker(tmp_path)
    risk = RiskController()
    # 高价股一手 10000,总资产 50000;股票上限 15%=7500,一手即超限应被拒。
    # 若误按 ETF 30%=15000 放行,则一手可通过——正是要拦截的情形。
    order = OrderIntent(
        code="600000",
        action="buy",
        price=100.0,
        shares=100,
        name="误带标签的股票",
        strategy="全市场扫描+组合策略",
        strategy_tag="rps_rotation",
        date="20260528",
    )
    approved, rejected = risk.filter_order_intents(
        [order],
        broker.portfolio,
        {"600000": {"current_price": 100, "prev_close": 99, "is_st": False, "is_suspended": False}},
    )
    assert approved == []
    assert "MIN_LOT_EXCEEDS_POSITION_LIMIT" in rejected[0].reason


def test_drawdown_circuit_requires_two_recovery_days() -> None:
    """最大回撤熔断应锁存，并在连续两个企稳交易日后解锁。

    恢复判据:自触发熔断起,连续 ``DRAWDOWN_RECOVERY_DAYS`` 个交易日总市值
    **不再创新低**(即下跌已止住)。恢复时重置 peak_value 为当前市值,使回撤
    归零、check_max_drawdown 不再立即重新触发。
    """

    class FakePortfolio:
        """模拟 PositionManager 的回撤/市值行为,基于 state["peak_value"]。"""

        def __init__(self, total: float, peak: float) -> None:
            self._total = total
            self.state = {"peak_value": peak}

        def get_total_value(self, prices=None) -> float:
            return self._total

        def get_drawdown(self, value=None) -> float:
            peak = self.state.get("peak_value", self._total)
            cur = value if value is not None else self._total
            return max((peak - cur) / peak, 0.0) if peak > 0 else 0.0

    risk = RiskController()
    # 峰值 53763、当前 50000 → 回撤 7%,触发熔断,基准低点=50000。
    pf = FakePortfolio(50_000.0, 53_763.0)
    assert risk.check_max_drawdown(pf)[0] is True
    assert risk._drawdown_latch_value == 50_000.0

    # 企稳:市值略高于基准低点(未创新低),连续两个交易日。
    pf_stable = FakePortfolio(50_100.0, 53_763.0)
    risk.set_daily_start(pf_stable, date="20260611")
    assert risk.check_max_drawdown(pf_stable)[0] is True
    risk.set_daily_start(pf_stable, date="20260612")
    # 恢复时 peak_value 被重置为当前市值 → 回撤归零 → 不再 exceeded。
    assert risk.check_max_drawdown(pf_stable)[0] is False


def test_drawdown_circuit_recovers_after_extended_low_period() -> None:
    """回撤熔断在长期空仓盘整后必须能解除,不得死锁。

    回归回测 baseline 中动量 Combo 尾部空仓根因:净值峰值约 59658、回撤到
    55454 后空仓,峰值只增不减,回撤长期 >= 6%。旧恢复判据要求"回撤回落到
    阈值下方",但空仓后净值走平、峰值不降,回撤永远 >= 阈值,熔断永远无法
    解除 → 策略永久空仓。

    新判据:从熔断起连续 N 日总市值**不再创新低**即视为企稳、解除熔断,
    并把 peak_value 重置为当前市值(回撤归零,避免立即重新触发)。"不再创新低"
    只要求下跌停止——空仓走平即满足,避免"要恢复得先开仓、要开仓得先恢复"
    的自我参照死锁。
    """

    class FakePortfolio:
        def __init__(self, total: float, peak: float) -> None:
            self._total = total
            self.state = {"peak_value": peak}

        def get_total_value(self, prices=None) -> float:
            return self._total

        def get_drawdown(self, value=None) -> float:
            peak = self.state.get("peak_value", self._total)
            cur = value if value is not None else self._total
            return max((peak - cur) / peak, 0.0) if peak > 0 else 0.0

    risk = RiskController()
    # 模拟回测 0312 状态:峰值 59658、当前 55454 → 回撤 ~7%,触发熔断(基准低点=55454)。
    pf_trigger = FakePortfolio(55_454.0, 59_658.0)
    assert risk.check_max_drawdown(pf_trigger)[0] is True

    # 空仓期间净值走平:市值等于基准低点(未创新低),连续 N 日即恢复。
    pf_flat = FakePortfolio(55_454.0, 59_658.0)
    days_needed = DRAWDOWN_RECOVERY_DAYS  # 默认 2
    for i in range(days_needed - 1):
        risk.set_daily_start(pf_flat, date=f"2026061{i}")
    # 还差一日才到恢复阈值,熔断仍激活。
    assert risk.check_max_drawdown(pf_flat)[0] is True
    risk.set_daily_start(pf_flat, date="20260613")
    # 达到连续 N 日企稳,熔断解除(peak_value 已重置 → 回撤归零)。
    assert risk.check_max_drawdown(pf_flat)[0] is False

    # 反向验证:若熔断后继续创新低(市值跌破基准低点),不得恢复。
    risk2 = RiskController()
    risk2.check_max_drawdown(FakePortfolio(55_454.0, 59_658.0))  # 基准低点=55454
    pf_worse = FakePortfolio(54_000.0, 59_658.0)  # 54000 < 55454,创新低
    risk2.set_daily_start(pf_worse, date="20260611")  # 重置计数,基准更新为 54000
    risk2.set_daily_start(pf_worse, date="20260612")
    assert risk2.check_max_drawdown(pf_worse)[0] is True


def test_check_max_drawdown_does_not_reset_recovery_on_latched() -> None:
    """filter_orders 逐单调用 check_max_drawdown 时,不得清零恢复计数。

    回归:旧 check_max_drawdown 每次 drawdown>=阈值 都执行 latch=True + 清零,
    而 filter_orders 对每笔订单都调用它;即使 set_daily_start 推进了恢复计数,
    紧接着的 filter_orders 又会清零,导致熔断永远无法解除(回测尾部空仓)。
    修复后:set_daily_start 推进计数后,即使 check_max_drawdown 仍读到
    drawdown>=阈值 也不得清零(latch 已为真,走不进触发分支)。
    """

    class FakePortfolio:
        def __init__(self, total: float, peak: float) -> None:
            self._total = total
            self.state = {"peak_value": peak}

        def get_total_value(self, prices=None) -> float:
            return self._total

        def get_drawdown(self, value=None) -> float:
            peak = self.state.get("peak_value", self._total)
            cur = value if value is not None else self._total
            return max((peak - cur) / peak, 0.0) if peak > 0 else 0.0

    risk = RiskController()
    # 触发熔断(熔断点=50000)并推进一日恢复计数(市值企稳略高于熔断点)。
    risk.check_max_drawdown(FakePortfolio(50_000.0, 53_763.0))
    risk.set_daily_start(FakePortfolio(50_100.0, 53_763.0), date="20260611")
    assert risk._drawdown_recovery_days == 1

    # 模拟 filter_orders 内部对多笔订单反复调用 check_max_drawdown,
    # 即便回撤更高,恢复计数也必须保留,不被清零。
    for _ in range(5):
        risk.check_max_drawdown(FakePortfolio(49_500.0, 53_763.0))
    assert risk._drawdown_recovery_days == 1, "check_max_drawdown 不得清零已推进的恢复计数"


def test_position_limit_uses_current_price_not_cost(tmp_path) -> None:
    """单票仓位限制应按现价计算:上涨后实际市值超限时,加仓必须被拒。

    回归 A3:旧实现用成本价 avg_cost*shares 估算持仓市值,标的上涨后实际市值
    已超单票上限,却因成本口径偏低被误判未超、放行加仓,导致单票集中度失控。
    """
    from rules.position import PositionManager

    pm = PositionManager(
        state_file=str(tmp_path / "state.json"),
        trade_log_file=str(tmp_path / "trade_log.json"),
        snapshot_log_file=str(tmp_path / "snapshots.jsonl"),
    )
    # 成本价 5、现价 13、持有 600 股:现价市值 7800,成本市值仅 3000。
    pm.state["positions"]["600000"] = {
        "name": "测试", "shares": 600, "total_qty": 600, "sellable_qty": 600,
        "avg_cost": 5.0, "current_price": 13.0,
        "buy_date": "20260101", "strategy_tag": "combo_trend",
    }
    total_value = 54795.0  # cash + 现价市值
    # 再加一手约 1300 元:按现价 (7800+1300)/54795=16.6% 超 15% 上限,应拒绝;
    # 旧成本口径 (3000+1300)/54795=7.8% 会错误放行。
    within, _ = pm.check_position_limit("600000", 1300, total_value=total_value)
    assert within is False


def test_drawdown_deleverage_target_ratio() -> None:
    """回撤熔断激活且仓位超 30% 时返回应削减比例;否则 0(A4)。"""
    class _FakePortfolio:
        def __init__(self, drawdown, cash, total):
            self._dd, self._cash, self._total = drawdown, cash, total
        def get_total_value(self, prices=None):
            return self._total
        def get_drawdown(self, value=None):
            return self._dd
        def get_cash(self):
            return self._cash

    # 回撤 7% 触发 + 仓位 (50000-20000)/50000=60% → 削减 (0.60-0.30)/0.60 = 0.5
    assert RiskController().drawdown_deleverage_target_ratio(
        _FakePortfolio(0.07, 20000.0, 50000.0)) == 0.5
    # 回撤未触发(2%)→ 0
    assert RiskController().drawdown_deleverage_target_ratio(
        _FakePortfolio(0.02, 20000.0, 50000.0)) == 0.0
    # 回撤触发但仓位已低于 30%(20%)→ 0
    assert RiskController().drawdown_deleverage_target_ratio(
        _FakePortfolio(0.07, 40000.0, 50000.0)) == 0.0


def test_drawdown_deleverage_reduces_oversized_position(tmp_path) -> None:
    """A4: 回撤熔断激活且仓位超限时,对存量同比例减仓(执行层集成测试)。"""
    from live_runner import _handle_drawdown_deleverage

    broker = _paper_broker(tmp_path)
    # 直接构造高仓位 + 大回撤状态:成本100、现价80、300股;现金6000;峰值50000
    # → 总值 6000+300*80=30000,回撤 (50000-30000)/50000=40%>6%,仓位 24000/30000=80%>30%
    broker.portfolio.state["positions"]["600000"] = {
        "name": "测试", "shares": 300, "total_qty": 300, "sellable_qty": 300,
        "avg_cost": 100.0, "current_price": 80.0, "buy_date": "20260101",
        "strategy_tag": "combo_trend", "buy_lots": [{"date": "20260101", "qty": 300}],
    }
    broker.portfolio.state["cash"] = 6000.0
    broker.portfolio.state["peak_value"] = 50000.0
    risk = RiskController()
    recorder = EventRecorder(path=str(tmp_path / "events.jsonl"))
    prices = {"600000": 80.0}
    market_data = {"600000": {"current_price": 80.0, "prev_close": 81.0,
                              "is_st": False, "is_suspended": False}}

    _handle_drawdown_deleverage(broker, risk, prices, market_data, recorder, shared=SharedState())

    remaining = broker.query_positions().get("600000", {}).get("shares", 0)
    assert remaining < 300  # 已对存量减仓


def test_limit_down_exit_cooldown_is_short_not_until_close(tmp_path) -> None:
    """跌停拒单后冷却应是短周期(EXIT_LIMIT_DOWN_COOLDOWN_SECONDS),而非锁到收盘。

    回归真实事故:第一天买入 T+1 锁定,第二天跌停无法卖出,但下午跌停打开。
    旧实现 _submit_exit_order 对跌停拒单调 set_exit_cooldown_until_close(锁到 15:00),
    导致上午检测到跌停后下午开口子时仍被冷却跳过、错过止损 → 亏损放大。

    修复后:跌停冷却用 set_exit_cooldown(seconds=EXIT_LIMIT_DOWN_COOLDOWN_SECONDS),
    冷却结束后下一轮盯盘会重新检测跌停是否已打开。
    """
    import time as _time
    from datetime import timedelta
    from config.time_utils import now_local

    broker = _paper_broker(tmp_path)
    risk = RiskController()
    shared = SharedState()
    recorder = EventRecorder(path=str(tmp_path / "events.jsonl"))
    # 建立持仓(T+1 已解锁,可在次日卖出)。
    broker.place_order(OrderIntent(
        code="603773", action="buy", price=100.0, shares=100,
        name="沃格光电", strategy="测试", date="20260527",
    ))

    # 构造跌停:现价 = 昨收 * 0.9,触发 check_price_limit 返回 limit_type="跌停"。
    sell_order = OrderIntent(
        code="603773", action="sell", price=90.0, shares=100,
        name="沃格光电", strategy="止损", date="20260528",
        reason="CATASTROPHIC_STOP_LOSS",
    )
    market_data = {"603773": {"current_price": 90.0, "prev_close": 100.0,
                              "is_st": False, "is_suspended": False}}

    report = _submit_exit_order(sell_order, broker, risk, market_data, recorder, shared)

    # 跌停 → 卖单被风控拒绝,report 为 None,进入冷却。
    assert report is None, "跌停时应被风控拒绝,不应成交"
    assert shared.is_exit_cooling_down("603773") is True

    # 关键断言:冷却截止时间应在 ~现在+EXIT_LIMIT_DOWN_COOLDOWN_SECONDS 附近,
    # 而不是当天 15:00 收盘。读取 rejected_exits 的冷却截止时间戳校验。
    cooldown_deadline = shared.rejected_exits.get("603773", 0)
    now_ts = _time.time()
    # 冷却时长应在 [EXIT_LIMIT_DOWN_COOLDOWN_SECONDS - 10, EXIT_LIMIT_DOWN_COOLDOWN_SECONDS + 10] 内
    # (留 10s 容差应对测试执行耗时)。
    cooldown_seconds = cooldown_deadline - now_ts
    assert cooldown_seconds <= EXIT_LIMIT_DOWN_COOLDOWN_SECONDS + 10, (
        f"跌停冷却时长 {cooldown_seconds:.0f}s 过长,应≈{EXIT_LIMIT_DOWN_COOLDOWN_SECONDS}s,不得锁到收盘"
    )
    # 反向验证:绝不能锁到当天收盘(15:00)。若锁到收盘,deadline ≈ 当天15:00时间戳,
    # 远大于 now + EXIT_LIMIT_DOWN_COOLDOWN_SECONDS。
    close_today = now_local().replace(hour=15, minute=0, second=0, microsecond=0)
    if now_local() < close_today:
        # 当天还没收盘:锁到收盘的 deadline 应 == close_today 时间戳,
        # 我们的 deadline 必须明显小于它(否则就是锁到收盘的旧 bug)。
        assert cooldown_deadline < close_today.timestamp() - 60, (
            "跌停冷却被锁到当天收盘,下午开口子时仍会被跳过、错过止损"
        )

"""Broker 适配层。

虚拟盘和未来 QMT 实盘必须共享同一套接口，避免策略代码直接依赖具体券商实现。
"""

from __future__ import annotations

import copy
import os
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from config.settings import (
    LIVE_TRADING_ENABLED,
    QMT_ACCOUNT_ID,
    QMT_CLIENT_PATH,
    STATE_FILE,
    is_a_share_stock,
)
from rules.position import PositionManager
from trading.models import ExecutionReport, OrderIntent, PortfolioSnapshot


class BrokerAdapter(ABC):
    """交易通道抽象接口。"""

    @abstractmethod
    def connect(self) -> None:
        """连接交易通道。"""

    @abstractmethod
    def query_cash(self) -> float:
        """查询可用现金。"""

    @abstractmethod
    def query_positions(self) -> dict[str, dict[str, Any]]:
        """查询持仓。"""

    @abstractmethod
    def place_order(self, order: OrderIntent) -> ExecutionReport:
        """提交订单。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销订单。"""

    @abstractmethod
    def query_orders(self) -> list[ExecutionReport]:
        """查询订单回报。"""

    @abstractmethod
    def close(self) -> None:
        """关闭交易通道。"""


class PaperBrokerAdapter(BrokerAdapter):
    """基于本地 JSON 持仓的虚拟盘 Broker。"""

    def __init__(
        self,
        portfolio: PositionManager | None = None,
        state_file: str | None = None,
        trade_log_file: str | None = None,
        snapshot_log_file: str | None = None,
    ) -> None:
        self.portfolio = portfolio or PositionManager(
            state_file=state_file or STATE_FILE,
            trade_log_file=trade_log_file,
            snapshot_log_file=snapshot_log_file,
        )
        self._orders: list[ExecutionReport] = []
        self._lock = threading.RLock()
        self._connected = False

    def connect(self) -> None:
        """连接虚拟盘。"""
        self._connected = True

    def query_cash(self) -> float:
        """查询虚拟盘现金。"""
        with self._lock:
            return float(self.portfolio.get_cash())

    def query_positions(self) -> dict[str, dict[str, Any]]:
        """查询虚拟盘持仓。"""
        with self._lock:
            return copy.deepcopy(self.portfolio.get_all_positions())

    def query_snapshot(self, current_prices: dict[str, float] | None = None) -> PortfolioSnapshot:
        """查询账户快照。"""
        with self._lock:
            return PortfolioSnapshot.from_portfolio(self.portfolio, current_prices, source="paper")

    def place_order(self, order: OrderIntent) -> ExecutionReport:
        """提交虚拟盘订单并立即按规则成交。"""
        if not self._connected:
            raise RuntimeError("虚拟盘 Broker 尚未连接")
        if order.shares <= 0:
            return self._reject(order, "委托数量必须大于 0")
        if order.price <= 0:
            return self._reject(order, "委托价格必须大于 0")
        if not is_a_share_stock(order.code):
            return self._reject(order, f"仅支持沪深 A 股股票代码: {order.code}")

        trade_date = order.date or datetime.now().strftime("%Y%m%d")
        with self._lock:
            if order.action == "buy":
                result = self.portfolio.buy(
                    order.code,
                    order.name or order.code,
                    order.price,
                    order.shares,
                    trade_date,
                    order.strategy or "paper",
                )
            else:
                result = self.portfolio.sell(
                    order.code,
                    order.price,
                    order.shares,
                    trade_date,
                    order.strategy or "paper",
                )

            if not result.get("success"):
                return self._reject(order, str(result.get("reason", "虚拟成交失败")))

            report = ExecutionReport(
                order_id=self._new_order_id(),
                status="filled",
                code=order.code,
                action=order.action,
                price=order.price,
                actual_price=float(result.get("actual_price", order.price)),
                shares=int(result.get("shares", order.shares)),
                amount=round(order.price * int(result.get("shares", order.shares)), 2),
                cost=float(result.get("cost_detail", {}).get("total", 0.0)),
                strategy=order.strategy,
                message="虚拟盘成交",
                profit=result.get("profit"),
                date=trade_date,
                raw=copy.deepcopy(result),
            )
            self._orders.append(report)
            return report

    def cancel_order(self, order_id: str) -> bool:
        """虚拟盘订单立即成交，暂不支持撤单。"""
        return False

    def query_orders(self) -> list[ExecutionReport]:
        """查询虚拟盘订单回报。"""
        with self._lock:
            return list(self._orders)

    def close(self) -> None:
        """关闭虚拟盘连接。"""
        self._connected = False

    def _reject(self, order: OrderIntent, message: str) -> ExecutionReport:
        report = ExecutionReport(
            order_id=self._new_order_id(),
            status="rejected",
            code=order.code,
            action=order.action,
            price=order.price,
            actual_price=0.0,
            shares=order.shares,
            amount=0.0,
            strategy=order.strategy,
            message=message,
            date=order.date,
        )
        self._orders.append(report)
        return report

    @staticmethod
    def _new_order_id() -> str:
        """生成内部订单编号。"""
        return f"PAPER-{uuid.uuid4().hex[:12]}"


class QmtBrokerAdapter(BrokerAdapter):
    """QMT 预留适配器。

    默认只允许 dry-run。只有显式设置 LIVE_TRADING_ENABLED=true 后，才会进入真实 QMT
    初始化路径；当前版本仍阻断真实下单，避免在未完成联调前误发委托。
    """

    def __init__(
        self,
        account_id: str | None = None,
        client_path: str | None = None,
        live_enabled: bool | None = None,
    ) -> None:
        self.account_id = account_id or QMT_ACCOUNT_ID
        self.client_path = client_path or QMT_CLIENT_PATH
        self.live_enabled = LIVE_TRADING_ENABLED if live_enabled is None else live_enabled
        self._connected = False
        self._orders: list[ExecutionReport] = []

    def connect(self) -> None:
        """连接 QMT dry-run 通道。"""
        if self.live_enabled:
            raise NotImplementedError("真实 QMT 下单尚未完成联调，当前版本拒绝连接实盘通道")
        self._connected = True

    def query_cash(self) -> float:
        """dry-run 模式不读取真实资金。"""
        self._ensure_connected()
        return 0.0

    def query_positions(self) -> dict[str, dict[str, Any]]:
        """dry-run 模式不读取真实持仓。"""
        self._ensure_connected()
        return {}

    def place_order(self, order: OrderIntent) -> ExecutionReport:
        """记录 dry-run 委托，不发送真实订单。"""
        self._ensure_connected()
        if not is_a_share_stock(order.code):
            report = ExecutionReport(
                order_id=f"QMT-DRYRUN-{uuid.uuid4().hex[:12]}",
                status="rejected",
                code=order.code,
                action=order.action,
                price=order.price,
                actual_price=0.0,
                shares=order.shares,
                amount=0.0,
                strategy=order.strategy,
                message=f"仅支持沪深 A 股股票代码: {order.code}",
                date=order.date,
            )
            self._orders.append(report)
            return report
        report = ExecutionReport(
            order_id=f"QMT-DRYRUN-{uuid.uuid4().hex[:12]}",
            status="submitted",
            code=order.code,
            action=order.action,
            price=order.price,
            actual_price=0.0,
            shares=order.shares,
            amount=0.0,
            strategy=order.strategy,
            message="QMT dry-run：未发送真实委托",
            date=order.date,
            raw={
                "account_id": self.account_id,
                "client_path": self.client_path,
                "live_enabled": self.live_enabled,
            },
        )
        self._orders.append(report)
        return report

    def cancel_order(self, order_id: str) -> bool:
        """dry-run 撤单只更新本地状态。"""
        self._ensure_connected()
        return any(order.order_id == order_id for order in self._orders)

    def query_orders(self) -> list[ExecutionReport]:
        """查询 dry-run 订单回报。"""
        self._ensure_connected()
        return list(self._orders)

    def close(self) -> None:
        """关闭 QMT dry-run 通道。"""
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("QMT Broker 尚未连接")


def create_broker(mode: str | None = None) -> BrokerAdapter:
    """根据环境配置创建 Broker。"""
    broker_mode = (mode or os.getenv("BROKER_MODE", "paper")).lower()
    if broker_mode == "paper":
        return PaperBrokerAdapter()
    if broker_mode == "qmt":
        return QmtBrokerAdapter()
    raise ValueError(f"不支持的 Broker 模式: {broker_mode}")

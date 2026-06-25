"""交易约束配置测试。"""

import pytest

from config.settings import _load_permission_flags
from rules.engine import TradingRules


def test_calc_lot_size_respects_min_order_amount() -> None:
    """买入数量计算应拒绝低于最低建议成交额的小碎单。"""
    shares = TradingRules.calc_lot_size(
        price=10.0,
        cash=50_000.0,
        max_ratio=0.20,
        total_value=50_000.0,
        min_amount=8_000.0,
    )
    assert shares == 900

    too_small = TradingRules.calc_lot_size(
        price=10.0,
        cash=50_000.0,
        max_ratio=0.10,
        total_value=50_000.0,
        min_amount=8_000.0,
    )
    assert too_small == 0


def test_permission_yaml_parser_reads_shared_account_permissions(tmp_path) -> None:
    """权限文件应能表达报告要求的账户交易边界。"""
    permission_file = tmp_path / "permissions.yaml"
    permission_file.write_text(
        "\n".join([
            "permissions:",
            "  main_board: true",
            "  chinext: false",
            "  star: true",
            "  convertible: false",
            "  hk_connect: false",
            "  margin: false",
        ]),
        encoding="utf-8",
    )

    flags = _load_permission_flags(str(permission_file))

    assert flags["main_board"] is True
    assert flags["chinext"] is False
    assert flags["star"] is True
    assert flags["convertible"] is False
    assert flags["hk_connect"] is False
    assert flags["margin"] is False


def test_permission_yaml_parser_fails_on_invalid_boolean(tmp_path) -> None:
    """非法权限值必须失败，避免权限误开或误关。"""
    permission_file = tmp_path / "permissions.yaml"
    permission_file.write_text("permissions:\n  chinext: maybe\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="读取账户权限配置失败"):
        _load_permission_flags(str(permission_file))

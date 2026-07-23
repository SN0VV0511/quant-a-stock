"""BaoStock 有限冷却与自动恢复测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from data.ak_loader import AKDataLoader


def _history_rows() -> list[list[str]]:
    """构造基础历史行情返回行。"""
    return [
        [
            "2026-07-23",
            "10",
            "11",
            "9",
            "10.5",
            "1000",
            "10000",
            "10",
            "5",
        ]
    ]


def _extended_history_rows() -> list[list[str]]:
    """构造扩展历史行情返回行。"""
    return [
        [
            "2026-07-23",
            "10",
            "11",
            "9",
            "10.5",
            "1000",
            "10000",
            "1.5",
            "10",
            "5",
            "0",
            "1",
            "1.2",
        ]
    ]


def test_ensure_login_skips_remote_during_cooldown_and_retries_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """冷却期不触网，期满后应恢复一次登录尝试。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    loader._bs_available = False
    loader._bs_unavailable_since = 100.0
    now = [200.0]
    calls: list[str] = []

    def _run(command: str, *_args: object, **_kwargs: object) -> dict[str, str]:
        calls.append(command)
        return {"error_code": "0"}

    monkeypatch.setattr("data.ak_loader._BS_RETRY_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr("data.ak_loader.time.time", lambda: now[0])
    monkeypatch.setattr("data.ak_loader.bs", object())
    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _run)

    with pytest.raises(ConnectionError, match="熔断冷却中"):
        loader._ensure_login()
    assert calls == []
    assert loader._bs_available is False

    now[0] = 401.0
    loader._ensure_login()

    assert calls == ["login"]
    assert loader._bs_available is True
    assert loader._bs_logged_in is True
    assert loader._bs_unavailable_since is None


def test_failed_login_enters_cooldown_without_recursive_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """三次失败应在同一锁内安全熔断，后续调用不得立即形成热循环。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    calls: list[str] = []

    def _fail(command: str, *_args: object, **_kwargs: object) -> None:
        calls.append(command)
        return None

    monkeypatch.setattr("data.ak_loader._BS_RETRY_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr("data.ak_loader.time.time", lambda: 1_000.0)
    monkeypatch.setattr("data.ak_loader.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("data.ak_loader.bs", object())
    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _fail)

    with pytest.raises(ConnectionError, match="登录重试 3 次均失败"):
        loader._ensure_login()

    assert calls == ["login", "login", "login"]
    assert loader._bs_available is False
    assert loader._bs_unavailable_since == 1_000.0

    with pytest.raises(ConnectionError, match="熔断冷却中"):
        loader._ensure_login()
    assert calls == ["login", "login", "login"]


@pytest.mark.parametrize(
    ("method_name", "command", "rows"),
    [
        ("get_batch_history", "query_history_batch", _history_rows()),
        (
            "get_batch_history_ext",
            "query_history_ext_batch",
            _extended_history_rows(),
        ),
    ],
)
def test_batch_history_reenters_remote_after_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
    command: str,
    rows: list[list[str]],
) -> None:
    """基础和扩展批量历史路径都应在冷却期满后重新进入远端。"""
    loader = AKDataLoader(cache_dir=str(tmp_path))
    loader._bs_available = False
    loader._bs_unavailable_since = 100.0
    now = [200.0]
    calls: list[str] = []

    def _run(
        actual_command: str,
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        calls.append(actual_command)
        return {
            "results": {
                "sh.600000": {
                    "error_code": "0",
                    "rows": rows,
                }
            }
        }

    monkeypatch.setattr("data.ak_loader._BS_RETRY_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr("data.ak_loader.time.time", lambda: now[0])
    monkeypatch.setattr("data.ak_loader._run_bs_with_subprocess", _run)
    method = getattr(loader, method_name)

    assert method(["600000"], days=260) == {}
    assert calls == []
    assert loader._bs_available is False

    now[0] = 401.0
    result = method(["600000"], days=260)

    assert set(result) == {"600000"}
    assert calls == [command]
    assert loader._bs_available is True

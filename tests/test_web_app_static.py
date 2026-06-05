"""Web 仪表盘静态资源与鉴权路由测试。"""

import hashlib
import io
from types import SimpleNamespace

from web import app as web_app


class DummyHandler:
    """用于直接测试 QuantHandler 辅助方法的轻量替身。"""

    def __init__(self, cookie: str = "") -> None:
        self.headers = {"Cookie": cookie}
        self.status: int | None = None
        self.response_headers: dict[str, object] = {}
        self.wfile = io.BytesIO()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: object) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        return None

    def send_error(self, status: int) -> None:
        self.status = status


def test_serve_spa_prefers_react_dist(tmp_path, monkeypatch) -> None:
    """React 构建存在时应优先返回 dist/index.html。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    monkeypatch.setattr(web_app, "DIST_DIR", str(dist))

    handler = DummyHandler()
    web_app.QuantHandler._serve_spa(handler, "index.html")

    assert handler.status == 200
    assert handler.response_headers["Content-Type"] == "text/html; charset=utf-8"
    assert b'id="root"' in handler.wfile.getvalue()


def test_static_assets_are_served_with_mime_and_cache_headers(tmp_path, monkeypatch) -> None:
    """Vite assets 应公开服务，并带有合理 MIME 与缓存头。"""
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(web_app, "DIST_DIR", str(dist))

    handler = DummyHandler()
    web_app.QuantHandler._serve_static_asset(handler, "/assets/app.js")

    assert handler.status == 200
    assert handler.response_headers["Content-Type"] in {"text/javascript", "application/javascript"}
    assert handler.response_headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert b"console.log" in handler.wfile.getvalue()


def test_static_asset_head_omits_body(tmp_path, monkeypatch) -> None:
    """HEAD 检查应返回同样的资源元信息，但不写入响应体。"""
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("body{color:white}", encoding="utf-8")
    monkeypatch.setattr(web_app, "DIST_DIR", str(dist))

    handler = DummyHandler()
    web_app.QuantHandler._serve_static_asset(handler, "/assets/app.css", write_body=False)

    assert handler.status == 200
    assert handler.response_headers["Content-Type"] == "text/css"
    assert handler.response_headers["Content-Length"] == len(b"body{color:white}")
    assert handler.wfile.getvalue() == b""


def test_require_auth_redirects_when_cookie_missing(monkeypatch) -> None:
    """未登录访问受保护页面应重定向到登录页。"""
    monkeypatch.setattr(web_app, "_DASHBOARD_PASSWORD_HASH", hashlib.sha256(b"secret").hexdigest())
    web_app._sessions.clear()

    handler = DummyHandler()
    ok = web_app.QuantHandler._require_auth(handler)

    assert ok is False
    assert handler.status == 302
    assert handler.response_headers["Location"] == "login"


def test_require_auth_accepts_valid_session() -> None:
    """有效会话 token 应通过鉴权。"""
    web_app._sessions.clear()
    token = "token-for-test"
    web_app._sessions[token] = 0

    handler = DummyHandler(cookie=f"quant_token={token}")
    # 直接把时间替换成稳定值，避免测试依赖真实时钟。
    original_time = web_app.time.time
    web_app.time.time = lambda: 1
    try:
        ok = web_app.QuantHandler._require_auth(handler)
    finally:
        web_app.time.time = original_time

    assert ok is True
    assert handler.status is None

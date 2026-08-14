"""test_danmaku_api.py — POST /api/danmaku 测试弹幕注入（直播测试台）"""
from tests.test_web_routes import _make_context
from src.shared.events import DANMAKU_RECEIVED
from src.web.app_factory import create_app


def test_danmaku_api_publishes_event():
    ctx = _make_context()
    app = create_app(ctx)
    client = app.test_client()
    seen = []
    ctx["event_bus"].subscribe(DANMAKU_RECEIVED,
                               lambda event, **kw: seen.append(kw))
    resp = client.post("/api/danmaku",
                       json={"content": "你好呀", "user_name": "测试观众"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "command_id" in data
    assert len(seen) == 1
    assert seen[0]["content"] == "你好呀"
    assert seen[0]["user_name"] == "测试观众"


def test_danmaku_api_missing_content_400():
    ctx = _make_context()
    app = create_app(ctx)
    resp = app.test_client().post("/api/danmaku", json={})
    assert resp.status_code == 400

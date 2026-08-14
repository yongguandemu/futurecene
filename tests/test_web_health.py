"""test_web_health.py — /api/health 健康检查（不依赖任何调度官）"""
from src.web.app_factory import create_app


def test_health_returns_ok():
    app = create_app()
    client = app.test_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}

"""test_worldbook_api.py — 世界书 Web API 单测（临时 WorldBook 注入，不污染真实数据）"""
import json
import tempfile
from pathlib import Path

from src.shared.world_book import WorldBook
from src.web.app_factory import create_app


def _make_app_with_book(entries=None):
    """构造临时 WorldBook + Flask 应用（context 注入，隔离真实 worldbook.json）。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "wb.json"
    book = {"version": 2, "entry_count": len(entries or []),
            "entries": entries or [], "categories": {}}
    for e in book["entries"]:
        book["categories"].setdefault(e["category"], []).append(e["entry_id"])
    p.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
    wb = WorldBook(p)
    app = create_app({"world_book": wb})
    return app, wb


def _entry(eid, title, content, category="character", role="yuki", tags=None):
    return {"entry_id": eid, "title": title, "content": content,
            "category": category, "tags": tags or [],
            "metadata": {"role": role}}


def test_list_empty():
    app, _ = _make_app_with_book()
    resp = app.test_client().get("/api/worldbook")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["data"]["entries"] == []


def test_create_get_update_delete_flow():
    """完整 CRUD 流：创建 → 列表 → 单条 → 更新 → 删除 → 404。"""
    app, _ = _make_app_with_book()
    client = app.test_client()

    # 创建
    r = client.post("/api/worldbook", json={"entry_id": "wb_test_1",
                                            "title": "测试条目",
                                            "content": "测试内容",
                                            "category": "character",
                                            "metadata": {"role": "yuki"}})
    assert r.status_code == 200 and r.get_json()["data"]["content"] == "测试内容"

    # 冲突 409
    r = client.post("/api/worldbook", json={"entry_id": "wb_test_1",
                                            "title": "重复", "content": "x"})
    assert r.status_code == 409

    # 缺字段 400
    r = client.post("/api/worldbook", json={"entry_id": "wb_test_2"})
    assert r.status_code == 400

    # 列表含新条目
    r = client.get("/api/worldbook")
    assert r.get_json()["data"]["count"] == 1

    # 单条
    r = client.get("/api/worldbook/wb_test_1")
    assert r.status_code == 200 and r.get_json()["data"]["title"] == "测试条目"

    # 更新
    r = client.put("/api/worldbook/wb_test_1", json={"content": "新内容"})
    assert r.status_code == 200 and r.get_json()["data"]["content"] == "新内容"

    # 删除
    r = client.delete("/api/worldbook/wb_test_1")
    assert r.status_code == 200 and r.get_json()["data"]["deleted"] == "wb_test_1"

    # 删除后再查 404
    assert client.get("/api/worldbook/wb_test_1").status_code == 404
    assert client.put("/api/worldbook/wb_test_1",
                      json={"content": "x"}).status_code == 404
    assert client.delete("/api/worldbook/wb_test_1").status_code == 404


def test_list_filters():
    """列表筛选：category/role/tag/keyword 组合。"""
    app, _ = _make_app_with_book([
        _entry("wb_y_1", "Yuki 的身份", "AI 实习生", role="yuki",
               tags=["身份"]),
        _entry("wb_l_1", "莉莉丝的身份", "魔族王族", role="lilith",
               tags=["身份"]),
        _entry("wb_topic_1", "观众话题：哈哈", "高频话题",
               category="audience_insight", role="", tags=["话题"]),
    ])
    client = app.test_client()
    assert client.get("/api/worldbook?category=character").get_json()["data"]["count"] == 2
    assert client.get("/api/worldbook?role=lilith").get_json()["data"]["count"] == 1
    assert client.get("/api/worldbook?tag=话题").get_json()["data"]["count"] == 1
    assert client.get("/api/worldbook?keyword=魔族").get_json()["data"]["count"] == 1
    assert client.get("/api/worldbook?category=character&role=lilith").get_json()["data"]["count"] == 1


def test_suggest_endpoint():
    """建议接口：传 topics/characters 上下文返回 high 优先级建议。"""
    app, _ = _make_app_with_book([
        _entry("wb_y_1", "Yuki 的身份", "AI 实习生"),
    ])
    resp = app.test_client().post(
        "/api/worldbook/suggest",
        json={"topics": ["新梗"], "characters": ["新人设"]})
    assert resp.status_code == 200
    suggestions = resp.get_json()["data"]["suggestions"]
    assert suggestions and suggestions[0]["priority"] == "high"


def test_suggest_route_not_swallowed_by_entry_id():
    """路由优先级：/suggest 不被 /<entry_id> 吞掉（注册顺序验证）。"""
    app, _ = _make_app_with_book()
    resp = app.test_client().post("/api/worldbook/suggest", json={})
    assert resp.status_code == 200  # 走 suggest 而非 404/创建


def test_worldbook_page_served():
    """世界书管理前端页面可访问（GET /worldbook/）。"""
    app, _ = _make_app_with_book()
    resp = app.test_client().get("/worldbook/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert "世界书" in html and "/api/worldbook" in html  # 页面骨架 + API 引用

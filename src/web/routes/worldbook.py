"""routes/worldbook.py — 世界书 Web API（CRUD + 建议）

端点（/api/worldbook）：
- GET    /api/worldbook              列表（category/role/tag/keyword 查询参数筛选）
- GET    /api/worldbook/<entry_id>   单条
- POST   /api/worldbook              创建（body: entry_id/title/content/category/tags/metadata）
- PUT    /api/worldbook/<entry_id>   更新（body: content/title/tags/reason）
- DELETE /api/worldbook/<entry_id>   删除
- POST   /api/worldbook/suggest      进化建议（body: events/topics/characters）

数据源优先取 APP_CONTEXT["world_book"]（测试注入用），未装配回退模块单例 get_world_book()。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · worldbook · 世界书 CRUD + 建议 API
2. 配置契约：current_app.config["APP_CONTEXT"]["world_book"]（WorldBook 实例）；未装配回退 get_world_book() 单例
3. 输入契约：GET 查询参数 category/role/tag/keyword；POST/PUT 读 JSON 请求体
4. 输出契约：JSON {ok, data, error}；列表/单条返回条目 dict；冲突/不存在返回 409/404
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、src.shared.world_book.get_world_book
6. 错误定义：entry_id 已存在 409；条目不存在 404；创建缺必填字段 400；suggest 未传上下文返回空列表
7. 生命周期方法：无（Blueprint 路由函数）
8. 领域状态说明：无模块级可变状态；读写经 WorldBook（save_to_disk 落盘 data/worldbook.json）
"""
from flask import Blueprint, current_app, jsonify, request

from src.shared.world_book import get_world_book

bp = Blueprint("worldbook", __name__, url_prefix="/api/worldbook")


def _book():
    """取世界书实例：context 注入优先（测试可控），未装配回退模块单例。"""
    context = current_app.config.get("APP_CONTEXT", {})
    return context.get("world_book") or get_world_book()


@bp.route("/suggest", methods=["POST"])
def suggest():
    """进化建议：基于 events/topics/characters 上下文生成候选。"""
    body = request.get_json(silent=True) or {}
    context = {"events": body.get("events", []),
               "topics": body.get("topics", []),
               "characters": body.get("characters", [])}
    suggestions = _book().suggest(context)
    return jsonify({"ok": True, "data": {"suggestions": suggestions}, "error": None})


@bp.route("", methods=["GET"])
def list_entries():
    """列表：支持 category/role/tag/keyword 组合筛选。"""
    book = _book()
    category = request.args.get("category", "")
    role = request.args.get("role", "")
    tag = request.args.get("tag", "")
    keyword = request.args.get("keyword", "")

    entries = book.get_entries(category=category, tag=tag)
    if role:
        role_ids = {e["entry_id"] for e in book.entries_for_role(role)}
        entries = [e for e in entries if e["entry_id"] in role_ids]
    if keyword:
        kw_ids = {e["entry_id"] for e in book.search(keyword)}
        entries = [e for e in entries if e["entry_id"] in kw_ids]
    return jsonify({"ok": True, "data": {"entries": entries,
                                         "count": len(entries)}, "error": None})


@bp.route("", methods=["POST"])
def create_entry():
    """创建条目（body 必填 entry_id/title/content）。"""
    body = request.get_json(silent=True) or {}
    entry_id = body.get("entry_id", "")
    title = body.get("title", "")
    content = body.get("content", "")
    if not entry_id or not title or not content:
        return jsonify({"ok": False, "error": "entry_id/title/content 必填"}), 400
    ok = _book().add_entry(
        entry_id, title, content,
        category=body.get("category", "general"),
        tags=body.get("tags", []),
        metadata=body.get("metadata", {}))
    if not ok:
        return jsonify({"ok": False, "error": f"entry_id 已存在: {entry_id}"}), 409
    _book().save_to_disk()
    return jsonify({"ok": True, "data": _book().get_entry(entry_id), "error": None})


@bp.route("/<entry_id>", methods=["GET"])
def get_entry(entry_id):
    entry = _book().get_entry(entry_id)
    if entry is None:
        return jsonify({"ok": False, "error": f"条目不存在: {entry_id}"}), 404
    return jsonify({"ok": True, "data": entry, "error": None})


@bp.route("/<entry_id>", methods=["PUT"])
def update_entry(entry_id):
    body = request.get_json(silent=True) or {}
    ok = _book().update_entry(
        entry_id,
        content=body.get("content"),
        title=body.get("title"),
        tags=body.get("tags"),
        metadata=body.get("metadata"),
        reason=body.get("reason", ""))
    if not ok:
        return jsonify({"ok": False, "error": f"条目不存在: {entry_id}"}), 404
    _book().save_to_disk()
    return jsonify({"ok": True, "data": _book().get_entry(entry_id), "error": None})


@bp.route("/<entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    ok = _book().remove_entry(entry_id)
    if not ok:
        return jsonify({"ok": False, "error": f"条目不存在: {entry_id}"}), 404
    _book().save_to_disk()
    return jsonify({"ok": True, "data": {"deleted": entry_id}, "error": None})

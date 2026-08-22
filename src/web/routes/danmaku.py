"""routes/danmaku.py — POST /api/danmaku（直播测试台 · 模拟弹幕注入）

测试用弹幕入口：前端直播测试台输入 → 发布 danmaku:received → 走完整链路
（记忆 → LLM → 字幕 → TTS → Live2D 口型）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · danmaku · POST /api/danmaku
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 event_bus
3. 输入契约：JSON body {content 必填、user_name 可选、user_id 可选}
4. 输出契约：{ok, command_id}；发布 danmaku:received（与 normalizer 同构 payload）
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、uuid、shared.events
6. 错误定义：event_bus 未装配 503；content 缺失 400
7. 生命周期方法：无（Blueprint 路由函数 inject_danmaku()）
8. 领域状态说明：无模块级可变状态；依赖 APP_CONTEXT 注入的 event_bus
"""
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

from src.shared.events import DANMAKU_RECEIVED

logger = logging.getLogger(__name__)

bp = Blueprint("danmaku", __name__, url_prefix="/api")


@bp.route("/danmaku", methods=["POST"])
def inject_danmaku():
    context = current_app.config.get("APP_CONTEXT", {})
    event_bus = context.get("event_bus")
    if event_bus is None:
        return jsonify({"ok": False, "error": "EventBus 未装配"}), 503

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": "content 必填"}), 400

    command_id = uuid.uuid4().hex
    event_bus.publish(
        DANMAKU_RECEIVED,
        event_type="danmaku",
        content=content,
        user_name=data.get("user_name") or "测试观众",
        user_id=data.get("user_id") or "test-user",
        extra={},
        timestamp=0.0,
    )
    return jsonify({"ok": True, "command_id": command_id})

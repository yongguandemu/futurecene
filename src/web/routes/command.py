"""routes/command.py — POST /api/command（规格书 6.5）

命令入口：前端指令 → Intent Parser 解析 → Command Router 分发。
session:switch / system:* 为指挥官内部命令，不进调度官路由。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · command · POST /api/command
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 intent_parser/command_router/session
3. 输入契约：JSON body {text 或 command、session_id}；text 必填
4. 输出契约：{ok, data, error} JSON；session:switch 返回会话快照；system:status 返回内部命令说明；其余返回 router.dispatch 结果
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、asyncio、logging
6. 错误定义：parser/router 未装配返回 503；text 缺失返回 400
7. 生命周期方法：无（Blueprint 路由函数 command()）
8. 领域状态说明：无模块级可变状态；依赖 APP_CONTEXT 注入的解析器/路由器/会话
"""
import asyncio
import logging
import uuid

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

bp = Blueprint("command", __name__, url_prefix="/api")


@bp.route("/command", methods=["POST"])
def command():
    context = current_app.config.get("APP_CONTEXT", {})
    parser = context.get("intent_parser")
    router = context.get("command_router")
    session = context.get("session")
    if parser is None or router is None:
        return jsonify({"ok": False, "error": "指挥官未装配"}), 503

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or data.get("command") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 必填"}), 400

    cmd = parser.parse(text, source="command",
                       session_id=data.get("session_id", "default"))

    # 透传前端携带的对话历史（llm:chat 多轮上下文；_inject_llm_context 兜底为空列表）
    if isinstance(data.get("history"), list):
        cmd.payload["history"] = data["history"]

    # 指挥官内部命令（规格书 4.4）；统一生成 command_id 供前端追踪
    if cmd.capability == "session:switch" and session is not None:
        cid = uuid.uuid4().hex
        ok = session.switch_role(cmd.payload.get("role", "yuki"))
        return jsonify({"ok": ok, "command_id": cid,
                        "data": session.snapshot()})
    if cmd.capability == "system:status":
        cid = uuid.uuid4().hex
        return jsonify({"ok": True, "command_id": cid,
                        "data": {"note": "内部命令，由指挥官处理", "capability": cmd.capability}})
    # system:command（未知 ! 命令）不拦截，落入路由层返回 unknown capability（验收契约）

    result = asyncio.run(router.dispatch(cmd))
    return jsonify(result)

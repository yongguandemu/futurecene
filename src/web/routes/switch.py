"""routes/switch.py — POST /api/switch/{name}（规格书 6.5）

开关控制（启/停调度官），经 SwitchManager.set_manual（手动覆盖优先级最高）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · switch · POST /api/switch/{name}
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 switch_manager
3. 输入契约：URL 路径 name；JSON body {enabled: bool} 必填
4. 输出契约：{ok, data:{name, enabled}} JSON
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）
6. 错误定义：switch_manager 未装配返回 503；enabled 缺失返回 400
7. 生命周期方法：无（Blueprint 路由函数 switch()）
8. 领域状态说明：无模块级状态；经 SwitchManager.set_manual 手动覆盖（优先级最高）
"""
import uuid
from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("switch", __name__, url_prefix="/api")


@bp.route("/switch/<name>", methods=["POST"])
def switch(name):
    context = current_app.config.get("APP_CONTEXT", {})
    switch_manager = context.get("switch_manager")
    if switch_manager is None:
        return jsonify({"ok": False, "error": "开关管理器未装配"}), 503
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"ok": False, "error": "enabled 必填"}), 400
    switch_manager.set_manual(name, bool(data["enabled"]))
    return jsonify({"ok": True,
                    "data": {"name": name, "enabled": switch_manager.is_enabled(name)},
                    "command_id": uuid.uuid4().hex})

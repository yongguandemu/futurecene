"""routes/health.py — GET /api/health（规格书 8.1）

无装配上下文时返回最小 {"status": "ok"}（M0 契约）；
装配了 registry 时附加调度官注册表快照（M3 验收）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · health · GET /api/health
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 registry
3. 输入契约：无请求参数
4. 输出契约：registry 未装配返回 {"status":"ok"}；装配后附加 {"orchestrators":[名称列表]}
5. 依赖声明：flask（Blueprint/current_app/jsonify）
6. 错误定义：registry 缺失时降级返回最小健康体（M0 契约）
7. 生命周期方法：无（Blueprint 路由函数 health()）
8. 领域状态说明：无模块级状态；仅读 registry 快照
"""
from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__, url_prefix="/api")


@bp.route("/health", methods=["GET"])
def health():
    context = current_app.config.get("APP_CONTEXT", {})
    registry = context.get("registry")
    if registry is None:
        return jsonify({"status": "ok"})
    return jsonify({"status": "ok",
                    "orchestrators": [o.name for o in registry.all()]})

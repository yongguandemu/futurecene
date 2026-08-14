"""routes/state.py — GET /api/state（规格书 6.5）

系统状态快照：会话 + 开关 + 注册表 + 降级 + 成本 + 看门狗 + version（供总控台轮询）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · state · GET /api/state
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 state_provider；未装配时回退 session/switch_manager/registry/degradation_manager
3. 输入契约：无请求参数
4. 输出契约：{version, session, switches, orchestrators, degradation, cost, watchdog} JSON 快照
5. 依赖声明：flask（Blueprint/current_app/jsonify）、src.web.state_provider
6. 错误定义：各组件未装配时对应字段返回空（{} / []）；version 回退 0
7. 生命周期方法：无（Blueprint 路由函数 state()）
8. 领域状态说明：只读聚合 StateProvider 快照，无模块级可变状态
"""
from flask import Blueprint, current_app, jsonify

from src.web.state_provider import StateProvider

bp = Blueprint("state", __name__, url_prefix="/api")


@bp.route("/state", methods=["GET"])
def state():
    context = current_app.config.get("APP_CONTEXT", {})
    provider = context.get("state_provider")
    if provider is None:
        # 回退：无 provider 时返回旧字段结构（兼容未装配场景）
        session = context.get("session")
        switch_manager = context.get("switch_manager")
        registry = context.get("registry")
        return jsonify({
            "version": context.get("event_bus").current_seq()
            if context.get("event_bus") else 0,
            "session": session.snapshot() if session else {},
            "switches": switch_manager.snapshot() if switch_manager else {},
            "orchestrators": [o.name for o in registry.all()] if registry else [],
            "degradation": context.get("degradation_manager").snapshot()
            if context.get("degradation_manager") else {},
            "cost": {}, "watchdog": {},
        })
    return jsonify(provider.snapshot())

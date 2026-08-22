"""app_factory.py — Flask 应用工厂（规格书 6.5）

Blueprint 拆分（规格书 8.1 强制，禁止单体文件）：
  routes/health.py  → GET /api/health
  routes/state.py   → GET /api/state
  routes/command.py → POST /api/command
  routes/switch.py  → POST /api/switch/{name}
  routes/ws.py      → WS /ws/events
  /api/metrics      → 成本 + 看门狗快照（context.metrics_provider）
  /api/collab/config → POST 多角色运行时调参（白名单 COLLAB_RUNTIME_FIELDS 与
                       coordinator 共用；依赖 flask request 读取 JSON 请求体与
                       context.collaboration，非法字段返回 400）

装配上下文（context dict）由 src/app.py 注入：registry / switch_manager /
session / intent_parser / command_router / event_bus / metrics_provider 等。

# 模块内容清单（8 项契约）
1. 模块身份标识：web · app_factory · create_app() 应用工厂，注册端点 GET /api/health、GET /api/state、POST /api/command、POST /api/switch/{name}、WS /ws/events、GET /api/metrics、POST /api/collab/config 与前端静态路由
2. 配置契约：context dict 由 src/app.py 注入（registry/switch_manager/session/intent_parser/command_router/event_bus/metrics_provider/degradation_manager），存于 app.config["APP_CONTEXT"]；_FRONTEND_DIR=PROJECT_ROOT/frontend
3. 输入契约：create_app(context: Optional[Dict]=None) 装配上下文 dict；POST /api/collab/config 读取 JSON 请求体（request.get_json），字段白名单与校验规则取 coordinator.COLLAB_RUNTIME_FIELDS / coerce_runtime_field
4. 输出契约：返回配置完成的 Flask 实例；/api/metrics 返回 {cost, watchdog, version, circuit_breaker} JSON
5. 依赖声明：flask（Flask/jsonify/redirect/request/send_from_directory）、pathlib、typing、src.shared.config_loader、src.web.routes.{command,health,state,switch,ws}、src.orchestrators.collaboration.coordinator（COLLAB_RUNTIME_FIELDS/coerce_runtime_field）
6. 错误定义：state_provider 未装配时 /api/metrics 返回 {cost:{}, watchdog:{}, version:0, circuit_breaker:{}}；/api/collab/config 未装配返回 404、字段非法/无有效字段返回 400 {"ok":False,"error":...}
7. 生命周期方法：create_app()（注册全部 Blueprint + init_ws + 静态路由）
8. 领域状态说明：无模块级可变状态；装配上下文存于 app.config["APP_CONTEXT"]，前端目录常量 _FRONTEND_DIR
"""
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, jsonify, redirect, request, send_from_directory

from src.shared.config_loader import PROJECT_ROOT
from src.orchestrators.collaboration.coordinator import (
    COLLAB_RUNTIME_FIELDS, coerce_runtime_field,
)
from src.web.routes import command as command_route
from src.web.routes import config as config_route
from src.web.routes import danmaku as danmaku_route
from src.web.routes import health as health_route
from src.web.routes import memory as memory_route
from src.web.routes import state as state_route
from src.web.routes import switch as switch_route
from src.web.routes import worldbook as worldbook_route
from src.web.routes import ws as ws_route

_FRONTEND_DIR = PROJECT_ROOT / "frontend"


def create_app(context: Optional[Dict] = None) -> Flask:
    """应用工厂：注册全部 Blueprint + WS + 静态前端。"""
    context = context or {}
    app = Flask(__name__)
    app.config["APP_CONTEXT"] = context

    app.register_blueprint(health_route.bp)
    app.register_blueprint(state_route.bp)
    app.register_blueprint(command_route.bp)
    app.register_blueprint(switch_route.bp)
    app.register_blueprint(danmaku_route.bp)
    app.register_blueprint(worldbook_route.bp)
    app.register_blueprint(config_route.bp)   # 任务五：GET/PUT /api/config
    app.register_blueprint(memory_route.bp)   # 任务五：/api/memory + /api/memory/review

    ws_route.init_ws(app, context.get("event_bus"),
                     seq_provider=lambda: context.get("event_bus").current_seq()
                     if context.get("event_bus") else 0)

    # /api/metrics：成本 + 看门狗快照（P5，统一快照 + version）
    @app.get("/api/metrics")
    def metrics():
        provider = context.get("state_provider")
        if provider is None:
            return jsonify({"cost": {}, "watchdog": {}, "version": 0,
                            "circuit_breaker": {}})
        snap = provider.snapshot()
        return jsonify({"cost": snap["cost"], "watchdog": snap["watchdog"],
                        "version": snap["version"],
                        "circuit_breaker": context.get("cost_breaker").snapshot()
                        if context.get("cost_breaker") else {}})

    # /api/decisions：决策日志（区分「没收到」与「决定不回应」，规格书 5.6.4）
    @app.get("/api/decisions")
    def decisions():
        dlog = context.get("decision_log")
        if dlog is None:
            return jsonify({"entries": [], "stats": {}})
        entries = [e.to_dict() for e in dlog["recent"](100)]
        return jsonify({"entries": entries, "stats": dlog["stats"]()})

    # POST /api/collab/config：多角色运行时调参（白名单，重启回落 config.yaml）
    # 未装配（collaboration.enabled=false / COLLAB_ENABLED 未设）返回 404；
    # 请求体无白名单字段返回 400；白名单字段非法（类型/范围，见 coerce_runtime_field）
    # 返回 400 {"ok": False, "error": ...}；合法字段委托 collaboration.update_runtime()。
    @app.post("/api/collab/config")
    def collab_config():
        collab = context.get("collaboration")
        if collab is None:
            return jsonify({"ok": False, "error": "collaboration 未启用"}), 404
        body = request.get_json(silent=True) or {}
        update = {}
        for k, v in body.items():
            if k not in COLLAB_RUNTIME_FIELDS:
                continue
            ok, coerced, err = coerce_runtime_field(k, v)
            if not ok:
                return jsonify({"ok": False, "error": err}), 400
            update[k] = coerced
        if not update:
            return jsonify({"ok": False, "error": "无有效字段"}), 400
        return jsonify({"ok": True, "data": collab.update_runtime(**update)})

    # 前端静态资源服务
    @app.get("/")
    def index():
        return redirect("/dashboard/")

    @app.get("/dashboard/")
    def dashboard():
        return send_from_directory(_FRONTEND_DIR / "dashboard", "index.html")

    @app.get("/subtitle/")
    def subtitle():
        return send_from_directory(_FRONTEND_DIR / "subtitle_overlay", "index.html")

    @app.get("/worldbook/")
    def worldbook():
        return send_from_directory(_FRONTEND_DIR / "worldbook", "index.html")

    @app.get("/live2d/")
    def live2d():
        return send_from_directory(_FRONTEND_DIR / "live2d_stream", "index.html")

    @app.get("/assistant/")
    def assistant():
        # UI 合并（方案 A）：助手并入 dashboard 智能助手视图
        return redirect("/dashboard/#assistant")

    # 共享静态资源：设计令牌 / vendor 库 / Live2D 模型等（规格书 12.1）
    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(PROJECT_ROOT / "assets", filename)

    @app.get("/frontend/<path:filename>")
    def frontend_assets(filename):
        return send_from_directory(_FRONTEND_DIR, filename)

    return app

"""app_factory.py — Flask 应用工厂（规格书 6.5）

Blueprint 拆分（规格书 8.1 强制，禁止单体文件）：
  routes/health.py  → GET /api/health
  routes/state.py   → GET /api/state
  routes/command.py → POST /api/command
  routes/switch.py  → POST /api/switch/{name}
  routes/ws.py      → WS /ws/events
  /api/metrics      → 成本 + 看门狗快照（context.metrics_provider）

装配上下文（context dict）由 src/app.py 注入：registry / switch_manager /
session / intent_parser / command_router / event_bus / metrics_provider 等。

# 模块内容清单（8 项契约）
1. 模块身份标识：web · app_factory · create_app() 应用工厂，注册端点 GET /api/health、GET /api/state、POST /api/command、POST /api/switch/{name}、WS /ws/events、GET /api/metrics 与前端静态路由
2. 配置契约：context dict 由 src/app.py 注入（registry/switch_manager/session/intent_parser/command_router/event_bus/metrics_provider/degradation_manager），存于 app.config["APP_CONTEXT"]；_FRONTEND_DIR=PROJECT_ROOT/frontend
3. 输入契约：create_app(context: Optional[Dict]=None) 装配上下文 dict
4. 输出契约：返回配置完成的 Flask 实例；/api/metrics 返回 {cost, watchdog} JSON
5. 依赖声明：flask（Flask/jsonify/redirect/send_from_directory）、pathlib、typing、src.shared.config_loader、src.web.routes.{command,health,state,switch,ws}
6. 错误定义：metrics_provider 未装配时 /api/metrics 返回 {cost:{}, watchdog:{}}
7. 生命周期方法：create_app()（注册全部 Blueprint + init_ws + 静态路由）
8. 领域状态说明：无模块级可变状态；装配上下文存于 app.config["APP_CONTEXT"]，前端目录常量 _FRONTEND_DIR
"""
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, jsonify, redirect, send_from_directory

from src.shared.config_loader import PROJECT_ROOT
from src.web.routes import command as command_route
from src.web.routes import health as health_route
from src.web.routes import state as state_route
from src.web.routes import switch as switch_route
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

    ws_route.init_ws(app, context.get("event_bus"))

    # /api/metrics：成本 + 看门狗快照（P5）
    @app.get("/api/metrics")
    def metrics():
        provider = context.get("metrics_provider")
        if provider is None:
            return jsonify({"cost": {}, "watchdog": {}})
        return jsonify(provider())

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

    @app.get("/live2d/")
    def live2d():
        return send_from_directory(_FRONTEND_DIR / "live2d_stream", "index.html")

    @app.get("/assistant/")
    def assistant():
        return send_from_directory(_FRONTEND_DIR / "assistant", "index.html")

    # 共享静态资源：设计令牌 / vendor 库 / Live2D 模型等（规格书 12.1）
    @app.get("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(PROJECT_ROOT / "assets", filename)

    @app.get("/frontend/<path:filename>")
    def frontend_assets(filename):
        return send_from_directory(_FRONTEND_DIR, filename)

    return app

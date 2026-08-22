"""routes/memory.py — 记忆库 + 世界书提案审阅 Web API（任务五 5.4）

端点：
- GET  /api/memory            记忆库概览（L0 今日行数 / L1 缓冲 / L2 / L3 条数）
- GET  /api/memory/recall     分层检索（query/strength/session_id/character_id）
- GET  /api/memory/review     提案列表（status 过滤）
- POST /api/memory/review     审阅处置（{action: accept|reject, proposal_id, reason}）

数据源：APP_CONTEXT["memory"]（MemoryOrchestrator 实例）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · memory · 记忆库概览/检索/审阅 API
2. 配置契约：current_app.config["APP_CONTEXT"]["memory"]（MemoryOrchestrator）
3. 输入契约：GET 查询参数；POST /api/memory/review 读 JSON body
4. 输出契约：JSON {ok, data, error}；未装配 503；非法 action 400
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、asyncio
6. 错误定义：memory 未装配 503；review 非法 action/proposal 不存在返回对应错误
7. 生命周期方法：无（Blueprint 路由函数）
8. 领域状态说明：无模块级可变状态；读写经 MemoryOrchestrator
"""
import asyncio
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from src.shared.config_loader import PROJECT_ROOT

bp = Blueprint("memory", __name__, url_prefix="/api/memory")


def _memory():
    context = current_app.config.get("APP_CONTEXT", {})
    orch = context.get("memory")
    if orch is None:
        return None, (jsonify({"ok": False, "error": "记忆调度官未装配"}), 503)
    return orch, None


def _run(orch, capability, payload):
    return asyncio.run(orch.handle({"capability": capability, "payload": payload}))


@bp.route("", methods=["GET"])
def overview():
    """记忆库概览：L0 今日行数 / L1 缓冲 / L2 / L3 条数。"""
    orch, err = _memory()
    if err:
        return err
    l0_dir = PROJECT_ROOT / "data" / "memory" / "l0"
    today = datetime.now().strftime("%Y%m%d")
    l0_lines = 0
    path = l0_dir / f"{today}.jsonl"
    if path.exists():
        try:
            l0_lines = sum(1 for _ in path.open(encoding="utf-8"))
        except OSError:
            l0_lines = 0
    l1_count = orch._logger.count_l1()
    l2_count = orch._mid.count()
    l3_count = orch._long.count()
    return jsonify({"ok": True, "data": {
        "l0": {"dir": str(l0_dir), "today": today, "today_lines": l0_lines},
        "l1": {"count": l1_count, "window_sec": orch._config.l1_window_sec},
        "l2": {"count": l2_count},
        "l3": {"count": l3_count},
        "strength": orch._config.strength_default,
    }, "error": None})


@bp.route("/recall", methods=["GET"])
def recall():
    """分层检索：query/strength/session_id/character_id。"""
    orch, err = _memory()
    if err:
        return err
    payload = {
        "query": request.args.get("query", ""),
        "strength": request.args.get("strength", ""),
        "session_id": request.args.get("session_id", "default"),
        "character_id": request.args.get("character_id", ""),
        "mode": request.args.get("mode", "hybrid"),
    }
    result = _run(orch, "memory:recall", payload)
    return jsonify(result)


@bp.route("/review", methods=["GET"])
def review_list():
    orch, err = _memory()
    if err:
        return err
    result = _run(orch, "memory:review",
                  {"action": "list", "status": request.args.get("status", "")})
    return jsonify(result)


@bp.route("/review", methods=["POST"])
def review_resolve():
    orch, err = _memory()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    action = body.get("action", "")
    proposal_id = body.get("proposal_id", "")
    if action not in ("accept", "reject"):
        return jsonify({"ok": False,
                        "error": f"非法 action: {action!r}（可选 accept/reject）"}), 400
    if not proposal_id:
        return jsonify({"ok": False, "error": "proposal_id 必填"}), 400
    result = _run(orch, "memory:review", {
        "action": action, "proposal_id": proposal_id,
        "reason": body.get("reason", "")})
    return jsonify(result)

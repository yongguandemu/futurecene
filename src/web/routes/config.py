"""routes/config.py — 用户设置 Web API（任务五 5.2）

端点（/api/config）：
- GET /api/config   全部设置当前值 + 默认值 + 合法选项 + 推理强度映射
- PUT /api/config   更新设置（body: {memory_strength?, tts_output_target?,
                    allow_memory_to_worldbook?, reasoning_intensity?}）
  - allow_memory_to_worldbook 即时同步 switch_manager（review 开关）
  - 其余持久化到 UserConfigStore（data/config_user.json），装配层启动时消费

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · config · GET/PUT /api/config
2. 配置契约：从 current_app.config["APP_CONTEXT"] 取 user_config / switch_manager
3. 输入契约：PUT 读 JSON body（设置项子集）；未知/非法字段返回 400
4. 输出契约：GET 返回 {ok, data:{settings, defaults, options, reasoning_map}}；
             PUT 返回 {ok, data:{settings}}（更新后全量）
5. 依赖声明：flask（Blueprint/current_app/jsonify/request）、src.shared.user_config
6. 错误定义：user_config 未装配 503；非法字段/值 400
7. 生命周期方法：无（Blueprint 路由函数）
8. 领域状态说明：无模块级可变状态；读写经 UserConfigStore（data/config_user.json）
"""
from flask import Blueprint, current_app, jsonify, request

from src.shared.user_config import DEFAULTS, VALID_VALUES

bp = Blueprint("config", __name__, url_prefix="/api")

# 推理强度 → LLM 参数映射（规格书 5.2：engine + max_tokens + temperature；不碰后台任务）
REASONING_MAP = {
    "power_save": {"engine": "fast", "max_tokens": 120, "temperature": 0.8,
                   "desc": "省电：flash 引擎、短回复"},
    "standard": {"engine": "fast", "max_tokens": 300, "temperature": 0.7,
                 "desc": "标准：flash 引擎、正常回复"},
    "enhanced": {"engine": "pro", "max_tokens": 600, "temperature": 0.5,
                 "desc": "增强：pro 引擎、长回复、低温度"},
}


def _user_config():
    context = current_app.config.get("APP_CONTEXT", {})
    store = context.get("user_config")
    if store is None:
        return None, {"ok": False, "error": "用户设置未装配"}, 503
    return store, None, None


@bp.route("/config", methods=["GET"])
def get_config():
    store, err, code = _user_config()
    if err:
        return jsonify(err), code
    return jsonify({
        "ok": True,
        "data": {
            "settings": store.all(),
            "defaults": dict(DEFAULTS),
            "options": {k: sorted(v) for k, v in VALID_VALUES.items()},
            "reasoning_map": REASONING_MAP,
        },
        "error": None,
    })


@bp.route("/config", methods=["PUT"])
def put_config():
    store, err, code = _user_config()
    if err:
        return jsonify(err), code
    body = request.get_json(silent=True) or {}
    updates = {k: v for k, v in body.items() if k in DEFAULTS}
    if not updates:
        return jsonify({"ok": False, "error": "无有效设置字段"}), 400
    context = current_app.config.get("APP_CONTEXT", {})
    switch_manager = context.get("switch_manager")
    applied = {}
    for key, value in updates.items():
        try:
            store.set(key, value)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        applied[key] = store.get(key)
        # 即时副作用：世界书提案开关直接写 switch_manager（review 即时生效）
        if key == "allow_memory_to_worldbook" and switch_manager is not None:
            switch_manager.set_manual("allow_memory_to_worldbook", bool(applied[key]))
    return jsonify({"ok": True, "data": {"settings": store.all(),
                                          "applied": applied}, "error": None})

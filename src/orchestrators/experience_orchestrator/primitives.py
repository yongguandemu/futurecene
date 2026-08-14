"""primitives.py — 动作原语注册表（游戏经验学习域）

游戏无关的通用动作集合，可插拔扩展。
内置通用动作：press_key / click / select_option / advance / open_inventory。
新游戏动作（如 MC Mineflayer 适配）由适配器注册 ACTION_PRIMITIVES。

# 模块内容清单（8 项契约摘录）
- 所属调度官：experience
- 能力名：experience:decide 的候选动作来源
- 配置契约：无
- 输入契约：validate_action(action, args) -> (ok, error)
- 输出契约：(bool, str) 二元组
"""
import logging

logger = logging.getLogger(__name__)

# 通用动作原语：name -> {"desc": str, "validate": callable(args)->(ok,err)}
ACTION_PRIMITIVES = {
    "press_key": {"desc": "按键", "validate": lambda a: (True, "")},
    "click": {"desc": "窗口内点击", "validate": lambda a: (True, "")},
    "select_option": {"desc": "选项选择", "validate": lambda a: (True, "")},
    "advance": {"desc": "推进对话", "validate": lambda a: (True, "")},
    "open_inventory": {"desc": "打开背包/物品栏（E）", "validate": lambda a: (True, "")},
}


def register_primitive(name: str, desc: str = "", validate=None) -> bool:
    """注册新动作原语（如 MC Mineflayer 的移动/挖掘）。"""
    if name in ACTION_PRIMITIVES:
        return False
    ACTION_PRIMITIVES[name] = {
        "desc": desc,
        "validate": validate or (lambda a: (True, "")),
    }
    logger.info("[Primitives] 注册动作原语: %s (%s)", name, desc)
    return True


def validate_action(action: str, args: dict) -> tuple:
    """校验动作是否存在且参数合法。返回 (ok, error)。"""
    prim = ACTION_PRIMITIVES.get(action)
    if prim is None:
        return False, "未知动作: {}".format(action)
    try:
        return prim["validate"](args or {})
    except Exception as e:
        return False, "参数校验异常: {}".format(e)


def actions() -> list:
    """当前可用动作列表（供 LLM 探索候选）。"""
    return sorted(ACTION_PRIMITIVES.keys())


# ========== MC 原语（Mineflayer API 优先） ==========

def _validate_coord(args):
    for k in ("x", "z"):
        v = args.get(k)
        if not isinstance(v, (int, float)) or not -30000000 <= v <= 30000000:
            return False, "非法坐标 {}: {}".format(k, v)
    return True, ""


def _validate_xyz(args):
    for k in ("x", "y", "z"):
        v = args.get(k)
        if not isinstance(v, (int, float)) or not -30000000 <= v <= 30000000:
            return False, "非法坐标 {}: {}".format(k, v)
    return True, ""


def _validate_name_count(args):
    name = args.get("name")
    count = args.get("count", 1)
    if not isinstance(name, str) or not name or len(name) > 64:
        return False, "非法物品名: {}".format(name)
    if not isinstance(count, int) or not 1 <= count <= 64:
        return False, "非法数量: {}".format(count)
    return True, ""


register_primitive("move_to", "移动到坐标", lambda a: _validate_coord(a))
register_primitive("dig_block", "挖掘方块", lambda a: _validate_xyz(a))
register_primitive("place_block", "放置方块(含ref坐标与朝向)", lambda a: _validate_xyz(a) if "x" in a else (False, "缺坐标"))
register_primitive("craft_item", "合成物品", lambda a: _validate_name_count(a))
register_primitive("attack", "攻击附近目标", lambda a: (True, ""))
register_primitive("chat", "游戏内发言", lambda a: (True, "") if isinstance(a.get("text"), str) else (False, "text 必填"))
register_primitive("get_state", "读取状态", lambda a: (True, ""))


def _validate_gather(args):
    target = args.get("target")
    count = args.get("count", 1)
    if not isinstance(target, str) or not target or len(target) > 32:
        return False, "非法目标: {}".format(target)
    if not isinstance(count, int) or not 1 <= count <= 64:
        return False, "非法数量: {}".format(count)
    return True, ""


register_primitive("gather", "收集资源(目标+数量)", lambda a: _validate_gather(a))
register_primitive("stop", "停止当前操作", lambda a: (True, ""))
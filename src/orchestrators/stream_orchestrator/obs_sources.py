"""obs_sources.py — OBS 浏览器源登记（stream 域 · 单一事实源）

登记 OBS 直播所需的浏览器源（Live2D 模型/弹幕显示/弹幕输入/独立字幕），供智能助手
查询（stream 能力 obs:sources）与打开（obs:open）。本模块是源清单的唯一权威来源，
前端/调度官/知识注入若需源地址，一律从此读取。

# 模块内容清单 — obs_sources

## 1. 模块身份标识
- 所属调度官：stream
- 能力名：obs:sources / obs:open

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| DEFAULT_BASE | 否 | http://127.0.0.1:5000 | str | 后端基础地址（与 src/app.py run port 对齐） |

## 3. 输入契约
- manifest() -> List[Dict]：源清单（key/name/purpose/url/notes）
- get_source(key) -> Dict|None：按 key 取单个源
- resolve_key(name) -> str|None：中文/别名 → 规范化 key
- open_source(key) -> Dict：打开指定源（{ok,data,error}）

## 4. 输出契约
- 成功：源清单 dict 列表 / 打开成功的 {ok:True,data:{key,url}}
- 失败：未知 key 返回 {ok:False,error:"unknown source: ..."}；打开失败返回 {ok:False,error}

## 5. 依赖声明
- 外部服务：系统默认浏览器（打开源用，webbrowser）
- 内部模块：无
- 预先配置：后端已启动（源地址可访问）

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| unknown source | 传入不在 SOURCES 的 key | 返回 error，建议先用 obs:sources 查清单 |
| 打开失败 | webbrowser 不可用/无默认浏览器 | 返回 error，可手动复制 url 到浏览器 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| 无 | 否 | 纯数据模块，无启动/停止 |

## 8. 领域状态说明
- 状态项：无（SOURCES 为只读常量）
- 持久化：无
- 恢复：无
"""
from typing import Any, Dict, List, Optional

# 后端基础地址（与 src/app.py run port=5000 对齐；production 可改此处或改读 config）
DEFAULT_BASE = "http://127.0.0.1:5000"

# 源清单：key/name/purpose/url/notes（单一事实源）
SOURCES: List[Dict[str, Any]] = [
    {
        "key": "live2d",
        "name": "Live2D 模型（含字幕）",
        "purpose": "纯透明模型源，叠加角色说话字幕，OBS 主体",
        "url": DEFAULT_BASE + "/frontend/live2d_stream/live2d.html",
        "notes": "透明背景；Yuki 当前启用，Lilith 已临时停用",
    },
    {
        "key": "danmaku_display",
        "name": "弹幕显示",
        "purpose": "透明叠加层，显示观众弹幕 / AI 字幕 / 拦截记录",
        "url": DEFAULT_BASE + "/frontend/live2d_stream/danmaku_display.html",
        "notes": "透明背景，建议放画布右侧",
    },
    {
        "key": "danmaku_input",
        "name": "弹幕输入",
        "purpose": "透明输入框，本地/测试发送弹幕",
        "url": DEFAULT_BASE + "/frontend/live2d_stream/danmaku_input.html",
        "notes": "透明背景，建议放画布底部",
    },
    {
        "key": "subtitle_overlay",
        "name": "独立字幕源（可选）",
        "purpose": "单独的字幕叠加；默认已并入 live2d 源",
        "url": DEFAULT_BASE + "/subtitle/",
        "notes": "透明背景；仅在需要把字幕与模型分开摆放时使用",
    },
]

# 中文/别名 → 规范化 key（供 resolve_key 使用）
KEY_ALIASES: Dict[str, str] = {
    "live2d": "live2d", "l2d": "live2d", "模型": "live2d",
    "danmaku_display": "danmaku_display", "弹幕显示": "danmaku_display", "显示": "danmaku_display",
    "danmaku_input": "danmaku_input", "弹幕输入": "danmaku_input", "输入": "danmaku_input",
    "弹幕": "danmaku_input",
    "subtitle_overlay": "subtitle_overlay", "字幕": "subtitle_overlay", "独立字幕": "subtitle_overlay",
}


def manifest() -> List[Dict[str, Any]]:
    """返回源清单（防御性拷贝，避免外部改动常量）。"""
    return [dict(s) for s in SOURCES]


def get_source(key: str) -> Optional[Dict[str, Any]]:
    """按规范化 key 取单个源，未命中返回 None。"""
    for s in SOURCES:
        if s["key"] == key:
            return dict(s)
    return None


def resolve_key(name: str) -> Optional[str]:
    """中文/别名 → 规范化 key；无法识别返回 None。"""
    key = (name or "").strip().lower()
    if key in KEY_ALIASES:
        return KEY_ALIASES[key]
    if get_source(key):
        return key
    return None


def open_url(url: str) -> bool:
    """用系统默认浏览器打开 URL。"""
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def open_source(name: str) -> Dict[str, Any]:
    """打开指定源（先经 resolve_key 解析别名）。返回 {ok,data,error}。"""
    key = resolve_key(name)
    if not key:
        return {"ok": False, "data": {}, "error": f"unknown source: {name}"}
    src = get_source(key)
    ok = open_url(src["url"])
    return {"ok": ok, "data": {"key": key, "url": src["url"]},
            "error": None if ok else "打开浏览器失败"}
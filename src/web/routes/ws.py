"""routes/ws.py — /ws/events（规格书 6.5）

事件推送：订阅 EventBus 全部事件，广播给已连接前端（总控台/字幕层/Live2D）。
基于 flask_sock；载荷序列化不兼容时以 str 兜底（default=str）。

# 模块内容清单（8 项契约）
1. 模块身份标识：web.routes · ws · WS /ws/events（事件广播）+ /ws/tts_audio（音频流）
2. 配置契约：init_ws(app, event_bus) 由应用工厂注入 event_bus；TTS_CACHE_DIR=PROJECT_ROOT/data/cache/tts
3. 输入契约：/ws/events 客户端消息忽略；/ws/tts_audio 客户端 JSON {"audio_id": ...} 或裸字符串
4. 输出契约：/ws/events 广播 {"type": event, ...} JSON（default=str 兜底）；/ws/tts_audio 返回音频字节或 {"error": ...} JSON
5. 依赖声明：json、logging、flask_sock（Sock）、src.shared.config_loader
6. 错误定义：audio_id 缺失/音频不存在返回 {"error": ...}；事件序列化失败记日志并跳过；连接关闭/心跳超时静默退出
7. 生命周期方法：init_ws(app, event_bus)（注册路由 + 订阅 EventBus 全事件广播）
8. 领域状态说明：模块级 _clients 集合维护在线 WS 客户端；TTS_CACHE_DIR 音频缓存目录
"""
import json
import logging

from flask_sock import Sock

from src.shared.config_loader import PROJECT_ROOT

logger = logging.getLogger(__name__)

TTS_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "tts"

sock = Sock()
_clients = set()


def _extract_audio_id(msg) -> str:
    """解析客户端消息（JSON {"audio_id": ...} 或裸字符串）为 audio_id。"""
    try:
        data = json.loads(msg) if isinstance(msg, str) else msg
        if isinstance(data, dict):
            return data.get("audio_id", "")
    except json.JSONDecodeError:
        pass
    return ""


def init_ws(app, event_bus) -> None:
    """注册 WS 路由并订阅 EventBus 广播。"""
    sock.init_app(app)

    @sock.route("/ws/events")
    def ws_events(ws):
        _clients.add(ws)
        try:
            while True:
                ws.receive(timeout=10)  # 保持连接；收到消息则忽略
        except Exception:
            pass  # 连接关闭/心跳超时均退出循环
        finally:
            _clients.discard(ws)

    # /ws/tts_audio：音频流推送（规格书 6.5 端点清单；前端发 {"audio_id": "..."} 取音频字节）
    @sock.route("/ws/tts_audio")
    def ws_tts_audio(ws):
        try:
            while True:
                msg = ws.receive()
                audio_id = _extract_audio_id(msg)
                if not audio_id:
                    ws.send(json.dumps({"error": "audio_id 必填"}))
                    continue
                path = TTS_CACHE_DIR / audio_id
                if path.exists():
                    ws.send(path.read_bytes())
                else:
                    ws.send(json.dumps({"error": f"audio not found: {audio_id}"}))
        except Exception:
            pass  # 连接关闭退出

    if event_bus is not None:
        event_bus.subscribe("*", _broadcast)
        logger.info("[WS] 已订阅 EventBus 全事件广播")


def _broadcast(event: str, **data) -> None:
    """EventBus 回调：广播事件给所有 WS 客户端。"""
    if not _clients:
        return
    try:
        message = json.dumps({"type": event, **data}, ensure_ascii=False,
                             default=str)
    except (TypeError, ValueError) as e:
        logger.warning("[WS] 事件序列化失败: %s", e)
        return
    dead = []
    for client in list(_clients):
        try:
            client.send(message)
        except Exception:
            dead.append(client)
    for client in dead:
        _clients.discard(client)

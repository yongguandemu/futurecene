"""schedule_orchestrator.py — 日程调度官（P0 补迁：旧系统 schedule_engine）

排期管理：维护 cron 表达式定时任务（含固定间隔），后台线程周期检查，
到点发布 schedule:fired 事件（action + payload），动作由指挥官订阅后经正常命令分发链执行
（D3：调度官只负责"到点发事件"，不直接调其他调度官）。

排期来源：config.yaml schedule.jobs（预置）+ schedule:add 运行时添加（落盘 data/schedule.json）。

# 模块内容清单 — schedule_orchestrator

## 1. 模块身份标识
- 所属调度官：schedule
- 能力名：schedule:list / schedule:add / schedule:remove / schedule:status

## 2. 配置契约
| 配置项 | 必填 | 默认值 | 类型/范围 | 说明 |
|--------|------|--------|-----------|------|
| enabled | 否 | true | bool | 调度总开关 |
| jobs | 否 | [] | list | 预置排期 [{id, cron, action, payload, title?}] |
| data_file | 否 | data/schedule.json | str | 运行时排期落盘路径 |

## 3. 输入契约
- `schedule:add` payload: {id, cron, action, payload?, title?}；cron 为 5 段表达式（分 时 日 月 周），
  支持 `*` / `*/n` / `a-b` / `a,b,c` / 纯数字；action 为要触发的能力名（如 stream:start）
- `schedule:remove` payload: {id}
- `schedule:list` / `schedule:status` 无参数

## 4. 输出契约
- 成功：list/add/remove/status 返回 {ok, data, error}；到点发布 SCHEDULE_FIRED 事件
  （schedule_id / title / action / payload / fired_at）
- 失败：cron 非法 400；id 重复 409；remove 不存在 404

## 5. 依赖声明
- 外部服务：无
- 内部模块：json、logging、threading、time、datetime、pathlib、src.shared.config_loader（PROJECT_ROOT）、
  src.shared.events（SCHEDULE_FIRED）、registry

## 6. 错误定义
| 错误类型 | 触发条件 | 处理建议 |
|----------|----------|----------|
| cron 非法 | schedule:add 传非法表达式 | 返回 400，提示 5 段 cron 格式 |
| id 重复 | add 时 id 已存在 | 返回 409 |
| 不存在 | remove 时 id 不存在 | 返回 404 |

## 7. 生命周期方法
| 方法 | 必须 | 行为 |
|------|------|------|
| start | 是 | 加载预置排期 + 启动检查线程（间隔 CHECK_INTERVAL 秒） |
| stop | 是 | 停止检查线程 + 落盘排期 |

## 8. 领域状态说明
- 状态项：_jobs（排期字典）、_last_fired、_thread / _stop
- 持久化：运行时增删的排期落盘 data/schedule.json
- 恢复：start 时加载预置 + 落盘排期
"""
import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.orchestrators.schedule_orchestrator import registry
from src.shared.config_loader import PROJECT_ROOT
from src.shared.events import SCHEDULE_FIRED

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30.0          # 检查线程间隔（秒）
DEFAULT_DATA_FILE = PROJECT_ROOT / "data" / "schedule.json"


class CronExpr:
    """简易 5 段 cron 解析器（分 时 日 月 周），支持 * / */n / a-b / a,b,c / 数字。"""

    FIELDS = ("minute", "hour", "day", "month", "weekday")
    RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31),
              "month": (1, 12), "weekday": (0, 6)}  # 0=周日

    def __init__(self, expr: str):
        self._sets: Dict[str, set] = {}
        parts = str(expr).strip().split()
        if len(parts) != 5:
            raise ValueError("cron 表达式需要 5 段（分 时 日 月 周）: {}".format(expr))
        for field, part in zip(self.FIELDS, parts):
            lo, hi = self.RANGES[field]
            self._sets[field] = self._parse_field(part, lo, hi)

    @staticmethod
    def _parse_field(part: str, lo: int, hi: int) -> set:
        result: set = set()
        for token in part.split(","):
            token = token.strip()
            if token == "*":
                result.update(range(lo, hi + 1))
            elif "/" in token:
                base, step = token.split("/", 1)
                step = int(step)
                start = lo if base == "*" else int(base.split("-")[0])
                result.update(range(start, hi + 1, step))
            elif "-" in token:
                a, b = token.split("-", 1)
                result.update(range(int(a), int(b) + 1))
            else:
                result.add(int(token))
        return result

    def matches(self, t: float) -> bool:
        dt = datetime.datetime.fromtimestamp(t)
        weekday = (dt.weekday() + 1) % 7  # Python 周一=0 → cron 周日=0
        return (dt.minute in self._sets["minute"]
                and dt.hour in self._sets["hour"]
                and dt.day in self._sets["day"]
                and dt.month in self._sets["month"]
                and weekday in self._sets["weekday"])


class ScheduleOrchestrator:
    """日程调度官：cron 排期管理 + 到点发布 schedule:fired。"""

    name = "schedule"

    def __init__(self, event_bus, config: Optional[Dict[str, Any]] = None):
        self._event_bus = event_bus
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._data_file = Path(cfg.get("data_file") or DEFAULT_DATA_FILE)
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._last_fired: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False
        # 预置排期（config.yaml schedule.jobs）
        for job in cfg.get("jobs", []) or []:
            try:
                self._add_job(job)
            except Exception as e:
                logger.warning("[Schedule] 预置排期加载失败 %s: %s", job.get("id"), e)
        self._load_disk_jobs()
        registry.bind(self.handle)

    def _add_job(self, job: Dict[str, Any]) -> None:
        """内部添加：构造时使用（不落盘标记）。"""
        jid = job.get("id", "")
        cron = job.get("cron", "")
        action = job.get("action", "")
        if not jid or not cron or not action:
            raise ValueError("排期需 id/cron/action")
        CronExpr(cron)  # 校验表达式合法性
        self._jobs[jid] = {
            "id": jid, "title": job.get("title", jid),
            "cron": cron, "action": action,
            "payload": job.get("payload", {}) or {},
            "created_at": time.time(),
        }

    def _load_disk_jobs(self) -> None:
        try:
            if not self._data_file.exists():
                return
            with open(self._data_file, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            for job in jobs:
                try:
                    self._add_job(job)
                except Exception as e:
                    logger.warning("[Schedule] 落盘排期加载失败 %s: %s",
                                   job.get("id"), e)
        except Exception as e:
            logger.warning("[Schedule] 排期文件读取失败: %s", e)

    def _save_disk(self) -> None:
        try:
            self._data_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._data_file, "w", encoding="utf-8") as f:
                json.dump(list(self._jobs.values()), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[Schedule] 排期落盘失败: %s", e)

    # ---------- 能力分发 ----------

    def capabilities(self) -> List[str]:
        return registry.capabilities()

    def start(self) -> None:
        if self._started:
            return
        if not self._enabled:
            logger.info("[Schedule] enabled=false，检查线程不启动（能力可用，仅不自动触发）")
            self._started = True  # 标记已启动但无线程
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        self._started = True
        logger.info("[Schedule] 日程调度已启动（%d 条排期，检查间隔 %.0fs）",
                    len(self._jobs), CHECK_INTERVAL)

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._started = False
        self._save_disk()
        logger.info("[Schedule] 日程调度已停止")

    def _check_loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(CHECK_INTERVAL)

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            try:
                if not CronExpr(job["cron"]).matches(now):
                    continue
            except Exception:
                continue
            fired = {"schedule_id": job["id"], "title": job.get("title", ""),
                     "action": job["action"], "payload": job.get("payload", {}),
                     "fired_at": now}
            with self._lock:
                self._last_fired = fired
            self._event_bus.publish(SCHEDULE_FIRED, **fired)
            logger.info("[Schedule] 排期触发: %s → %s", job["id"], job["action"])

    async def handle(self, command: Dict[str, Any]) -> Dict[str, Any]:
        capability = command.get("capability", "")
        payload = command.get("payload", {})
        if capability == "schedule:list":
            with self._lock:
                return {"ok": True, "data": {"jobs": list(self._jobs.values())},
                        "error": None}
        if capability == "schedule:add":
            return self._h_add(payload)
        if capability == "schedule:remove":
            return self._h_remove(payload)
        if capability == "schedule:status":
            return {"ok": True,
                    "data": {"enabled": self._started,
                             "job_count": len(self._jobs),
                             "last_fired": self._last_fired},
                    "error": None}
        return {"ok": False, "data": {}, "error": f"unknown capability: {capability}"}

    def _h_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        jid = payload.get("id", "")
        cron = payload.get("cron", "")
        action = payload.get("action", "")
        if not jid or not cron or not action:
            return {"ok": False, "data": {}, "error": "id/cron/action 必填"}
        try:
            CronExpr(cron)
        except ValueError as e:
            return {"ok": False, "data": {}, "error": str(e)}
        with self._lock:
            if jid in self._jobs:
                return {"ok": False, "data": {}, "error": f"排期已存在: {jid}"}
            self._jobs[jid] = {
                "id": jid, "title": payload.get("title", jid),
                "cron": cron, "action": action,
                "payload": payload.get("payload", {}) or {},
                "created_at": time.time(),
            }
        self._save_disk()
        logger.info("[Schedule] 添加排期: %s → %s (%s)", jid, action, cron)
        return {"ok": True, "data": {"id": jid}, "error": None}

    def _h_remove(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        jid = payload.get("id", "")
        with self._lock:
            if jid not in self._jobs:
                return {"ok": False, "data": {}, "error": f"排期不存在: {jid}"}
            del self._jobs[jid]
        self._save_disk()
        logger.info("[Schedule] 移除排期: %s", jid)
        return {"ok": True, "data": {"removed": jid}, "error": None}

    def health(self) -> Dict[str, Any]:
        return {"status": "ok" if self._started else "down",
                "detail": f"jobs={len(self._jobs)}"}

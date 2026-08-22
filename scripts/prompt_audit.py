"""prompt_audit.py — System Prompt 模拟输出测试（真实 LLM，不打桩）

对审计清单中的 system prompt 构造代表性输入，经真实 LLM（llm 调度官 fast 引擎）
生成输出，校验：输出数据结构是否合规（JSON schema / 行格式）、内容是否足够详细可靠
（长度 / 角色特征 / 要素齐全）。结果写 data/audit/prompt_audit_results.json。

用法（需服务环境密钥，.env 已配置）：
  python scripts/prompt_audit.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RESULTS = []


def check(prompt_id: str, name: str, ok: bool, detail: str, extra: dict = None) -> None:
    entry = {"id": prompt_id, "name": name, "ok": bool(ok),
             "detail": detail, "ts": round(time.time(), 2)}
    if extra:
        entry["extra"] = extra
    RESULTS.append(entry)
    print(f"  [{'OK' if ok else 'XX'}] {prompt_id} {name} — {detail}")


def load_role_prompt(role: str) -> str:
    p = PROJECT_ROOT / "config" / "profiles" / role / "system_prompt.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


# ---------- LLM 客户端 ----------

async def llm_chat(payload: dict) -> str:
    """经 LLM 调度官 handle 调用真实模型，返回 reply 文本。"""
    from src.shared.config_loader import load as load_env
    from src.shared.event_bus import EventBus
    from src.orchestrators.llm_orchestrator import LLMOrchestrator
    from src.shared.config_loader import ConfigLoader

    load_env()
    bus = EventBus()
    cfg = ConfigLoader()
    orch = LLMOrchestrator(event_bus=bus, config_loader=cfg)
    orch.start()  # 创建 openai/fast/zhipu 客户端（不 start 则全链 None → 兜底回复）
    result = await orch.handle({"capability": "llm:chat", "payload": payload})
    if not result or not result.get("ok"):
        return ""
    return (result.get("data") or {}).get("reply", "") or ""


# ---------- 各 prompt 测试用例 ----------

def test_p1_p2_roles():
    """角色人设：回复是否带角色特征、长度是否合适。"""
    for role, kw in (("yuki", ["嗯", "呀", "呢", "害羞"]),
                     ("lilith", ["哼", "才不是", "别误会"])):
        sp = load_role_prompt(role)
        text = "你好呀，今天有空和我聊天吗？"
        reply = asyncio.run(llm_chat({"text": text, "system_prompt": sp,
                                      "history": [], "engine": "fast"}))
        if not reply:
            check(f"P1/P2[{role}]", "角色人设", False, "LLM 无回复")
            continue
        length_ok = 5 <= len(reply) <= 300
        char_hit = sum(1 for k in kw if k in reply)
        check(f"P1/P2[{role}]", "角色人设",
              length_ok and char_hit >= 1,
              f"回复 {len(reply)} 字，特征词命中 {char_hit}/{len(kw)}：{reply[:60]}",
              {"reply": reply})


def test_p3_p4_p5_command_router():
    """能力/身份 prompt：能正确回答系统使用问题，不虚构。"""
    from src.commander.command_router import _ASSISTANT_NOTICE, _ROLE_NOTICE
    cases = [
        ("P3/P4", _ASSISTANT_NOTICE, "怎么查看系统状态？"),
        ("P5", _ROLE_NOTICE.format(display_name="Yuki", role="yuki"),
         "介绍一下你自己"),
    ]
    for pid, sp, text in cases:
        reply = asyncio.run(llm_chat({"text": text, "system_prompt": sp,
                                      "history": [], "engine": "fast"}))
        if not reply:
            check(pid, "身份/能力", False, "LLM 无回复")
            continue
        has_real = any(k in reply for k in
                       ("状态", "调度官", "开关", "成本", "指令", "角色", "Yuki", "虚拟主播"))
        check(pid, "身份/能力", 5 <= len(reply) <= 400 and has_real,
              f"回复 {len(reply)} 字，含真实能力关键词={has_real}：{reply[:60]}",
              {"reply": reply})


def test_p6_worldbook():
    """世界书注入块：格式正确（【世界设定】+ 条目行）。"""
    from src.shared.world_book import get_world_book
    block = get_world_book().system_prompt_block("yuki")
    if not block:
        check("P6", "世界书块", False, "无条目可注入（空块）")
        return
    lines = block.splitlines()
    ok = lines[0] == "【世界设定】" and all(l.startswith("- ") for l in lines[1:])
    check("P6", "世界书块", ok,
          f"{len(lines)} 行，格式头={'OK' if ok else 'BAD'}：{block[:60]}")


def test_p7_toolblock():
    """工具清单块：调用格式说明清晰。"""
    from src.commander.tool_registry import ToolRegistry
    tr = ToolRegistry()
    block = tr.prompt_block()
    ok = block and "[[TOOL:" in block and "【可用工具】" in block
    check("P7", "工具清单块", ok,
          f"含工具调用格式={'OK' if ok else 'MISSING'}：{block[:60]}")


def test_p8_judge():
    """发言权判断：输出可解析 JSON，含 yuki/lilith/silent 三键。"""
    from src.orchestrators.collaboration.judge import _JUDGE_SYSTEM
    text = ("弹幕：今天天气真好啊\n最近对话：\nyuki: 嗯～谢谢大家来看我\n"
            "lilith: 哼，我才不是特意来的\n角色画像：\nyuki: 温柔害羞\n"
            "lilith: 高冷傲娇\n在场：['yuki', 'lilith']")
    reply = asyncio.run(llm_chat({"text": text, "system_prompt": _JUDGE_SYSTEM,
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P8", "发言权判断", False, "LLM 无回复")
        return
    m = re.search(r"\{.*\}", reply, re.S)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
    ok = parsed is not None and {"yuki", "lilith", "silent"} <= set(parsed)
    check("P8", "发言权判断", ok,
          f"JSON 解析={'OK' if parsed else 'FAIL'}：{reply[:80]}",
          {"reply": reply, "parsed": parsed})


def test_p9_awareness():
    """感知彼此：拼接块格式正确。"""
    from src.orchestrators.collaboration.context_manager import ContextManager
    ctx = ContextManager()
    prompt = ctx.build_system_prompt("yuki", "你是Yuki。",
                                     partner_lines=["lilith: 哼，今天也要好好播"])
    ok = "【感知彼此】" in prompt and "对方最近发言" in prompt and prompt.startswith("你是Yuki。")
    check("P9", "感知彼此", ok, f"拼接正确={'OK' if ok else 'BAD'}：{prompt[:60]}")


def test_p10_active_topic():
    """主动话题生成：回复是否简短、无表情符号。"""
    prompt = ("直播间有些冷场，请以yuki的身份主动发起一个轻松闲聊话题。"
              "擅长话题：音乐。回避话题：。在场搭档：lilith。"
              "对方最近发言：哼。回复控制在两句话以内，不要使用表情符号。")
    reply = asyncio.run(llm_chat({"text": prompt,
                                  "system_prompt": load_role_prompt("yuki"),
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P10", "主动话题", False, "LLM 无回复")
        return
    no_emoji = not re.search(r"[\U0001F300-\U0001FAFF]", reply)
    short = len(reply) <= 100
    check("P10", "主动话题", short and no_emoji,
          f"{len(reply)} 字，无表情={no_emoji}：{reply[:60]}",
          {"reply": reply})


def test_p11_batch_planner():
    """批量发言策划：输出 JSON 数组，字段完整。"""
    from src.orchestrators.llm_orchestrator.batch_planner import _PROMPT_TEMPLATE
    prompt = _PROMPT_TEMPLATE.format(count=2, context="直播间比较安静",
                                     role="Yuki", topics="天气；音乐")
    messages = [{"role": "system", "content": "你是严谨的结构化输出助手，只输出 JSON。"},
                {"role": "user", "content": prompt}]
    reply = asyncio.run(llm_chat({"text": messages[-1]["content"],
                                  "system_prompt": messages[0]["content"],
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P11", "批量发言", False, "LLM 无回复")
        return
    m = re.search(r"\[[\s\S]*\]", reply)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
    ok = isinstance(parsed, list) and len(parsed) >= 1 and all(
        {"text", "mood"} <= set(p) for p in parsed)
    check("P11", "批量发言", ok,
          f"JSON 数组解析={'OK' if ok else 'FAIL'}（{len(parsed) if parsed else 0} 条）：{reply[:80]}",
          {"reply": reply, "parsed": parsed})


def test_p12_memory_summarize():
    """记忆摘要：输出为纯摘要正文，不含解释与 markdown。"""
    sp = ("你是直播记忆压缩器。把输入的事件流水压缩为不超过 200 字的中文摘要，"
          "保留人物、事件、观众偏好与时间线。只输出摘要正文，不要任何解释。")
    sample = ("20:00 观众小明说想看唱歌；20:05 yuki 唱了《晴天》；"
              "20:10 观众小红送了礼物；20:15 lilith 说今天有点累；"
              "20:20 观众小明再次刷屏要求再来一首。")
    reply = asyncio.run(llm_chat({"text": sample, "system_prompt": sp,
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P12", "记忆摘要", False, "LLM 无回复")
        return
    ok = len(reply) <= 220 and not reply.startswith(("摘要", "好的", "以下"))
    check("P12", "记忆摘要", ok,
          f"{len(reply)} 字，正文风格={'OK' if ok else 'BAD'}：{reply[:60]}",
          {"reply": reply})


def test_p13_game_planner():
    """游戏操作规划：输出 JSON 数组，动作枚举合法。"""
    from src.orchestrators.game_orchestrator.game_operation_planner import _SYSTEM_PROMPT
    prompt = _SYSTEM_PROMPT + "\n\n用户指令：往前走两步然后跳\n"
    reply = asyncio.run(llm_chat({"text": prompt, "system_prompt": "",
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P13", "游戏操作规划", False, "LLM 无回复")
        return
    m = re.search(r"\[[\s\S]*\]", reply)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
    valid_actions = {"click", "move", "keypress", "hold", "release", "type", "wait"}
    ok = isinstance(parsed, list) and all(
        isinstance(a, dict) and a.get("action") in valid_actions for a in parsed)
    check("P13", "游戏操作规划", ok,
          f"JSON 数组={'OK' if ok else 'FAIL'}：{reply[:80]}",
          {"reply": reply, "parsed": parsed})


def test_p14_p15_learn_brain():
    """经验学习：操作建议/失败修正输出可解析 JSON。"""
    cases = [
        ("P14", ("当前游戏场景:survival 文本:玩家在森林。\n"
                 "建议一个游戏操作（仅输出动作名+参数JSON，可选动作:press_key/move_mouse/click，"
                 "如 press_key {\"vk\":32} 空格）。")),
        ("P15", ("操作 press_key 失败。错误: 树没倒。 状态: 玩家面对树。\n"
                 "给出修正后的动作参数 JSON（仅参数，如 {\"x\":10,\"z\":20}）。")),
    ]
    for pid, prompt in cases:
        reply = asyncio.run(llm_chat({"text": prompt, "system_prompt": "",
                                      "history": [], "engine": "fast"}))
        if not reply:
            check(pid, "经验学习", False, "LLM 无回复")
            continue
        m = re.search(r"\{.*\}", reply, re.S)
        parsed = None
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
        check(pid, "经验学习", parsed is not None,
              f"JSON 解析={'OK' if parsed else 'FAIL'}：{reply[:80]}",
              {"reply": reply, "parsed": parsed})


def test_p16_task_planner():
    """MC 任务拆解：输出可解析的子任务行。"""
    prompt = ("目标: 制作一把木剑，当前背包: 木头。\n"
              "拆解为 MC 子任务序列（每行一个: gather 物品 / craft 物品 / move_to x z），最多 6 步。")
    reply = asyncio.run(llm_chat({"text": prompt, "system_prompt": "",
                                  "history": [], "engine": "fast"}))
    if not reply:
        check("P16", "任务拆解", False, "LLM 无回复")
        return
    ok_lines = 0
    for line in reply.splitlines():
        line = line.strip()
        parts = line.split()
        if parts and parts[0] in ("gather", "craft", "move_to") and len(parts) >= 2:
            ok_lines += 1
    check("P16", "任务拆解", ok_lines >= 1,
          f"{ok_lines} 行可解析（共 {len(reply.splitlines())} 行）：{reply[:80]}",
          {"reply": reply})


def main() -> int:
    print("== System Prompt 模拟输出测试（真实 LLM）==")
    test_p1_p2_roles()
    test_p3_p4_p5_command_router()
    test_p6_worldbook()
    test_p7_toolblock()
    test_p8_judge()
    test_p9_awareness()
    test_p10_active_topic()
    test_p11_batch_planner()
    test_p12_memory_summarize()
    test_p13_game_planner()
    test_p14_p15_learn_brain()
    test_p16_task_planner()

    out = PROJECT_ROOT / "data" / "audit"
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt_audit_results.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n合计 {len(RESULTS)} 项：{passed} 通过 / {len(RESULTS) - passed} 失败")
    print(f"结果已写入 data/audit/prompt_audit_results.json")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())


(function () {
  "use strict";

  // ============ 主题 ============
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    localStorage.setItem("fs-theme", t);
  }
  function initTheme() {
    var t = localStorage.getItem("fs-theme") || "light";
    applyTheme(t);
    return t;
  }
  initTheme();
  document.getElementById("themeToggle").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "light" ? "dark" : "light");
  });

  // ============ 视图切换 ============
  var PAGE_TITLES = { assistant: "智能助手", live: "直播间", master: "系统总览", orchestrators: "调度官管理", events: "事件流", schedule: "日程设置", config: "配置管理" };
  var navItems = document.querySelectorAll(".nav-item[data-page]");
  navItems.forEach(function (item) {
    item.addEventListener("click", function () {
      navItems.forEach(function (n) { n.classList.remove("active"); });
      item.classList.add("active");
      var page = item.getAttribute("data-page");
      document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
      document.getElementById("view-" + page).classList.add("active");
      document.getElementById("topbarTitle").textContent = PAGE_TITLES[page];
      try { location.hash = page; } catch (e) {}
    });
  });

  // hash 定位：/dashboard/#assistant 加载时激活对应视图（默认智能助手）
  function initHashRoute() {
    var page = (location.hash || "").replace("#", "");
    if (!PAGE_TITLES[page]) page = "assistant";
    navItems.forEach(function (n) {
      n.classList.toggle("active", n.getAttribute("data-page") === page);
    });
    document.querySelectorAll(".view").forEach(function (v) {
      v.classList.toggle("active", v.id === "view-" + page);
    });
    document.getElementById("topbarTitle").textContent = PAGE_TITLES[page];
  }

  // ============ 智能助手常量与数据（由 assistant 迁入） ============
  const SESSION_ID = 'assistant';               // 当前会话 ID
  const HISTORY_KEY = 'fs-chat-' + SESSION_ID;  // localStorage 键（按会话隔离）
  const MAX_HISTORY = 200;                      // 历史消息上限
  const WS_MAX_DELAY = 30000;                   // 重连最大间隔（state_sync 使用）
  const TIMEOUT_COMMAND = 300000;               // 指令超时（中转站不稳定）

  // API 封装（同源；POST /api/command 等）
  const API = {
    get: function (url) {
      return fetch(url, { signal: AbortSignal.timeout(TIMEOUT_COMMAND) })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
    },
    post: function (url, body) {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(TIMEOUT_COMMAND)
      }).then(function (r) { return r.json(); });
    }
  };

  // SVG 图标库（由 assistant 迁入；dashboard 导航使用内联 SVG，本表供 data-svg 注入）
  const ICONS = {
    chat: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>',
    modules: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 5h8v8H3zm10 0h8v5h-8zM3 15h8v4H3zm10-2h8v6h-8z"/></svg>',
    calendar: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 3h-1V1h-2v2H8V1H6v2H5c-1.11 0-1.99.9-1.99 2L3 19c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11zM7 10h5v5H7z"/></svg>',
    overview: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.43 12.98c.04-.32.07-.64.07-.98s-.03-.66-.07-.98l2.11-1.65c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.3-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65C14.46 2.18 14.25 2 14 2h-4c-.25 0-.46.18-.49.42l-.38 2.65c-.61.25-1.17.59-1.69.98l-2.49-1c-.23-.09-.49 0-.61.22l-2 3.46c-.13.22-.07.49.12.64l2.11 1.65c-.04.32-.07.65-.07.98s.03.66.07.98l-2.11 1.65c-.19.15-.24.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.03.24.24.42.49.42h4c.25 0 .46-.18.49-.42l.38-2.65c.61-.25 1.17-.59 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.65zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"/></svg>'
  };

  // 事件类型映射（来自 src/shared/events.py，由 assistant 迁入）
  const EVENT_META = {
    'commander:command_received':   { label: '指令已接收',  cls: 'ev-info',  tag: '指挥官', domain: 'commander' },
    'commander:command_routed':     { label: '指令已路由',  cls: 'ev-info',  tag: '指挥官', domain: 'commander' },
    'commander:command_completed':  { label: '指令完成',    cls: 'ev-ok',    tag: '指挥官', domain: 'commander' },
    'commander:command_failed':     { label: '指令失败',    cls: 'ev-err',   tag: '指挥官', domain: 'commander' },
    'session:created':              { label: '会话创建',    cls: 'ev-info',  tag: '会话', domain: 'session' },
    'session:switched':             { label: '角色/场景切换', cls: 'ev-warn', tag: '会话', domain: 'session' },
    'session:state_changed':        { label: '会话状态变更', cls: 'ev-info',  tag: '会话', domain: 'session' },
    'switch:changed':               { label: '开关变更',    cls: 'ev-warn',  tag: '开关', domain: 'switch' },
    'state:changed':                { label: '状态快照',    cls: 'ev-info',  tag: '状态', domain: 'state' },
    'watchdog:changed':             { label: '看门狗翻转',  cls: 'ev-warn',  tag: '运维', domain: 'ops' },
    'degradation:changed':          { label: '降级状态变更', cls: 'ev-warn',  tag: '运维', domain: 'ops' },
    'cost:milestone':               { label: '成本里程碑',  cls: 'ev-warn',  tag: '成本', domain: 'cost' },
    'llm:requested':                { label: 'LLM 请求',    cls: 'ev-info',  tag: 'LLM', domain: 'llm' },
    'llm:responded':                { label: 'LLM 响应',    cls: 'ev-ok',    tag: 'LLM', domain: 'llm' },
    'llm:stream_chunk':             { label: 'LLM 流式分片', cls: 'ev-info',  tag: 'LLM', domain: 'llm' },
    'llm:failed':                   { label: 'LLM 失败',    cls: 'ev-err',   tag: 'LLM', domain: 'llm' },
    'tts:requested':                { label: 'TTS 合成请求', cls: 'ev-info',  tag: 'TTS', domain: 'tts' },
    'tts:completed':                { label: 'TTS 合成完成', cls: 'ev-ok',    tag: 'TTS', domain: 'tts' },
    'tts:failed':                   { label: 'TTS 失败',    cls: 'ev-err',   tag: 'TTS', domain: 'tts' },
    'tts:audio_ready':              { label: '音频就绪',    cls: 'ev-ok',    tag: 'TTS', domain: 'tts' },
    'live2d:loaded':                { label: '模型加载',    cls: 'ev-ok',    tag: 'Live2D', domain: 'live2d' },
    'live2d:expression_changed':    { label: '表情切换',    cls: 'ev-info',  tag: 'Live2D', domain: 'live2d' },
    'live2d:motion_triggered':      { label: '动作触发',    cls: 'ev-info',  tag: 'Live2D', domain: 'live2d' },
    'live2d:lip_sync_start':        { label: '口型同步开始', cls: 'ev-info',  tag: 'Live2D', domain: 'live2d' },
    'live2d:lip_sync_end':          { label: '口型同步结束', cls: 'ev-info',  tag: 'Live2D', domain: 'live2d' },
    'bilibili:connected':           { label: 'B站已连接',   cls: 'ev-ok',    tag: 'B站', domain: 'bilibili' },
    'bilibili:disconnected':        { label: 'B站断开',     cls: 'ev-err',   tag: 'B站', domain: 'bilibili' },
    'danmaku:received':             { label: '弹幕',        cls: 'ev-info',  tag: '弹幕', domain: 'danmaku' },
    'gift:received':                { label: '礼物',        cls: 'ev-warn',  tag: '弹幕', domain: 'danmaku' },
    'guard:received':               { label: '上舰',        cls: 'ev-warn',  tag: '弹幕', domain: 'danmaku' },
    'superchat:received':           { label: 'SuperChat',   cls: 'ev-warn',  tag: '弹幕', domain: 'danmaku' },
    'audience:entered':             { label: '观众进场',    cls: 'ev-info',  tag: '弹幕', domain: 'danmaku' },
    'audience:filtered':            { label: '观众被过滤',  cls: 'ev-err',   tag: '弹幕', domain: 'danmaku' },
    'memory:stored':                { label: '记忆写入',    cls: 'ev-info',  tag: '记忆', domain: 'memory' },
    'memory:retrieved':             { label: '记忆检索',    cls: 'ev-info',  tag: '记忆', domain: 'memory' },
    'memory:consolidated':          { label: '记忆固化',    cls: 'ev-ok',    tag: '记忆', domain: 'memory' },
    'safety:blocked':               { label: '内容拦截',    cls: 'ev-err',   tag: '安全', domain: 'safety' },
    'safety:flagged':               { label: '内容标记',    cls: 'ev-warn',  tag: '安全', domain: 'safety' },
    'game:vn_state_changed':        { label: 'VN 状态变更', cls: 'ev-info',  tag: '游戏', domain: 'game' },
    'game:mc_state_changed':        { label: 'MC 状态变更', cls: 'ev-info',  tag: '游戏', domain: 'game' },
    'game:commentary_requested':    { label: '解说请求',    cls: 'ev-info',  tag: '游戏', domain: 'game' },
    'frontend:status_update':       { label: '状态推送',    cls: 'ev-info',  tag: '前端', domain: 'frontend' },
    'frontend:subtitle_update':     { label: '字幕更新',    cls: 'ev-info',  tag: '前端', domain: 'frontend' },
    'audio:segment_ready':          { label: '音频分片就绪', cls: 'ev-ok',    tag: '音频', domain: 'audio' },
    'cost:circuit_open':            { label: '成本熔断触发', cls: 'ev-err',   tag: '成本', domain: 'cost' }
  };

  // 调度官元信息（由 assistant 迁入）
  const ORCH_META = {
    llm: { icon: 'LL', name: 'LLM 调度官', desc: '大语言模型调用' },
    tts: { icon: 'TS', name: 'TTS 调度官', desc: '语音合成' },
    live2d: { icon: 'L2', name: 'Live2D 调度官', desc: '虚拟形象驱动' },
    bilibili: { icon: 'B', name: 'B站 调度官', desc: '直播平台接入' },
    screen: { icon: 'SC', name: '屏幕控制', desc: '屏幕捕获与输入' },
    screen_control: { icon: 'SC', name: '屏幕控制', desc: '屏幕捕获与输入' },
    memory: { icon: 'ME', name: '记忆调度官', desc: '短期/长期记忆' },
    safety: { icon: 'SA', name: '安全调度官', desc: '内容审核过滤' },
    game: { icon: 'GM', name: '游戏实况', desc: '游戏状态接入' }
  };

  // 智能助手全局状态（主题由 dashboard fs-theme 统一管理，不重复定义）
  const STATE = {
    currentView: 'assistant',
    eventLogCollapsed: false,
    orchEventCount: {},   // 每个 domain 的事件计数
    pendingCommands: [],  // 等待状态回执的指令队列（FIFO + command_id 关联）
    sending: false,
    metricsBreaker: null, // 熔断器状态（/api/metrics 首屏一次拉取，快照不含该字段）
    schedules: JSON.parse(localStorage.getItem('fs-assistant-schedules') || '[]')
  };

  // 默认日程（无数据时展示；键名与 assistant 一致）
  if (STATE.schedules.length === 0) {
    STATE.schedules = [
      { time: '14:00', dur: '60min', title: '午后闲聊·Minecraft建筑', scene: 'minecraft_chat', roles: ['lumi'], status: 'upcoming' },
      { time: '20:00', dur: '120min', title: '晚间音乐分享会', scene: 'music_sharing', roles: ['lumi'], status: 'upcoming' },
      { time: '22:00', dur: '60min', title: '深夜聊天·社区回复', scene: 'community_reply', roles: ['lilith'], status: 'upcoming' }
    ];
    saveSchedules();
  }

  // 角色 / 场景配置（由 assistant 迁入）
  const ROLES = [
    { id: 'lumi', name: 'Lumi', avatar: 'Lu', color: 'lumi', tag: '温柔害羞 · 发言者' },
    { id: 'lilith', name: 'Lilith', avatar: 'Li', color: 'lilith', tag: '高冷骄傲 · 操作者' }
  ];
  const SCENES = [
    { name: 'minecraft_chat', desc: 'Minecraft 实况聊天' },
    { name: 'pure_chat', desc: '纯聊天直播' },
    { name: 'music_sharing', desc: '音乐分享' },
    { name: 'co_op_gaming', desc: '双角色协作游戏' },
    { name: 'screen_demo', desc: '屏幕操作演示' },
    { name: 'community_reply', desc: '社区互动回复' }
  ];

  // ============ 工具 ============
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(ts) {
    var d = ts ? new Date(ts) : new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }
  function toast(msg, type) {
    var el = document.createElement("div");
    el.className = "fs-toast " + (type || "info");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3200);
  }
  function setText(id, val) {
    var el = $(id);
    if (el) el.textContent = val;
  }

  // ============ Store + 同步 ============
  var wsOnline = false; // WS 连接状态（state_sync onStatusChange 更新，状态面板使用）
  var store = FSStore.createStore(FSStore.makeReducer(), FSStore.initialState());
  var sync = new FSStateSync(store, {
    onStatusChange: function (st) {
      wsOnline = (st === "online");
      var dot = $("wsDot"), t = $("wsText");
      if (st === "online") { dot.className = "status-dot green"; t.textContent = "实时连接"; }
      else if (st === "reconnecting") { dot.className = "status-dot yellow"; t.textContent = "重连中…"; }
      else if (st === "connecting") { dot.className = "status-dot gray"; t.textContent = "连接中…"; }
      else { dot.className = "status-dot red"; t.textContent = "已断开"; }
      if (st === "online") renderStatusPanel();
    }
  });

  function getState() { return store.getState(); }
  function dispatch(evt) { return store.dispatch(evt); }

  // ============ 渲染（全部由 store 快照驱动，去掉 5s 轮询） ============
  function systemStatus(wd) {
    var names = Object.keys(wd || {});
    if (!names.length) return { text: "-", cls: "" };
    var down = names.filter(function (n) { return wd[n] === "down" || wd[n] === "failed"; }).length;
    var deg = names.filter(function (n) { return wd[n] === "degraded"; }).length;
    if (down) return { text: "异常", cls: "danger" };
    if (deg) return { text: "降级", cls: "warning" };
    return { text: "正常", cls: "accent" };
  }

  function renderKpi(s) {
    var cost = s.cost || {};
    $("kpiTotal").textContent = (cost.total_cost || 0).toFixed(4);
    var byType = cost.by_type || {};
    var llm = byType.llm || {}, tts = byType.tts || {};
    $("kpiTotalSub").textContent = "LLM " + (llm.cost || 0).toFixed(4) + " · TTS " + (tts.cost || 0).toFixed(4);
    $("kpiCalls").textContent = cost.total_calls || 0;
    $("kpiCallsSub").textContent = "LLM " + (llm.calls || 0) + " · TTS " + (tts.calls || 0) + " 次";
    var st = systemStatus(s.watchdog);
    $("kpiStatus").textContent = st.text;
    $("kpiStatus").className = "kpi-value" + (st.cls ? " " + st.cls : "");
    $("masterSub").textContent = "系统状态: " + st.text + " · 快照 v" + (s.version || 0);
    renderKpiOrch(s);
  }

  function renderKpiOrch(s) {
    var names = s.orchestrators.length ? s.orchestrators : Object.keys(s.watchdog);
    var online = names.filter(function (n) { return s.watchdog[n] === "ok"; }).length;
    $("kpiOrch").textContent = online + " / " + names.length;
    $("kpiOrchSub").textContent = "共 " + names.length + " 个调度官";
    $("orchCount").textContent = names.length;
  }

  function renderOrchHealth(s) {
    var names = Object.keys(s.watchdog);
    if (!names.length) {
      $("orchHealthRow").innerHTML = '<span class="badge unknown">未注入健康数据</span>';
      return;
    }
    $("orchHealthRow").innerHTML = names.map(function (n) {
      var st = s.watchdog[n];
      var cls = st === "ok" ? "ok" : (st === "degraded" ? "degraded" : (st === "down" ? "down" : "unknown"));
      return '<span class="badge ' + cls + '">' + esc(n) + " · " + esc(st) + "</span>";
    }).join("");
  }

  function renderSession(s) {
    var session = s.session || {};
    var row = $("sessionRow");
    if (!Object.keys(session).length) {
      row.innerHTML = '<div class="muted">无会话数据</div>';
    } else {
      row.innerHTML = Object.keys(session).map(function (k) {
        return '<div class="state-row"><span class="key">' + esc(k) + '</span><span class="val">' + esc(session[k]) + "</span></div>";
      }).join("");
    }
  }

  // 系统总览扩展：功能开关 + 调度官健康（由 assistant view-overview 信息并入，见 Task 4）
  function renderMasterExtras(s) {
    var sw = s.switches || {};
    $("masterSwitches").innerHTML = Object.keys(sw).length
      ? Object.keys(sw).map(function (n) { return '<div class="state-row"><span class="key">' + esc(n) + '</span><span class="val" style="color:' + (sw[n] ? "var(--success)" : "var(--ink-dim)") + '">' + (sw[n] ? "已启用" : "已停用") + "</span></div>"; }).join("")
      : '<div class="muted">无开关数据</div>';
    var wd = s.watchdog || {};
    var wkeys = Object.keys(wd);
    $("masterWatchdog").innerHTML = wkeys.length
      ? wkeys.map(function (n) { return '<div class="state-row"><span class="key">' + esc(n) + '</span><span class="val" style="color:' + (wd[n] === "ok" ? "var(--success)" : wd[n] === "down" ? "var(--danger)" : "var(--ink-muted)") + '">' + esc(wd[n]) + "</span></div>"; }).join("")
      : '<div class="muted">无健康数据</div>';
  }

  function renderLiveBadge(s) {
    var badge = $("liveBadge");
    var live = (s.session || {}).live_mode;
    if (live === true || live === "on" || live === "live") {
      badge.textContent = "● 直播中";
      badge.className = "topbar-live-badge live";
    } else {
      badge.textContent = "未开播";
      badge.className = "topbar-live-badge";
    }
  }

  // ============ 调度官网格（开关：只发事件不预测） ============
  var ORCH_LABELS = {
    llm: "LLM 调度官", tts: "TTS 调度官", live2d: "Live2D 调度官", bilibili: "B站调度官",
    memory: "记忆调度官", safety: "安全调度官", screen: "屏幕控制调度官", game: "游戏实况调度官"
  };
  var ORCH_DESC = {
    llm: "大模型对话与回复生成", tts: "语音合成（Wusound/CosyVoice）", live2d: "Live2D 模型与口型同步",
    bilibili: "B站直播连接与弹幕", memory: "短期/长期记忆存取", safety: "敏感词与内容安全过滤",
    screen: "屏幕捕获与输入控制", game: "游戏实况与 VN 陪看"
  };

  function renderOrchGrid(s) {
    var names = s.orchestrators.length ? s.orchestrators : Object.keys(s.switches);
    if (!names.length) {
      $("orchGrid").innerHTML = '<div class="card muted">未获取到调度官列表</div>';
      return;
    }
    var sw = s.switches || {};
    $("orchGrid").innerHTML = names.map(function (n) {
      var on = sw[n] !== false;
      var health = s.watchdog[n] || "unknown";
      var hCls = health === "ok" ? "ok" : (health === "degraded" ? "degraded" : (health === "down" ? "down" : "unknown"));
      var label = ORCH_LABELS[n] || n;
      var desc = ORCH_DESC[n] || "调度官";
      return '<div class="orch-card">' +
        '<div class="orch-head"><span class="orch-name">' + esc(label) + '</span>' +
        '<label class="switch"><input type="checkbox" data-orch="' + esc(n) + '"' + (on ? " checked" : "") + '><span class="slider"></span></label></div>' +
        '<div class="orch-desc">' + esc(desc) + '</div>' +
        '<div class="orch-foot"><span class="orch-health badge ' + hCls + '">' + esc(health) + "</span>" +
        '<span class="mono dim" style="font-size:11px">' + esc(n) + "</span></div>" +
        "</div>";
    }).join("");

    document.querySelectorAll(".switch input[data-orch]").forEach(function (input) {
      input.addEventListener("change", function () {
        var name = input.getAttribute("data-orch");
        var enabled = input.checked;
        input.disabled = true;
        // 不预测：立即还原为当前 store 状态，等待 state:changed 事件到达后由 renderAll 更新
        input.checked = getState().switches[name] !== false;
        fetch("/api/switch/" + encodeURIComponent(name), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: enabled })
        }).then(function (r) { return r.json(); }).then(function (res) {
          if (!res.ok) { toast("开关操作失败: " + (res.error || ""), "error"); return; }
          if (res.command_id) {
            // 记录命令状态，等待 state:changed 到达后 UI 才更新（不预测）；
            // 经 dispatch 提交以触发订阅，且不覆盖已前进的状态（running/success）
            var cur = getState().commands[res.command_id];
            if (!cur || cur.status === "failed") {
              dispatch({ type: "commander:command_received", seq: 0, command_id: res.command_id });
            }
          }
          // 不直接改 UI！等待 state:changed 事件
        }).catch(function () { toast("网络错误", "error"); })
        .finally(function () { input.disabled = false; });
      });
    });
  }

  // ============ 事件流（store 驱动，按 seq 合并） ============
  var pageEvents = [];
  var MAX_EVENTS = 200;
  var paused = false;
  var filterText = "";

  function renderEvents() {
    var list = $("eventList");
    var filtered = pageEvents.filter(function (e) {
      if (!filterText) return true;
      return (e.type + " " + e.data).toLowerCase().indexOf(filterText.toLowerCase()) >= 0;
    });
    if (!filtered.length) {
      list.innerHTML = '<div class="events-empty">' + (pageEvents.length ? "无匹配事件" : "等待事件推送…") + "</div>";
      return;
    }
    list.innerHTML = filtered.map(function (e) {
      var cls = e.data.indexOf('"error"') >= 0 || e.data.indexOf('"ok":false') >= 0 ? "ev-err" : "ev-info";
      return '<div class="event-item ' + cls + '">' +
        '<span class="ev-time">' + e.time + "</span>" +
        '<span class="ev-type">' + esc(e.type) + "</span>" +
        '<span class="ev-data">' + esc(e.data.slice(0, 200)) + "</span></div>";
    }).join("");
  }

  function pushEventFromStore(event) {
    var dataObj = Object.assign({}, event);
    delete dataObj.type; delete dataObj.seq; delete dataObj.ts;
    var item = { time: fmtTime(event.ts), type: event.type, data: JSON.stringify(dataObj), seq: event.seq || 0 };
    // 按 seq 降序插入（事件到达可能与 seq 序不一致，以后端事件序为准）
    var idx = 0;
    while (idx < pageEvents.length && pageEvents[idx].seq > item.seq) idx++;
    pageEvents.splice(idx, 0, item);
    if (pageEvents.length > MAX_EVENTS) pageEvents.pop();
    $("eventCount").textContent = pageEvents.length;
    var badge = $("navEventCount");
    badge.style.display = pageEvents.length ? "" : "none";
    badge.textContent = pageEvents.length > 99 ? "99+" : pageEvents.length;
    if (!paused) renderEvents();
  }

  $("eventFilter").addEventListener("input", function () { filterText = this.value; renderEvents(); });
  $("clearEvents").addEventListener("click", function () {
    pageEvents = [];
    $("eventCount").textContent = 0;
    $("navEventCount").style.display = "none";
    renderEvents();
  });
  $("pauseEvents").addEventListener("click", function () {
    paused = !paused;
    this.textContent = paused ? "继续" : "暂停";
    this.classList.toggle("primary", paused);
    if (!paused) renderEvents();
  });

  // ============ 智能助手逻辑（由 assistant 迁入，store 复用 dashboard 唯一实例） ============
  // 事件日志别名：订阅回调语义化（合并到 dashboard 事件流视图）
  function appendEventLog(event) { pushEventFromStore(event); }

  const cidMap = {};      // command_id -> msgId（发送指令时建立关联）
  let streamMsgId = null; // 当前流式消息 id
  let streamBuf = '';     // 流式分片累积

  // 指令状态回执：command_id 优先精确匹配，FIFO 兜底（事件带 command_id 时由订阅回调调用）
  function updateCommandStatus(event) {
    const data = event || {};
    const type = data.type || '';
    const cid = data.command_id;
    const findPending = () => {
      if (cid && cidMap[cid]) {
        const hit = STATE.pendingCommands.find(p => p.msgId === cidMap[cid]);
        if (hit) return hit;
      }
      return null;
    };
    if (type === 'commander:command_received') {
      // peek 不消费：received 标记送达，completed 才消费队列
      const p = findPending() || STATE.pendingCommands[0];
      if (p) updateMsgStatus(p.msgId, 'delivered');
    } else if (type === 'commander:command_routed') {
      const p = findPending() || STATE.pendingCommands[0];
      if (p) updateMsgStatus(p.msgId, 'running');
    } else if (type === 'commander:command_completed') {
      const p = findPending() || STATE.pendingCommands.shift();
      if (p) {
        updateMsgStatus(p.msgId, 'replied');
        // 事件先于 HTTP 到达时，直接渲染 AI 回复（避免回复丢失）
        finalizeReply(p, data.result);
      }
    } else if (type === 'commander:command_failed') {
      const p = findPending() || STATE.pendingCommands.shift();
      if (p) updateMsgStatus(p.msgId, 'failed', 'backend');
    }
  }

  // 普通事件处理：流式分片实时渲染 + 会话/开关事件重渲染（事件日志由事件流视图统一展示）
  function handleEvent(msg) {
    if (!msg || !msg.type) return;
    const type = msg.type;
    const data = Object.assign({}, msg);
    delete data.type;

    // 每条事件携带 session_id 时，仅处理匹配当前会话的事件
    if (data.session_id !== undefined && data.session_id !== null && String(data.session_id) !== SESSION_ID) {
      return;
    }

    // —— LLM 流式：实时渲染分片（无分片字段时由 command_completed 兜底） ——
    if (type === 'llm:stream_chunk') {
      const chunkText = data.text || data.chunk || (typeof data.delta === 'string' ? data.delta : '');
      const p = STATE.pendingCommands[0];
      if (chunkText && p) {
        if (streamMsgId !== p.msgId) { streamMsgId = p.msgId; streamBuf = ''; }
        streamBuf += chunkText;
        updateStreamBubble(p.msgId, streamBuf);
      }
    } else if (type === 'llm:responded') {
      const p = STATE.pendingCommands[0];
      if (p) {
        const text = (streamMsgId === p.msgId && streamBuf) ? streamBuf : null;
        finalizeReply(p, data.result || (text ? { reply: text } : null));
      }
      streamMsgId = null;
      streamBuf = '';
    }

    // —— 会话/开关事件：store 状态已由 reducer 合并，重渲染状态面板 ——
    if (type === 'session:switched' || type === 'session:state_changed' ||
        type === 'switch:changed' || type === 'frontend:status_update') {
      renderStatusPanel();
    }
  }

  // 流式分片实时更新用户消息气泡下方的回复预览
  function appendStreamChunk(msgId, text) {
    const row = document.querySelector('.msg[data-msg-id="' + msgId + '"]');
    if (!row) return;
    let bubble = row.querySelector('.bubble .stream-preview');
    if (!bubble) {
      const b = row.querySelector('.bubble');
      if (!b) return;
      bubble = document.createElement('div');
      bubble.className = 'stream-preview';
      bubble.style.cssText = 'margin-top:6px;font-size:12px;color:var(--ink-muted);white-space:pre-wrap;word-break:break-word';
      b.appendChild(bubble);
    }
    bubble.innerHTML = esc(text) + '<span class="text-xs" style="color:var(--ink-dim)"> ▍</span>';
    scrollChatBottom(false);
  }
  function updateStreamBubble(msgId, text) { appendStreamChunk(msgId, text); }

  // —— 对话持久化（localStorage · 按会话隔离） ——
  function loadHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; } catch (e) { return []; }
  }
  function saveHistory(msgs) {
    if (msgs.length > MAX_HISTORY) msgs = msgs.slice(msgs.length - MAX_HISTORY);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(msgs));
  }
  function clearHistory() {
    localStorage.removeItem(HISTORY_KEY);
  }
  function renderHistory() {
    const box = $('chatMsgs');
    const msgs = loadHistory();
    box.innerHTML = '';
    if (msgs.length === 0) {
      box.innerHTML = '<div style="text-align:center;color:var(--ink-dim);font-size:12px;padding:40px 0">暂无对话，试试下方建议指令</div>';
      welcomeOnce();
      return;
    }
    msgs.forEach(m => renderMsg(m, false, false));
    scrollChatBottom(false);
  }
  function renderMsg(m, persist = true, doScroll = true) {
    const box = $('chatMsgs');
    if (box.querySelector('.msg-empty')) box.querySelector('.msg-empty').remove();
    const row = buildMsgRow(m);
    box.appendChild(row);
    if (doScroll) scrollChatBottom();
    if (persist) {
      const msgs = loadHistory();
      msgs.push(m);
      saveHistory(msgs);
    }
    return row;
  }
  function updateMsgStatus(msgId, status, errType) {
    const msgs = loadHistory();
    const m = msgs.find(x => x.id === msgId);
    if (m) { m.status = status; if (errType) m.errType = errType; saveHistory(msgs); }
    // 原位重建该行（避免重复追加）
    const row = document.querySelector('.msg[data-msg-id="' + msgId + '"]');
    if (row) {
      const newRow = buildMsgRow(m || { role: 'user', id: msgId, text: '', ts: Date.now(), status, errType, html: null });
      row.replaceWith(newRow);
    }
    scrollChatBottom();
  }
  function buildMsgRow(m) {
    const row = document.createElement('div');
    row.className = 'msg ' + m.role;
    row.dataset.msgId = m.id;
    const avatar = m.role === 'user' ? '我' : (m.role === 'system' ? '⚙' : 'AI');
    let html = '<div class="avatar">' + avatar + '</div><div class="bubble">';
    if (m.status === 'sending') {
      html += '<div class="typing-dots"><span></span><span></span><span></span></div>';
    } else {
      html += m.html || esc(m.text);
    }
    // 错误分类展示（含重试按钮）
    if (m.status === 'failed') {
      const errMeta = {
        network: { title: '网络错误', tip: '连接失败，请检查网络或后端地址', cls: 'err-network' },
        backend: { title: '后端错误', tip: '服务器处理失败，建议稍后重试', cls: 'err-backend' },
        param:   { title: '参数错误', tip: '输入格式有误，请检查后重试', cls: 'err-param' }
      }[m.errType] || { title: '错误', tip: '请重试', cls: 'err-backend' };
      html += '<div class="err-box ' + errMeta.cls + '"><span class="err-title">✗ ' + errMeta.title + '</span>' +
        '<span class="err-tip">' + errMeta.tip + '</span>' +
        (m.role === 'user'
          ? '<button class="btn btn-primary btn-sm" onclick="retryMsg(\'' + m.id + '\')">重试</button>'
          : '<button class="btn btn-ghost btn-sm" onclick="retryMsg(\'' + m.id + '\')">重发</button>') +
        '</div>';
    }
    // 状态标记 + 时间戳
    const statusMeta = {
      sending:   ['st-sending', '⏳ 发送中'],
      delivered: ['st-delivered', '✓ 已送达'],
      running:   ['st-delivered', '⏳ 执行中'],
      replied:   ['st-replied', '✓✓ 已回复'],
      failed:    ['st-failed', '✗ 失败']
    }[m.status] || ['st-sending', '⏳ 发送中'];
    html += '<div class="mb-meta">' +
      '<span class="mb-status ' + statusMeta[0] + '">' + statusMeta[1] + '</span>' +
      '<span class="mb-time">' + fmtTime(m.ts) + '</span></div>';
    html += '</div>';
    row.innerHTML = html;
    return row;
  }
  function scrollChatBottom(smooth = true) {
    const box = $('chatMsgs');
    box.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  }
  function clearChat() {
    clearHistory();
    const box = $('chatMsgs');
    box.innerHTML = '<div style="text-align:center;color:var(--ink-dim);font-size:12px;padding:40px 0">对话已清空</div>';
    welcomeOnce();
    toast('对话已清空（localStorage 同步清除）', 'info');
  }
  function welcomeOnce() {
    const box = $('chatMsgs');
    if (box.querySelector('.msg')) return;
    appendMsg('assistant', '你好，我是 Future Scene 智能助手。\n\n我可以帮你：\n· <b>系统总览</b>：查看开关状态、调度官健康、成本统计\n· <b>模块调试</b>：启停调度官、检查运行状态\n· <b>日程设置</b>：查看、添加、编辑直播日程\n· <b>配置管理</b>：切换角色、场景配置\n\n直接输入指令（如「查看系统状态」），或点击下方建议指令。所有指令将发送至 <span class="mono">POST /api/command</span>，由指挥官解析并路由到调度官执行。', { status: 'replied', persist: false });
  }

  // —— 对话：发送 / 重试 / 导出 / 语音 ——
  function appendMsg(role, text, opts = {}) {
    const m = {
      id: 'm' + Date.now() + Math.random().toString(36).slice(2, 7),
      role: role, text: text, ts: Date.now(),
      status: opts.status || 'replied', errType: opts.errType || null,
      html: opts.html !== undefined ? opts.html : text   // 默认将 text 作为 HTML 渲染
    };
    renderMsg(m, opts.persist !== false);
    return m;
  }
  function addMessage(role, text, opts) { return appendMsg(role, text, opts || {}); }
  // @角色 定向解析：@yuki/@lilith 前缀 → target_role（走后端仲裁 MentionRule，大小写不敏感）
  function parseTarget(text) {
    const m = /^\s*@(yuki|lilith)[\s:：]/i.exec(text);
    if (m) return { role: m[1].toLowerCase(), rest: text.slice(m[0].length) };
    return { role: '', rest: text };
  }
  function sendCommand(text, existingMsg) {
    const input = $('chatInput');
    const raw = (text !== undefined && text !== null) ? String(text) : input.value.trim();
    if (!raw || STATE.sending) return;
    if (!existingMsg) { input.value = ''; input.style.height = 'auto'; }
    // 用户消息：状态 = 发送中（重试时复用原消息 id，保留原内容）
    const m = existingMsg || { id: 'm' + Date.now() + Math.random().toString(36).slice(2, 7), role: 'user', text: raw, ts: Date.now(), status: 'sending', errType: null, html: null };
    m.text = raw;
    if (!existingMsg) {
      renderMsg(m);
    } else {
      // 重试：重置回复渲染标记，复用原消息
      const msgs = loadHistory();
      const um = msgs.find(x => x.id === m.id);
      if (um) { um.replyRendered = false; um.status = 'sending'; saveHistory(msgs); }
      updateMsgStatus(m.id, 'sending');
    }
    STATE.sending = true;
    $('chatSendBtn').disabled = true;
    STATE.pendingCommands.push({ msgId: m.id, text: raw, cid: null });

    // 超时兜底：指令超时仍未回执则标记失败
    const failTimer = setTimeout(() => {
      if (STATE.pendingCommands.some(p => p.msgId === m.id)) {
        STATE.pendingCommands = STATE.pendingCommands.filter(p => p.msgId !== m.id);
        updateMsgStatus(m.id, 'failed', 'network');
        STATE.sending = false;
        $('chatSendBtn').disabled = false;
      }
    }, TIMEOUT_COMMAND);

    // 携带最近对话历史（修复：后端多轮上下文），user 消息取纯文本，assistant 消息剥离 HTML
    const historyMsgs = loadHistory().filter(x => x.role === 'user' || x.role === 'assistant')
      .slice(-20).map(x => ({
        role: x.role === 'user' ? 'user' : 'assistant',
        content: x.role === 'user' ? String(x.text || '') : stripHtml(x.html || x.text || '')
      }));

    API.post('/api/command', (function () {
      // @角色 定向：剥离前缀进 target_role，正文发剥离后的 rest（空则回退原文）
      const target = parseTarget(raw);
      const payload = { text: target.rest.trim() || raw, session_id: SESSION_ID, history: historyMsgs };
      if (target.role) payload.target_role = target.role;
      return payload;
    })()).then(res => {
      clearTimeout(failTimer);
      STATE.sending = false;
      $('chatSendBtn').disabled = false;

      // 后端业务错误（HTTP 4xx/5xx 由 API.post 统一返回 JSON body）
      if (!res || res.ok === false) {
        STATE.pendingCommands = STATE.pendingCommands.filter(p => p.msgId !== m.id);
        updateMsgStatus(m.id, 'failed', 'backend');
        appendMsg('assistant', '<span class="mb-err">指令执行失败</span><pre>' + esc((res && res.error) || '未知错误') + '</pre>', { status: 'failed', errType: 'backend' });
        return;
      }

      const result = res || {};
      // 关联 command_id：事件回执按 cid 精确匹配消息（不预测回复）
      if (result.command_id) {
        cidMap[result.command_id] = m.id;
        STATE.pendingCommands.forEach(p => { if (p.msgId === m.id) p.cid = result.command_id; });
        // 经 store.dispatch 提交（触发订阅）；不覆盖已前进状态（running/success）
        store.dispatch({ type: 'commander:command_received', command_id: result.command_id, raw: raw });
      }
      // 事件可能已消费队列（WS 先到）；若尚未渲染回复则在此兜底
      const stillPending = STATE.pendingCommands.some(p => p.msgId === m.id);
      if (stillPending) {
        // 不消费队列：保留给 WS command_completed 按序消费（FIFO 防错位）
        updateMsgStatus(m.id, 'delivered');
      }
      // 若事件已渲染回复（replyRendered=true），HTTP 侧不再重复渲染
      const msgs = loadHistory();
      const um = msgs.find(x => x.id === m.id);
      if (um && um.replyRendered) return;
      if (um) { um.replyRendered = true; saveHistory(msgs); }
      const replyText = extractReplyText(result);
      const body = replyText !== null ? replyText : JSON.stringify(result, null, 2);
      appendMsg('assistant', '<span class="mb-ok">指令已执行</span><pre>' + esc(body.slice(0, 1500)) + '</pre>');
      // UI 状态由 store 订阅驱动（state:changed / commander:*），此处不预测
    }).catch(() => {
      // 网络错误（fetch 抛异常/超时）
      clearTimeout(failTimer);
      STATE.sending = false;
      $('chatSendBtn').disabled = false;
      STATE.pendingCommands = STATE.pendingCommands.filter(p => p.msgId !== m.id);
      updateMsgStatus(m.id, 'failed', 'network');
      appendMsg('assistant', '<span class="mb-err">网络错误</span>：连接失败，请检查网络或后端地址。<br>当前地址：<span class="mono">' + location.origin + '</span>', { status: 'failed', errType: 'network' });
    });
  }
  /* 从 command 响应/事件 result 中提取可读回复文本 */
  function extractReplyText(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    if (typeof obj.reply === 'string' && obj.reply) return obj.reply;
    if (typeof obj.text === 'string' && obj.text) return obj.text;
    if (obj.data && typeof obj.data === 'object') {
      if (typeof obj.data.reply === 'string' && obj.data.reply) return obj.data.reply;
      if (typeof obj.data.text === 'string' && obj.data.text) return obj.data.text;
    }
    return null;
  }
  /* 事件驱动：command_completed 到达时渲染 AI 回复（若尚未渲染） */
  function finalizeReply(p, result) {
    const replyText = extractReplyText(result);
    if (replyText === null) return;
    const msgs = loadHistory();
    const um = msgs.find(x => x.id === p.msgId);
    if (!um || um.replyRendered) return;
    um.replyRendered = true;
    saveHistory(msgs);
    appendMsg('assistant', '<span class="mb-ok">指令已执行</span><pre>' + esc(replyText.slice(0, 1500)) + '</pre>');
  }
  function renderReplyFromEvent(p, result) { finalizeReply(p, result); }
  function retryMsg(msgId) {
    const msgs = loadHistory();
    const m = msgs.find(x => x.id === msgId);
    if (!m) return;
    // 标记为发送中并复用原消息重发（不重复添加）
    sendCommand(m.text, m);
  }
  function stripHtml(s) {
    const div = document.createElement('div');
    div.innerHTML = s || '';
    return div.textContent || '';
  }
  function useSuggest(el) {
    $('chatInput').value = el.textContent;
    sendCommand();
  }
  /* 导出对话 */
  function exportChat() {
    const msgs = loadHistory();
    const now = new Date();
    const p = n => String(n).padStart(2, '0');
    const stamp = now.getFullYear() + p(now.getMonth() + 1) + p(now.getDate()) + '_' + p(now.getHours()) + p(now.getMinutes()) + p(now.getSeconds());
    let content;
    if (msgs.length === 0) {
      content = '暂无对话记录';
    } else {
      content = msgs.map(m => {
        const who = m.role === 'user' ? '用户' : (m.role === 'system' ? '系统' : '助手');
        return '[' + fmtTime(m.ts) + '] [' + who + '] ' + stripHtml(m.text || '');
      }).join('\n');
    }
    const blob = new Blob(['\ufeff' + content], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'chat_history_' + stamp + '.txt';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
    toast('对话已导出（' + msgs.length + ' 条）', 'success');
  }

  // —— 语音输入（Web Speech API） ——
  let SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recorder = null, micTimer = null, micActive = false;
  if (SpeechRec) {
    $('micBtn').style.display = 'flex';
    recorder = new SpeechRec();
    recorder.lang = 'zh-CN';
    recorder.continuous = true;
    recorder.interimResults = false;
    recorder.maxAlternatives = 5;
    recorder.onresult = (e) => {
      // 按置信度排序取最高结果
      let best = null;
      for (let i = e.resultIndex; i < e.results.length; i++) {
        for (let j = 0; j < e.results[i].length; j++) {
          const alt = e.results[i][j];
          if (!best || alt.confidence > best.confidence) best = alt;
        }
      }
      if (best && best.transcript) {
        $('chatInput').value = best.transcript.trim();
        // 识别结果自动填入并发送（不额外等待手动点击）
        sendCommand();
        stopMic();
      }
      resetMicTimer();
    };
    recorder.onend = () => { stopMic(); };
    recorder.onerror = (e) => {
      stopMic();
      if (e.error !== 'aborted') toast('语音识别错误：' + e.error, 'warning');
    };
  }
  function resetMicTimer() {
    clearTimeout(micTimer);
    micTimer = setTimeout(() => { if (micActive) stopMic(); }, 5000); // 5 秒无输入自动结束
  }
  function toggleMic() {
    if (!recorder) return;
    if (micActive) { stopMic(); return; }
    micActive = true;
    $('micBtn').classList.add('listening');
    try { recorder.start(); resetMicTimer(); toast('正在聆听…（再次点击或 5 秒无输入自动结束）', 'info'); }
    catch (e) { stopMic(); }
  }
  function stopMic() {
    micActive = false;
    clearTimeout(micTimer);
    $('micBtn').classList.remove('listening');
    try { recorder.stop(); } catch (e) {}
  }

  // —— 状态面板（数据源：store 快照） ——
  // 多角色在场展示：角色名映射（store.characters 键为后端角色 id）
  const CHAR_ROLE_NAMES = { yuki: 'Yuki', lilith: 'Lilith', lumi: 'Lumi' };
  function renderCharacters(s) {
    s = s || store.getState();
    const chars = s.characters || {};
    const el = $('sideCharacters');
    if (!el) return;
    const keys = Object.keys(chars);
    if (!keys.length) { el.innerHTML = '<div class="muted">无在场角色</div>'; return; }
    el.innerHTML = keys.map(function (role) {
      const c = chars[role] || {};
      const speaking = !!c.speaking;
      const name = CHAR_ROLE_NAMES[role] || role;
      return '<div class="sp-session-row"' + (speaking ? ' style="background:var(--success-bg);border-radius:var(--radius-sm);padding-left:6px"' : '') + '>' +
        '<span class="k">' + esc(name) + '</span>' +
        '<span class="v"' + (speaking ? ' style="color:var(--accent);font-weight:700"' : '') + '>' +
        (speaking ? '● 说话中' : '在场') + '</span></div>';
    }).join('');
  }
  function renderStatusPanel(s) {
    s = s || store.getState();
    const session = s.session || {};
    const conn = wsOnline ? 'WS 在线' : ((s.version || 0) > 0 ? 'HTTP 在线' : '离线');
    // 右侧状态面板（系统状态卡片）
    const sb = $('sideSession');
    if (sb) {
      sb.innerHTML =
        '<div class="sp-session-row"><span class="k">连接</span><span class="v">' + esc(conn) + '</span></div>' +
        '<div class="sp-session-row"><span class="k">当前角色</span><span class="v">' + esc(session.role || '—') + '</span></div>' +
        '<div class="sp-session-row"><span class="k">场景</span><span class="v">' + esc(session.scene || '—') + '</span></div>' +
        '<div class="sp-session-row"><span class="k">会话 ID</span><span class="v">' + esc(SESSION_ID) + '</span></div>';
    }
    // 在场角色（characters 段：在场 + speaking 高亮）
    renderCharacters(s);
    // 顶栏角色/场景
    renderTopbar(s);
    renderCostCards();
  }
  function renderCostCards() {
    const s = store.getState();
    const cost = s.cost || {};
    const breaker = STATE.metricsBreaker || {};
    const todayCost = cost.today_cost !== undefined ? cost.today_cost : cost.total_cost;
    const tripped = breaker.tripped || breaker.state === 'open';
    const healthy = (s.orchestrators || []).filter(n => (s.watchdog[n] || 'unknown') === 'ok').length;
    const el = $('sideCost');
    if (el) {
      el.innerHTML =
        '<div class="sp-cost">' +
        '<div class="cost-item"><div class="ci-label">今日成本</div><div class="ci-value">' + (todayCost !== undefined ? '¥' + Number(todayCost).toFixed(4) : '—') + '</div></div>' +
        '<div class="cost-item"><div class="ci-label">API 调用</div><div class="ci-value">' + (cost.total_calls !== undefined ? String(cost.total_calls) : '—') + '</div></div>' +
        '<div class="cost-item"><div class="ci-label">熔断器</div><div class="ci-value ' + (tripped ? 'bad' : '') + '">' + (tripped ? '已熔断' : '正常') + '</div></div>' +
        '<div class="cost-item"><div class="ci-label">健康调度官</div><div class="ci-value">' + healthy + '/' + (s.orchestrators || []).length + '</div></div>' +
        '</div>';
    }
  }
  function renderTopbar(s) {
    s = s || store.getState();
    const session = s.session || {};
    setText('topbarRole', session.role || '—');
    setText('topbarScene', session.scene || '—');
  }
  /* 状态同步：由 store 驱动（去掉 /api/state、/api/metrics 轮询） */
  function refreshState() {
    renderStatusPanel();
    return Promise.resolve();
  }
  function refreshMetrics() {
    // 仅首屏/手动拉取一次：快照不含 circuit_breaker，熔断器状态单独取
    return API.get('/api/metrics').then(function (data) {
      STATE.metricsBreaker = (data && data.circuit_breaker) || null;
      renderCostCards();
      renderMasterExtras(store.getState());
    }).catch(function () { /* 熔断器拉取失败不影响主流程 */ });
  }
  function verifyStateSync() {
    const v = (store.getState() || {}).version || 0;
    return { ok: v > 0, version: v };
  }
  function toggleEventLog() {
    const el = $('eventLog');
    if (!el) return; // 事件日志已并入"事件流"视图，此处保持兼容空实现
    STATE.eventLogCollapsed = !STATE.eventLogCollapsed;
    el.classList.toggle('collapsed', STATE.eventLogCollapsed);
  }
  /* 视图跳转（assistant 页名 → dashboard 视图） */
  function navTo(page) {
    const map = { chat: 'assistant', modules: 'orchestrators', overview: 'master' };
    const p = map[page] || page;
    if (!PAGE_TITLES[p]) return;
    if (!document.getElementById('view-' + p)) return;
    navItems.forEach(n => { n.classList.toggle('active', n.getAttribute('data-page') === p); });
    document.querySelectorAll('.view').forEach(v => { v.classList.toggle('active', v.id === 'view-' + p); });
    document.getElementById('topbarTitle').textContent = PAGE_TITLES[p];
    try { location.hash = p; } catch (e) {}
    if (p === 'schedule') renderSchedule();
    if (p === 'config') { renderRoles(); renderScenes(); }
  }
  function sendCommandText(text) {
    $('chatInput').value = text;
    navTo('chat');
    sendCommand();
  }

  // —— 日程 ——
  function loadSchedule() {
    try { STATE.schedules = JSON.parse(localStorage.getItem('fs-assistant-schedules') || '[]'); }
    catch (e) { STATE.schedules = []; }
    return STATE.schedules;
  }
  function saveSchedules() {
    localStorage.setItem('fs-assistant-schedules', JSON.stringify(STATE.schedules));
  }
  function renderSchedule() {
    setText('scheduleCount', '共 ' + STATE.schedules.length + ' 项排期');
    const list = $('scheduleList');
    if (STATE.schedules.length === 0) {
      list.innerHTML = '<div class="text-muted text-sm" style="text-align:center;padding:30px">暂无排期，点击右上角「添加排期」</div>';
      return;
    }
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    list.innerHTML = STATE.schedules.map((s, i) => {
      const [h, m] = String(s.time).split(':').map(Number);
      const isCurrent = !isNaN(h) && h * 60 + (m || 0) <= nowMin;
      const roleDots = (s.roles || []).map(r =>
        '<span class="role-dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
        (r === 'lumi' ? 'var(--lumi-color)' : 'var(--lilith-color)') + '"></span>').join('');
      return '<div class="timeline-item' + (isCurrent ? ' current' : '') + '">' +
        '<div class="tl-time">' + s.time + '<span class="dur">' + s.dur + '</span></div>' +
        '<div class="tl-body"><div class="tl-title">' + esc(s.title) + '</div>' +
        '<div class="tl-meta"><span class="tag tag-scene">' + esc(s.scene) + '</span>' +
        '<span class="tl-roles">' + roleDots + '</span>' +
        (isCurrent ? '<span class="badge badge-red">已到时间</span>' : '<span class="badge badge-yellow">待开始</span>') +
        '</div></div>' +
        '<div class="flex gap-8">' +
        '<button class="btn btn-ghost btn-sm" onclick="editSchedule(' + i + ')">编辑</button>' +
        '<button class="btn btn-ghost btn-sm" onclick="removeSchedule(' + i + ')">删除</button>' +
        '</div></div>';
    }).join('');
  }
  function showScheduleModal() {
    $('schedTime').value = '';
    $('schedDur').value = '60';
    $('schedTitle').value = '';
    $('schedScene').value = 'pure_chat';
    $('schedRoleLumi').checked = true;
    $('schedRoleLilith').checked = false;
    STATE._editIdx = null;
    const btn = $('schedSubmitBtn');
    btn.textContent = '提交指令';
    btn.onclick = submitSchedule;
    updateSchedPreview();
    $('scheduleModal').classList.add('show');
  }
  function closeScheduleModal() { $('scheduleModal').classList.remove('show'); }
  function updateSchedPreview() {
    const time = $('schedTime').value || '20:00';
    const dur = $('schedDur').value || '60';
    const title = $('schedTitle').value || '直播日程';
    const scene = $('schedScene').value;
    $('schedPreview').textContent = '安排' + time + '的' + title + '（场景：' + scene + '，时长' + dur + '分钟）';
  }
  ['schedTime', 'schedDur', 'schedTitle', 'schedScene'].forEach(id => {
    $(id).addEventListener('input', updateSchedPreview);
  });
  function submitSchedule() {
    const time = $('schedTime').value.trim();
    const dur = $('schedDur').value || '60';
    const title = $('schedTitle').value.trim();
    const scene = $('schedScene').value;
    const roles = [];
    if ($('schedRoleLumi').checked) roles.push('lumi');
    if ($('schedRoleLilith').checked) roles.push('lilith');
    if (!time || !title) { toast('请填写时间和标题', 'warning'); return; }
    if (roles.length === 0) { toast('请至少选择一个角色', 'warning'); return; }
    STATE.schedules.push({ time, dur: dur + 'min', title, scene, roles, status: 'upcoming' });
    saveSchedules();
    renderSchedule();
    closeScheduleModal();
    const cmd = '安排' + time + '的' + title + '（场景：' + scene + '，时长' + dur + '分钟，角色：' + roles.join('和') + '）';
    $('chatInput').value = cmd;
    navTo('chat');
    sendCommand();
    toast('已添加排期并生成指令', 'success');
  }
  function editSchedule(idx) {
    const s = STATE.schedules[idx];
    if (!s) return;
    $('schedTime').value = s.time;
    $('schedDur').value = parseInt(s.dur) || 60;
    $('schedTitle').value = s.title;
    $('schedScene').value = s.scene;
    $('schedRoleLumi').checked = (s.roles || []).includes('lumi');
    $('schedRoleLilith').checked = (s.roles || []).includes('lilith');
    STATE._editIdx = idx;
    updateSchedPreview();
    $('scheduleModal').classList.add('show');
    const btn = $('schedSubmitBtn');
    btn.textContent = '保存修改';
    btn.onclick = function () {
      const t = $('schedTime').value.trim();
      const title = $('schedTitle').value.trim();
      if (!t || !title) { toast('请填写时间和标题', 'warning'); return; }
      const roles = [];
      if ($('schedRoleLumi').checked) roles.push('lumi');
      if ($('schedRoleLilith').checked) roles.push('lilith');
      STATE.schedules[STATE._editIdx] = { time: t, dur: $('schedDur').value + 'min', title, scene: $('schedScene').value, roles, status: 'upcoming' };
      saveSchedules(); renderSchedule(); closeScheduleModal();
      toast('排期已更新', 'success');
    };
  }
  function removeSchedule(idx) {
    STATE.schedules.splice(idx, 1);
    saveSchedules();
    renderSchedule();
    toast('已删除排期', 'info');
  }

  // —— 配置管理 ——
  function renderRoles() {
    const s = store.getState();
    const curRole = (s.session && s.session.role) || 'lumi';
    $('roleGrid').innerHTML = ROLES.map(r => {
      const active = r.id === curRole;
      return '<div class="role-card">' +
        '<div class="rc-head"><div class="rc-avatar ' + r.color + '">' + r.avatar + '</div>' +
        '<div class="rc-info"><div class="rc-name">' + r.name + '</div><div class="rc-tag">' + r.tag + '</div></div>' +
        (active ? '<span class="badge badge-green">当前角色</span>' : '') + '</div>' +
        '<div class="rc-body flex between center">' +
        '<span class="text-xs text-muted">切换为 ' + r.name + ' 发言</span>' +
        '<button class="btn btn-primary btn-sm" ' + (active ? 'disabled' : '') + ' onclick="switchRole(\'' + r.id + '\')">切换</button>' +
        '</div></div>';
    }).join('');
  }
  function renderScenes() {
    const s = store.getState();
    const curScene = (s.session && s.session.scene) || '';
    $('sceneGrid').innerHTML = SCENES.map(sc =>
      '<div class="scene-card' + (sc.name === curScene ? ' selected' : '') + '" onclick="selectScene(this, \'' + sc.name + '\')">' +
      '<div class="sc-name">' + sc.name + '</div><div class="sc-desc">' + sc.desc + '</div></div>'
    ).join('');
  }
  function switchRole(roleId) {
    const role = ROLES.find(r => r.id === roleId);
    sendCommandText('切换到角色 ' + role.name + '（' + roleId + '）');
  }
  function selectScene(el, name) {
    document.querySelectorAll('#sceneGrid .scene-card').forEach(c => c.classList.remove('selected'));
    el.classList.add('selected');
    sendCommandText('将直播场景切换为 ' + name);
  }

  // —— 键盘快捷键 + 输入框自适应 ——
  document.addEventListener('keydown', e => {
    const input = $('chatInput');
    if (!input) return;
    const typingInModal = document.querySelector('.modal-overlay.show');
    if (typingInModal) return;
    // Ctrl+Enter 发送（兼容 Enter 发送）
    if (e.key === 'Enter') {
      if (e.ctrlKey || e.metaKey) { e.preventDefault(); sendCommand(); return; }
      if (!e.shiftKey && document.activeElement === input) { e.preventDefault(); sendCommand(); }
      return;
    }
    // Esc 清空输入框
    if (e.key === 'Escape' && document.activeElement === input) {
      e.preventDefault();
      input.value = '';
      input.style.height = 'auto';
    }
  });
  $('chatInput').addEventListener('input', e => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
  });

  // —— 时钟 ——
  function updateClock() { setText('topbarClock', fmtTime()); }

  // ============ 订阅 ============
  function renderAll(s) {
    s = s || getState();
    renderKpi(s);
    renderOrchHealth(s);
    renderSession(s);
    renderOrchGrid(s);
    renderLiveBadge(s);
    renderMasterExtras(s);
  }

  // ============ 启动（统一初始化：hash 定位 + 对话/日程/配置恢复 + 单一 store 订阅） ============
  initTheme();
  initHashRoute();
  renderHistory();            // 从 localStorage 恢复对话
  renderSchedule();
  renderRoles();
  renderScenes();
  FSStore.restoreFromSession(store, 'fs-dash-state'); // 恢复上次状态面板（可被事件流覆盖，dashboard 既有逻辑保留）
  FSStore.persistOnUnload(store, 'fs-dash-state');    // 刷新时持久化状态面板（dashboard 既有逻辑保留）
  sync.init();                // 首屏拉取 /api/state → 建 WS；断线/重连自动补数
  store.subscribe('*', function (state, event) {
    if (event.type === 'state:changed') { renderAll(state); renderStatusPanel(state); renderTopbar(state); return; }
    if (event.type === 'switch:changed') { renderAll(state); return; }
    // 多角色说话状态：speech 事件即时刷新在场面板（不等下一次 state:changed）
    if (event.type === 'speech:arbitrated' || event.type === 'speech:completed') renderCharacters(state);
    if (event.command_id) updateCommandStatus(event);
    appendEventLog(event);
    handleEvent(event);
  });
  setInterval(updateClock, 1000);

  // 内联 onclick 需全局可见（assistant 原为顶层函数；合并进 IIFE 后经 window 暴露）
  window.exportChat = exportChat;
  window.clearChat = clearChat;
  window.useSuggest = useSuggest;
  window.toggleMic = toggleMic;
  window.sendCommand = sendCommand;
  window.refreshState = refreshState;
  window.refreshMetrics = refreshMetrics;
  window.showScheduleModal = showScheduleModal;
  window.closeScheduleModal = closeScheduleModal;
  window.submitSchedule = submitSchedule;
  window.editSchedule = editSchedule;
  window.removeSchedule = removeSchedule;
  window.retryMsg = retryMsg;
  window.switchRole = switchRole;
  window.selectScene = selectScene;
})();

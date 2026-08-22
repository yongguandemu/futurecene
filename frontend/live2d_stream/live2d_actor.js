/**
 * live2d_actor.js — Live2D 角色加载与事件驱动核心（被 index.html / live2d.html 复用）
 *
 * 职责：加载 PIXI + Cubism + 连续动画驱动，订阅 live2d:/tts:/audio:/speech:/session:/
 * danmaku:/audience:/frontend: 事件，驱动口型/表情/动作/说话高亮/角色切换/视线互动。
 * 不含弹幕显示与输入 UI（由独立 OBS 源 danmaku_display.html / danmaku_input.html 提供）。
 *
 * 依赖页面元素（均可选，缺失自动跳过）：
 *   #hint          模型加载提示（live2d.html 有）
 *   #danmu-list    弹幕列表（仅 index.html 组合预览时存在；模型源无 → pushDanmu 跳过）
 *   #danmuInput    弹幕输入（同上，仅组合预览）
 *   #danmuSend     弹幕发送（同上）
 */
(function () {
  "use strict";

  // 角色 → 模型配置（与 config.yaml roles 对应；无后端时前端兜底）
  // 受限人设：restrictBreath=true 抑制呼吸幅度/频率（刚被唤醒的 AI 实习生，双角色统一）
  var ROLES = window.__ROLES || [
    { name: "yuki", model: "Hiyori", restrictBreath: true, idleSilent: "Hiyori_m03",
      x: 0.25,
      expressionMap: {}, motionMap: { wave: "Hiyori_m01", nod: "Hiyori_m02",
        shake: "Hiyori_m05", idle: "Hiyori_m01" } },
    { name: "lilith", model: "Haru", restrictBreath: true, idleSilent: "idle",
      x: 0.55,
      expressionMap: { 开心: "唱歌", 难过: "流泪", 惊讶: "头发", 害羞: "嘟嘴", 生气: "脸黑", 平静: "头发" },
      motionMap: { wave: "wave", nod: "nod", shake: "shake", idle: "idle" } }
  ];
  // 临时停用角色开关：从这里加角色名即不加载（启动装载 + 角色切换联动均跳过）；改回空对象即恢复
  var DISABLED_ROLES = { lilith: true };
  function isRoleEnabled(role) { return !DISABLED_ROLES[role]; }

  var hint = document.getElementById("hint") || { textContent: "", style: { display: "none" } };
  var app = null;
  var actors = {};   // role -> { model, driver, state: 'silent'|'talking', timer, lastAudio, lipTimer }
  var activeRole = "yuki";   // 当前聚焦角色（弹幕互动/视线转向对象）

  var danmuList = document.getElementById("danmu-list");
  var MAX_DANMU = 50;

  // ===== 驱动库接入：语义名 → 驱动参数映射（连续动画系统） =====
  var MOTION_ALIAS = {
    wave:'wave', 招手:'wave', 挥手:'wave', nod:'nod', 点头:'nod',
    shake:'shake', 摇头:'shake', tilt:'tilt', 歪头:'tilt',
    bow:'bow', 鞠躬:'bow', clap:'clap', 拍手:'clap',
    bounce:'bounce', 跳动:'bounce', look_around:'look_around', 环顾:'look_around',
    hold_microphone:'hold_microphone', 拿麦:'hold_microphone',
    raise_hand:'raise_hand', 举手:'raise_hand',
    fold_arms:'fold_arms', 抱臂:'fold_arms'
  };
  function mapMotion(semantic) {
    if (!semantic) return null;
    return MOTION_ALIAS[semantic] || MOTION_ALIAS[String(semantic).toLowerCase()] || null;
  }
  // 中文情绪名 → 驱动 5D 状态（复用驱动库内置 EXPRESSION_STATE_MAP）
  function buildExpressionMap() {
    var base = (window.Live2DDriver && window.Live2DDriver.EXPRESSION_STATE_MAP) || {};
    var alias = { 开心:'happy', 快乐:'happy', 高兴:'happy', 难过:'sad', 伤心:'sad',
      悲伤:'sad', 惊讶:'surprised', 吃惊:'surprised', 震惊:'surprised',
      害羞:'shy', 羞涩:'shy', 生气:'angry', 愤怒:'angry',
      平静:'calm', 冷静:'calm', 思考:'thinking', 沉思:'thinking',
      好奇:'curious', 兴奋:'excited', 中性:'neutral', 默认:'neutral' };
    var map = {};
    for (var k in alias) { if (base[alias[k]]) map[k] = base[alias[k]]; }
    return map;
  }
  // 为模型创建连续动画驱动（30Hz 参数注入），失败则回退到旧离散触发
  function makeDriver(modelName, model) {
    if (!window.Live2DDriver || !window.Live2DDriver.Live2DDriver) return null;
    try {
      var cfg = ROLES.find(function (r) { return r.model === modelName; });
      var driver = new window.Live2DDriver.Live2DDriver({
        model: model, characterId: modelName,
        restrictBreath: !!(cfg && cfg.restrictBreath),   // 受限角色：抑制呼吸幅度/频率
        expressionMap: buildExpressionMap()
      });
      driver.start();
      return driver;
    } catch (e) { return null; }
  }
  // 说话口型：正弦波调制 mouth + 能量反馈，驱动说话头部微动（真音频接入后替换为能量分析）
  function startLipDriver(a) {
    stopLipDriver(a);
    var t0 = performance.now();
    a.lipTimer = setInterval(function () {
      if (!a.driver || a.state !== "talking") { clearInterval(a.lipTimer); a.lipTimer = null; return; }
      var t = (performance.now() - t0) / 1000;
      var v = 0.25 + 0.35 * Math.abs(Math.sin(t * 6));
      a.driver.setLipSync(v);
      a.driver.updateAudioEnergy(0.3 + 0.3 * Math.abs(Math.sin(t * 6)));
    }, 45);
  }
  function stopLipDriver(a) {
    if (a && a.lipTimer) { clearInterval(a.lipTimer); a.lipTimer = null; }
  }
  // 弹幕互动：重置空闲计时 + 当前角色视线转向弹幕
  function onViewerMessage() {
    var a = actors[activeRole];
    if (a && a.driver) { a.driver.onInteraction(); a.driver.setGazeTarget("danmaku"); }
  }

  function showHint(text) { hint.textContent = text; hint.style.display = "block"; }

  function mappedMotion(role, semantic) {
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg || !cfg.motionMap) return null;
    return cfg.motionMap[semantic] || cfg.motionMap.idle || null;
  }

  function mappedExpression(role, semantic) {
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg || !cfg.expressionMap) return null;
    return cfg.expressionMap[semantic] || null;   // 映射缺失 → null（跳过）
  }

  function playMotion(role, motion) {
    var a = actors[role];
    if (!a || !a.model || !motion) return;
    try { a.model.motion(motion); } catch (e) { /* ignore */ }
  }

  function setState(role, state) {
    var a = actors[role];
    if (!a) return;
    if (a.state === state) return;
    a.state = state;
    if (a.driver) {
      // 驱动库接管：静默态由驱动循环自驱（呼吸/视线/空闲行为），不再播离散模型动作
      if (state === "silent") a.driver.setSpeaking(false, 0);
      else if (state === "talking") activeRole = role;
      return;
    }
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (state === "silent") {
      var idle = cfg && cfg.restrictBreath
        ? (cfg.idleSilent || (cfg.motionMap && cfg.motionMap.idle))
        : (cfg && cfg.motionMap ? cfg.motionMap.idle : null);
      playMotion(role, idle);           // 呼吸压制：只播低动作 idle
    }
  }

  function stopLipSync(role) {
    var a = actors[role];
    if (!a || !a.model) return;
    if (a.driver) {
      stopLipDriver(a);
      a.driver.setSpeaking(false, 0);
      a.driver.setLipSync(0);
      return;
    }
    try {
      a.model.internalModel.motionManager.update(0, 0);
    } catch (e) { /* ignore */ }
  }

  function clearLipTimer(role) {
    var a = actors[role];
    if (a && a.timer) { clearTimeout(a.timer); a.timer = null; }
  }

  function endLipSync(role) {
    stopLipSync(role);
    setState(role, "silent");
  }

  // ---- 说话高亮（M2）：speech:arbitrated(role) 点亮，speech:completed 熄灭 ----
  function setSpeakingHighlight(role, on) {
    var a = actors[role];
    if (!a) return;
    if (a.speaking === !!on) return;
    a.speaking = !!on;
    var holder = document.getElementById("canvas-holder");
    if (!holder) return;
    if (on) {
      holder.classList.add("actor-speaking");
      holder.setAttribute("data-speaking", role);
    } else {
      holder.classList.remove("actor-speaking");
      holder.removeAttribute("data-speaking");
    }
  }

  // ---- 口型统一入口（per-role 收尾 timer + 按 audio_id 去重） ----
  // tts:audio_ready / live2d:lip_sync_start / audio:segment_ready 与 speech:completed
  // 兜底均经此处理：同一音频段仅启动一次口型；新段到达先取消旧 timer，避免截断。
  function handleLipSync(role, audioId, dur) {
    var a = actors[role];
    if (!a || !a.model || !audioId) return;
    if (a.lastAudio === audioId) return;          // 去重：双路/兜底同段只处理一次
    clearLipTimer(role);                          // 取消本角色旧段收尾 timer
    a.lastAudio = audioId;
    // 其他角色收尾：口型归零 + 回 idle + 取消其待触发 timer
    Object.keys(actors).forEach(function (r) {
      if (r !== role) { clearLipTimer(r); stopLipSync(r); setState(r, "silent"); }
    });
    setState(role, "talking");
    if (a.driver) {
      a.driver.setSpeaking(true, 0.8);
      startLipDriver(a);
    } else {
      try {
        a.model.internalModel.motionManager.update(0, 0);
      } catch (e) { /* ignore */ }
    }
    var ms = dur || 1500;
    a.timer = setTimeout(function () {
      a.timer = null;
      if (a.lastAudio === audioId) endLipSync(role);   // 仍是当前段才收尾
    }, ms);
  }

  // ---- store 事件处理（按 role 路由） ----
  function handleEvent(type, ev) {
    var role = ev.role || "yuki";
    var a = actors[role];
    if (!a || !a.model) return;
    if (type === "live2d:motion_triggered") {
      if (a.driver) {
        var m = mapMotion(ev.motion);
        if (m) a.driver.playMotion(m, ev.intensity || 1.0);
      } else {
        playMotion(role, mappedMotion(role, ev.motion));
      }
    } else if (type === "live2d:expression_changed") {
      if (a.driver) {
        if (ev.expression) a.driver.setEmotion(ev.expression, ev.intensity || 1.0);
      } else {
        var expr = mappedExpression(role, ev.expression);
        if (expr) { try { a.model.expression(expr); } catch (e) { /* ignore */ } }
      }
    } else if (type === "live2d:lip_sync_start") {
      handleLipSync(role, ev.audio_id, ev.duration_ms);
    } else if (type === "live2d:lip_sync_end") {
      // 权威结束事件（后端按时长触发，带 role/audio_id）：仅当前段结束时收尾
      if (a.lastAudio === ev.audio_id) { clearLipTimer(role); endLipSync(role); }
    } else if (type === "live2d:params_batch") {
      // 任务三：批量参数帧（10Hz）→ 触发前端 30fps 插值
      onParamsBatch(ev.params);
    }
  }

  // ---- 任务三：live2d:params_batch 参数帧消费 + 30fps 插值 ----
  // 后端 10Hz 聚合帧 → 前端按 100ms 线性插值到渲染循环，避免参数跳变。
  var _targetParams = {};   // 最近一帧目标参数
  var _interpParams = {};   // 插值中的参数
  var _paramLerp = 1.0;     // 0..1 插值进度（>=1 表示已完成）

  function onParamsBatch(params) {
    if (!params) return;
    _targetParams = Object.assign({}, params);
    _paramLerp = 0.0;
  }

  function applyInterpolatedParams(dt) {
    if (_paramLerp >= 1.0) return;
    _paramLerp = Math.min(1.0, _paramLerp + (dt || 0.016) * 10.0);
    for (var pid in _targetParams) {
      var target = _targetParams[pid];
      var prev = _interpParams[pid] !== undefined ? _interpParams[pid] : target;
      var value = prev + (target - prev) * _paramLerp;
      _interpParams[pid] = value;
      Object.keys(actors).forEach(function (r) {
        var a = actors[r];
        if (a && a.model && a.model.internalModel && a.model.internalModel.coreModel) {
          try {
            a.model.internalModel.coreModel.setParameterValueById(pid, value);
          } catch (e) { /* 参数不存在时静默 */ }
        }
      });
    }
    if (_paramLerp >= 1.0) { _interpParams = Object.assign({}, _targetParams); }
  }

  function pushDanmu(user, text, cls) {
    if (!danmuList) return;   // 纯模型源无弹幕列表：跳过显示，仅触发视线互动
    var el = document.createElement("div");
    el.className = "dm-item" + (cls ? " " + cls : "");
    el.innerHTML = '<span class="dm-user">' + escHtml(user) + '</span>' + escHtml(text);
    danmuList.appendChild(el);
    while (danmuList.children.length > MAX_DANMU) {
      danmuList.removeChild(danmuList.firstChild);
    }
  }
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function wireStore() {
    if (!window.FSStore || !window.FSStateSync) return;
    var store = window.FSStore.createStore(window.FSStore.makeReducer(), window.FSStore.initialState());
    var sync = new window.FSStateSync(store);
    sync.init();
    store.subscribe("live2d:", function (state, event) { handleEvent(event.type, event); });
    // tts:/audio: 合成路径同样走 handleLipSync（audio_id 去重，双路只处理一次）
    store.subscribe("tts:", function (state, event) {
      if (event.type === "tts:audio_ready") handleLipSync(event.role || "yuki",
        event.audio_id, event.duration_ms);
      if (event.type === "tts:audio_ready") playTtsAudio(event.audio_id, event.role || "yuki");
    });
    store.subscribe("audio:", function (state, event) {
      if (event.type === "audio:segment_ready") handleLipSync(event.role || "yuki",
        event.audio_id, event.duration_ms || 1500);
    });
    // 静默态兜底：加 audio_id 守卫，仅当仍是当前口型段时才收尾（不截断新段）
    store.subscribe("speech:completed", function (state, event) {
      var role = event.role;
      if (!role) return;
      // M2：发言完成 → 移除说话高亮（与口型收尾解耦，不依赖 audio_id）
      setSpeakingHighlight(role, false);
      if (!actors[role]) return;
      if (actors[role].lastAudio !== event.audio_id) return;
      setTimeout(function () {
        if (actors[role].lastAudio === event.audio_id) {
          clearLipTimer(role);
          endLipSync(role);
        }
      }, 220);
    });
    // M2 说话高亮：仲裁放行（deferred=False 即实际开说）点亮对应角色高亮；
    // 排队待发（deferred=True）不点亮，避免未发声先高亮
    store.subscribe("speech:arbitrated", function (state, event) {
      if (event.role && !event.deferred) setSpeakingHighlight(event.role, true);
    });

    // ---- 弹幕互动（仅驱动视线/空闲，不负责显示） ----
    store.subscribe("danmaku:", function (state, event) {
      if (event.type === "danmaku:received") {
        pushDanmu(event.user_name || "观众", event.content || "", "");
        onViewerMessage();
      }
    });
    // 被拦截弹幕仅在组合预览(有列表)时显示
    store.subscribe("audience:", function (state, event) {
      if (event.type === "audience:filtered") {
        pushDanmu("拦截", event.content || "", "filtered");
      }
    });
    store.subscribe("frontend:", function (state, event) {
      if (event.type === "frontend:subtitle_update") {
        pushDanmu("AI · " + (event.role || "yuki"), event.text || "", "ai");
      }
    });

    // ---- 直播间联动：角色切换 → 加载对应模型（智能助手"切换到X"驱动） ----
    store.subscribe("session:", function (state, event) {
      if (event.type === "session:switched" && event.role) {
        switchActor(event.role);
      }
    });
  }

  // ---- TTS 播放（/ws/tts_audio 拉音频字节 → HTML5 Audio） ----
  var ttsSock = null;
  var ttsAudioEls = {};    // role -> Audio 实例（每角色一个，避免切歌打断）
  var playedAudioIds = {}; // audio_id 去重（WS 广播 + 双路可能重复）

  function ensureTtsSock() {
    if (ttsSock && (ttsSock.readyState === 0 || ttsSock.readyState === 1)) return ttsSock;
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    ttsSock = new WebSocket(proto + location.host + "/ws/tts_audio");
    ttsSock.onmessage = function (ev) {
      if (ev.data instanceof Blob && ttsSock._pendingAudioId) {
        var url = URL.createObjectURL(ev.data);
        var role = ttsSock._pendingRole || "yuki";
        var audio = ttsAudioEls[role] || (ttsAudioEls[role] = new Audio());
        audio.src = url;
        audio.play().catch(function () { /* 自动播放策略拦截时静默 */ });
        ttsSock._pendingAudioId = null;
      }
    };
    ttsSock.onclose = function () { ttsSock = null; };
    return ttsSock;
  }
  function playTtsAudio(audioId, role) {
    if (!audioId || playedAudioIds[audioId]) return;
    playedAudioIds[audioId] = true;
    var ws = ensureTtsSock();
    if (ws.readyState === 1) {
      ws._pendingAudioId = audioId;
      ws._pendingRole = role;
      ws.send(JSON.stringify({ audio_id: audioId }));
    }
  }

  async function boot() {
    if (!window.PIXI || !window.PIXI.live2d) {
      showHint("PixiJS / Live2D 库加载失败");
      return;
    }
    app = new PIXI.Application({
      view: document.createElement("canvas"), transparent: true, autoStart: true,
      width: 1120, height: 720, antialias: true
    });
    document.getElementById("canvas-holder").appendChild(app.view);
    window.__app = app;
    // 任务三：参数帧 30fps 插值挂到 PIXI 渲染循环
    app.ticker.add(applyInterpolatedParams);

    for (var i = 0; i < ROLES.length; i++) {
      var cfg = ROLES[i];
      if (!isRoleEnabled(cfg.name)) continue;   // 临时停用角色：跳过装载
      try {
        await loadActor(cfg);
      } catch (e) {
        showHint("模型未就绪：" + "/assets/live2d/" + cfg.model + "/" + cfg.model + ".model3.json");
      }
    }
    showHint("");
    hint.style.display = "none";
  }

  // 加载单角色模型（可复用：boot 初始化 + 角色切换联动）
  function loadActor(cfg) {
    var url = "/assets/live2d/" + cfg.model + "/" + cfg.model + ".model3.json";
    return PIXI.live2d.Live2DModel.from(url).then(function (model) {
      // 按模型实际高度动态适配画布（fit-to-canvas）：
      model.anchor.set(0.5, 1.0);
      var canvasH = app.screen.height;
      var modelH = model.height || 1;
      var fillRatio = 1.0;           // 上半身约占画布比例
      var scale = (canvasH * fillRatio) / modelH;
      model.scale.set(scale);
      model.x = app.screen.width * (cfg.x !== undefined ? cfg.x : 0.5);
      model.y = canvasH;             // 底部对齐：脚底贴画布底边
      app.stage.addChild(model);
      actors[cfg.name] = {
        model: model, driver: makeDriver(cfg.model, model),
        state: "silent", timer: null, lastAudio: null, lipTimer: null
      };
      setState(cfg.name, "silent");   // 呼吸限制：显式进入受控 idle
      return model;
    });
  }

  // 角色切换联动（智能助手"切换到X"）：已加载直接静默，未加载按配置装载
  function switchActor(role) {
    if (!app || !window.PIXI) return;
    if (!isRoleEnabled(role)) return;   // 临时停用角色：切换联动也跳过
    if (actors[role]) { setState(role, "silent"); return; }
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg) return;
    loadActor(cfg).catch(function () {
      showHint("模型未就绪：" + cfg.model);
    });
  }

  // ---- 弹幕输入接线（仅组合预览页存在 #danmuInput/#danmuSend 时启用；纯模型源跳过） ----
  var danmuInput = document.getElementById("danmuInput");
  var danmuSend = document.getElementById("danmuSend");
  function postDanmaku(text) {
    fetch("/api/danmaku", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, user_name: "测试观众" })
    }).then(function (r) { return r.json(); }).catch(function () {
      pushDanmu("测试观众", "（网络错误，弹幕未发送）", "filtered");
    });
  }
  if (danmuSend) {
    danmuSend.addEventListener("click", function () {
      var t = danmuInput.value.trim();
      if (!t) return;
      pushDanmu("测试观众", t, "");
      postDanmaku(t);
      danmuInput.value = "";
      onViewerMessage();
    });
  }
  if (danmuInput) {
    danmuInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); if (danmuSend) danmuSend.click(); }
    });
  }

  wireStore();
  boot();
  // 暴露给外部（角色切换/调试）
  window.__actors = actors;
})();
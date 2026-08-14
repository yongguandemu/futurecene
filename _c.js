
(function () {
  "use strict";

  // 角色 → 模型配置（与 config.yaml roles 对应；无后端时前端兜底）
  var ROLES = window.__ROLES || [
    { name: "yuki", model: "Hiyori", restrictBreath: true, idleSilent: "Hiyori_m03",
      x: 0.27, scale: 0.42,
      expressionMap: {}, motionMap: { wave: "Hiyori_m01", nod: "Hiyori_m02",
        shake: "Hiyori_m05", idle: "Hiyori_m01" } },
    { name: "lilith", model: "小恶魔", restrictBreath: false, idleSilent: "idle",
      x: 0.73, scale: 0.42,
      expressionMap: { 开心: "唱歌", 难过: "流泪", 惊讶: "头发", 害羞: "嘟嘴", 生气: "脸黑", 平静: "头发" },
      motionMap: { wave: "wave", nod: "nod", shake: "shake", idle: "idle" } }
  ];
  var hint = document.getElementById("hint");
  var app = null;
  var actors = {};   // role -> { model, motion, state: 'silent'|'talking', timer, lastAudio }

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
  // 单画布多模型：同一时刻至多一人发声（互斥），高亮 class 作用于模型容器；
  // actor 对象记录 speaking 状态，供后续 per-role DOM 扩展（终审 M2）。
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
    try {
      a.model.internalModel.motionManager.update(0, 0);
    } catch (e) { /* ignore */ }
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
      playMotion(role, mappedMotion(role, ev.motion));
    } else if (type === "live2d:expression_changed") {
      var expr = mappedExpression(role, ev.expression);
      if (expr) { try { a.model.expression(expr); } catch (e) { /* ignore */ } }
    } else if (type === "live2d:lip_sync_start") {
      handleLipSync(role, ev.audio_id, ev.duration_ms);
    } else if (type === "live2d:lip_sync_end") {
      // 权威结束事件（后端按时长触发，带 role/audio_id）：仅当前段结束时收尾
      if (a.lastAudio === ev.audio_id) { clearLipTimer(role); endLipSync(role); }
    }
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

    // ---- 直播测试台：弹幕显示 + AI 字幕（订阅 danmaku/audience/subtitle 事件） ----
    store.subscribe("danmaku:", function (state, event) {
      if (event.type === "danmaku:received") {
        pushDanmu(event.user_name || "观众", event.content || "", "");
      }
    });
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

  // ---- 直播测试台：弹幕发送 / 显示 ----
  var danmuInput = document.getElementById("danmuInput");
  var danmuSend = document.getElementById("danmuSend");
  var danmuList = document.getElementById("danmu-list");
  var MAX_DANMU = 50;

  function pushDanmu(user, text, cls) {
    if (!danmuList) return;
    var el = document.createElement("div");
    el.className = "dm-item" + (cls ? " " + cls : "");
    el.innerHTML = '<span class="dm-user">' + escHtml(user) + '</span>' + escHtml(text);
    danmuList.appendChild(el);
    while (danmuList.children.length > MAX_DANMU) {
      danmuList.removeChild(danmuList.firstChild);
    }
  }
  function postDanmaku(text) {
    fetch("/api/danmaku", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, user_name: "测试观众" })
    }).then(function (r) { return r.json(); }).catch(function () {
      pushDanmu("测试观众", "（网络错误，弹幕未发送）", "filtered");
    });
  }
  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  if (danmuSend) {
    danmuSend.addEventListener("click", function () {
      var t = danmuInput.value.trim();
      if (!t) return;
      pushDanmu("测试观众", t, "");
      postDanmaku(t);
      danmuInput.value = "";
    });
  }
  if (danmuInput) {
    danmuInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); danmuSend.click(); }
    });
  }

  // ---- 直播测试台：TTS 播放（/ws/tts_audio 拉音频字节 → HTML5 Audio，修复"有声无响"） ----
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

    for (var i = 0; i < ROLES.length; i++) {
      var cfg = ROLES[i];
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
      model.anchor.set(0.5, 1.0);
      model.scale.set(cfg.scale || 0.42);
      model.x = app.screen.width * (cfg.x !== undefined ? cfg.x : 0.5);
      model.y = app.screen.height;
      app.stage.addChild(model);
      actors[cfg.name] = { model: model, state: "silent", timer: null, lastAudio: null };
      setState(cfg.name, "silent");   // 呼吸限制：显式进入受控 idle
      return model;
    });
  }

  // 角色切换联动（智能助手"切换到X"）：已加载直接静默，未加载按配置装载
  function switchActor(role) {
    if (!app || !window.PIXI) return;
    if (actors[role]) { setState(role, "silent"); return; }
    var cfg = ROLES.find(function (r) { return r.name === role; });
    if (!cfg) return;
    loadActor(cfg).catch(function () {
      showHint("模型未就绪：" + cfg.model);
    });
  }

  wireStore();
  boot();
})();

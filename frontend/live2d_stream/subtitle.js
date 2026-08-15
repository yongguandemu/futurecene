/**
 * subtitle.js — 角色说话字幕叠加模块（自包含，注入即用）
 *
 * 时序（关键）：
 *   frontend:subtitle_update  → 仅缓存文本（LLM 文本就绪，TTS 尚未开始，不显示）
 *   tts:audio_ready           → TTS 音频就绪=开始播放 → 显示字幕
 *   speech:completed          → 发言完成 → 隐藏
 * 这样"开始说话"的时机是 TTS 开始播放，而非 LLM 生成文本。
 *
 * 样式：自注入半透明黑底字幕卡，默认定位于视图垂直中部（人物中部），可经 opts.top 调整。
 *
 * 用法：
 *   <script src="/frontend/live2d_stream/subtitle.js"></script>
 *   <script>new window.SubtitleOverlay({ roleNames: { yuki: "Yuki", lilith: "Lilith" } }).attach();</script>
 */
(function (global) {
  'use strict';

  var STYLE_ID = 'fs-subtitle-style';

  function escHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function SubtitleOverlay(opts) {
    opts = opts || {};
    this._roleNames = opts.roleNames || { yuki: "Yuki", lilith: "Lilith" };
    this._pendingText = {};      // role -> 缓存文本（TTS 开始前暂存）
    this._hideTimer = null;
    this._top = opts.top != null ? opts.top : 58;   // 默认位于视图垂直中部（人物中部）
    this._maxShowMs = opts.maxShowMs != null ? opts.maxShowMs : 10000;  // 兜底防字幕卡死
    this._injectStyles();
    this._build(opts);
  }

  SubtitleOverlay.prototype._injectStyles = function () {
    if (document.getElementById(STYLE_ID)) return;
    var el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent =
      ".fs-sub-wrap{position:fixed;left:50%;top:" + this._top + "%;transform:translate(-50%,-50%);" +
      "width:auto;max-width:78%;pointer-events:none;z-index:8;display:flex;justify-content:center;}" +
      ".fs-sub-card{background:rgba(10,10,16,0.72);border:1px solid rgba(255,255,255,0.16);" +
      "border-radius:12px;padding:10px 20px;color:#fff;text-align:center;" +
      "box-shadow:0 4px 22px rgba(0,0,0,0.45);opacity:0;transform:translateY(8px);" +
      "transition:opacity .3s ease,transform .3s ease;}" +
      ".fs-sub-card.fs-sub-show{opacity:1;transform:translateY(0);}" +
      ".fs-sub-role{font-size:13px;letter-spacing:1px;margin-bottom:3px;color:#ffd9a0;}" +
      ".fs-sub-role.fs-sub-yuki{color:#6366f1;}" +
      ".fs-sub-role.fs-sub-lilith{color:#ef4444;}" +
      ".fs-sub-card.fs-sub-yuki{border-color:rgba(99,102,241,0.7);box-shadow:0 0 16px rgba(99,102,241,0.32),0 4px 22px rgba(0,0,0,0.45);}" +
      ".fs-sub-card.fs-sub-lilith{border-color:rgba(239,68,68,0.7);box-shadow:0 0 16px rgba(239,68,68,0.32),0 4px 22px rgba(0,0,0,0.45);}" +
      ".fs-sub-text{font-size:24px;line-height:1.4;font-weight:600;text-shadow:0 2px 6px rgba(0,0,0,0.7);}";
    (document.head || document.documentElement).appendChild(el);
  };

  SubtitleOverlay.prototype._build = function () {
    this._wrap = document.createElement("div");
    this._wrap.className = "fs-sub-wrap";
    this._card = document.createElement("div");
    this._card.className = "fs-sub-card";
    this._roleEl = document.createElement("div");
    this._roleEl.className = "fs-sub-role";
    this._textEl = document.createElement("div");
    this._textEl.className = "fs-sub-text";
    this._card.appendChild(this._roleEl);
    this._card.appendChild(this._textEl);
    this._wrap.appendChild(this._card);
    (document.body || document.documentElement).appendChild(this._wrap);
  };

  // 缓存文本（TTS 开始前不显示）
  SubtitleOverlay.prototype.cache = function (role, text) {
    if (!text) return;
    this._pendingText[role || "yuki"] = text;
  };

  // TTS 开始播放 → 显示
  SubtitleOverlay.prototype.show = function (role) {
    var content = this._pendingText[role || "yuki"];
    if (!content) return;
    var name = this._roleNames[role] || (role ? role.charAt(0).toUpperCase() + role.slice(1) : "");
    this._roleEl.textContent = name;
    this._textEl.textContent = content;
    this._card.classList.remove("fs-sub-yuki", "fs-sub-lilith");
    this._roleEl.classList.remove("fs-sub-yuki", "fs-sub-lilith");
    if (role === "yuki" || role === "lilith") {
      this._card.classList.add("fs-sub-" + role);
      this._roleEl.classList.add("fs-sub-" + role);
    }
    this._card.classList.add("fs-sub-show");
    if (this._hideTimer) clearTimeout(this._hideTimer);
    // 兜底：speech:completed 未到也不让字幕卡死
    var self = this;
    this._hideTimer = setTimeout(function () { self.hide(); }, this._maxShowMs);
  };

  // 发言完成 → 隐藏
  SubtitleOverlay.prototype.hide = function () {
    this._card.classList.remove("fs-sub-show");
    if (this._hideTimer) { clearTimeout(this._hideTimer); this._hideTimer = null; }
  };

  SubtitleOverlay.prototype.attach = function () {
    if (!global.FSStore || !global.FSStateSync) return this;
    var self = this;
    var store = global.FSStore.createStore(global.FSStore.makeReducer(), global.FSStore.initialState());
    var sync = new global.FSStateSync(store);
    sync.init();
    this._store = store;   // 暴露内部 store，便于测试/调试注入合成事件
    this._sync = sync;
    store.subscribe("frontend:", function (state, ev) {
      if (ev.type === "frontend:subtitle_update") self.cache(ev.role, ev.text);
    });
    store.subscribe("tts:", function (state, ev) {
      if (ev.type === "tts:audio_ready") self.show(ev.role);   // TTS 开始播放
    });
    store.subscribe("speech:", function (state, ev) {
      if (ev.type === "speech:completed") self.hide();
    });
    return this;
  };

  global.SubtitleOverlay = SubtitleOverlay;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
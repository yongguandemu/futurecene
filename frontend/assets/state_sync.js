/* ============================================
   state_sync.js — WS 客户端 + 快照初始化 + 断线补数
   初始化顺序：先拉 /api/state（带 version）→ 再建 WS。
   断线：立即拉一次 /api/state → 指数退避重连 → 重连成功再拉一次。
   onStatusChange 回调：connecting / online / reconnecting / offline
   ============================================ */
(function (global) {
  'use strict';

  function StateSync(store, opts) {
    opts = opts || {};
    this.store = store;
    this.ws = null;
    this.reconnectDelay = opts.reconnectDelay || 1000;
    this.maxDelay = opts.maxDelay || 30000;
    this.pollInterval = opts.pollInterval || 30000;
    this.onStatusChange = opts.onStatusChange || null;
    this._pollTimer = null;
    this._manualStop = false;
    this._emit('connecting');
  }

  StateSync.prototype._emit = function (status) {
    if (this.onStatusChange) {
      try { this.onStatusChange(status); } catch (e) { /* ignore */ }
    }
  };

  StateSync.prototype._fetchState = function () {
    var self = this;
    return fetch('/api/state').then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.version !== undefined) {
        self.store.dispatch({ type: 'state:changed', snapshot: data, version: data.version });
      }
      return data;
    }).catch(function () { return null; });
  };

  StateSync.prototype.init = function () {
    var self = this;
    // 首载：先 GET /api/state（带 version）初始化 store → 再建立 WS
    return this._fetchState().then(function () {
      self.connect();
    });
  };

  StateSync.prototype.connect = function () {
    var self = this;
    if (this._manualStop) return;
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    this._emit('connecting');
    try {
      this.ws = new WebSocket(proto + location.host + '/ws/events');
    } catch (e) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      self.store.dispatch(msg);
    };
    this.ws.onclose = function () {
      self._onDisconnect();
    };
    this.ws.onerror = function () {
      try { self.ws.close(); } catch (e) {}
    };
    this.ws.onopen = function () {
      self.reconnectDelay = 1000;
      self._stopPolling();
      self._emit('online');
      // 重连成功：拉一次快照补缺口（双游标自动防回退）
      self._fetchState();
    };
  };

  StateSync.prototype._onDisconnect = function () {
    var self = this;
    this._emit('reconnecting');
    // 断线立即拉一次（兜底可见性）
    this._fetchState();
    this._startPolling();
    this._scheduleReconnect();
  };

  StateSync.prototype._scheduleReconnect = function () {
    var self = this;
    setTimeout(function () { self.connect(); }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxDelay);
  };

  StateSync.prototype._startPolling = function () {
    var self = this;
    this._stopPolling();
    this._pollTimer = setInterval(function () {
      self._fetchState();
    }, this.pollInterval);
  };

  StateSync.prototype._stopPolling = function () {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  };

  StateSync.prototype.stop = function () {
    this._manualStop = true;
    this._stopPolling();
    if (this.ws) { try { this.ws.close(); } catch (e) {} }
    this._emit('offline');
  };

  global.FSStateSync = StateSync;
})(window);

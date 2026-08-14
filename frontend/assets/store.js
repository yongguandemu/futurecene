/* ============================================
   store.js — Future Scene 前端单一状态层（方案 A）
   状态唯一性：以后端 EventBus seq 为准，后到覆盖先到。
   双游标：lastSnapshotSeq（快照）/ lastEventSeq（事件）独立合并。
   ============================================ */
(function (global) {
  'use strict';

  function createStore(reducer, initialState) {
    let state = initialState;
    const listeners = []; // {filter, fn}

    function getState() { return state; }

    function dispatch(event) {
      const prev = state;
      state = reducer(state, event);
      if (state !== prev) {
        listeners.forEach(function (l) {
          if (l.filter === '*' || (event.type || '').indexOf(l.filter) === 0) {
            l.fn(state, event, prev);
          }
        });
      }
      return state;
    }

    function subscribe(filter, fn) {
      const l = { filter: filter || '*', fn: fn };
      listeners.push(l);
      return function () {
        const i = listeners.indexOf(l);
        if (i >= 0) listeners.splice(i, 1);
      };
    }

    return { getState: getState, dispatch: dispatch, subscribe: subscribe };
  }

  /* ---- reducer：双游标合并 ---- */
  function makeReducer() {
    return function (state, event) {
      const type = event.type || '';
      const seq = event.seq || 0;
      const next = JSON.parse(JSON.stringify(state));

      if (type === 'state:changed' || (event.snapshot && event.version !== undefined)) {
        // 快照：仅当 version > lastSnapshotSeq 接受
        const version = event.version !== undefined ? event.version : (event.snapshot && event.snapshot.version);
        if (version === undefined || version > next.snapshotSeq) {
          const snap = event.snapshot || event;
          next.snapshotSeq = version !== undefined ? version : next.snapshotSeq;
          next.session = snap.session || next.session;
          next.switches = snap.switches || next.switches;
          next.orchestrators = snap.orchestrators || next.orchestrators;
          next.degradation = snap.degradation || next.degradation;
          next.cost = snap.cost || next.cost;
          next.watchdog = snap.watchdog || next.watchdog;
          next.version = version !== undefined ? version : next.version;
        }
        return next;
      }

      // 普通事件：仅当 seq > lastEventSeq 接受（事件日志）
      if (seq <= next.seq) return state; // 过期事件丢弃，不产生新引用
      next.seq = seq;
      next.events = next.events.concat([{
        type: type, seq: seq, ts: event.ts || Date.now(), data: event
      }]).slice(-200);

      // 命令状态机
      if (event.command_id) {
        const cmds = next.commands;
        if (type === 'commander:command_received') {
          cmds[event.command_id] = { status: 'sent', raw: '', error: null };
        } else if (type === 'commander:command_routed') {
          if (cmds[event.command_id]) cmds[event.command_id].status = 'running';
        } else if (type === 'commander:command_completed') {
          if (cmds[event.command_id]) cmds[event.command_id].status = 'success';
        } else if (type === 'commander:command_failed') {
          if (cmds[event.command_id]) {
            cmds[event.command_id].status = 'failed';
            cmds[event.command_id].error = event.error || 'unknown';
          }
        }
      }

      // 会话/开关增量（无快照时的兜底）
      if (type === 'switch:changed' && event.name !== undefined) {
        next.switches = Object.assign({}, next.switches, { [event.name]: event.enabled });
      }
      if (type === 'session:switched' && event.role) {
        next.session = Object.assign({}, next.session, { role: event.role });
      }
      return next;
    };
  }

  /* ---- 初始状态 ---- */
  function initialState() {
    return {
      seq: 0,            // lastEventSeq
      snapshotSeq: -1,   // lastSnapshotSeq
      version: 0,
      session: {}, switches: {}, orchestrators: [], degradation: {},
      cost: {}, watchdog: {},
      events: [],
      commands: {}
    };
  }

  /* ---- 刷新恢复（sessionStorage；不含对话历史，对话历史由页面单独管理） ---- */
  function persistOnUnload(store, key) {
    window.addEventListener('pagehide', function () {
      try {
        var s = store.getState();
        sessionStorage.setItem(key, JSON.stringify({
          session: s.session, switches: s.switches, cost: s.cost,
          watchdog: s.watchdog, degradation: s.degradation,
          snapshotSeq: s.snapshotSeq
        }));
      } catch (e) {}
    });
  }

  function restoreFromSession(store, key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (!raw) return false;
      var saved = JSON.parse(raw);
      store.dispatch({ type: 'state:changed', snapshot: saved, version: saved.snapshotSeq });
      return true;
    } catch (e) { return false; }
  }

  global.FSStore = {
    createStore: createStore,
    makeReducer: makeReducer,
    initialState: initialState,
    persistOnUnload: persistOnUnload,
    restoreFromSession: restoreFromSession
  };
})(window);

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
          next.characters = snap.characters || next.characters;
          next.version = version !== undefined ? version : next.version;
        }
        return next;
      }

      // 命令状态机（不依赖 seq 门槛：本地 dispatch 的 command_received 也可生效）
      if (event.command_id) {
        const cmds = next.commands;
        if (type === 'commander:command_received') {
          const cur = cmds[event.command_id];
          if (!cur || cur.status === 'failed' || cur.status === 'sent') {
            cmds[event.command_id] = { status: 'sent', raw: event.raw || '', error: null };
          }
          // 已到 running/success 不覆盖（防 HTTP 响应晚于事件到达造成回退）
        } else if (type === 'commander:command_routed') {
          if (cmds[event.command_id] && cmds[event.command_id].status !== 'success') {
            cmds[event.command_id].status = 'running';
          }
        } else if (type === 'commander:command_completed') {
          if (cmds[event.command_id]) {
            cmds[event.command_id].status = 'success';
            cmds[event.command_id].error = null;
          }
        } else if (type === 'commander:command_failed') {
          if (cmds[event.command_id]) {
            cmds[event.command_id].status = 'failed';
            cmds[event.command_id].error = event.error || 'unknown';
          }
        }
      }

      // 普通事件：仅当 seq > lastEventSeq 接受（事件日志）；本地合成事件（seq=0）不记日志
      if (seq <= next.seq) {
        // 过期事件：若命令状态机有更新则保留（返回 next），否则丢弃（返回原 state）
        return event.command_id ? next : state;
      }
      next.seq = seq;
      next.events = next.events.concat([{
        type: type, seq: seq, ts: event.ts || Date.now(), data: event
      }]).slice(-200);

      // 会话/开关增量（快照之后的状态事件才应用；seq <= snapshotSeq 的状态已被快照包含）
      if (type === 'switch:changed' && event.name !== undefined && seq > next.snapshotSeq) {
        next.switches = Object.assign({}, next.switches, { [event.name]: event.enabled });
      }
      if (type === 'session:switched' && event.role && seq > next.snapshotSeq) {
        next.session = Object.assign({}, next.session, { role: event.role });
      }
      // 多角色在场说话状态（快照之后的角色事件才应用；快照本身已含 speaking）
      if (event.role && next.characters[event.role] && seq > next.snapshotSeq) {
        if (type === 'speech:arbitrated') next.characters[event.role].speaking = true;
        if (type === 'speech:completed') next.characters[event.role].speaking = false;
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
      cost: {}, watchdog: {}, characters: {},
      events: [],
      commands: {}
    };
  }

  /* ---- 刷新恢复（sessionStorage；不含对话历史，对话历史由页面单独管理） ---- */
  function persistOnUnload(store, key) {
    window.addEventListener('pagehide', function () {
      try {
        var s = store.getState();
        // 不持久化游标（snapshotSeq/seq）：服务器可能重启，seq 会重置，
        // 恢复旧游标会导致新快照被永久拒绝（状态卡死）。游标由首屏快照重置。
        sessionStorage.setItem(key, JSON.stringify({
          session: s.session, switches: s.switches, cost: s.cost,
          watchdog: s.watchdog, degradation: s.degradation,
          orchestrators: s.orchestrators
        }));
      } catch (e) {}
    });
  }

  function restoreFromSession(store, key) {
    try {
      var raw = sessionStorage.getItem(key);
      if (!raw) return false;
      var saved = JSON.parse(raw);
      // 只恢复展示字段，不恢复游标：version 置 0（快照游标仍 -1，可被任何新快照重置）
      store.dispatch({ type: 'state:changed', snapshot: saved, version: 0 });
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

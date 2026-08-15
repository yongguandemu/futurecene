/**
 * live2d_driver.js — 独立可复用 Live2D 连续动画驱动库
 *
 * 移植自原系统 LumiProject 的前端连续动画驱动（后端 30Hz 参数注入 → 前端自驱）。
 * 本库为自包含前端实现，不依赖后端桥接/WebSocket，一处实现多处复用。
 *
 * 能力（与原系统 LumiProject 对齐）：
 *   - 5 维连续情绪状态 + 2 维活力（happiness/shyness/sadness/surprise/calmness + energy/openness）
 *   - 表达式平滑过渡（StateInterpolator，500ms ease-in-out）
 *   - 呼吸调制（BreathModulator，0.4Hz 正弦波，情绪调制频率/幅度）
 *   - 视线控制（GazeController，5 目标 300ms 过渡 + think 游移）
 *   - 说话头部驱动（SpeakingHeadDriver，音频能量 → 点头/侧倾/问句抬头）
 *   - 情绪微动作（EmotionMicroActions，5 种复合叠加）
 *   - 空闲行为调度（IdleBehaviorScheduler，10s/30s/120s 三级）
 *   - 表达状态机（ExpressionStateMachine，表情+动作+优先级 + 11 个预设动作）
 *   - 参数注入（30Hz rAF，优先级合并 → coreModel.setParameterValueById）
 *
 * 依赖：已加载的 PIXI Live2DModel（pixi-live2d-display）。本库不负责加载模型。
 *
 * 用法：
 *   const model = await PIXI.live2d.Live2DModel.from(url);
 *   const driver = new Live2DDriver({ model, characterId: 'yuki' });
 *   driver.start();
 *   driver.setEmotion('happy', 0.8);
 *   driver.playMotion('wave', 1.0);
 *   driver.setSpeaking(true, 0.8);
 *   driver.onInteraction();      // 弹幕互动，重置空闲计时
 *   driver.setGazeTarget('danmaku');
 *   driver.setLipSync(0.5);      // 或由 AudioAnalyser 回调驱动
 */
(function (global) {
  'use strict';

  // ====================================================================
  // 工具函数
  // ====================================================================
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function easeInOutCubic(t) {
    t = clamp(t, 0, 1);
    return t * t * (3 - 2 * t);
  }
  function easeOutCubic(t) {
    t = clamp(t, 0, 1);
    return 1 - Math.pow(1 - t, 3);
  }
  function easeInCubic(t) {
    t = clamp(t, 0, 1);
    return t * t * t;
  }
  function easeOutBack(t, s) {
    s = s || 1.7;
    t = clamp(t, 0, 1);
    return 1 + (s + 1) * Math.pow(t - 1, 3) + s * Math.pow(t - 1, 2);
  }

  // ====================================================================
  // Live2DState — 多维连续情绪状态（移植 live2d_state.py）
  // ====================================================================
  class Live2DState {
    constructor(o) {
      o = o || {};
      this.happiness = o.happiness != null ? o.happiness : 0.0;
      this.shyness = o.shyness != null ? o.shyness : 0.0;
      this.sadness = o.sadness != null ? o.sadness : 0.0;
      this.surprise = o.surprise != null ? o.surprise : 0.0;
      this.calmness = o.calmness != null ? o.calmness : 1.0;
      this.energy = o.energy != null ? o.energy : 0.5;
      this.openness = o.openness != null ? o.openness : 0.5;
      this.is_speaking = !!o.is_speaking;
      this.speech_intensity = o.speech_intensity != null ? o.speech_intensity : 0.0;
      this.gaze_target = o.gaze_target || 'camera';
      this.motion_params = o.motion_params || {};   // 动作层参数（不参与插值）
      this.active_motion = o.active_motion || null;
    }
    clone() {
      return new Live2DState({
        happiness: this.happiness, shyness: this.shyness, sadness: this.sadness,
        surprise: this.surprise, calmness: this.calmness,
        energy: this.energy, openness: this.openness,
        is_speaking: this.is_speaking, speech_intensity: this.speech_intensity,
        gaze_target: this.gaze_target,
        motion_params: this.motion_params, active_motion: this.active_motion,
      });
    }
    normalize() {
      const total = this.happiness + this.shyness + this.sadness + this.surprise + this.calmness;
      if (total > 1.0) {
        this.happiness /= total; this.shyness /= total; this.sadness /= total;
        this.surprise /= total; this.calmness /= total;
      }
      this.energy = clamp(this.energy, 0, 1);
      this.openness = clamp(this.openness, 0, 1);
      return this;
    }
    blend(other, alpha) {
      alpha = clamp(alpha, 0, 1);
      const s = this, o = other;
      return new Live2DState({
        happiness: s.happiness * (1 - alpha) + o.happiness * alpha,
        shyness: s.shyness * (1 - alpha) + o.shyness * alpha,
        sadness: s.sadness * (1 - alpha) + o.sadness * alpha,
        surprise: s.surprise * (1 - alpha) + o.surprise * alpha,
        calmness: s.calmness * (1 - alpha) + o.calmness * alpha,
        energy: s.energy * (1 - alpha) + o.energy * alpha,
        openness: s.openness * (1 - alpha) + o.openness * alpha,
        is_speaking: o.is_speaking,
        speech_intensity: s.speech_intensity * (1 - alpha) + o.speech_intensity * alpha,
        gaze_target: o.gaze_target,
        // motion_params/active_motion 不混合：由状态机直接管理
      });
    }
  }

  // ====================================================================
  // computeExpressionParams — 从状态计算表情参数（移植 expression_params.py）
  // 所有参数以 0.5 为中性基线，变化幅度 ±0.3。动作层参数覆盖表情参数。
  // ====================================================================
  function computeExpressionParams(state) {
    const h = state.happiness, s = state.shyness, d = state.sadness,
          u = state.surprise, c = state.calmness, e = state.energy;
    const params = {};

    // 嘴部
    params.ParamMouthForm = clamp(0.5 + 0.25 * h - 0.2 * d - 0.1 * s, 0.2, 0.8);
    params.ParamMouthOpenY = 0.1 + 0.2 * state.speech_intensity;

    // 眉毛
    const brow_y = clamp(0.5 - 0.15 * d + 0.15 * u - 0.1 * s, 0.2, 0.8);
    params.ParamBrowLY = brow_y;
    params.ParamBrowRY = brow_y;
    params.ParamBrowForm = clamp(0.5 - 0.1 * s - 0.1 * d, 0.3, 0.7);

    // 眼睛
    const eye_open = clamp(0.7 + 0.1 * u - 0.15 * s - 0.1 * d, 0.4, 0.9);
    params.ParamEyeLOpen = eye_open;
    params.ParamEyeROpen = eye_open;
    params.ParamEyeSmile = clamp(0.3 * h, 0.0, 0.3);

    // 身体
    params.ParamBodyAngleX = clamp(-0.1 * s + 0.05 * h, -0.2, 0.15);
    params.ParamBodyAngleY = clamp(-0.05 * s - 0.05 * d, -0.15, 0.0);
    params.ParamBodyAngleZ = clamp(0.05 * h, 0.0, 0.15);

    // 头部
    params.ParamAngleY = clamp(0.05 * s - 0.03 * h, -0.1, 0.15);
    params.ParamAngleX = 0.0;
    params.ParamAngleZ = clamp(-0.03 * s, -0.1, 0.0);

    // 呼吸基线
    params.ParamBreath = 0.5;

    // 动作层参数覆盖表情（动作优先于表情）
    if (state.motion_params) {
      for (const k in state.motion_params) params[k] = state.motion_params[k];
    }
    return params;
  }

  // ====================================================================
  // StateInterpolator — 状态平滑过渡器（移植 live2d_interpolator.py）
  // ====================================================================
  class StateInterpolator {
    constructor() {
      this._current = new Live2DState();
      this._target = new Live2DState();
      this._source = new Live2DState();
      this._transitionStart = { emotions: 0, vitality: 0, gaze: 0 };
      this._lastUpdate = 0;
    }
    setTarget(target) {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      const cur = this._current;
      this._source = new Live2DState({
        happiness: cur.happiness, shyness: cur.shyness, sadness: cur.sadness,
        surprise: cur.surprise, calmness: cur.calmness,
        energy: cur.energy, openness: cur.openness, gaze_target: cur.gaze_target,
      });
      this._target = target;
      if (this._emotionsChanged()) this._transitionStart.emotions = now;
      if (this._vitalityChanged()) this._transitionStart.vitality = now;
      if (cur.gaze_target !== target.gaze_target) this._transitionStart.gaze = now;
    }
    _emotionsChanged() {
      const c = this._current, t = this._target;
      return Math.abs(c.happiness - t.happiness) > 0.01 ||
             Math.abs(c.shyness - t.shyness) > 0.01 ||
             Math.abs(c.sadness - t.sadness) > 0.01 ||
             Math.abs(c.surprise - t.surprise) > 0.01 ||
             Math.abs(c.calmness - t.calmness) > 0.01;
    }
    _vitalityChanged() {
      const c = this._current, t = this._target;
      return Math.abs(c.energy - t.energy) > 0.01 || Math.abs(c.openness - t.openness) > 0.01;
    }
    get current() { return this._current; }
    set target(t) { this._target = t; }
    tick() {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      this._lastUpdate = now;
      const emo = this._easeAlpha(now, 'emotions', 0.5);
      const cur = this._current, src = this._source, tgt = this._target;
      cur.happiness = src.happiness + (tgt.happiness - src.happiness) * emo;
      cur.shyness = src.shyness + (tgt.shyness - src.shyness) * emo;
      cur.sadness = src.sadness + (tgt.sadness - src.sadness) * emo;
      cur.surprise = src.surprise + (tgt.surprise - src.surprise) * emo;
      cur.calmness = src.calmness + (tgt.calmness - src.calmness) * emo;
      const vit = this._easeAlpha(now, 'vitality', 0.2);
      cur.energy = src.energy + (tgt.energy - src.energy) * vit;
      cur.openness = src.openness + (tgt.openness - src.openness) * vit;
      cur.is_speaking = tgt.is_speaking;
      cur.speech_intensity = tgt.speech_intensity;
      cur.gaze_target = tgt.gaze_target;
      return cur;
    }
    _easeAlpha(now, group, duration) {
      const start = this._transitionStart[group] || 0;
      if (duration <= 0 || start <= 0) return 1.0;
      const t = Math.min(1.0, (now - start) / duration);
      return easeInOutCubic(t);
    }
  }

  // ====================================================================
  // EmotionMoodDriver — 持久性情绪基调驱动（平静基底 + 微表情 + 姿态接管）
  // 区别于 ExpressionStateMachine（有指令才动）：本驱动自持状态、持续演化，
  // 在无外部表情指令时让角色始终保持自然的表情与姿态，而非面无表情的静止。
  // ====================================================================
  const MOOD_ARCHETYPES = {
    calm: {
      label: 'calm',
      base: { happiness: 0.08, surprise: 0.05, shyness: 0.05, sadness: 0, calmness: 0.85, energy: 0.45, openness: 0.5 },
      wander: { happiness: 0.18, surprise: 0.10, shyness: 0.06, energy: 0.08, openness: 0.06 },
    },
    curious: {
      label: 'curious',
      base: { happiness: 0.18, surprise: 0.28, shyness: 0.05, sadness: 0, calmness: 0.50, energy: 0.60, openness: 0.62 },
      wander: { happiness: 0.08, surprise: 0.10, energy: 0.08, openness: 0.08 },
    },
    pleased: {
      label: 'pleased',
      base: { happiness: 0.32, surprise: 0.06, shyness: 0.05, sadness: 0, calmness: 0.35, energy: 0.55, openness: 0.60 },
      wander: { happiness: 0.10, surprise: 0.06, shyness: 0.04, energy: 0.06, openness: 0.06 },
    },
    sleepy: {
      label: 'sleepy',
      base: { happiness: 0.05, surprise: 0.02, shyness: 0.03, sadness: 0, calmness: 0.90, energy: 0.20, openness: 0.30 },
      wander: { happiness: 0.04, surprise: 0.03, energy: 0.05 },
    },
  };
  const MOOD_DIMS = ['happiness', 'shyness', 'sadness', 'surprise', 'calmness', 'energy', 'openness'];

  class EmotionMoodDriver {
    constructor(opts) {
      opts = opts || {};
      this._archetypes = opts.archetypes || MOOD_ARCHETYPES;
      this._onTarget = opts.onTarget || null;
      this._initialMood = opts.initialMood || 'calm';
      this._mood = this._initialMood;
      this._phase = 0;
      this._nextShift = 0;
      this._nextMicro = 0;
    }
    // 每帧调用：低频推进相位，节拍性更新目标(经 onTarget 平滑过渡)，避免每帧打断插值
    tick(now, ctx) {
      ctx = ctx || {};
      const speaking = !!ctx.is_speaking;
      this._phase += 0.15 / 30;   // 以 30Hz 为基准的相位推进

      // 基调游走：无说话时定时小概率切向相邻基调，否则回归平静基底
      if (!speaking && now > this._nextShift) {
        this._nextShift = now + 6 + Math.random() * 10;
        if (Math.random() < 0.45) this._mood = this._pickNeighbor(this._mood);
        else this._mood = 'calm';
      }
      // 微表情更新节拍：约 0.8s 一次，让情绪平滑跟随微波动(过渡 0.5s 可完成)
      if (now >= this._nextMicro) {
        this._nextMicro = now + 0.8;
        const target = this._buildTarget(speaking);
        if (this._onTarget) this._onTarget(target);
      }
      return this._archetypes[this._mood] || this._archetypes.calm;
    }
    _pickNeighbor(mood) {
      const neighbors = {
        calm: ['curious', 'pleased', 'sleepy'],
        curious: ['calm', 'pleased'],
        pleased: ['calm', 'curious'],
        sleepy: ['calm'],
      };
      const list = neighbors[mood] || ['calm'];
      return list[Math.floor(Math.random() * list.length)];
    }
    _buildTarget(speaking) {
      const arch = this._archetypes[this._mood] || this._archetypes.calm;
      const base = arch.base, w = arch.wander || {};
      // 双频率正弦混合，避免机械感；幅度按基调 wander 缩放
      const s1 = Math.sin(this._phase * 0.6);
      const s2 = Math.sin(this._phase * 0.35 + 1.7);
      const t = {};
      for (let i = 0; i < MOOD_DIMS.length; i++) {
        const dim = MOOD_DIMS[i];
        const baseVal = base[dim] != null ? base[dim] : (dim === 'calmness' ? 0.8 : 0.5);
        let v = baseVal;
        if (w[dim]) v += s1 * w[dim] * 0.6 + s2 * w[dim] * 0.4;
        t[dim] = clamp(v, 0, 1);
      }
      // 说话：推向专注活跃，表情微动
      if (speaking) {
        t.energy = clamp(t.energy + 0.15, 0, 1);
        t.surprise = clamp(t.surprise + 0.05, 0, 1);
        t.calmness = clamp(t.calmness - 0.10, 0, 1);
      }
      return new Live2DState(t);
    }
  }

  // 姿态接管：内置动画会驱动手脚/手臂参数(如 Haru idle 把 ParamArmLA/RA 设为 1 → 双臂僵硬张开、
// Hiyori 的 Arm/Hand 呼吸式微动)，afterMotionUpdate 钩子只覆盖驱动算过的键，未算的手脚会被动画残留。
// 此处用 NaturalPoseDriver 统一接管：自然 resting 姿态 + 呼吸微动 + 动作结束后阻尼归位。
class NaturalPoseDriver {
  constructor(opts) {
    opts = opts || {};
    this._resting = Object.assign({}, NATURAL_POSE, opts.resting || {});
    this._damping = opts.damping || 0.08;   // 每帧向 resting 归位速率(约 0.5s 归位)
    this._breathAmp = opts.breathAmp || 0.014;
    this._phase = 0;
    this._current = {};   // 当前自然姿态值(阻尼态)
    this._fallback = {};  // 动作结束瞬间的值，作为归位起点
  }
  // 每帧：base 层恒生效——手脚/身体参数始终被拉到自然 resting+呼吸微动，
  // 只有当动作层"显式"写出某个参数时才让动作临时接管(记录回落起点)，动作结束即阻尼归位。
  // 这样模型内置循环待机(如 Haru idle 把 ParamArmLA/RA 抬到 1)永远无法冻结手臂僵硬张开的姿态。
  apply(params, state, ctx) {
    ctx = ctx || {};
    const motionActive = !!ctx.motionActive;
    const motionParams = ctx.motionParams || {};   // 动作层真正写出的参数(以动作为准，而非 params 残留)
    const energy = (state && state.energy != null) ? state.energy : 0.5;
    const micro = this._microOffset(energy);
    for (const id in this._resting) {
      const base = (this._resting[id] || 0) + (micro[id] || 0);
      if (motionActive && (id in motionParams)) {
        // 动作显式接管该参数：记录 base 回落起点，跟随动作值
        const mv = motionParams[id];
        if (this._fallback[id] == null) this._fallback[id] = base;
        this._current[id] = this._current[id] != null ? this._current[id] : mv;
        this._current[id] = this._current[id] + (mv - this._current[id]) * 0.5;
        params[id] = this._current[id];
        continue;
      }
      // base 层：从当前/回落起点阻尼归位到 resting+微动
      const from = this._current[id] != null ? this._current[id]
                 : (this._fallback[id] != null ? this._fallback[id] : base);
      this._current[id] = from + (base - from) * this._damping;
      if (Math.abs(this._current[id] - base) < 0.002) this._current[id] = base;
      params[id] = this._current[id];
    }
  }
  // 呼吸微动：双频率正弦，幅度随 energy，极小而连续，打破静止僵直
  _microOffset(energy) {
    this._phase += 0.02;
    const amp = this._breathAmp * (1 + 0.5 * energy);
    const s1 = Math.sin(this._phase * 0.9);
    const s2 = Math.sin(this._phase * 0.4 + 1.3);
    return {
      ParamArmLA: s1 * amp, ParamArmRA: -s1 * amp * 0.8,
      ParamArmLB: s2 * amp * 0.6, ParamArmRB: -s2 * amp * 0.6,
      ParamHandL: s1 * amp * 0.4, ParamHandR: -s1 * amp * 0.4,
      ParamHandAngleL: s2 * amp * 0.5, ParamHandAngleR: -s2 * amp * 0.5,
      ParamBodyAngleX: s1 * amp * 0.5,
      ParamBustY: s1 * amp,
    };
  }
}

// 自然 resting 姿态（兼容 Hiyori 与 Haru 两套手/臂参数名；模型不存在的参数注入时被忽略无害）
// 注意：手势动作(wave/hold)写的是 ParamArmL/R(无 A 后缀)，模型 idle 写的是 ParamArmLA/RA，
// 两套都必须纳入 base 层，否则动作结束后会残留僵硬抬臂。
const NATURAL_POSE = {
  // Haru(idle 驱动) 与 Hiyori 的上臂/前臂
  ParamArmLA: 0.12, ParamArmRA: 0.12,   // 上臂自然下垂微张(替代 Haru idle 的僵硬 1)
  ParamArmLB: 0.06, ParamArmRB: 0.06,   // 前臂微屈
  // 手势动作(wave/hold_microphone/raise_hand) 写出的无后缀变体
  ParamArmL: 0.12, ParamArmR: 0.12,
  // Hiyori 手
  ParamHandL: 0.10, ParamHandR: 0.10,
  ParamHandLB: 0.05, ParamHandRB: 0.05,
  // Haru 手
  ParamHandChangeL: 0, ParamHandChangeR: 0,
  ParamHandAngleL: 0.06, ParamHandAngleR: -0.06,
  ParamBustY: 0,
};

// 兼容旧导出：无状态的简单接管(已被 NaturalPoseDriver 取代，保留供外部直接调用)
function applyPosture(params, state) {
  const p = new NaturalPoseDriver();
  p.apply(params, state, {});
}

  // ====================================================================
  // BreathModulator — 呼吸调制器（移植 live2d_breath.py）
  // ====================================================================
  class BreathModulator {
    constructor(opts) {
      opts = opts || {};
      this._phase = 0;
      this._lastTime = 0;
      // restrict=true 时呼吸被抑制（适用"呼吸受限"角色，如刚被唤醒的 AI 实习生）：
      // 幅度压到近乎静止、频率明显放缓，仅在情绪/说话时轻微起伏。
      this._restrict = !!opts.restrict;
      this.BASE_FREQUENCY = this._restrict ? 0.15 : 0.4;
      this.BASE_AMPLITUDE = this._restrict ? 0.02 : 0.15;
    }
    computeBreath(state) {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      const dt = this._lastTime ? (now - this._lastTime) : 0;
      this._lastTime = now;

      let freqMul = 1.0;
      if (state.surprise > 0.3) freqMul = 0.5;
      else if (state.shyness > 0.3) freqMul = 1.0 + 0.3 * state.shyness;
      else if (state.calmness > 0.7) freqMul = 0.8;
      if (state.is_speaking) freqMul *= 1.2;

      let ampMul = 1.0;
      if (state.energy > 0.7) ampMul = 1.0 + 0.1 * (state.energy - 0.7);
      else if (state.energy < 0.3) ampMul = 1.0 - 0.1 * (0.3 - state.energy);

      const frequency = this.BASE_FREQUENCY * freqMul;
      this._phase += 2 * Math.PI * frequency * dt;
      const amplitude = this.BASE_AMPLITUDE * ampMul;
      const breath = 0.5 + amplitude * Math.sin(this._phase);
      return clamp(breath, 0.2, 0.8);
    }
    onTtsBreath() { this._phase = Math.PI / 2; }
  }

  // ====================================================================
  // GazeController — 视线控制器（移植 live2d_gaze.py）
  // ====================================================================
  const GAZE_TARGETS = {
    camera:  { x: 0.0,  y: 0.0,  priority: 0,  duration: null },
    danmaku: { x: 0.3,  y: -0.1, priority: 10, duration: 1.5 },
    book:    { x: -0.2, y: -0.3, priority: 5,  duration: null },
    lilith:  { x: 0.4,  y: 0.1,  priority: 8,  duration: 2.0 },
    think:   { x: 0.0,  y: 0.2,  priority: 3,  duration: null },
  };
  class GazeController {
    constructor() {
      this._currentTarget = 'camera';
      this._targetX = 0.0; this._targetY = 0.0;
      this._currentX = 0.0; this._currentY = 0.0;
      this._sourceX = 0.0; this._sourceY = 0.0;
      this._transitionStart = 0;
      this._gazeUntil = 0;
      this._thinkPhase = 0;
      this.TRANSITION_DURATION = 0.3;
    }
    setTarget(target, duration) {
      const cfg = GAZE_TARGETS[target];
      if (!cfg) return;
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      this._currentTarget = target;
      this._targetX = cfg.x; this._targetY = cfg.y;
      this._sourceX = this._currentX; this._sourceY = this._currentY;
      this._transitionStart = now;
      const d = duration != null ? duration : cfg.duration;
      this._gazeUntil = d != null ? now + d : 0;
    }
    computeParams(state) {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      if (this._gazeUntil > 0 && now > this._gazeUntil) this.setTarget('camera');
      const alpha = this._easeAlpha(now);
      this._currentX = this._sourceX + (this._targetX - this._sourceX) * alpha;
      this._currentY = this._sourceY + (this._targetY - this._sourceY) * alpha;
      let x = this._currentX, y = this._currentY;
      if (this._currentTarget === 'think') {
        this._thinkPhase += 0.05;
        x += Math.sin(this._thinkPhase) * 0.2;
        y += Math.cos(this._thinkPhase * 0.7) * 0.1;
      }
      return {
        ParamEyeBallX: clamp(x, -0.5, 0.5),
        ParamEyeBallY: clamp(y, -0.5, 0.5),
      };
    }
    _easeAlpha(now) {
      if (!this._transitionStart) return 1.0;
      const t = Math.min(1.0, (now - this._transitionStart) / this.TRANSITION_DURATION);
      return easeInOutCubic(t);
    }
    get currentTarget() { return this._currentTarget; }
  }

  // ====================================================================
  // SpeakingHeadDriver — 说话头部驱动器（移植 live2d_speaking_driver.py）
  // ====================================================================
  class SpeakingHeadDriver {
    constructor() {
      this._lastEnergy = 0;
      this._energyHistory = [];
      this._phraseStart = 0;
      this.NOD_AMPLITUDE = 0.06;
      this.TILT_AMPLITUDE = 0.09;
      this.RAISE_AMOUNT = 0.03;
      this.SWAY_AMPLITUDE = 0.09;
    }
    updateAudio(energy) {
      this._energyHistory.push(energy);
      if (this._energyHistory.length > 30) this._energyHistory.shift();
      this._lastEnergy = energy;
    }
    onPhraseStart() {
      this._phraseStart = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
    }
    _detectBeat() {
      const h = this._energyHistory;
      if (h.length < 5) return false;
      const avg = h.reduce(function (a, b) { return a + b; }, 0) / h.length;
      return this._lastEnergy > avg * 1.5 && this._lastEnergy > 0.3;
    }
    _detectQuestionEnding() {
      const h = this._energyHistory;
      if (h.length < 10) return false;
      const sum = function (arr) { return arr.reduce(function (a, b) { return a + b; }, 0); };
      const recent = sum(h.slice(-5)), earlier = sum(h.slice(-10, -5));
      return recent > earlier * 1.2;
    }
    computeParams(state) {
      if (!state.is_speaking) return {};
      const params = {};
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      const intensity = state.speech_intensity;
      if (this._detectBeat()) {
        params.ParamAngleY = -this.NOD_AMPLITUDE * intensity;
      }
      const speakingDuration = now - this._phraseStart;
      if (speakingDuration > 2.0) {
        const sway = Math.sin(now * 2 * Math.PI / 1.2) * this.SWAY_AMPLITUDE * intensity;
        params.ParamAngleX = sway;
        params.ParamAngleZ = sway * 0.5;
      }
      if (this._detectQuestionEnding()) {
        params.ParamAngleY = this.RAISE_AMOUNT * intensity;
      }
      const breathPhase = (now * 2.5) % (2 * Math.PI);
      params.ParamBreath = 0.5 + 0.15 * Math.sin(breathPhase) * intensity;
      return params;
    }
  }

  // ====================================================================
  // EmotionMicroActions — 情绪微动作（移植 live2d_micro_actions.py）
  // ====================================================================
  function applyEmotionMicroActions(params, state) {
    const d = function (p, k, def) { return p[k] != null ? p[k] : def; };
    if (state.shyness > 0.1) {
      const i = state.shyness;
      params.ParamBodyAngleY = d(params, 'ParamBodyAngleY', 0) - 0.06 * i;
      params.ParamAngleY = d(params, 'ParamAngleY', 0) + 0.06 * i;
    }
    if (state.happiness > 0.1) {
      const i = state.happiness;
      params.ParamBodyAngleY = d(params, 'ParamBodyAngleY', 0) + 0.03 * i;
      params.ParamAngleY = d(params, 'ParamAngleY', 0) - 0.03 * i;
    }
    if (state.surprise > 0.1) {
      const i = state.surprise;
      params.ParamBrowForm = d(params, 'ParamBrowForm', 0.5) - 0.1;
      params.ParamAngleZ = d(params, 'ParamAngleZ', 0) - 0.03;
      params.ParamBrowLY = d(params, 'ParamBrowLY', 0.5) + 0.15 * i;
      params.ParamBrowRY = d(params, 'ParamBrowRY', 0.5) + 0.15 * i;
      params.ParamEyeLOpen = d(params, 'ParamEyeLOpen', 0.7) + 0.1 * i;
      params.ParamEyeROpen = d(params, 'ParamEyeROpen', 0.7) + 0.1 * i;
    }
    if (state.sadness > 0.1) {
      const i = state.sadness;
      params.ParamBrowLY = d(params, 'ParamBrowLY', 0.5) - 0.15 * i;
      params.ParamBrowRY = d(params, 'ParamBrowRY', 0.5) - 0.15 * i;
      params.ParamAngleY = d(params, 'ParamAngleY', 0) + 0.06 * i;
    }
    return params;
  }

  // ====================================================================
  // IdleBehaviorScheduler — 空闲行为调度器（移植 live2d_idle_behavior.py）
  // ====================================================================
  const IDLE_BEHAVIORS = {
    light: [
      { name: 'push_glasses', params: { ParamHandR: 0.3 }, duration: 1.0 },
      { name: 'blink', params: { ParamEyeLOpen: 0.1, ParamEyeROpen: 0.1 }, duration: 0.15 },
      { name: 'look_away', params: { ParamAngleX: 0.09, ParamEyeBallX: 0.2 }, duration: 2.0 },
    ],
    medium: [
      { name: 'flip_page', params: { ParamHandL: 0.4 }, duration: 1.5 },
      { name: 'look_window', params: { ParamAngleX: -0.12, ParamEyeBallX: -0.3, ParamEyeBallY: -0.2 }, duration: 3.0 },
    ],
    heavy: [
      { name: 'stand_up', params: { ParamBodyAngleY: 0.12, ParamBodyAngleZ: 0.05 }, duration: 5.0 },
    ],
  };
  class IdleBehaviorScheduler {
    constructor() {
      this.LIGHT_THRESHOLD = 10.0;
      this.MEDIUM_THRESHOLD = 30.0;
      this.HEAVY_THRESHOLD = 120.0;
      this._lastInteraction = 0;
      this._currentBehavior = null;
      this._behaviorEnd = 0;
      this._nextCheck = 0;
    }
    onInteraction() {
      this._lastInteraction = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      this._currentBehavior = null;
      this._behaviorEnd = 0;
    }
    tick(state) {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      if (now < this._nextCheck) return {};
      this._nextCheck = now + 2.0;
      const idleDuration = now - this._lastInteraction;
      if (this._currentBehavior && now > this._behaviorEnd) this._currentBehavior = null;
      if (this._currentBehavior) return this._adjustByEmotion(this._currentBehavior, state);
      let behavior = null;
      if (idleDuration > this.HEAVY_THRESHOLD) behavior = this._pick('heavy');
      else if (idleDuration > this.MEDIUM_THRESHOLD) behavior = this._pick('medium');
      else if (idleDuration > this.LIGHT_THRESHOLD) behavior = this._pick('light');
      if (behavior) {
        this._currentBehavior = behavior;
        this._behaviorEnd = now + behavior.duration;
        return this._adjustByEmotion(behavior, state);
      }
      return {};
    }
    _pick(level) {
      const list = IDLE_BEHAVIORS[level] || [];
      if (!list.length) return null;
      return list[Math.floor(Math.random() * list.length)];
    }
    _adjustByEmotion(behavior, state) {
      let mul = 1.0;
      if (state.shyness > 0.3) mul = 0.6;
      else if (state.happiness > 0.3) mul = 1.2;
      const out = {};
      for (const k in behavior.params) out[k] = behavior.params[k] * mul;
      return out;
    }
  }

  // ====================================================================
  // 表达状态机 — 表情 + 动作 + 优先级（移植 expression_state_machine.py）
  // ====================================================================
  const ExpressionPriority = { IDLE: 0, GAME_STATE: 15, MOTION: 20, SPEAKING: 30 };

  // 时间驱动动作动画函数（progress 0-1, intensity 0-1）→ 参数 dict
  function _animNod(p, i) {
    const angle = p < 0.5
      ? easeInOutCubic(p * 2) * 0.12 * i
      : easeInOutCubic((1 - p) * 2) * 0.12 * i;
    return { ParamAngleY: angle };
  }
  function _animShake(p, i) {
    return { ParamAngleX: Math.sin(p * 2 * Math.PI * 2.5) * 0.10 * i };
  }
  function _animWave(p, i) {
    if (p < 0.2) return { ParamHandR: easeInOutCubic(p / 0.2) * 0.8 * i, ParamArmR: 0.6 * i };
    if (p < 0.8) return {
      ParamHandR: 0.8 * i, ParamArmR: 0.6 * i,
      ParamHandAngleR: Math.sin((p - 0.2) * 2 * Math.PI * 3) * 0.15 * i,
    };
    return { ParamHandR: easeInOutCubic((1 - p) / 0.2) * 0.8 * i, ParamArmR: 0.6 * i };
  }
  function _animTilt(p, i) {
    let angle;
    if (p < 0.2) angle = easeInOutCubic(p / 0.2) * 0.10 * i;
    else if (p < 0.8) angle = 0.10 * i;
    else angle = easeInOutCubic((1 - p) / 0.2) * 0.10 * i;
    return { ParamAngleZ: angle };
  }
  function _animBow(p, i) {
    let angle;
    if (p < 0.4) angle = easeInOutCubic(p / 0.4) * 0.20 * i;
    else if (p < 0.6) angle = 0.20 * i;
    else angle = easeInOutCubic((1 - p) / 0.4) * 0.20 * i;
    return { ParamBodyAngleY: angle };
  }
  function _animClap(p, i) {
    const clap = Math.abs(Math.sin(p * 2 * Math.PI * 4)) * 0.3 * i;
    const base = 0.3 * i;
    return { ParamHandL: base + clap, ParamHandR: base + clap };
  }
  function _animBounce(p, i) {
    return { ParamBodyAngleY: Math.abs(Math.sin(p * 2 * Math.PI * 2)) * 0.08 * i };
  }
  function _animLookAround(p, i) {
    let angle, eyeball;
    if (p < 0.33) { angle = easeInOutCubic(p / 0.33) * 0.12 * i; eyeball = easeInOutCubic(p / 0.33) * 0.3 * i; }
    else if (p < 0.66) {
      angle = easeInOutCubic((p - 0.33) / 0.33) * (-0.24 * i) + 0.12 * i;
      eyeball = easeInOutCubic((p - 0.33) / 0.33) * (-0.6 * i) + 0.3 * i;
    } else { angle = easeInOutCubic((1 - p) / 0.34) * (-0.12 * i); eyeball = easeInOutCubic((1 - p) / 0.34) * (-0.3 * i); }
    return { ParamAngleX: angle, ParamEyeBallX: eyeball };
  }

  // 动作注册表（nod/shake/wave/tilt/bow/clap/bounce/look_around + 3 持续动作）
  const MOTION_REGISTRY = {
    nod: { name: 'nod', fn: _animNod, duration: 0.6 },
    shake: { name: 'shake', fn: _animShake, duration: 0.8 },
    wave: { name: 'wave', fn: _animWave, duration: 2.0 },
    tilt: { name: 'tilt', fn: _animTilt, duration: 1.0 },
    bow: { name: 'bow', fn: _animBow, duration: 1.5 },
    clap: { name: 'clap', fn: _animClap, duration: 2.0 },
    bounce: { name: 'bounce', fn: _animBounce, duration: 1.2 },
    look_around: { name: 'look_around', fn: _animLookAround, duration: 2.5 },
    // 持续动作：参数固定，持续到显式停止
    hold_microphone: { name: 'hold_microphone', static: { ParamHandR: 0.8, ParamArmR: 0.7 }, continuous: true },
    raise_hand: { name: 'raise_hand', static: { ParamHandR: 0.9, ParamArmR: 0.8 }, continuous: true },
    fold_arms: { name: 'fold_arms', static: { ParamHandL: 0.6, ParamHandR: 0.6, ParamArmL: 0.5, ParamArmR: 0.5 }, continuous: true },
  };

  class ExpressionStateMachine {
    constructor() {
      this._currentExpression = 'neutral';
      this._currentIntensity = 0.5;
      this._prevExpression = 'neutral';
      this._prevIntensity = 0.5;
      this._expressionTransitionStart = 0;
      this._activeMotion = null;       // { def, startTime, intensity }
      this._continuousMotions = {};    // name -> active
      this._motionCooldowns = {};      // name -> until
      this._isSpeaking = false;
      this._speakingIntensity = 0;
      this._onExpressionChange = null; // 回调(expressionInfo)
      this.EXPRESSION_TRANSITION_DURATION = 0.5;
    }
    setExpressionCallback(cb) { this._onExpressionChange = cb; }
    get expressionInfo() {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      let alpha = 1.0;
      if (this._expressionTransitionStart > 0) {
        alpha = easeInOutCubic(Math.min(1.0, (now - this._expressionTransitionStart) / this.EXPRESSION_TRANSITION_DURATION));
      }
      return {
        expression: this._currentExpression,
        intensity: this._currentIntensity,
        prev_expression: this._prevExpression,
        prev_intensity: this._prevIntensity,
        transition_alpha: alpha,
      };
    }
    submitDirective(d) {
      const changes = {};
      if (d.expression != null) {
        if (this._updateExpression(d.expression, d.intensity != null ? d.intensity : 0.5)) {
          changes.expression = d.expression; changes.intensity = d.intensity;
        }
      }
      if (d.motion != null) {
        if (this._startMotion(d.motion, d.intensity != null ? d.intensity : 1.0)) changes.motion = d.motion;
      }
      return changes;
    }
    setSpeaking(isSpeaking, intensity) {
      this._isSpeaking = !!isSpeaking;
      this._speakingIntensity = clamp(intensity != null ? intensity : 0, 0, 1);
    }
    _updateExpression(expression, intensity) {
      intensity = clamp(intensity, 0, 1);
      if (expression === this._currentExpression && Math.abs(intensity - this._currentIntensity) < 0.05) return false;
      this._prevExpression = this._currentExpression;
      this._prevIntensity = this._currentIntensity;
      this._currentExpression = expression;
      this._currentIntensity = intensity;
      this._expressionTransitionStart = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      if (this._onExpressionChange) {
        try { this._onExpressionChange(this.expressionInfo); } catch (e) {}
      }
      return true;
    }
    _startMotion(name, intensity) {
      const def = MOTION_REGISTRY[name];
      if (!def) return false;
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      if (this._motionCooldowns[name] && now < this._motionCooldowns[name]) return false;
      intensity = clamp(intensity, 0, 1);
      const active = { def: def, startTime: now, intensity: intensity };
      if (def.continuous) {
        this._continuousMotions[name] = active;
      } else {
        if (!this._activeMotion || this._isExpired(this._activeMotion, now)) {
          this._activeMotion = active;
        } else if (this._activeMotion.def.name === name) {
          this._activeMotion = active; // 同名动作重新开始
        } else {
          return false; // 有更高/其他动作在执行，排队策略：简单丢弃
        }
      }
      return true;
    }
    stopContinuous(name) {
      delete this._continuousMotions[name];
    }
    _isExpired(m, now) {
      if (m.def.continuous || m.def.duration < 0) return false;
      return (now - m.startTime) >= m.def.duration;
    }
    _progress(m, now) {
      if (m.def.continuous || m.def.duration <= 0) return 1.0;
      return clamp((now - m.startTime) / m.def.duration, 0, 1);
    }
    // 是否有动作在播（时间驱动未过期，或存在持续动作）——供姿态控制器做动作回落
    get hasActiveMotion() {
      if (this._continuousMotions && Object.keys(this._continuousMotions).length > 0) return true;
      const m = this._activeMotion;
      if (!m) return false;
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      return !this._isExpired(m, now);
    }
    // 计算当前帧动作参数（合并时间驱动 + 持续动作）
    computeMotionParams() {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      const out = {};
      if (this._activeMotion && !this._isExpired(this._activeMotion, now)) {
        const m = this._activeMotion;
        if (m.def.continuous) {
          if (m.def.static) for (const k in m.def.static) out[k] = m.def.static[k] * m.intensity;
        } else if (m.def.fn) {
          Object.assign(out, m.def.fn(this._progress(m, now), m.intensity));
        }
      } else if (this._activeMotion) {
        this._activeMotion = null; // 过期清除
      }
      for (const name in this._continuousMotions) {
        const m = this._continuousMotions[name];
        if (m.def.static) for (const k in m.def.static) out[k] = m.def.static[k] * m.intensity;
      }
      return out;
    }
  }

  // ====================================================================
  // 表情名 → 5D 状态映射表（移植 continuous_expression_driver.EXPRESSION_STATE_MAP）
  // ====================================================================
  const EXPRESSION_STATE_MAP = {
    happy:     { happiness: 0.8, calmness: 0.2, energy: 0.7, openness: 0.7 },
    shy:       { shyness: 0.8, calmness: 0.2, energy: 0.3, openness: 0.3 },
    sad:       { sadness: 0.7, calmness: 0.3, energy: 0.2, openness: 0.4 },
    surprised: { surprise: 0.8, calmness: 0.2, energy: 0.8, openness: 0.8 },
    calm:      { calmness: 0.9, energy: 0.4, openness: 0.5 },
    curious:   { surprise: 0.3, happiness: 0.2, calmness: 0.5, energy: 0.6, openness: 0.6 },
    angry:     { sadness: 0.4, energy: 0.9, openness: 0.2, calmness: 0.1 },
    excited:   { happiness: 0.7, surprise: 0.3, energy: 0.9, openness: 0.8, calmness: 0.0 },
    thinking:  { calmness: 0.6, energy: 0.4, openness: 0.3 },
    neutral:   { calmness: 0.8, energy: 0.5, openness: 0.5 },
  };

  // ====================================================================
  // Live2DDriver — 整合驱动（30Hz rAF 参数注入）
  // ====================================================================
  class Live2DDriver {
    /**
     * @param {object} opts
     * @param {object} opts.model - 已加载的 PIXI Live2DModel
     * @param {string} [opts.characterId] - 角色标识
     * @param {number} [opts.fps] - 注入频率（默认 30）
     * @param {object} [opts.expressionMap] - 自定义表情名→5D状态映射（覆盖默认）
     */
    constructor(opts) {
      const o = opts || {};
      this._model = o.model;
      this._characterId = o.characterId || 'default';
      this._fps = o.fps || 30;
      this._expressionMap = Object.assign({}, EXPRESSION_STATE_MAP, o.expressionMap || {});

      this._interp = new StateInterpolator();
      this._mood = new EmotionMoodDriver({ onTarget: (t) => this._interp.setTarget(t) });
      this._pose = new NaturalPoseDriver();
      this._breath = new BreathModulator({ restrict: !!o.restrictBreath });
      this._gaze = new GazeController();
      this._speaking = new SpeakingHeadDriver();
      this._idle = new IdleBehaviorScheduler();
      this._sm = new ExpressionStateMachine();

      // 表情状态机 → 插值器目标
      this._sm.setExpressionCallback(this._onExpressionChanged.bind(this));

      this._running = false;
      this._rafId = null;
      this._lastTick = 0;
      this._lipSyncValue = 0;
      this._onFrame = o.onFrame || null;
      this._fixedParams = {};   // 外部固定参数（最低优先级）
    }

    // ---- 生命周期 ----
    start() {
      if (this._running) return;
      this._running = true;
      this._lastTick = 0;
      this._idle.onInteraction();
      this._attachInjectHook();
      this._loop(this._tick.bind(this));
    }
    stop() {
      this._running = false;
      this._detachInjectHook();
      if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
    }
    // 动画注入钩子：pixi-live2d-display 每帧在 motionManager.update() 里推进模型内置动画
    // 并写参数，随后触发 internalModel 的 "afterMotionUpdate"。rAF 注入发生在其前，若模型
    // 正在播内置 Idle 动作（如 Hiyori 的 ParamBreath 呼吸曲线 ±0.45），动画会覆盖驱动注入，
    // 导致 restrictBreath(幅度0.02) 失效。改在 afterMotionUpdate 之后写回，驱动参数必然覆盖动画。
    _attachInjectHook() {
      const m = this._model && this._model.internalModel;
      if (!m || typeof m.on !== 'function') return;
      if (!this._frameInject) {
        this._frameInject = () => { if (this._lastParams) this._inject(this._lastParams); };
      }
      m.on("afterMotionUpdate", this._frameInject);
      this._hookAttached = true;
    }
    _detachInjectHook() {
      const m = this._model && this._model.internalModel;
      if (this._hookAttached && m && typeof m.off === 'function') {
        m.off("afterMotionUpdate", this._frameInject);
      }
      this._hookAttached = false;
    }
    _loop(fn) {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
      const interval = 1 / this._fps;
      fn(now);
      this._rafId = requestAnimationFrame(this._loop.bind(this, fn));
    }

    // ---- 公开 API ----
    setEmotion(name, intensity) {
      const base = this._expressionMap[name];
      if (!base) return false;
      this._sm.submitDirective({ expression: name, intensity: intensity != null ? intensity : 1.0 });
      return true;
    }
    playMotion(name, intensity) {
      return this._sm.submitDirective({ motion: name, intensity: intensity != null ? intensity : 1.0 });
    }
    stopContinuous(name) { this._sm.stopContinuous(name); }
    setSpeaking(isSpeaking, intensity) {
      this._sm.setSpeaking(isSpeaking, intensity);
      if (isSpeaking) this._speaking.onPhraseStart();
    }
    setLipSync(mouthOpen) { this._lipSyncValue = clamp(mouthOpen != null ? mouthOpen : 0, 0, 1); }
    updateAudioEnergy(energy) { this._speaking.updateAudio(energy); }
    onInteraction() { this._idle.onInteraction(); }
    setGazeTarget(target, duration) { this._gaze.setTarget(target, duration); }
    onTtsBreath() { this._breath.onTtsBreath(); }
    setStableParameter(name, value) { this._fixedParams[name] = value; }
    get model() { return this._model; }
    get state() { return this._interp.current; }

    // ---- 表情变化 → 插值器 ----
    _onExpressionChanged(info) {
      const base = this._expressionMap[info.expression] || this._expressionMap.neutral;
      const target = new Live2DState(Object.assign({}, base, {
        is_speaking: this._sm._isSpeaking,
        speech_intensity: this._sm._speakingIntensity,
        gaze_target: this._gaze.currentTarget,
      }));
      this._interp.setTarget(target);
    }

    // ---- 每帧：插值 → 计算 → 合并 → 注入 ----
    _tick(now) {
      if (!this._running || !this._model) return;
      const dt = this._lastTick ? (now - this._lastTick) : 0;
      this._lastTick = now;

      // 0. 情绪基调驱动：无外部表情指令时由 MoodDriver 持续演化目标(平静基底+微表情)
      const explicitExpr = this._sm._currentExpression && this._sm._currentExpression !== 'neutral';
      if (!explicitExpr) this._mood.tick(now, { is_speaking: this._sm._isSpeaking });

      // 1. 状态插值
      const state = this._interp.tick();
      state.is_speaking = this._sm._isSpeaking;
      state.speech_intensity = this._sm._speakingIntensity;
      state.motion_params = this._sm.computeMotionParams();

      // 2. 表情基线参数
      const params = computeExpressionParams(state);

      // 3. 动作参数（已在 computeExpressionParams 内通过 motion_params 合并）
      // 4. 视线参数覆盖
      Object.assign(params, this._gaze.computeParams(state));

      // 5. 说话头部驱动覆盖
      if (state.is_speaking) Object.assign(params, this._speaking.computeParams(state));

      // 6. 呼吸调制（覆盖）
      params.ParamBreath = this._breath.computeBreath(state);

      // 7. 情绪微动作叠加
      applyEmotionMicroActions(params, state);

      // 8. 空闲行为叠加
      if (!state.is_speaking) Object.assign(params, this._idle.tick(state));

      // 9. 口型同步（最高优先级，说话时独占 mouth 类参数）
      if (this._lipSyncValue > 0.001) {
        params.ParamMouthOpenY = this._lipSyncValue;
        params.ParamMouthForm = this._lipSyncValue;
      }

      // 10. 外部固定参数（最低优先级，先写入再被覆盖）
      const finalParams = Object.assign({}, this._fixedParams, params);

      // 10.5 姿态接管：base 层恒覆盖自然 resting+呼吸微动，仅动作显式写出的参数临时接管，动作结束阻尼归位
      this._pose.apply(finalParams, state, {
        motionActive: this._sm.hasActiveMotion,
        motionParams: state.motion_params || {},
      });

      // 11. 注入模型：优先由 afterMotionUpdate 钩子(动画写参数后)注入，确保覆盖模型内置动画；
      //     无钩子(模型 internalModel 不支持 on 事件)时回退为 rAF 直接注入，避免完全不渲染。
      this._lastParams = finalParams;
      if (!this._hookAttached) this._inject(finalParams);

      if (this._onFrame) {
        try { this._onFrame({ params: finalParams, state: state, dt: dt }); } catch (e) {}
      }
    }

    _inject(params) {
      try {
        const core = this._model.internalModel.coreModel;
        if (!core || typeof core.setParameterValueById !== 'function') return;
        for (const id in params) {
          try { core.setParameterValueById(id, params[id]); } catch (e) {}
        }
      } catch (e) {}
    }
  }

  // ====================================================================
  // 导出
  // ====================================================================
  const Live2DDriverLib = {
    Live2DDriver: Live2DDriver,
    Live2DState: Live2DState,
    ExpressionStateMachine: ExpressionStateMachine,
    StateInterpolator: StateInterpolator,
    EmotionMoodDriver: EmotionMoodDriver,
    MOOD_ARCHETYPES: MOOD_ARCHETYPES,
    NaturalPoseDriver: NaturalPoseDriver,
    applyPosture: applyPosture,
    BreathModulator: BreathModulator,
    GazeController: GazeController,
    SpeakingHeadDriver: SpeakingHeadDriver,
    IdleBehaviorScheduler: IdleBehaviorScheduler,
    computeExpressionParams: computeExpressionParams,
    applyEmotionMicroActions: applyEmotionMicroActions,
    MOTION_REGISTRY: MOTION_REGISTRY,
    EXPRESSION_STATE_MAP: EXPRESSION_STATE_MAP,
    easing: { easeInOutCubic: easeInOutCubic, easeOutCubic: easeOutCubic, easeInCubic: easeInCubic, easeOutBack: easeOutBack },
  };
  global.Live2DDriver = Live2DDriverLib;
  if (typeof module !== 'undefined' && module.exports) { module.exports = Live2DDriverLib; }

})(typeof window !== 'undefined' ? window : this);
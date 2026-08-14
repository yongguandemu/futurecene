"""events.py — Event Schema Registry（v1.0）

所有事件名唯一来源。新增事件必须在此定义，禁止在业务代码中手写字符串。
命名：{domain}:{action}，全小写。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · events · 事件名唯一来源，对外暴露全部事件常量 + ALL_EVENTS
2. 配置契约：无配置（纯常量声明模块）
3. 输入契约：无输入接口（仅常量定义，供 EventBus 校验引用）
4. 输出契约：导出 {domain}:{action} 全小写事件常量；ALL_EVENTS frozenset 供 EventBus 校验
5. 依赖声明：无（纯常量，无 import）
6. 错误定义：无运行时错误；新增事件未收录 ALL_EVENTS 将不被 EventBus 校验通过
7. 生命周期方法：无（纯声明模块）
8. 领域状态说明：ALL_EVENTS 已注册事件全集（唯一校验来源）
"""

# ========== 指挥官域 ==========
COMMAND_RECEIVED = "commander:command_received"      # 指挥官收到新指令
COMMAND_ROUTED = "commander:command_routed"           # 指令已路由到调度官
COMMAND_COMPLETED = "commander:command_completed"     # 指令执行完成
COMMAND_FAILED = "commander:command_failed"           # 指令执行失败

# ========== 会话域 ==========
SESSION_CREATED = "session:created"                   # 会话创建
SESSION_SWITCHED = "session:switched"                 # 角色/场景切换
SESSION_STATE_CHANGED = "session:state_changed"       # 会话状态变更

# ========== 开关域 ==========
SWITCH_CHANGED = "switch:changed"                     # 开关状态变化

# ========== LLM 域 ==========
LLM_REQUESTED = "llm:requested"                       # LLM 调用请求
LLM_RESPONDED = "llm:responded"                       # LLM 响应完成（含完整文本）
LLM_STREAM_CHUNK = "llm:stream_chunk"                 # LLM 流式分片
LLM_FAILED = "llm:failed"                             # LLM 调用失败

# ========== TTS 域 ==========
TTS_REQUESTED = "tts:requested"                       # TTS 合成请求
TTS_COMPLETED = "tts:completed"                       # TTS 合成完成
TTS_FAILED = "tts:failed"                             # TTS 合成失败
TTS_AUDIO_READY = "tts:audio_ready"                   # 音频就绪（payload 含音频 ID/路径）

# ========== Live2D 域 ==========
LIVE2D_LOADED = "live2d:loaded"                       # 模型加载完成
LIVE2D_EXPRESSION_CHANGED = "live2d:expression_changed"  # 表情切换
LIVE2D_MOTION_TRIGGERED = "live2d:motion_triggered"   # 动作触发
LIVE2D_LIP_SYNC_START = "live2d:lip_sync_start"       # 口型同步开始
LIVE2D_LIP_SYNC_END = "live2d:lip_sync_end"           # 口型同步结束

# ========== B站平台域 ==========
BILIBILI_CONNECTED = "bilibili:connected"             # B站连接建立
BILIBILI_DISCONNECTED = "bilibili:disconnected"       # B站连接断开

# ========== 归一化观众域（多平台统一） ==========
DANMAKU_RECEIVED = "danmaku:received"                 # 弹幕（归一化后）
GIFT_RECEIVED = "gift:received"                       # 礼物（归一化后）
GUARD_RECEIVED = "guard:received"                     # 上舰（归一化后）
SUPERCHAT_RECEIVED = "superchat:received"             # SuperChat（归一化后）
AUDIENCE_ENTERED = "audience:entered"                 # 进场（归一化后）
AUDIENCE_FILTERED = "audience:filtered"               # 被安全过滤拦截

# ========== 记忆域 ==========
MEMORY_STORED = "memory:stored"                       # 记忆已写入
MEMORY_RETRIEVED = "memory:retrieved"                 # 记忆已检索
MEMORY_CONSOLIDATED = "memory:consolidated"           # 短期→长期固化完成

# ========== 安全域 ==========
SAFETY_BLOCKED = "safety:blocked"                     # 内容被拦截
SAFETY_FLAGGED = "safety:flagged"                     # 内容被标记（需人工确认）

# ========== 游戏实况域 ==========
GAME_VN_STATE_CHANGED = "game:vn_state_changed"       # VN 画面状态变化
GAME_MC_STATE_CHANGED = "game:mc_state_changed"       # MC 状态变化
GAME_COMMENTARY_REQUESTED = "game:commentary_requested"  # 请求生成解说

# ========== 前端域 ==========
FRONTEND_STATUS_UPDATE = "frontend:status_update"     # 系统状态推送（供总控台）
FRONTEND_SUBTITLE_UPDATE = "frontend:subtitle_update" # 字幕更新（供字幕叠加层）

# ========== 音频域 ==========
AUDIO_SEGMENT_READY = "audio:segment_ready"           # 音频分片就绪（供播放/口型）

# ========== 运维域 ==========
COST_CIRCUIT_OPEN = "cost:circuit_open"               # 成本熔断器触发（P5）
STATE_CHANGED = "state:changed"                       # 状态快照推送（含 version，供前端全量更新）

# ========== QQ 平台域（P2） ==========
QQ_CONNECTED = "qq:connected"                         # QQ 连接建立
QQ_DISCONNECTED = "qq:disconnected"                   # QQ 连接断开
QQ_GROUP_MESSAGE = "qq:group_message"                 # 群聊 @ 消息
QQ_C2C_MESSAGE = "qq:c2c_message"                     # 单聊消息
QQ_CHANNEL_MESSAGE = "qq:channel_message"             # 频道 @ 消息

# ========== OBS 域（P2） ==========
OBS_CONNECTED = "obs:connected"                       # OBS 连接成功
OBS_DISCONNECTED = "obs:disconnected"                 # OBS 连接断开
OBS_STREAM_STARTED = "obs:stream_started"             # OBS 推流开始
OBS_STREAM_STOPPED = "obs:stream_stopped"             # OBS 推流停止
OBS_SCENE_CHANGED = "obs:scene_changed"               # 场景切换

# ========== VTS 域（P2） ==========
VTS_CONNECTED = "vts:connected"                       # VTube Studio 连接
VTS_DISCONNECTED = "vts:disconnected"                 # VTube Studio 断开

# ========== 无人值守直播域（P2） ==========
STREAM_STATE_CHANGED = "stream:state_changed"         # 直播状态变更（idle/starting/live/stopping/failed）

# ========== 音乐域（P2） ==========
MUSIC_STATE_CHANGED = "music:state_changed"           # 播放状态变更（供 VoiceBridge 互斥）
MUSIC_SONG_REQUESTED = "music:song_requested"         # 点歌请求已入队

# ========== 经验学习域（P2） ==========
EXPERIENCE_RECORDED = "experience:recorded"           # 经验三元组已回写
EXPERIENCE_QUERIED = "experience:queried"             # 经验检索完成
EXPERIENCE_GOAL_COMPLETED = "experience:goal_completed"  # 外部任务完成/超时释放

# ========== 直播间智能域（P1 精细子模块） ==========
DANMAKU_REACTED = "danmaku:reacted"                   # 弹幕反应器产出回复
DANMAKU_POOLED = "danmaku:pooled"                     # 弹幕已入池
SPEECH_ENQUEUED = "speech:enqueued"                   # 发言已入队
SPEECH_DEQUEUED = "speech:dequeued"                   # 发言已出队
CONTEXT_SNAPSHOT_READY = "context:snapshot_ready"     # 情境快照已组装
COMMENTARY_GENERATED = "commentary:generated"         # VN 解说已生成
PACE_DECIDED = "pace:decided"                         # 解说节奏已决策

# ========== 热度追踪域（P2） ==========
HEAT_UPDATED = "heat:updated"                         # 直播间热度指标更新

# ========== 主动对话域（P2 补全） ==========
ACTIVE_DIALOGUE = "dialogue:active"                     # 主动对话生成（冷场救星）

# ========== 注册表（实现辅助，供 EventBus 校验） ==========
# 所有已注册事件名集合。新增事件常量后，此处须同步收录（唯一校验来源）。
ALL_EVENTS = frozenset({
    COMMAND_RECEIVED,
    COMMAND_ROUTED,
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    SESSION_CREATED,
    SESSION_SWITCHED,
    SESSION_STATE_CHANGED,
    SWITCH_CHANGED,
    LLM_REQUESTED,
    LLM_RESPONDED,
    LLM_STREAM_CHUNK,
    LLM_FAILED,
    TTS_REQUESTED,
    TTS_COMPLETED,
    TTS_FAILED,
    TTS_AUDIO_READY,
    LIVE2D_LOADED,
    LIVE2D_EXPRESSION_CHANGED,
    LIVE2D_MOTION_TRIGGERED,
    LIVE2D_LIP_SYNC_START,
    LIVE2D_LIP_SYNC_END,
    BILIBILI_CONNECTED,
    BILIBILI_DISCONNECTED,
    DANMAKU_RECEIVED,
    GIFT_RECEIVED,
    GUARD_RECEIVED,
    SUPERCHAT_RECEIVED,
    AUDIENCE_ENTERED,
    AUDIENCE_FILTERED,
    MEMORY_STORED,
    MEMORY_RETRIEVED,
    MEMORY_CONSOLIDATED,
    SAFETY_BLOCKED,
    SAFETY_FLAGGED,
    GAME_VN_STATE_CHANGED,
    GAME_MC_STATE_CHANGED,
    GAME_COMMENTARY_REQUESTED,
    FRONTEND_STATUS_UPDATE,
    FRONTEND_SUBTITLE_UPDATE,
    AUDIO_SEGMENT_READY,
    COST_CIRCUIT_OPEN,
    STATE_CHANGED,
    QQ_CONNECTED,
    QQ_DISCONNECTED,
    QQ_GROUP_MESSAGE,
    QQ_C2C_MESSAGE,
    QQ_CHANNEL_MESSAGE,
    OBS_CONNECTED,
    OBS_DISCONNECTED,
    OBS_STREAM_STARTED,
    OBS_STREAM_STOPPED,
    OBS_SCENE_CHANGED,
    VTS_CONNECTED,
    VTS_DISCONNECTED,
    STREAM_STATE_CHANGED,
    MUSIC_STATE_CHANGED,
    MUSIC_SONG_REQUESTED,
    EXPERIENCE_RECORDED,
    EXPERIENCE_QUERIED,
    EXPERIENCE_GOAL_COMPLETED,
    DANMAKU_REACTED,
    DANMAKU_POOLED,
    SPEECH_ENQUEUED,
    SPEECH_DEQUEUED,
    CONTEXT_SNAPSHOT_READY,
    COMMENTARY_GENERATED,
    PACE_DECIDED,
    HEAT_UPDATED,
    ACTIVE_DIALOGUE,
})

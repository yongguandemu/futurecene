"""config_loader.py — 配置加载与启动校验

职责：
1. 加载项目根目录 .env 文件（python-dotenv）。
2. 校验必填环境变量：硬缺（非 B站）打印错误并退出；B站为"延后必填"（缺失仅警告，
   接入 B站时补齐即恢复，避免阻塞其他功能运行）。
3. ConfigLoader 类：加载 config/config.yaml，解析 ${{ env.XXX }} 占位符为真实环境变量，
   缺失即抛 ConfigError（规格书 6.2 密钥管理规则）。

密钥管理规则（规格书 6.2 P0 生效）：密钥只从环境变量/.env 读取，
config/ 下任何配置文件不写明文密钥，只写 ${{ env.XXX }} 占位符。

# 模块内容清单（8 项契约）
1. 模块身份标识：shared · ConfigLoader · 对外接口 load()/validate_or_exit()/get_missing_env_vars()/ConfigLoader.get()/snapshot()
2. 配置契约：加载项目根 .env（python-dotenv）与 config/config.yaml；解析 ${{ env.XXX }} 占位符，密钥只从环境变量读取
3. 输入契约：load(env_path, required) 启动加载；ConfigLoader.get(dotted_key, default) 点路径取值
4. 输出契约：get() 返回解析后真实值；snapshot() 返回完整配置字典；validate_or_exit() 缺失打印并 sys.exit(1)
5. 依赖声明：os、re、sys、pathlib、typing、dotenv（可选降级）、yaml（可选降级）
6. 错误定义：ConfigError（占位符引用的环境变量缺失/配置文件不存在/PyYAML 未安装）；硬缺必填变量 sys.exit(1)
7. 生命周期方法：load()（启动入口）/validate_or_exit()（启动校验）
8. 领域状态说明：_data 解析后配置字典、_env 环境变量提供器、PROJECT_ROOT/CONFIG_FILE 常量
"""
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # 降级：未安装 python-dotenv 时跳过 .env 加载
    load_dotenv = None

try:
    import yaml
except ImportError:
    yaml = None

# 项目根目录：src/shared/config_loader.py 上溯三级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

# 必填环境变量清单（v1.1，与规格书 6.2 节一致）
MANDATORY_ENV_VARS: List[str] = [
    "OPENAI_API_KEY",
    "ZHIPU_API_KEY",
    "DASHSCOPE_API_KEY",
    "WUSOUND_API_KEY",
    "BILIBILI_ACCESS_KEY_ID",
    "BILIBILI_ACCESS_KEY_SECRET",
    "BILIBILI_COOKIE",
    "OBS_WS_PASSWORD",
]

# 延后必填：B站三项（缺失仅警告不退出；接入 B站时补齐即恢复硬校验）
# TODO: 确认 — 用户约定 B站密钥暂不迁移，手动接入时填写
DEFERRED_ENV_VARS: List[str] = [
    "BILIBILI_ACCESS_KEY_ID",
    "BILIBILI_ACCESS_KEY_SECRET",
    "BILIBILI_COOKIE",
]

_PLACEHOLDER_RE = re.compile(r"^\$\{\{\s*env\.([A-Za-z0-9_]+)\s*\}\}$")


class ConfigError(Exception):
    """配置错误（占位符引用的环境变量缺失等）。"""


def get_missing_env_vars(required: Optional[List[str]] = None) -> List[str]:
    """返回缺失（不存在或为空字符串）的必填变量名列表。"""
    required = required or MANDATORY_ENV_VARS
    return [name for name in required if not os.environ.get(name)]


def validate_or_exit(required: Optional[List[str]] = None,
                     deferred: Optional[List[str]] = None) -> None:
    """校验必填环境变量；硬缺打印错误并 sys.exit(1)，延后缺打印警告继续。"""
    required = required or MANDATORY_ENV_VARS
    deferred = DEFERRED_ENV_VARS if deferred is None else deferred
    hard = [name for name in required if name not in deferred and not os.environ.get(name)]
    soft = [name for name in deferred if not os.environ.get(name)]
    if hard:
        for name in hard:
            print(f"[config] missing required env: {name}")
        print(f"[config] 共缺失 {len(hard)} 个必填变量，请在项目根目录 .env 或系统环境中配置（参考 .env.example）")
        sys.exit(1)
    for name in soft:
        print(f"[config] warning: 延后必填变量缺失（接入 B站前可忽略）: {name}")


def load(env_path: Optional[str] = None, required: Optional[List[str]] = None) -> None:
    """应用启动入口：加载 .env 并校验必填变量。"""
    dotenv_path = env_path or str(PROJECT_ROOT / ".env")
    if load_dotenv is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    else:
        print("[config] warning: python-dotenv 未安装，跳过 .env 加载，仅校验系统环境变量")
    validate_or_exit(required)


class ConfigLoader:
    """加载 config/config.yaml 并解析 ${{ env.XXX }} 占位符。

    模块通过 get("llm.openai.api_key") 获取已解析的真实配置值，
    完全不感知密钥来源（规格书 6.2 密钥管理规则落地）。
    """

    def __init__(self, config_path: str = "", env_provider: Optional[callable] = None):
        self._path = Path(config_path) if config_path else CONFIG_FILE
        self._env = env_provider or os.getenv
        self._data: Dict[str, Any] = {}
        if yaml is None:
            raise ConfigError("PyYAML 未安装，请执行 pip install pyyaml")
        if not self._path.exists():
            raise ConfigError(f"配置文件不存在: {self._path}")
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        self._data = self._resolve(raw, "")

    def _resolve(self, value: Any, path: str) -> Any:
        """递归解析占位符；硬缺环境变量抛 ConfigError，延后必填（B站）缺失放行为空。"""
        if isinstance(value, str):
            m = _PLACEHOLDER_RE.match(value.strip())
            if m:
                name = m.group(1)
                real = self._env(name)
                if real is None or real == "":
                    if name in DEFERRED_ENV_VARS:
                        return ""  # 延后必填（B站）：缺失放行，接入时补齐
                    raise ConfigError(
                        f"config 占位符引用了缺失的环境变量: {name}（{path or self._path.name}）"
                    )
                return real
            return value
        if isinstance(value, dict):
            return {k: self._resolve(v, f"{path}.{k}" if path else k)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, path) for v in value]
        return value

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按点路径取值：get("llm.openai.api_key")。"""
        node: Any = self._data
        for part in dotted_key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def snapshot(self) -> Dict[str, Any]:
        """解析后的完整配置（含真实密钥，仅内部使用，勿外泄）。"""
        return dict(self._data)

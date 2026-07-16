"""
配置加载器

从 config.yaml 和本机 local.yaml 加载配置
"""

import json
import copy
import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional


def set_yaml_config_value(config_path: Path, key: str, value: Any) -> None:
    """将值写入 YAML 配置中的点号路径键。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件根节点必须是映射: {config_path}")
        data = loaded

    parts = key.split(".")
    if any(not part for part in parts):
        raise ValueError(f"无效配置键: {key}")

    cursor = data
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"配置路径不是映射: {part}")
        cursor = child
    cursor[parts[-1]] = value

    config_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


class Config:
    """配置管理器"""

    def __init__(
        self,
        config_path: Optional[str] = None,
        local_config_path: Optional[str] = None,
    ):
        """
        初始化配置管理器

        Args:
            config_path: 基础配置文件路径，默认为 config/config.yaml
            local_config_path: 本机配置文件路径，默认使用 config/local.yaml；
                显式指定 config_path 时默认不加载本机配置
        """
        # 确定配置文件路径
        use_default_config = config_path is None
        if config_path is None:
            # 获取项目根目录
            project_root = Path(__file__).parent.parent.parent
            config_path = project_root / "config" / "config.yaml"
        else:
            config_path = Path(config_path)

        # 加载 YAML 配置
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            base_config = yaml.safe_load(f) or {}
        if not isinstance(base_config, dict):
            raise ValueError(f"配置文件根节点必须是映射: {config_path}")

        self._config: Dict[str, Any] = copy.deepcopy(base_config)
        if local_config_path is None and use_default_config:
            local_config_path = str(config_path.parent / "local.yaml")
        self._local_config_path = Path(local_config_path) if local_config_path else None

        if self._local_config_path and self._local_config_path.exists():
            with open(self._local_config_path, "r", encoding="utf-8") as f:
                local_config = yaml.safe_load(f) or {}
            if not isinstance(local_config, dict):
                raise ValueError(
                    f"本机配置文件根节点必须是映射: {self._local_config_path}"
                )
            self._deep_merge(self._config, local_config)

        self._project_root = Path(__file__).parent.parent.parent
        self._resolved_embedding_dim = self._load_persisted_embedding_dim()

    @staticmethod
    def _deep_merge(target: Dict[str, Any], overrides: Dict[str, Any]) -> None:
        """将本机配置递归合并到基础配置。"""
        for key, value in overrides.items():
            if (
                key in target
                and isinstance(target[key], dict)
                and isinstance(value, dict)
            ):
                Config._deep_merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值 (支持点号分隔的多级 key)

        Args:
            key: 配置 key，支持 "storage.vault_dir" 格式
            default: 默认值

        Returns:
            配置值

        Example:
            >>> config = Config()
            >>> config.get("storage.vault_dir")
            ".data/vault"
        """
        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def get_workflow_config(self, workflow_name: str) -> Dict[str, Any]:
        """
        获取工作流配置（优先加载 config/workflows 下的 YAML）。

        Args:
            workflow_name: 工作流名称

        Returns:
            工作流配置字典

        Raises:
            ValueError: workflow_name 为空
            FileNotFoundError: 未找到工作流配置
        """
        if not workflow_name or not workflow_name.strip():
            raise ValueError("workflow_name 不能为空")

        workflow_dir = self._project_root / "config" / "workflows"
        name_variants = [
            workflow_name,
            workflow_name.replace("_", "-"),
            workflow_name.replace("-", "_"),
        ]

        for name in dict.fromkeys(name_variants):
            workflow_path = workflow_dir / f"{name}.yaml"
            if workflow_path.exists():
                with open(workflow_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}

        # 兼容 config.yaml 中的 workflows 配置
        workflow_key = workflow_name.replace("-", "_")
        legacy_config = self.get(f"workflows.{workflow_key}")
        if legacy_config is None:
            raise FileNotFoundError(f"工作流配置不存在: {workflow_name}")

        steps = legacy_config.get("steps")
        if isinstance(steps, list) and steps and isinstance(steps[0], str):
            step_type_map = {
                "fetch": "fetch_content",
                "analyze": "ai_analyze",
                "sharpen": "idea_sharpen",
                "store": "store_entry",
            }
            steps = [
                {"id": step_name, "type": step_type_map.get(step_name, step_name)}
                for step_name in steps
            ]

        merged_config = {"name": workflow_name}
        merged_config.update(legacy_config)
        if steps is not None:
            merged_config["steps"] = steps
        return merged_config

    def get_env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """读取仅用于运行隔离的进程环境变量，不加载 .env 文件。"""
        return os.getenv(key, default)

    @property
    def vault_dir(self) -> Path:
        """Markdown Vault 目录"""
        path = self.get_env("VAULT_DIR") or self.get("storage.vault_dir", ".data/vault")
        return self._project_root / path

    @property
    def data_dir(self) -> Path:
        """数据根目录。"""
        data_dir_str = self.get_env("DATA_DIR") or self.get("storage.data_dir")
        if data_dir_str:
            return self._project_root / data_dir_str
        return self.db_path.parent.parent

    @property
    def db_path(self) -> Path:
        """SQLite 数据库路径"""
        db_path_str = self.get_env("DB_PATH") or self.get(
            "storage.db_path", ".data/db/knowledge_vault.db"
        )
        return self._project_root / db_path_str

    @property
    def vector_index_dir(self) -> Path:
        """向量索引目录"""
        path = (
            self.get_env("VECTOR_STORE_PATH")
            or self.get_env("VECTOR_DIR")
            or self.get("storage.vector_index_dir", ".data/vectors")
        )
        return self._project_root / path

    @property
    def log_dir(self) -> Path:
        """日志目录"""
        path = self.get("storage.log_dir", ".data/logs")
        return self._project_root / path

    @property
    def tmp_dir(self) -> Path:
        """临时文件目录"""
        path = self.get("storage.tmp_dir", ".data/tmp")
        return self._project_root / path

    @property
    def runtime_embedding_dim_path(self) -> Path:
        """自动探测出的 Embedding 维度缓存文件路径。"""
        return self.data_dir / "runtime" / "embedding_dim.json"

    @property
    def embedding_runtime_fingerprint(self) -> Dict[str, str]:
        """当前 Embedding 配置指纹，用于校验运行期维度缓存是否仍然有效。"""
        return {
            "base_url": self.embd_base_url,
            "embedding_model": self.embd_model,
        }

    def embedding_index_fingerprint(self, dim: int) -> Dict[str, str]:
        """当前 Embedding 索引契约指纹，不包含 API Key 等敏感信息。"""
        return {
            "base_url": self.embd_base_url,
            "embedding_model": self.embd_model,
            "embedding_dim": str(int(dim)),
        }

    @property
    def llm_api_key(self) -> Optional[str]:
        """OpenAI-compatible LLM API Key。"""
        return self.get("ai.llm.api_key")

    @property
    def llm_base_url(self) -> str:
        """OpenAI-compatible LLM API Base URL。"""
        return self.get("ai.llm.base_url") or "https://api.deepseek.com/v1"

    @property
    def llm_model(self) -> str:
        """OpenAI-compatible LLM 模型名称。"""
        return self.get("ai.llm.model") or "deepseek-chat"

    @property
    def deepseek_api_key(self) -> Optional[str]:
        """兼容旧代码属性名：OpenAI-compatible LLM API Key。"""
        return self.llm_api_key

    @property
    def deepseek_base_url(self) -> str:
        """兼容旧代码属性名：OpenAI-compatible LLM API Base URL。"""
        return self.llm_base_url

    @property
    def deepseek_model(self) -> str:
        """兼容旧代码属性名：OpenAI-compatible LLM 模型名称。"""
        return self.llm_model

    @property
    def embd_api_key(self) -> Optional[str]:
        """OpenAI-compatible Embedding API Key。"""
        return self.get("ai.embedding.api_key")

    @property
    def embd_base_url(self) -> str:
        """OpenAI-compatible Embedding API Base URL。"""
        return self.get("ai.embedding.base_url") or "https://api.openai.com/v1"

    @property
    def embd_model(self) -> str:
        """OpenAI-compatible Embedding 模型名称。"""
        return self.get("ai.embedding.model") or "text-embedding-3-small"

    @property
    def openai_api_key(self) -> Optional[str]:
        """兼容旧代码属性名：OpenAI-compatible Embedding API Key。"""
        return self.embd_api_key

    @property
    def openai_base_url(self) -> str:
        """兼容旧代码属性名：OpenAI-compatible Embedding API Base URL。"""
        return self.embd_base_url

    @property
    def openai_embedding_model(self) -> str:
        """兼容旧代码属性名：OpenAI-compatible Embedding 模型名称。"""
        return self.embd_model

    @property
    def embedding_dim_raw(self) -> Any:
        """Embedding 维度原始配置值。"""
        return self.get("ai.embedding.dim", 1536)

    @property
    def embedding_dim_is_auto(self) -> bool:
        """当前 Embedding 维度是否启用自动探测。"""
        raw_val = self.embedding_dim_raw
        return isinstance(raw_val, str) and raw_val.strip().lower() == "auto"

    @property
    def embedding_dim(self) -> Optional[int]:
        """Embedding 向量维度；auto 模式下返回已解析的运行期维度。"""
        if self.embedding_dim_is_auto:
            return self._resolved_embedding_dim

        raw_val = self.embedding_dim_raw
        if raw_val is None:
            return None
        return int(raw_val)

    def set_runtime_embedding_dim(self, dim: int) -> None:
        """写入运行期解析出的 Embedding 维度，并持久化到本地缓存。"""
        self._resolved_embedding_dim = int(dim)
        target_path = self.runtime_embedding_dim_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedding_dim": self._resolved_embedding_dim,
            "fingerprint": self.embedding_runtime_fingerprint,
        }
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_persisted_embedding_dim(self) -> Optional[int]:
        """加载持久化的 Embedding 维度缓存。"""
        target_path = self.data_dir / "runtime" / "embedding_dim.json"
        if not target_path.exists():
            return None

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        fingerprint = payload.get("fingerprint")
        if not self._runtime_embedding_fingerprint_matches(fingerprint):
            return None

        dim = payload.get("embedding_dim")
        return int(dim) if dim is not None else None

    def _runtime_embedding_fingerprint_matches(self, payload: Any) -> bool:
        """检查运行期维度缓存是否仍然对应当前 Embedding 配置。"""
        if not isinstance(payload, dict):
            return False

        expected = self.embedding_runtime_fingerprint
        return all(str(payload.get(key, "")) == value for key, value in expected.items())

    @property
    def zhihu_cookie(self) -> Optional[str]:
        """知乎 Cookie（可选，用于绕过登录墙获取完整内容）"""
        return self.get("processors.zhihu.cookie")

    @property
    def log_level(self) -> str:
        """日志级别"""
        return self.get_env("LOG_LEVEL") or self.get("logging.level", "INFO")

    def ensure_dirs(self):
        """确保所有必要的目录存在"""
        dirs = [
            self.vault_dir,
            self.db_path.parent,
            self.vector_index_dir,
            self.log_dir,
            self.tmp_dir,
            self.runtime_embedding_dim_path.parent,
        ]

        for dir_path in dirs:
            dir_path.mkdir(parents=True, exist_ok=True)


# 全局配置实例 (单例模式)
_config_instance: Optional[Config] = None


def get_config() -> Config:
    """
    获取全局配置实例 (单例)

    Returns:
        Config 实例
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
        _config_instance.ensure_dirs()
    return _config_instance


def get_workflow_config(workflow_name: str) -> Dict[str, Any]:
    """
    获取工作流配置（快捷入口）。

    Args:
        workflow_name: 工作流名称

    Returns:
        工作流配置字典
    """
    return get_config().get_workflow_config(workflow_name)

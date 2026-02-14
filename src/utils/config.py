"""
配置加载器

从 config.yaml 和环境变量加载配置
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv


class Config:
    """配置管理器"""

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径，默认为 config/config.yaml
        """
        # 加载环境变量
        load_dotenv()

        # 确定配置文件路径
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
            self._config: Dict[str, Any] = yaml.safe_load(f)

        self._project_root = Path(__file__).parent.parent.parent

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

    def get_env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        获取环境变量

        Args:
            key: 环境变量名
            default: 默认值

        Returns:
            环境变量值
        """
        return os.getenv(key, default)

    @property
    def vault_dir(self) -> Path:
        """Markdown Vault 目录"""
        path = self.get("storage.vault_dir", ".data/vault")
        return self._project_root / path

    @property
    def db_path(self) -> Path:
        """SQLite 数据库路径"""
        db_path_str = self.get_env("DB_PATH") or self.get("storage.db_path", ".data/db/knowledge_vault.db")
        return self._project_root / db_path_str

    @property
    def vector_index_dir(self) -> Path:
        """向量索引目录"""
        path = self.get("storage.vector_index_dir", ".data/vectors")
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
    def deepseek_api_key(self) -> Optional[str]:
        """DeepSeek API Key"""
        return self.get_env("DEEPSEEK_API_KEY")

    @property
    def deepseek_base_url(self) -> str:
        """DeepSeek API Base URL"""
        return self.get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    @property
    def openai_api_key(self) -> Optional[str]:
        """OpenAI API Key"""
        return self.get_env("OPENAI_API_KEY")

    @property
    def openai_base_url(self) -> str:
        """OpenAI API Base URL"""
        return self.get_env("OPENAI_BASE_URL", "https://api.openai.com/v1")

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

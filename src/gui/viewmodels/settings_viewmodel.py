"""设置 ViewModel。

提供 SettingsViewModel，管理应用设置的读取与持久化。
设置来源：
- API Key 及 Base URL: .env 文件 + os.environ
- 主题: QSettings（由 MainWindow 管理）
- 检索策略: config.yaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from src.utils.config import get_config

logger = logging.getLogger("pkv.gui.viewmodels.settings")

# .env 中需要管理的环境变量键名与设置字典键名的映射
_ENV_KEY_MAP: Dict[str, str] = {
    "deepseek_api_key": "PKV_LLM_API_KEY",
    "deepseek_base_url": "PKV_LLM_BASE_URL",
    "openai_api_key": "PKV_EMBD_API_KEY",
    "openai_base_url": "PKV_EMBD_BASE_URL",
}


class SettingsViewModel(QObject):
    """设置 ViewModel，管理应用配置的读取与持久化。

    读取设置时从 Config 单例和 os.environ 获取当前值；
    保存设置时写入 .env 文件并同步更新 os.environ，
    然后重置 Config 单例以使新配置生效。

    Signals:
        settings_saved: 设置保存成功后发射。
        error_occurred: 设置保存失败时发射，携带错误消息。
    """

    settings_saved = Signal()
    error_occurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """初始化 SettingsViewModel。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def load_settings(self) -> Dict[str, Any]:
        """加载当前设置。

        从 Config 单例和 os.environ 中读取所有可配置项。

        Returns:
            设置字典，包含以下键:
            - deepseek_api_key: LLM API Key（旧 UI 字段名，写入 PKV_LLM_API_KEY）
            - deepseek_base_url: LLM API Base URL（旧 UI 字段名，写入 PKV_LLM_BASE_URL）
            - openai_api_key: Embedding API Key（旧 UI 字段名，写入 PKV_EMBD_API_KEY）
            - openai_base_url: Embedding API Base URL（旧 UI 字段名，写入 PKV_EMBD_BASE_URL）
            - theme: 当前主题（从 QSettings 读取，此处返回空字符串占位）
            - search_strategy: 检索策略（auto / bm25 / vector / hybrid）
        """
        config = get_config()
        return {
            "deepseek_api_key": config.llm_api_key or "",
            "deepseek_base_url": config.llm_base_url or "",
            "openai_api_key": config.embd_api_key or "",
            "openai_base_url": config.embd_base_url or "",
            "theme": "",  # 主题由 MainWindow.current_theme 管理
            "search_strategy": config.get("retrieval.default_strategy", "auto") or "auto",
        }

    def save_settings(self, settings: Dict[str, Any]) -> None:
        """保存设置到 .env 文件并更新运行时环境。

        步骤:
        1. 筛选出与 API Key/URL 相关的变更项
        2. 更新 .env 文件（保留注释和其他行）
        3. 同步更新 os.environ
        4. 重置 Config 单例使新配置生效

        Args:
            settings: 设置字典（与 load_settings 返回格式一致）。
        """
        try:
            # 收集需要写入 .env 的更新项
            updates: Dict[str, str] = {}
            for setting_key, env_key in _ENV_KEY_MAP.items():
                if setting_key in settings:
                    value = str(settings[setting_key]).strip()
                    updates[env_key] = value

            if not updates:
                logger.info("无需更新 .env 文件")
                self.settings_saved.emit()
                return

            # 写入 .env 文件
            self._update_env_file(updates)

            # 同步更新 os.environ
            for env_key, value in updates.items():
                if value:
                    os.environ[env_key] = value
                elif env_key in os.environ:
                    # 空值时移除环境变量
                    del os.environ[env_key]

            # 重置 Config 单例，使下次 get_config() 重新加载
            import src.utils.config as config_module
            config_module._config_instance = None

            logger.info(f"设置已保存: 更新了 {len(updates)} 个配置项")
            self.settings_saved.emit()

        except Exception as exc:
            error_msg = f"保存设置失败: {exc}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _find_env_file(self) -> Path:
        """查找项目根目录的 .env 文件路径。

        Returns:
            .env 文件的 Path 对象（可能不存在）。
        """
        # 项目根目录：从 src/utils/config.py 推算
        project_root = Path(__file__).parent.parent.parent.parent
        return project_root / ".env"

    def _update_env_file(self, updates: Dict[str, str]) -> None:
        """读取-修改-写回 .env 文件。

        对于每个待更新的环境变量:
        - 如果 .env 中已有该变量行，则替换该行
        - 如果不存在，则追加到文件末尾

        保留所有注释行和其他非目标变量行。

        Args:
            updates: 环境变量名到值的映射，如 {"PKV_LLM_API_KEY": "sk-xxx"}。
        """
        env_path = self._find_env_file()

        # 读取已有内容
        existing_lines: list[str] = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()

        # 记录哪些 key 已被替换
        remaining_keys = set(updates.keys())
        new_lines: list[str] = []

        for line in existing_lines:
            stripped = line.strip()

            # 跳过空行和注释行，原样保留
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            # 尝试解析 KEY=VALUE
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining_keys:
                    # 替换该行
                    new_lines.append(f"{key}={updates[key]}")
                    remaining_keys.discard(key)
                    continue

            # 非目标行，原样保留
            new_lines.append(line)

        # 追加未找到的新键
        if remaining_keys:
            # 确保追加前有空行分隔
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            for key in sorted(remaining_keys):
                new_lines.append(f"{key}={updates[key]}")

        # 写回文件
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        logger.debug(f".env 文件已更新: {env_path}")

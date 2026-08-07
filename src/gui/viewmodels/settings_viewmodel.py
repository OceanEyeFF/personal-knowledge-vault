"""设置 ViewModel。

提供 SettingsViewModel，管理应用设置的读取与持久化。
设置来源：
- API Key 及 Base URL: config/local.yaml
- 主题: QSettings（由 MainWindow 管理）
- 检索策略: config.yaml
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from src.utils.config import (
    Config,
    get_config,
    redact_url_credentials,
    set_yaml_config_values,
    url_contains_credentials,
)

logger = logging.getLogger("pkv.gui.viewmodels.settings")

# GUI 字段名与 YAML 配置按能力命名，避免绑定具体供应商。
_CONFIG_KEY_MAP: Dict[str, str] = {
    "llm_api_key": "ai.llm.api_key",
    "llm_base_url": "ai.llm.base_url",
    "embedding_api_key": "ai.embedding.api_key",
    "embedding_base_url": "ai.embedding.base_url",
    "search_strategy": "retrieval.default_strategy",
}
_BASE_URL_SETTING_KEYS = {"llm_base_url", "embedding_base_url"}
_HIDDEN_URL_MARKER = "已隐藏"
_UNDISPLAYABLE_URL_PLACEHOLDER = "已设置（URL 格式不可显示）"
_CREDENTIAL_URL_ERROR = (
    "Base URL 含有认证信息，不能在普通设置界面中编辑；"
    "请直接编辑用户数据目录中的 config/local.yaml"
)
_REDACTED_URL_ERROR = (
    "Base URL 包含脱敏占位符，未保存；"
    "请输入不含认证信息的完整 URL，或直接编辑用户数据目录中的 config/local.yaml"
)


class SettingsViewModel(QObject):
    """设置 ViewModel，管理应用配置的读取与持久化。

    读取设置时从 Config 单例获取当前值；保存设置时写入
    config/local.yaml，然后重置 Config 单例以使新配置生效。

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
        self._protected_base_url_displays: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def load_settings(self) -> Dict[str, Any]:
        """加载当前设置。

        从 Config 单例中读取所有可配置项。

        Returns:
            设置字典，包含以下键:
            - llm_api_key: LLM API Key
            - llm_base_url: LLM API Base URL
            - embedding_api_key: Embedding API Key
            - embedding_base_url: Embedding API Base URL
            - theme: 当前主题（从 QSettings 读取，此处返回空字符串占位）
            - search_strategy: 检索策略（auto / bm25 / vector / hybrid）
        """
        config = get_config()
        llm_base_url = self._base_url_for_display(
            "llm_base_url", config.llm_base_url or ""
        )
        embedding_base_url = self._base_url_for_display(
            "embedding_base_url", config.embd_base_url or ""
        )
        return {
            "llm_api_key": config.llm_api_key or "",
            "llm_base_url": llm_base_url,
            "embedding_api_key": config.embd_api_key or "",
            "embedding_base_url": embedding_base_url,
            "theme": "",  # 主题由 MainWindow.current_theme 管理
            "search_strategy": config.get("retrieval.default_strategy", "auto") or "auto",
        }

    def save_settings(self, settings: Dict[str, Any]) -> None:
        """保存设置到 config/local.yaml。

        步骤:
        1. 筛选可持久化设置
        2. 更新本机私有 YAML 配置
        3. 重置 Config 单例使新配置生效

        Args:
            settings: 设置字典（与 load_settings 返回格式一致）。
        """
        try:
            # 收集需要写入 local.yaml 的更新项
            updates: Dict[str, str] = {}
            for setting_key, config_key in _CONFIG_KEY_MAP.items():
                if setting_key in settings:
                    value = str(settings[setting_key]).strip()
                    if setting_key in _BASE_URL_SETTING_KEYS:
                        protected_display = self._protected_base_url_displays.get(
                            setting_key
                        )
                        if protected_display is not None and value == protected_display:
                            # 脱敏展示未被编辑，保留 local.yaml 中的原始 endpoint。
                            continue
                        if _HIDDEN_URL_MARKER in value or (
                            value == _UNDISPLAYABLE_URL_PLACEHOLDER
                        ):
                            raise ValueError(_REDACTED_URL_ERROR)
                        if url_contains_credentials(value):
                            raise ValueError(_CREDENTIAL_URL_ERROR)
                    updates[config_key] = value

            if not updates:
                logger.info("无需更新本机配置文件")
                self.settings_saved.emit()
                return

            self._update_local_config(updates)

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

    def _base_url_for_display(self, setting_key: str, raw_value: str) -> str:
        """返回可放入普通文本框的 endpoint，并记录受保护展示快照。"""
        self._protected_base_url_displays.pop(setting_key, None)
        if not raw_value:
            return ""

        redacted = redact_url_credentials(raw_value)
        if redacted is None:
            display_value = _UNDISPLAYABLE_URL_PLACEHOLDER
        else:
            display_value = redacted

        if display_value != raw_value:
            self._protected_base_url_displays[setting_key] = display_value
        return display_value

    def _find_local_config_file(self) -> Path:
        """返回本机私有配置文件路径。

        Returns:
            config/local.yaml 的 Path 对象（可能不存在）。
        """
        return get_config().local_config_path

    def _update_local_config(self, updates: Dict[str, str]) -> None:
        """读取、修改并写回本机私有 YAML 配置。

        Args:
            updates: YAML 点号键到值的映射。
        """
        config = get_config()
        config_path = self._find_local_config_file()
        if isinstance(config, Config):
            config.update_local_config(updates)
        else:
            # Compatibility seam for an explicitly injected test/admin config.
            set_yaml_config_values(config_path, updates)
        logger.debug("本机配置文件已更新: %s", config_path)

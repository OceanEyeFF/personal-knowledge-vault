"""
配置加载器

从 config.yaml 和本机 local.yaml 加载配置
"""

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import yaml


_DISPLAY_CREDENTIAL_PARAMETER_MARKERS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "authorization",
    "basic_auth",
    "bearer",
    "bearer_token",
    "client_secret",
    "code",
    "cookie",
    "credential",
    "credentials",
    "jsession_id",
    "jsessionid",
    "jsessionidsso",
    "jwt",
    "key",
    "pass",
    "passcode",
    "passphrase",
    "passwd",
    "password",
    "phpsessid",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "session",
    "session_id",
    "sessionid",
    "sid",
    "sig",
    "signature",
    "subscription_key",
    "token",
}

# Endpoint contract fingerprints intentionally use exact normalized parameter
# names.  Display/log redaction is deliberately broader, but using that broad
# rule here would hide contract-bearing values such as ``region_code`` and
# ``routing_key`` and incorrectly reuse an embedding cache/index.
_ENDPOINT_CONTRACT_CREDENTIAL_PARAMETER_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "authorization",
    "basic_auth",
    "bearer",
    "bearer_token",
    "client_credentials",
    "client_secret",
    "code",
    "cookie",
    "credential",
    "credentials",
    "id_token",
    "jsession_id",
    "jsessionid",
    "jsessionidsso",
    "jwt",
    "jwt_token",
    "key",
    "oauth_token",
    "pass",
    "passcode",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "session",
    "session_id",
    "session_key",
    "sessionid",
    "session_token",
    "sid",
    "sig",
    "signature",
    "subscription_key",
    "token",
    "asp_net_session_id",
    "connect_sid",
    "ocp_apim_subscription_key",
    "phpsessid",
    "x_amz_credential",
    "x_amz_security_token",
    "x_amz_signature",
    "x_api_key",
    "x_auth_token",
    "x_goog_credential",
    "x_goog_signature",
}

_HTTP_TRANSPORT_LOGGER_NAMES = (
    "httpx",
    "httpx._client",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "openai",
    "openai._base_client",
)


def suppress_unsafe_http_transport_logs() -> None:
    """禁止第三方 HTTP 客户端在 INFO/DEBUG 日志中打印完整请求 URL。"""
    for logger_name in _HTTP_TRANSPORT_LOGGER_NAMES:
        transport_logger = logging.getLogger(logger_name)
        transport_logger.setLevel(
            max(logging.WARNING, transport_logger.getEffectiveLevel())
        )


def _normalize_security_identifier(value: str) -> str:
    """将 URL 参数名统一为小写 snake_case，便于边界匹配。"""
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()


def _is_display_credential_parameter(parameter_name: str) -> bool:
    """宽松识别日志、CLI 与普通界面中需要隐藏的凭据参数。"""
    normalized = _normalize_security_identifier(unquote_plus(parameter_name))
    return any(
        normalized == marker
        or normalized.startswith(f"{marker}_")
        or normalized.endswith(f"_{marker}")
        or f"_{marker}_" in normalized
        for marker in _DISPLAY_CREDENTIAL_PARAMETER_MARKERS
    )


def _is_endpoint_contract_credential_parameter(parameter_name: str) -> bool:
    """精确识别可从 endpoint 契约指纹中忽略的认证参数名。"""
    normalized = _normalize_security_identifier(unquote_plus(parameter_name))
    return normalized in _ENDPOINT_CONTRACT_CREDENTIAL_PARAMETER_NAMES


def _is_endpoint_decision_credential_parameter(parameter_name: str) -> bool:
    """精确识别 CLI/GUI 禁止通过普通输入渠道传递的 endpoint 凭据。"""
    normalized = _normalize_security_identifier(unquote_plus(parameter_name))
    return normalized in _ENDPOINT_CONTRACT_CREDENTIAL_PARAMETER_NAMES


def _parameter_component_has_credentials(
    component: str,
    predicate: Callable[[str], bool],
) -> bool:
    return any(
        predicate(match.group(1))
        for match in re.finditer(r"(?:^|[?&;])([^?&;=#]+)=", component)
    )


def _replace_url_parameters(
    component: str,
    replacement: str,
    *,
    predicate: Callable[[str], bool],
) -> str:
    def replace(match: re.Match[str]) -> str:
        if not predicate(match.group("key")):
            return match.group(0)
        return f"{match.group('prefix')}{match.group('key')}={replacement}"

    return re.sub(
        r"(?P<prefix>^|[?&;])(?P<key>[^?&;=#]+)=(?P<value>[^&;]*)",
        replace,
        component,
    )


def _path_matrix_has_credentials(
    path: str,
    predicate: Callable[[str], bool],
) -> bool:
    """检测 URL path segment 的 ``;name=value`` matrix 凭据参数。"""
    return any(
        predicate(match.group(1))
        for match in re.finditer(r";([^/;=?#]+)=", path)
    )


def _replace_path_matrix_parameters(
    path: str,
    replacement: str,
    *,
    predicate: Callable[[str], bool],
) -> str:
    """替换 path matrix 参数值，同时保留普通 path 与非敏感参数。"""

    def replace(match: re.Match[str]) -> str:
        if not predicate(match.group("key")):
            return match.group(0)
        return f";{match.group('key')}={replacement}"

    return re.sub(
        r";(?P<key>[^/;=?#]+)=(?P<value>[^/;]*)",
        replace,
        path,
    )


def _split_display_url(value: str):
    """仅识别带 host 的 URL，避免把普通文本当成 endpoint。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if not parsed.netloc or not (parsed.scheme or value.startswith("//")):
        return None
    return parsed


def url_contains_credentials(value: str) -> bool:
    """精确检测 URL userinfo 或 path/query/fragment 中的认证参数。"""
    parsed = _split_display_url(value)
    if parsed is not None:
        return (
            "@" in parsed.netloc
            or _path_matrix_has_credentials(
                parsed.path, _is_endpoint_decision_credential_parameter
            )
            or _parameter_component_has_credentials(
                parsed.query, _is_endpoint_decision_credential_parameter
            )
            or _parameter_component_has_credentials(
                parsed.fragment, _is_endpoint_decision_credential_parameter
            )
        )

    # 对无法解析的 endpoint 仍保守检测，避免绕过 CLI 禁令。
    authority = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)", value)
    return bool(
        (authority and "@" in authority.group(1))
        or _parameter_component_has_credentials(
            value, _is_endpoint_decision_credential_parameter
        )
    )


def redact_url_credentials(value: str) -> Optional[str]:
    """遮罩 URL 中的 userinfo 与敏感 query/fragment 参数值。"""
    parsed = _split_display_url(value)
    if parsed is None:
        return None

    netloc = parsed.netloc
    if "@" in netloc:
        _, _, host = netloc.rpartition("@")
        netloc = f"已隐藏@{host}"

    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            _replace_path_matrix_parameters(
                parsed.path,
                "已隐藏",
                predicate=_is_display_credential_parameter,
            ),
            _replace_url_parameters(
                parsed.query,
                "已隐藏",
                predicate=_is_display_credential_parameter,
            ),
            _replace_url_parameters(
                parsed.fragment,
                "已隐藏",
                predicate=_is_display_credential_parameter,
            ),
        )
    )


def endpoint_contract_sha256(value: str) -> str:
    """对去除凭据变化的 endpoint 契约生成稳定 SHA-256 指纹。"""
    parsed = _split_display_url(value)
    if parsed is None:
        contract = value
    else:
        netloc = parsed.netloc.rpartition("@")[2]
        contract = urlunsplit(
            (
                parsed.scheme,
                netloc,
                _replace_path_matrix_parameters(
                    parsed.path,
                    "<credential>",
                    predicate=_is_endpoint_contract_credential_parameter,
                ),
                _replace_url_parameters(
                    parsed.query,
                    "<credential>",
                    predicate=_is_endpoint_contract_credential_parameter,
                ),
                _replace_url_parameters(
                    parsed.fragment,
                    "<credential>",
                    predicate=_is_endpoint_contract_credential_parameter,
                ),
            )
        )
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def _load_yaml_mapping(config_path: Path, label: str) -> Dict[str, Any]:
    """加载 YAML 映射，并避免把含密钥的源文本带入异常消息。"""
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = ""
        if mark is not None:
            location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）"
        raise ValueError(f"{label} YAML 格式错误{location}: {config_path}") from None

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{label}根节点必须是映射: {config_path}")
    return loaded


def set_yaml_config_value(config_path: Path, key: str, value: Any) -> None:
    """单键兼容入口；底层仍使用一次原子 YAML 更新。"""
    set_yaml_config_values(config_path, {key: value})


def set_yaml_config_values(
    config_path: Path,
    updates: Mapping[str, Any],
) -> None:
    """一次加载、合并并原子写入多个 YAML 点号路径。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if config_path.exists():
        data = _load_yaml_mapping(config_path, "配置文件")

    for key, value in updates.items():
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

    if not updates:
        return

    serialized = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    temp_path = Path(temp_name)
    descriptor_open = True
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor_open = False
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        if os.name == "posix":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, config_path)
    finally:
        if descriptor_open:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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

        base_config = _load_yaml_mapping(config_path, "配置文件")

        self._config: Dict[str, Any] = copy.deepcopy(base_config)
        local_config: Dict[str, Any] = {}
        if local_config_path is None and use_default_config:
            local_config_path = str(config_path.parent / "local.yaml")
        self._local_config_path = Path(local_config_path) if local_config_path else None

        if self._local_config_path and self._local_config_path.exists():
            local_config = _load_yaml_mapping(self._local_config_path, "本机配置文件")
            self._deep_merge(self._config, local_config)
            self._rebase_inherited_storage_paths(base_config, local_config)

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

    def _rebase_inherited_storage_paths(
        self,
        base_config: Dict[str, Any],
        local_config: Dict[str, Any],
    ) -> None:
        """当本机数据根变化时，将未修改的存储子路径迁移到新根目录。"""
        base_storage = base_config.get("storage")
        local_storage = local_config.get("storage")
        merged_storage = self._config.get("storage")
        if not all(
            isinstance(storage, dict)
            for storage in (base_storage, local_storage, merged_storage)
        ):
            return

        if "data_dir" not in local_storage:
            return

        base_data_dir = base_storage.get("data_dir", ".data")
        local_data_dir = local_storage.get("data_dir")
        if not local_data_dir or local_data_dir == base_data_dir:
            return

        child_suffixes = {
            "vault_dir": Path("vault"),
            "db_path": Path("db") / "knowledge_vault.db",
            "vector_index_dir": Path("vectors"),
            "log_dir": Path("logs"),
            "tmp_dir": Path("tmp"),
        }
        missing = object()
        base_root = Path(str(base_data_dir))
        local_root = Path(str(local_data_dir))

        for key, default_suffix in child_suffixes.items():
            base_value = base_storage.get(key, base_root / default_suffix)
            local_value = local_storage.get(key, missing)

            # local.yaml 中与基础配置不同的值是显式覆盖，必须原样保留。
            if local_value is not missing and local_value != base_value:
                continue

            try:
                suffix = Path(str(base_value)).relative_to(base_root)
            except ValueError:
                # 基础子路径本就位于 data_dir 之外，不应擅自迁移。
                continue
            merged_storage[key] = str(local_root / suffix)

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
                return _load_yaml_mapping(workflow_path, "工作流配置文件")

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

    def _get_runtime_override(
        self, key: str, default: Optional[str] = None
    ) -> Optional[str]:
        """读取进程级运行覆盖，不把环境变量作为应用配置源。"""
        return os.getenv(key, default)

    def _runtime_data_subpath(self, name: str) -> Optional[Path]:
        """当 DATA_DIR 被覆盖时，将其作为其余运行目录的隔离根。"""
        data_dir = self._get_runtime_override("DATA_DIR")
        if not data_dir:
            return None
        return self._project_root / data_dir / name

    @property
    def vault_dir(self) -> Path:
        """Markdown Vault 目录"""
        path = self._get_runtime_override("VAULT_DIR")
        if path:
            return self._project_root / path
        runtime_path = self._runtime_data_subpath("vault")
        if runtime_path is not None:
            return runtime_path
        path = self.get("storage.vault_dir", ".data/vault")
        return self._project_root / path

    @property
    def data_dir(self) -> Path:
        """数据根目录。"""
        data_dir_str = self._get_runtime_override("DATA_DIR") or self.get(
            "storage.data_dir"
        )
        if data_dir_str:
            return self._project_root / data_dir_str
        return self.db_path.parent.parent

    @property
    def db_path(self) -> Path:
        """SQLite 数据库路径"""
        db_path_str = self._get_runtime_override("DB_PATH")
        if db_path_str:
            return self._project_root / db_path_str
        runtime_db_dir = self._runtime_data_subpath("db")
        if runtime_db_dir is not None:
            return runtime_db_dir / "knowledge_vault.db"
        db_path_str = self.get("storage.db_path", ".data/db/knowledge_vault.db")
        return self._project_root / db_path_str

    @property
    def vector_index_dir(self) -> Path:
        """向量索引目录"""
        path = self._get_runtime_override("VECTOR_DIR")
        if path:
            return self._project_root / path
        runtime_path = self._runtime_data_subpath("vectors")
        if runtime_path is not None:
            return runtime_path
        path = self.get("storage.vector_index_dir", ".data/vectors")
        return self._project_root / path

    @property
    def log_dir(self) -> Path:
        """日志目录"""
        path = self._get_runtime_override("LOG_DIR")
        if path:
            return self._project_root / path
        runtime_path = self._runtime_data_subpath("logs")
        if runtime_path is not None:
            return runtime_path
        path = self.get("storage.log_dir", ".data/logs")
        return self._project_root / path

    @property
    def tmp_dir(self) -> Path:
        """临时文件目录"""
        path = self._get_runtime_override("TMP_DIR")
        if path:
            return self._project_root / path
        runtime_path = self._runtime_data_subpath("tmp")
        if runtime_path is not None:
            return runtime_path
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
            "base_url_sha256": self._embedding_base_url_sha256,
            "embedding_model": self.embd_model,
        }

    def embedding_index_fingerprint(self, dim: int) -> Dict[str, str]:
        """当前 Embedding 索引契约指纹，不包含 API Key 等敏感信息。"""
        return {
            "base_url_sha256": self._embedding_base_url_sha256,
            "embedding_model": self.embd_model,
            "embedding_dim": str(int(dim)),
        }

    @property
    def _embedding_base_url_sha256(self) -> str:
        """对可能携带凭据的 endpoint 生成稳定、不可逆指纹。"""
        return endpoint_contract_sha256(self.embd_base_url)

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
        if isinstance(fingerprint, dict) and "base_url" in fingerprint:
            # 旧缓存可能将 userinfo/query 凭据与 endpoint 一起明文持久化。
            # 按旧格式失效处理，同时必须从磁盘清除该原文。
            try:
                target_path.unlink()
            except OSError:
                try:
                    target_path.write_text("{}\n", encoding="utf-8")
                except OSError:
                    raise RuntimeError(
                        f"无法清理旧 Embedding 维度缓存: {target_path}"
                    ) from None
            return None
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
        return self._get_runtime_override("LOG_LEVEL") or self.get(
            "logging.level", "INFO"
        )

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

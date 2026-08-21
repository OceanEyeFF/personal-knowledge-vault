"""
配置加载器

从 bundled `config/config.yaml` 与唯一用户 profile 配置
`%USERPROFILE%\\.pkv\\config.yaml` 加载业务配置。`PKV_DATA_ROOT` 优先于
用户配置的 `storage.data_root` 选择 data root；data-root 内
`config/local.yaml` 仅是独立、无敏感字段的运行时快照。
"""

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import warnings
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from src.runtime.layout import (
    RuntimeLayout,
    atomic_publish_file,
    open_user_file_nofollow,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError


_RUNTIME_EMBEDDING_DIM_LOCK = Lock()
_CONFIG_INSTANCE_LOCK = RLock()
_LOCAL_CONFIG_UPDATE_LOCK = RLock()
_MAX_RUNTIME_EMBEDDING_DIM = 65_536
_RUNTIME_SNAPSHOT_SCHEMA_VERSION = 1
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_POSITIVE_DECIMAL_RE = re.compile(r"[1-9][0-9]*")
_SEMVER_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
# A lifecycle plan is process-bound.  Its user-config revision must notice an
# external edit without becoming an oracle for the contents of config.yaml
# (which can include provider credentials).  This key is deliberately neither
# persisted nor exported.
_USER_CONFIG_SOURCE_REVISION_KEY = os.urandom(32)


def _select_runtime_data_root(environment: Mapping[str, str]) -> Optional[str]:
    """Select the process root without widening legacy environment support.

    ``PKV_DATA_ROOT`` is the only product root override.  ``DATA_DIR`` remains
    an internal isolation seam and may supersede it only in a process that the
    offline launcher explicitly marked.  This matches :class:`RuntimeLayout`
    and lets a unit fixture narrow the wrapper root without inheriting it.
    """

    if environment.get("PKV_TEST_OFFLINE") == "1":
        return environment.get("DATA_DIR") or environment.get("PKV_DATA_ROOT")
    return environment.get("PKV_DATA_ROOT")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            ) from None
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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
    """精确识别 CLI/外部 Wrapper 禁止通过普通输入渠道传递的 endpoint 凭据。"""
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
        predicate(match.group(1)) for match in re.finditer(r";([^/;=?#]+)=", path)
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
        with open_user_file_nofollow(
            config_path,
            "r",
            label=label,
            encoding="utf-8",
        ) as handle:
            loaded = yaml.load(handle, Loader=_UniqueKeySafeLoader)
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

    _apply_yaml_config_updates(data, updates)

    if not updates:
        return

    serialized = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    atomic_publish_file(
        config_path,
        label="配置文件",
        data=serialized.encode("utf-8"),
    )


def _apply_yaml_config_updates(
    data: Dict[str, Any],
    updates: Mapping[str, Any],
) -> None:
    """Apply dot-path settings to an in-memory YAML mapping.

    ``Config`` uses this same operation to validate a prospective immutable
    snapshot before it publishes a user setting.  Keeping one implementation
    prevents a preflight from approving a different tree than the writer would
    actually persist.
    """

    for key, value in updates.items():
        if not isinstance(key, str):
            raise ValueError("无效配置键")
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
        cursor[parts[-1]] = copy.deepcopy(value)


class Config:
    """业务配置与 RuntimeLayout 的不可变 snapshot 管理器。"""

    def __init__(
        self,
        config_path: Optional[str] = None,
        local_config_path: Optional[str] = None,
        *,
        user_config_path: Optional[str] = None,
        profile_root: Optional[str] = None,
        layout: Optional[RuntimeLayout] = None,
        environment: Optional[Mapping[str, str]] = None,
        resources_root: Optional[str] = None,
        _user_config_updates: Optional[Mapping[str, Any]] = None,
        _fallback_data_root: Optional[str] = None,
    ):
        """
        初始化配置管理器

        Args:
            config_path: 基础配置文件路径，默认为 config/config.yaml
            local_config_path: ``user_config_path`` 的弃用兼容别名。它始终
                表示用户可编辑的配置，而非数据根内 runtime snapshot。
            user_config_path: 用户可编辑配置路径；默认
                ``%USERPROFILE%\\.pkv\\config.yaml``。
            profile_root: 用户 profile 注入 seam；不传时从 USERPROFILE/HOME
                解析。测试应传入假路径或使用隔离 RuntimeLayout。
            layout: 已冻结的资源/数据根布局；显式传入时不因 YAML 悄然换根。
            environment: 纯解析测试的环境注入 seam；不传时读取当前进程环境。
            resources_root: 从既有 snapshot 重建时固定 bundled resources
                身份的内部 seam；产品默认不传。
            _user_config_updates: 写入前构造候选快照的内部 seam；绝不落盘。
            _fallback_data_root: 显式布局 snapshot 重建时的原数据根；
                仅在 user config/环境没有选择根时使用，绝不覆盖其选择。
        """
        if local_config_path is not None:
            if user_config_path is not None:
                raise ValueError("local_config_path 与 user_config_path 不能同时指定")
            warnings.warn(
                "local_config_path 已弃用；请改用 user_config_path",
                DeprecationWarning,
                stacklevel=2,
            )
            user_config_path = local_config_path

        # ``RuntimeLayout`` 是资源与用户数据路径的唯一来源。显式
        # config_path 仍作为测试/运维注入 seam，但其 storage 路径同样必须
        # 收敛到一个 data root 内。
        environment_was_supplied = environment is not None
        runtime_environment = dict(os.environ if environment is None else environment)
        use_default_config = config_path is None
        layout_was_supplied = layout is not None
        if layout is not None and config_path is not None:
            requested = Path(config_path).resolve(strict=False)
            expected = layout.base_config_path.resolve(strict=False)
            if requested != expected:
                raise ValueError(
                    f"config_path 与 RuntimeLayout 不一致: {requested} != {expected}"
                )
        if layout is not None and resources_root is not None:
            requested_resources = Path(resources_root).resolve(strict=False)
            expected_resources = layout.resources_root.resolve(strict=False)
            if requested_resources != expected_resources:
                raise ValueError(
                    "resources_root 与 RuntimeLayout 不一致: "
                    f"{requested_resources} != {expected_resources}"
                )

        if layout is None and use_default_config:
            layout = RuntimeLayout.resolve(
                resources_root=(
                    Path(resources_root) if resources_root is not None else None
                ),
                user_data_root=(
                    Path(_fallback_data_root)
                    if _fallback_data_root is not None
                    else None
                ),
                profile_root=Path(profile_root) if profile_root is not None else None,
                user_config_path=(
                    Path(user_config_path) if user_config_path is not None else None
                ),
                environment=runtime_environment,
            )

        if config_path is None:
            assert layout is not None
            config_path = layout.base_config_path
        else:
            config_path = Path(config_path)

        # 产品默认路径在首次读取前也必须通过 bundled/user 边界检查。
        if use_default_config:
            assert layout is not None
            config_path = layout.validate_bundled_path(
                Path(config_path), label="基础配置"
            )

        # 加载 YAML 配置
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        base_config = _load_yaml_mapping(config_path, "配置文件")

        self._config: Dict[str, Any] = copy.deepcopy(base_config)
        user_config: Dict[str, Any] = {}
        loaded_user_config_path: Optional[Path]
        if user_config_path is not None:
            # Explicit user config is an intentional test/admin seam.  If the
            # caller also supplied a RuntimeLayout, enforce its independent
            # profile boundary before reading it.
            loaded_user_config_path = Path(user_config_path)
            if layout is not None:
                layout.validate_user_config_file(
                    loaded_user_config_path,
                    label="用户配置",
                    allow_missing=True,
                )
        elif use_default_config:
            assert layout is not None
            loaded_user_config_path = layout.user_config_path
            layout.validate_user_config_file(
                loaded_user_config_path,
                label="用户配置",
                allow_missing=True,
            )
        else:
            loaded_user_config_path = None

        if loaded_user_config_path and loaded_user_config_path.exists():
            user_config = _load_yaml_mapping(loaded_user_config_path, "本机配置文件")
        if _user_config_updates is not None:
            _apply_yaml_config_updates(user_config, _user_config_updates)
        self._deep_merge(self._config, user_config)
        # The historical explicit-config seam supports copied storage trees.
        # Product Config() always derives child paths from one final root.
        if loaded_user_config_path is not None and not use_default_config:
            self._rebase_inherited_storage_paths(base_config, user_config)

        if layout is None:
            storage = self._config.get("storage")
            storage_mapping = storage if isinstance(storage, dict) else {}
            runtime_data_root = _select_runtime_data_root(runtime_environment)
            raw_data_root = runtime_data_root
            if not raw_data_root:
                raw_data_root = storage_mapping.get("data_root") or storage_mapping.get(
                    "data_dir"
                )
            if not raw_data_root:
                raw_data_root = _fallback_data_root
            if not raw_data_root:
                raise ValueError("显式配置缺少 storage.data_root")

            if config_path.parent.name.casefold() == "config":
                resources_root = config_path.parent.parent
            else:
                resources_root = config_path.parent

            # A standalone explicit config is a complete test/admin input.  Do
            # not let unrelated child-path variables inherited from a parent
            # process silently replace paths in that config.  Fine-grained
            # variables are meaningful only in explicit offline isolation,
            # where RuntimeLayout still validates their containment.
            layout_environment = dict(runtime_environment)
            if not runtime_data_root:
                for key in (
                    "DB_PATH",
                    "VAULT_DIR",
                    "VECTOR_DIR",
                    "LOG_DIR",
                    "TMP_DIR",
                ):
                    layout_environment.pop(key, None)
            layout = RuntimeLayout.resolve(
                resources_root=resources_root,
                user_data_root=Path(str(raw_data_root)),
                base_config_path=config_path,
                profile_root=Path(profile_root) if profile_root is not None else None,
                user_config_path=loaded_user_config_path,
                # A process-level root override deliberately rebases all
                # non-explicit child paths. Fine-grained legacy paths are only
                # accepted by RuntimeLayout under PKV_TEST_OFFLINE=1.
                storage_config={} if runtime_data_root else storage_mapping,
                environment=layout_environment,
            )
        # Stage 2: after bundled defaults + the user config have been merged,
        # choose one final data root.  Runtime local.yaml is never read or
        # merged here.  An explicitly supplied RuntimeLayout is a frozen
        # snapshot and is deliberately not retargeted by reload/config parsing.
        if use_default_config and not layout_was_supplied:
            assert layout is not None
            storage_from_user = user_config.get("storage")
            user_storage = (
                storage_from_user if isinstance(storage_from_user, dict) else {}
            )
            selected_root = _select_runtime_data_root(runtime_environment)
            selected_from_user_config = False
            if not selected_root:
                selected_root = user_storage.get("data_root")
                selected_from_user_config = bool(selected_root)
            if not selected_root and user_storage.get("data_dir"):
                warnings.warn(
                    "storage.data_dir 已弃用；请改用 storage.data_root",
                    DeprecationWarning,
                    stacklevel=2,
                )
                selected_root = user_storage["data_dir"]
                selected_from_user_config = True
            if selected_root and selected_from_user_config:
                selected_path = Path(str(selected_root))
                if not selected_path.is_absolute():
                    selected_root = layout.profile_root / selected_path
            layout = RuntimeLayout.resolve(
                resources_root=layout.resources_root,
                user_data_root=(
                    Path(str(selected_root))
                    if selected_root
                    else layout.user_data_root
                ),
                base_config_path=Path(config_path),
                profile_root=layout.profile_root,
                user_config_path=layout.user_config_path,
                # Children are always derived from the one final root in the
                # product path; user-config child paths are not a second layout.
                storage_config={},
                environment=runtime_environment,
            )

        assert layout is not None
        self._layout = layout
        # A process-level root override must not hide a pending change to the
        # user-config source.  Keep that source intent separate from the
        # effective RuntimeLayout root so update/reload can reject a dormant
        # switch before it is persisted or published.
        self._user_config_data_root_intent_identity = (
            self._resolve_user_config_data_root_intent_identity(
                user_config,
                profile_root=layout.profile_root,
            )
        )
        self._environment = dict(runtime_environment)
        self._environment_was_supplied = environment_was_supplied
        self._uses_default_config = use_default_config
        self._layout_was_supplied = layout_was_supplied
        # Backward-compatible inspection seams retain their historical names,
        # but their value is now the optional user configuration source.
        self._local_config_path = loaded_user_config_path
        self._loaded_local_config_path = loaded_user_config_path
        self._loaded_user_config_path = loaded_user_config_path
        self._project_root = layout.resources_root
        self._legacy_runtime_cache_requires_cleanup = False
        self._resolved_embedding_dim = self._load_persisted_embedding_dim()

    @staticmethod
    def _deep_merge(target: Dict[str, Any], overrides: Dict[str, Any]) -> None:
        """将用户配置递归合并到 bundled 基础配置。"""
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
        """历史显式配置 seam：为复制的测试/运维树重设继承子路径。

        产品 `Config()` 始终只从最终 `RuntimeLayout` data root 派生子路径；
        此兼容逻辑不读取或写入 data-root runtime snapshot。
        """
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

            # 旧 `local_config_path` 兼容 seam 所加载的用户配置若显式覆盖，
            # 则必须原样保留；这不是 data-root runtime snapshot。
            if local_value is not missing and local_value != base_value:
                continue

            try:
                suffix = Path(str(base_value)).relative_to(base_root)
            except ValueError:
                # 基础子路径本就位于 data_dir 之外，不应擅自迁移。
                continue
            merged_storage[key] = str(local_root / suffix)

    @staticmethod
    def _same_lexical_path(left: Path, right: Path) -> bool:
        """Compare paths without touching the filesystem or following links."""

        return os.path.normcase(
            os.path.abspath(os.path.normpath(os.fspath(left)))
        ) == os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(right))))

    @staticmethod
    def _resolve_user_config_data_root_intent_identity(
        user_config: Mapping[str, Any],
        *,
        profile_root: Path,
    ) -> str:
        """Return the unmasked user-config root selection as a lexical identity.

        ``PKV_DATA_ROOT`` may override a process's effective layout, but it must
        not let an update or reload store a different ``storage.data_root`` for
        a later process.  This mirrors the user-config portion of stage-two root
        selection without consulting the environment or touching the filesystem.
        """

        storage = user_config.get("storage")
        user_storage = storage if isinstance(storage, dict) else {}
        selected_root = user_storage.get("data_root") or user_storage.get(
            "data_dir"
        )
        if selected_root:
            root = Path(str(selected_root))
            if not root.is_absolute():
                root = profile_root / root
        else:
            root = profile_root / "data"
        return os.path.normcase(
            os.path.abspath(os.path.normpath(os.fspath(root)))
        )

    def _build_reloaded_snapshot(
        self,
        *,
        user_config_updates: Mapping[str, Any] | None = None,
    ) -> "Config":
        """Reparse this snapshot's exact sources without publishing it.

        A reload must construct a *new* layout from the same bundled resources,
        profile and frozen environment.  Reusing ``self.layout`` would make a
        newly parsed ``storage.data_root`` disagree with paths held by the
        returned Config.  The caller compares the fresh candidate before it can
        become a live Application/Kernel graph.
        """

        include_user_config = (
            self._loaded_user_config_path is not None
            or user_config_updates is not None
        )
        user_config_path = (
            str(self.user_config_path) if include_user_config else None
        )
        base_config_path = self.layout.base_config_path
        default_base_config_path = self.layout.resources_root / "config" / "config.yaml"
        reload_environment = dict(self._environment)
        if self._layout_was_supplied and not self._environment_was_supplied:
            # An explicit layout is already the authoritative offline/test
            # injection.  Replaying ambient root/child variables against a
            # fresh layout would point DB/Vault/vector paths at the wrapper's
            # DataRoot instead of this Config's captured root.  An explicitly
            # supplied environment remains part of the caller's snapshot and
            # is intentionally preserved for the normal root-switch gate.
            for key in (
                "PKV_DATA_ROOT",
                "DATA_DIR",
                "DB_PATH",
                "VAULT_DIR",
                "VECTOR_DIR",
                "LOG_DIR",
                "TMP_DIR",
            ):
                reload_environment.pop(key, None)
        common = {
            "user_config_path": user_config_path,
            "profile_root": str(self.layout.profile_root),
            "environment": reload_environment,
            "_user_config_updates": user_config_updates,
            "_fallback_data_root": (
                str(self.data_root) if self._layout_was_supplied else None
            ),
        }
        if self._uses_default_config and self._same_lexical_path(
            base_config_path,
            default_base_config_path,
        ):
            return Config(
                resources_root=str(self.layout.resources_root),
                **common,
            )
        return Config(
            config_path=str(base_config_path),
            **common,
        )

    def _require_same_data_root(
        self,
        candidate: "Config",
        *,
        stage: str,
    ) -> None:
        """Reject a silent root switch before any Config graph is published."""

        if (
            self.has_same_data_root(candidate)
            and self._user_config_data_root_intent_identity
            == candidate._user_config_data_root_intent_identity
        ):
            return
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_SWITCH_REQUIRED,
            "数据根变更需要经 inspect、plan、confirm、execute 生命周期确认",
            stage=stage,
            recoverable=True,
        )

    def reload_snapshot(self) -> "Config":
        """Build a coherent successor Config while forbidding root retargeting.

        The lifecycle owns a real data-root switch because it must explain
        impact, retain/backup old data and wait for explicit confirmation.  A
        settings reload may change values within one root only.
        """

        candidate = self._build_reloaded_snapshot()
        self._require_same_data_root(candidate, stage="config_reload")
        return candidate

    def user_config_source_revision(self) -> str:
        """Return an opaque, process-private revision of this snapshot's source.

        This is intentionally narrower than :meth:`reload_snapshot`: it reads
        only the raw editable user-config source and never parses, merges, or
        publishes it.  Runtime lifecycle plans use it to reject an external
        config/key edit between inspect and execute, while ordinary in-flight
        workflows continue to use their captured immutable ``Config`` object.

        The value is an HMAC rather than a plain content hash, so it is safe to
        retain inside a process-bound plan without revealing a path, setting or
        credential.  A Config constructed without a user-config source returns
        a stable opaque sentinel; it does not begin watching an unrelated
        profile file that was never part of its snapshot.
        """

        marker = hmac.new(
            _USER_CONFIG_SOURCE_REVISION_KEY,
            digestmod=hashlib.sha256,
        )
        source_path = self._loaded_user_config_path
        if source_path is None:
            marker.update(b"pkv:user-config-source-revision:v1:unconfigured")
        else:
            target_path = self._layout.validate_user_config_file(
                source_path,
                label="用户配置",
                allow_missing=True,
            )
            try:
                with open_user_file_nofollow(
                    target_path,
                    "rb",
                    label="用户配置",
                ) as handle:
                    marker.update(b"pkv:user-config-source-revision:v1:present\0")
                    while chunk := handle.read(64 * 1024):
                        marker.update(chunk)
            except FileNotFoundError:
                marker.update(b"pkv:user-config-source-revision:v1:missing")
        return marker.hexdigest()

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值 (支持点号分隔的多级 key)

        Args:
            key: 配置 key，支持 "ai.embedding.model" 等点号路径；存储子路径
                应读取 vault_dir / db_path 等 RuntimeLayout 属性
            default: 默认值

        Returns:
            配置值

        Example:
            >>> config = Config()
            >>> config.get("ai.embedding.model")
            'text-embedding-3-small'
        """
        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return copy.deepcopy(default)

        # ``Config`` is an immutable snapshot boundary.  Returning a nested
        # YAML mapping/list by reference lets a caller mutate the dependency
        # graph underneath already-running workflows and bypasses reload
        # generations.  Scalars are copied cheaply too, so callers receive one
        # consistent defensive contract for every key.
        return copy.deepcopy(value)

    def get_workflow_config(self, workflow_name: str) -> Dict[str, Any]:
        """
        获取工作流配置（仅加载 bundled config/workflows 下的版本化 YAML）。

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
        workflow_name = workflow_name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow_name):
            raise ValueError("workflow_name 只能包含字母、数字、下划线和连字符")

        workflow_dir = self._layout.workflows_dir
        name_variants = [
            workflow_name,
            workflow_name.replace("_", "-"),
            workflow_name.replace("-", "_"),
        ]

        for name in dict.fromkeys(name_variants):
            workflow_path = workflow_dir / f"{name}.yaml"
            if os.path.lexists(workflow_path):
                safe_path = self._layout.validate_bundled_path(
                    workflow_path,
                    label="工作流配置文件",
                )
                return _load_yaml_mapping(safe_path, "工作流配置文件")
        raise FileNotFoundError(f"工作流配置不存在: {workflow_name}")

    def _get_runtime_override(
        self, key: str, default: Optional[str] = None
    ) -> Optional[str]:
        """Read the small product runtime-env whitelist from this snapshot."""

        if key in {"PKV_DATA_ROOT", "PKV_LOG_LEVEL"}:
            return self._environment.get(key, default)
        if self._environment.get("PKV_TEST_OFFLINE") == "1":
            return self._environment.get(key, default)
        return default

    @property
    def layout(self) -> RuntimeLayout:
        """已经校验的只读资源/用户数据布局。"""
        return self._layout

    @property
    def local_config_path(self) -> Path:
        """Deprecated alias for the one editable user config path."""

        warnings.warn(
            "local_config_path 已弃用；请改用 user_config_path",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._layout.user_config_path

    @property
    def user_config_path(self) -> Path:
        """The only editable business-config path (may contain credentials)."""

        return self._layout.user_config_path

    @property
    def runtime_config_path(self) -> Path:
        """Internal data-root runtime snapshot; never a business config source."""

        return self._layout.runtime_config_path

    @property
    def vault_dir(self) -> Path:
        """Markdown Vault 目录"""
        return self._layout.vault_dir

    @property
    def data_dir(self) -> Path:
        """数据根目录。"""
        return self._layout.user_data_root

    @property
    def data_root(self) -> Path:
        """Stable spelling of the immutable snapshot's one data root."""

        return self._layout.user_data_root

    @property
    def data_root_identity(self) -> str:
        """Comparable lexical identity for reload/lifecycle guards.

        This intentionally does not create or resolve a path; callers compare
        captured Config snapshots rather than silently re-reading mutable state.
        """

        return os.path.normcase(
            os.path.abspath(os.path.normpath(os.fspath(self._layout.user_data_root)))
        )

    def has_same_data_root(self, other: "Config") -> bool:
        """Return whether two immutable Config snapshots target one root."""

        return isinstance(other, Config) and self.data_root_identity == other.data_root_identity

    @property
    def db_path(self) -> Path:
        """SQLite 数据库路径"""
        return self._layout.db_path

    @property
    def vector_index_dir(self) -> Path:
        """向量索引目录"""
        return self._layout.vector_index_dir

    @property
    def log_dir(self) -> Path:
        """日志目录"""
        return self._layout.log_dir

    @property
    def tmp_dir(self) -> Path:
        """临时文件目录"""
        return self._layout.tmp_dir

    @property
    def runtime_embedding_dim_path(self) -> Path:
        """自动探测出的 Embedding 维度缓存文件路径。"""
        return self._layout.runtime_state_dir / "embedding_dim.json"

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
    def llm_provider(self) -> str:
        """LLM Provider 类型；M13 仅发布 OpenAI-compatible 协议。"""
        return self.get("ai.llm.provider") or "openai_compatible"

    @property
    def llm_base_url(self) -> str:
        """OpenAI-compatible LLM API Base URL。"""
        return self.get("ai.llm.base_url") or "https://api.deepseek.com/v1"

    @property
    def llm_model(self) -> str:
        """OpenAI-compatible LLM 模型名称。"""
        return self.get("ai.llm.model") or "deepseek-chat"

    @property
    def llm_max_tokens(self) -> int:
        """Chat 单次响应最大 token 数。"""
        return int(self.get("ai.llm.max_tokens", 2000))

    @property
    def llm_temperature(self) -> float:
        """LLM 采样温度。"""
        return float(self.get("ai.llm.temperature", 0.7))

    @property
    def llm_timeout_seconds(self) -> float:
        """LLM 请求超时秒数。"""
        return float(self.get("ai.llm.timeout_seconds", 30.0))

    @property
    def llm_max_retries(self) -> int:
        """LLM Provider 最大重试次数。"""
        return int(self.get("ai.llm.max_retries", 2))

    @property
    def embd_api_key(self) -> Optional[str]:
        """OpenAI-compatible Embedding API Key。"""
        return self.get("ai.embedding.api_key")

    @property
    def embd_provider(self) -> str:
        """Embedding Provider 类型；M13 仅发布 OpenAI-compatible 协议。"""
        return self.get("ai.embedding.provider") or "openai_compatible"

    @property
    def embd_base_url(self) -> str:
        """OpenAI-compatible Embedding API Base URL。"""
        return self.get("ai.embedding.base_url") or "https://api.openai.com/v1"

    @property
    def embd_model(self) -> str:
        """OpenAI-compatible Embedding 模型名称。"""
        return self.get("ai.embedding.model") or "text-embedding-3-small"

    @property
    def embd_timeout_seconds(self) -> float:
        """Embedding 请求超时秒数。"""
        return float(self.get("ai.embedding.timeout_seconds", 30.0))

    @property
    def embd_max_retries(self) -> int:
        """Embedding Provider 最大重试次数。"""
        return int(self.get("ai.embedding.max_retries", 3))

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
        """以进程内 CAS 写入 auto 维度，成功发布后才更新内存。"""
        if type(dim) is not int or not 1 <= dim <= _MAX_RUNTIME_EMBEDDING_DIM:
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_PROTOCOL_FAILED,
                "Embedding Provider 响应非法",
                stage="embedding_protocol",
                recoverable=True,
            )
        # A normal Application archive reaches this from StoreStep's explicit
        # tracked worker bridge.  Direct Config callers take the same root-wide
        # lease themselves; an unregistered inherited thread is still rejected
        # rather than silently bypassing the single-writer contract.
        from src.runtime.write_lease import write_lease_scope

        resolved_dim = dim
        with write_lease_scope(self._layout):
            with _RUNTIME_EMBEDDING_DIM_LOCK:
                durable_dim = self._load_persisted_embedding_dim()
                existing_dim = (
                    durable_dim if durable_dim is not None else self._resolved_embedding_dim
                )
                if existing_dim is not None:
                    if existing_dim != resolved_dim:
                        raise PKVRuntimeError(
                            ErrorCode.PROVIDER_PROTOCOL_FAILED,
                            "Embedding Provider 响应非法",
                            stage="embedding_protocol",
                            recoverable=True,
                        )
                    if durable_dim is not None:
                        self._resolved_embedding_dim = durable_dim
                        return

                payload = {
                    "embedding_dim": resolved_dim,
                    "fingerprint": self.embedding_runtime_fingerprint,
                }
                self._write_runtime_embedding_payload(payload)
                self._resolved_embedding_dim = resolved_dim

    def _write_runtime_embedding_payload(self, payload: Mapping[str, Any]) -> None:
        """Atomically write the runtime cache after revalidating its data root."""

        self._layout.ensure_user_directories()
        target_path = self._layout.validate_user_file(
            self.runtime_embedding_dim_path,
            label="Embedding 运行缓存",
            allow_missing=True,
        )
        serialized = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        self._layout.atomic_publish_user_file(
            target_path,
            label="Embedding 运行缓存",
            data=serialized,
        )

    def _load_persisted_embedding_dim(self) -> Optional[int]:
        """只读加载持久化的 Embedding 维度缓存。

        构造 Config 不得修改文件系统。旧格式若含 endpoint 原文，仅记录为
        待清理状态；清理由完成路径校验后的 bootstrap 显式触发。
        """
        target_path = self._layout.validate_user_file(
            self.runtime_embedding_dim_path,
            label="Embedding 运行缓存",
            allow_missing=True,
        )
        if not target_path.exists():
            return None

        try:
            with self._layout.open_user_file(
                target_path,
                "r",
                label="Embedding 运行缓存",
                encoding="utf-8",
            ) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

        # 合法 JSON 但根节点不是 object (list/str/int/null 等) 一律按失效
        # 缓存处理，绝不调用 .get 或让 AttributeError 冒泡崩溃。
        if not isinstance(payload, dict):
            return None

        fingerprint = payload.get("fingerprint")
        if isinstance(fingerprint, dict) and "base_url" in fingerprint:
            self._legacy_runtime_cache_requires_cleanup = True
            return None
        if not self._runtime_embedding_fingerprint_matches(fingerprint):
            return None

        dim = payload.get("embedding_dim")
        if type(dim) is not int or not 1 <= dim <= _MAX_RUNTIME_EMBEDDING_DIM:
            return None
        return dim

    def sanitize_runtime_state(self) -> None:
        """在 RuntimeLayout 已验证后清理不安全的旧运行缓存。"""
        if not self._legacy_runtime_cache_requires_cleanup:
            return

        from src.runtime.write_lease import write_lease_scope

        with write_lease_scope(self._layout):
            # The flag can be cleared by a preceding nested bootstrap/config
            # call while waiting for this root's writer turn.
            if not self._legacy_runtime_cache_requires_cleanup:
                return
            target_path = self.runtime_embedding_dim_path
            self._layout.validate_user_file(
                target_path,
                label="Embedding 运行缓存",
                allow_missing=False,
            )
            try:
                target_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                try:
                    self._write_runtime_embedding_payload({})
                except OSError:
                    raise RuntimeError(
                        f"无法清理旧 Embedding 维度缓存: {target_path}"
                    ) from None
            self._legacy_runtime_cache_requires_cleanup = False

    def _runtime_embedding_fingerprint_matches(self, payload: Any) -> bool:
        """检查运行期维度缓存是否仍然对应当前 Embedding 配置。"""
        if not isinstance(payload, dict):
            return False

        expected = self.embedding_runtime_fingerprint
        return all(
            str(payload.get(key, "")) == value for key, value in expected.items()
        )

    @staticmethod
    def _runtime_snapshot_has_sensitive_field(value: Any) -> bool:
        """Reject credential-shaped keys anywhere in an internal snapshot."""

        if isinstance(value, Mapping):
            for key, child in value.items():
                if isinstance(key, str) and _is_display_credential_parameter(key):
                    return True
                if Config._runtime_snapshot_has_sensitive_field(child):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(Config._runtime_snapshot_has_sensitive_field(item) for item in value)
        return False

    @staticmethod
    def _runtime_snapshot_has_valid_v1_schema(snapshot: Mapping[str, Any]) -> bool:
        """Validate the secret-free, versioned runtime-snapshot contract.

        This stays intentionally generic: Config validates an on-disk runtime
        record, while lifecycle owns readiness interpretation and provider
        probing.  A mapping that merely happens not to contain a credential is
        not evidence that it describes the database/index it sits beside.
        """

        if snapshot.get("schema_version") != _RUNTIME_SNAPSHOT_SCHEMA_VERSION:
            return False

        database = snapshot.get("database")
        if not isinstance(database, Mapping):
            return False
        database_schema_version = database.get("schema_version")
        if not isinstance(database_schema_version, str) or not _SEMVER_RE.fullmatch(
            database_schema_version
        ):
            return False

        embedding = snapshot.get("embedding")
        if not isinstance(embedding, Mapping):
            return False
        provider = embedding.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            return False

        fingerprint = embedding.get("fingerprint")
        if not isinstance(fingerprint, Mapping):
            return False
        base_url_sha256 = fingerprint.get("base_url_sha256")
        embedding_model = fingerprint.get("embedding_model")
        embedding_dim = fingerprint.get("embedding_dim")
        return (
            isinstance(base_url_sha256, str)
            and _SHA256_HEX_RE.fullmatch(base_url_sha256) is not None
            and isinstance(embedding_model, str)
            and bool(embedding_model.strip())
            and isinstance(embedding_dim, str)
            and _POSITIVE_DECIMAL_RE.fullmatch(embedding_dim) is not None
        )

    def validate_runtime_config_snapshot(self) -> Dict[str, Any] | None:
        """Read and validate the internal, secret-free runtime snapshot.

        The snapshot is intentionally *not* merged into application config.  A
        credential-shaped field fails closed before any value is returned or
        included in an error message.
        """

        target_path = self._layout.validate_user_file(
            self.runtime_config_path,
            label="运行时配置快照",
            allow_missing=True,
        )
        if not target_path.exists():
            return None
        snapshot = _load_yaml_mapping(target_path, "运行时配置快照")
        if self._runtime_snapshot_has_sensitive_field(snapshot):
            raise ValueError("运行时配置快照包含敏感字段，拒绝读取")
        if not self._runtime_snapshot_has_valid_v1_schema(snapshot):
            raise ValueError("运行时配置快照结构无效")
        return copy.deepcopy(snapshot)

    def read_runtime_config_snapshot(self) -> Dict[str, Any] | None:
        """Compatibility-friendly spelling for validated runtime snapshot reads."""

        return self.validate_runtime_config_snapshot()

    def write_runtime_config_snapshot(self, payload: Mapping[str, Any]) -> None:
        """Merge and atomically publish one secret-free data-runtime snapshot.

        This file is internal runtime state, not a business configuration input:
        it is never merged into ``self._config``.  Multiple runtime features own
        distinct facts in the same document (for example R2 readiness and an
        R4 active-vector-generation pointer), so this compatibility writer must
        preserve extensions it does not own instead of replacing the whole YAML
        file.  Callers must already have reached an explicit lifecycle write
        boundary; the nested R3 lease keeps direct callers safe as well.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("运行时配置快照必须是映射对象")
        try:
            snapshot = copy.deepcopy(dict(payload))
        except (TypeError, ValueError) as exc:
            raise ValueError("运行时配置快照无法安全复制") from exc
        if self._runtime_snapshot_has_sensitive_field(snapshot):
            raise ValueError("运行时配置快照不得包含敏感字段")
        if not self._runtime_snapshot_has_valid_v1_schema(snapshot):
            raise ValueError("运行时配置快照结构无效")
        # Delayed imports avoid a module-level Config/runtime import cycle.  The
        # store supplies the raw-byte compare-and-swap and semantic merge needed
        # to preserve other secret-free feature extensions in the data-root
        # runtime snapshot (`<data-root>/config/local.yaml`).
        from src.runtime.runtime_snapshot import RuntimeSnapshotStore
        from src.runtime.write_lease import write_lease_scope

        with write_lease_scope(self._layout):
            store = RuntimeSnapshotStore(self._layout)
            observed = store.read()
            if observed.exists and not self._runtime_snapshot_has_valid_v1_schema(
                observed.payload
            ):
                raise ValueError("运行时配置快照结构无效")
            merged = observed.merged(snapshot)
            if not self._runtime_snapshot_has_valid_v1_schema(merged):
                raise ValueError("运行时配置快照结构无效")
            published = store.publish(observed, merged)
            if published.payload != merged:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "运行时配置快照发布后不可读取",
                    stage="runtime_snapshot",
                    recoverable=True,
                )

    @property
    def zhihu_cookie(self) -> Optional[str]:
        """知乎 Cookie（可选，用于绕过登录墙获取完整内容）"""
        return self.get("processors.zhihu.cookie")

    @property
    def log_level(self) -> str:
        """日志级别"""
        return (
            self._get_runtime_override("PKV_LOG_LEVEL")
            or self._get_runtime_override("LOG_LEVEL")
            or self.get("logging.level", "INFO")
        )

    def ensure_dirs(self):
        """确保所有必要的目录存在"""
        from src.runtime.write_lease import write_lease_scope

        with write_lease_scope(self._layout):
            self._layout.ensure_user_directories()

    def update_user_config(self, updates: Mapping[str, Any]) -> None:
        """Persist business settings to the independently validated user config.

        This is the write half of the Kernel reload contract.  Serializing the
        read/merge/publish sequence prevents two in-process settings writers
        from losing each other's dot-path update before the next immutable
        application snapshot is published.
        """

        # User configuration is profile-local and intentionally exists before a
        # data root is initialized.  Do not acquire the data-root writer lease
        # here: doing so would create ``<data-root>/runtime/write.lease`` merely
        # to edit ``~/.pkv/config.yaml``.  The profile-local lock and atomic
        # publish retain the existing Config writer contract; shared data-root
        # mutations acquire R3 at their Application/Kernel boundaries instead.
        with _LOCAL_CONFIG_UPDATE_LOCK:
            # Preflight against the exact same merge/parser used for a reload.
            # This runs before directory creation or YAML publication, so a
            # disallowed root switch cannot leave an inactive-but-confusing
            # user configuration behind.
            candidate = self._build_reloaded_snapshot(
                user_config_updates=updates,
            )
            self._require_same_data_root(candidate, stage="config_update")
            self._layout.ensure_user_config_directory()
            target_path = self._layout.validate_user_config_file(
                self.user_config_path,
                label="用户配置",
                allow_missing=True,
            )
            set_yaml_config_values(target_path, updates)
            self._layout.validate_user_config_file(
                target_path,
                label="用户配置",
                allow_missing=False,
            )

    def update_local_config(self, updates: Mapping[str, Any]) -> None:
        """Deprecated alias for :meth:`update_user_config`.

        The historical name is retained for API-major compatibility, but it no
        longer writes ``<data-root>/config/local.yaml``.  That file is solely an
        internal, secret-free runtime snapshot.
        """

        warnings.warn(
            "update_local_config 已弃用；请改用 update_user_config",
            DeprecationWarning,
            stacklevel=2,
        )
        self.update_user_config(updates)


# 全局配置实例 (单例模式)
_config_instance: Optional[Config] = None


def get_config() -> Config:
    """
    获取全局配置实例 (单例)

    Returns:
        Config 实例
    """
    global _config_instance
    with _CONFIG_INSTANCE_LOCK:
        if _config_instance is None:
            _config_instance = Config()
        return _config_instance


def replace_config_instance(config: Config) -> Config:
    """Atomically publish the legacy Config identity used by compatibility code.

    Kernel/Application owns when this is called.  Explicit isolated
    ``KnowledgeApplication(config)`` instances intentionally do not call this
    helper, so Config A can coexist with an operation-scoped Config B.
    """

    global _config_instance
    with _CONFIG_INSTANCE_LOCK:
        _config_instance = config
        return config


def get_workflow_config(workflow_name: str) -> Dict[str, Any]:
    """
    获取工作流配置（快捷入口）。

    Args:
        workflow_name: 工作流名称

    Returns:
        工作流配置字典
    """
    return get_config().get_workflow_config(workflow_name)

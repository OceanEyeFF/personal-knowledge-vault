"""纯 YAML 配置与 Embedding 维度持久化测试。"""

import json
import os
from pathlib import Path

import pytest
import yaml

import src.utils.config as config_module
from src.utils.config import (
    Config,
    endpoint_contract_sha256,
    redact_url_credentials,
    set_yaml_config_value,
    set_yaml_config_values,
    url_contains_credentials,
)


def _write_config(
    path: Path,
    data_dir: Path,
    llm_model: str = "yaml-chat",
    embd_model: str = "yaml-embedding",
    embd_dim: int | str = 1536,
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "storage": {
                    "data_dir": str(data_dir),
                    "db_path": str(data_dir / "db" / "knowledge_vault.db"),
                },
                "ai": {
                    "llm": {
                        "api_key": "",
                        "base_url": "https://llm.example.com/v1",
                        "model": llm_model,
                    },
                    "embedding": {
                        "api_key": "",
                        "base_url": "https://embd.example.com/v1",
                        "model": embd_model,
                        "dim": embd_dim,
                    },
                },
                "logging": {"level": "INFO"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_local_yaml_recursively_overrides_base_config(tmp_path: Path) -> None:
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    _write_config(base_path, tmp_path / "data")
    local_path.write_text(
        """
ai:
  llm:
    api_key: local-llm-key
    model: local-chat
  embedding:
    api_key: local-embedding-key
    dim: auto
""",
        encoding="utf-8",
    )

    config = Config(str(base_path), str(local_path))

    assert config.llm_api_key == "local-llm-key"
    assert config.llm_model == "local-chat"
    assert config.llm_base_url == "https://llm.example.com/v1"
    assert config.embd_api_key == "local-embedding-key"
    assert config.embedding_dim_is_auto is True


def test_local_data_dir_rebases_copied_default_storage_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """整份复制配置后仅修改 data_dir 时，默认子路径应迁移到新根。"""
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    base_data_dir = tmp_path / "base-data"
    local_data_dir = tmp_path / "local-data"
    _write_config(base_path, base_data_dir)

    base_config = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    base_config["storage"].update(
        {
            "vault_dir": str(base_data_dir / "vault"),
            "vector_index_dir": str(base_data_dir / "vectors"),
            "log_dir": str(base_data_dir / "logs"),
            "tmp_dir": str(base_data_dir / "tmp"),
        }
    )
    base_path.write_text(
        yaml.safe_dump(base_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    copied_config = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    copied_config["storage"]["data_dir"] = str(local_data_dir)
    local_path.write_text(
        yaml.safe_dump(copied_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    for key in ("DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    config = Config(str(base_path), str(local_path))

    assert config.data_dir == local_data_dir
    assert config.db_path == local_data_dir / "db" / "knowledge_vault.db"
    assert config.vault_dir == local_data_dir / "vault"
    assert config.vector_index_dir == local_data_dir / "vectors"
    assert config.log_dir == local_data_dir / "logs"
    assert config.tmp_dir == local_data_dir / "tmp"


def test_local_data_dir_preserves_explicit_storage_path_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    """修改数据根时，本机配置中不同于基础值的子路径仍应优先。"""
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    base_data_dir = tmp_path / "base-data"
    local_data_dir = tmp_path / "local-data"
    custom_vault = tmp_path / "external-vault"
    custom_db = tmp_path / "external-db" / "custom.db"
    _write_config(base_path, base_data_dir)
    local_path.write_text(
        yaml.safe_dump(
            {
                "storage": {
                    "data_dir": str(local_data_dir),
                    "vault_dir": str(custom_vault),
                    "db_path": str(custom_db),
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for key in ("DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    config = Config(str(base_path), str(local_path))

    assert config.data_dir == local_data_dir
    assert config.db_path == custom_db
    assert config.vault_dir == custom_vault
    assert config.vector_index_dir == local_data_dir / "vectors"
    assert config.log_dir == local_data_dir / "logs"
    assert config.tmp_dir == local_data_dir / "tmp"


def test_legacy_provider_environment_variables_are_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    """旧 Provider 环境变量不得重新成为应用配置入口。"""
    base_path = tmp_path / "config.yaml"
    _write_config(base_path, tmp_path / "data", llm_model="yaml-chat")
    monkeypatch.setenv("PKV_LLM_MODEL", "env-chat")
    monkeypatch.setenv("PKV_EMBD_MODEL", "env-embedding")
    monkeypatch.setenv("PKV_LLM_API_KEY", "env-secret")

    config = Config(str(base_path))

    assert config.llm_model == "yaml-chat"
    assert config.embd_model == "yaml-embedding"
    assert config.llm_api_key is None or config.llm_api_key == ""


def test_data_dir_derives_all_runtime_storage_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """DATA_DIR 应完整隔离数据库、Vault、向量、日志与临时目录。"""
    base_path = tmp_path / "config.yaml"
    _write_config(base_path, tmp_path / "yaml-data")
    runtime_data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    for key in ("DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    config = Config(str(base_path))

    assert config.data_dir == runtime_data_dir
    assert config.db_path == runtime_data_dir / "db" / "knowledge_vault.db"
    assert config.vault_dir == runtime_data_dir / "vault"
    assert config.vector_index_dir == runtime_data_dir / "vectors"
    assert config.log_dir == runtime_data_dir / "logs"
    assert config.tmp_dir == runtime_data_dir / "tmp"


def test_explicit_runtime_paths_override_data_dir(tmp_path: Path, monkeypatch) -> None:
    """细粒度运行隔离路径应优先于 DATA_DIR 派生路径。"""
    base_path = tmp_path / "config.yaml"
    _write_config(base_path, tmp_path / "yaml-data")
    runtime_data_dir = tmp_path / "runtime-data"
    overrides = {
        "DB_PATH": tmp_path / "custom" / "vault.db",
        "VAULT_DIR": tmp_path / "custom-vault",
        "VECTOR_DIR": tmp_path / "custom-vectors",
        "LOG_DIR": tmp_path / "custom-logs",
        "TMP_DIR": tmp_path / "custom-tmp",
    }
    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    for key, path in overrides.items():
        monkeypatch.setenv(key, str(path))

    config = Config(str(base_path))

    assert config.db_path == overrides["DB_PATH"]
    assert config.vault_dir == overrides["VAULT_DIR"]
    assert config.vector_index_dir == overrides["VECTOR_DIR"]
    assert config.log_dir == overrides["LOG_DIR"]
    assert config.tmp_dir == overrides["TMP_DIR"]


def test_legacy_vector_store_path_environment_variable_is_ignored(
    tmp_path: Path, monkeypatch
) -> None:
    """旧 VECTOR_STORE_PATH 不得重新成为向量目录配置入口。"""
    base_path = tmp_path / "config.yaml"
    _write_config(base_path, tmp_path / "yaml-data")
    runtime_data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    monkeypatch.delenv("VECTOR_DIR", raising=False)
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "legacy-vectors"))

    config = Config(str(base_path))

    assert config.vector_index_dir == runtime_data_dir / "vectors"


def test_set_yaml_config_value_preserves_sibling_values(tmp_path: Path) -> None:
    local_path = tmp_path / "local.yaml"
    set_yaml_config_value(local_path, "ai.llm.model", "first-model")
    set_yaml_config_value(local_path, "ai.llm.api_key", "secret")

    data = yaml.safe_load(local_path.read_text(encoding="utf-8"))

    assert data["ai"]["llm"]["model"] == "first-model"
    assert data["ai"]["llm"]["api_key"] == "secret"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_set_yaml_config_value_forces_private_posix_permissions(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "local.yaml"
    local_path.write_text("service:\n  mode: old\n", encoding="utf-8")
    local_path.chmod(0o644)

    set_yaml_config_value(local_path, "service.mode", "new")

    assert local_path.stat().st_mode & 0o777 == 0o600


def test_set_yaml_config_value_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local.yaml"
    original = b"service:\n  mode: original\n"
    local_path.write_bytes(original)

    def fail_replace(source, destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        set_yaml_config_value(local_path, "service.mode", "new")

    assert local_path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [local_path]


def test_set_yaml_config_values_uses_one_atomic_replace(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local.yaml"
    real_replace = config_module.os.replace
    replace_calls = []

    def record_replace(source, destination) -> None:
        replace_calls.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(config_module.os, "replace", record_replace)

    set_yaml_config_values(
        local_path,
        {
            "ai.llm.model": "model-a",
            "ai.embedding.model": "model-b",
            "retrieval.default_strategy": "hybrid",
        },
    )

    assert len(replace_calls) == 1
    data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert data["ai"]["llm"]["model"] == "model-a"
    assert data["ai"]["embedding"]["model"] == "model-b"


def test_set_yaml_config_values_validation_failure_is_zero_write(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "local.yaml"
    original = b"ai:\n  llm: scalar\n"
    local_path.write_bytes(original)

    with pytest.raises(ValueError, match="配置路径不是映射"):
        set_yaml_config_values(
            local_path,
            {
                "service.mode": "would-have-been-first",
                "ai.llm.model": "cannot-be-written",
            },
        )

    assert local_path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [local_path]


def test_malformed_local_yaml_error_does_not_expose_secret(tmp_path: Path) -> None:
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    _write_config(base_path, tmp_path / "data")
    secret = "do-not-print-this-api-key"
    local_path.write_text(
        f"ai:\n  llm:\n    api_key: [{secret}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        Config(str(base_path), str(local_path))

    message = str(exc_info.value)
    assert "本机配置文件 YAML 格式错误" in message
    assert str(local_path) in message
    assert secret not in message


def test_set_value_malformed_yaml_error_does_not_expose_secret(
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "local.yaml"
    secret = "do-not-print-this-existing-key"
    local_path.write_text(
        f"ai:\n  llm:\n    api_key: [{secret}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        set_yaml_config_value(local_path, "ai.llm.model", "model")

    message = str(exc_info.value)
    assert "配置文件 YAML 格式错误" in message
    assert str(local_path) in message
    assert secret not in message


@pytest.mark.parametrize("content", ["[]\n", "0\n", "false\n"])
def test_config_rejects_falsy_non_mapping_yaml_root(
    tmp_path: Path, content: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="配置文件根节点必须是映射"):
        Config(str(config_path))


def test_config_treats_empty_yaml_as_empty_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    config = Config(str(config_path))

    assert config.get("missing") is None


def test_config_persists_runtime_embedding_dim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_dim="auto")

    config = Config(str(base_path))
    assert config.embedding_dim is None

    config.set_runtime_embedding_dim(2560)

    reloaded_config = Config(str(base_path))
    assert reloaded_config.embedding_dim == 2560
    assert reloaded_config.runtime_embedding_dim_path == data_dir / "runtime" / "embedding_dim.json"


def test_embedding_fingerprints_hash_credential_bearing_base_url(
    tmp_path: Path, monkeypatch
) -> None:
    """运行缓存与索引指纹都不得持久化 endpoint 原文。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    data_dir = tmp_path / "data"
    endpoint = (
        "https://fingerprint-user:fingerprint-password@embd.example/v1"
        "?api_key=fingerprint-query"
    )
    _write_config(base_path, data_dir, embd_dim="auto")
    set_yaml_config_value(local_path, "ai.embedding.base_url", endpoint)

    config = Config(str(base_path), str(local_path))
    expected_hash = endpoint_contract_sha256(endpoint)
    config.set_runtime_embedding_dim(2560)

    persisted = config.runtime_embedding_dim_path.read_text(encoding="utf-8")
    payload = json.loads(persisted)
    assert endpoint not in persisted
    assert "fingerprint-user" not in persisted
    assert payload["fingerprint"] == {
        "base_url_sha256": expected_hash,
        "embedding_model": "yaml-embedding",
    }
    assert config.embedding_index_fingerprint(2560) == {
        "base_url_sha256": expected_hash,
        "embedding_model": "yaml-embedding",
        "embedding_dim": "2560",
    }
    rotated_endpoint = (
        "https://rotated-user:rotated-password@embd.example/v1"
        "?api_key=rotated-query"
    )
    assert endpoint_contract_sha256(rotated_endpoint) == expected_hash


@pytest.mark.parametrize(
    "parameter_name",
    [
        "api_key",
        "access_token",
        "code",
        "key",
        "subscription-key",
        "jwt",
        "JSESSIONID",
        "session-id",
        "session_id",
        "sessionId",
    ],
)
def test_endpoint_contract_ignores_exact_credential_parameter_rotation(
    parameter_name: str,
) -> None:
    first = f"https://embd.example/v1?{parameter_name}=first&region=cn"
    second = f"https://embd.example/v1?{parameter_name}=second&region=cn"

    assert endpoint_contract_sha256(first) == endpoint_contract_sha256(second)


@pytest.mark.parametrize("parameter_name", ["region_code", "routing_key"])
def test_endpoint_contract_keeps_noncredential_parameter_values(
    parameter_name: str,
) -> None:
    first = f"https://embd.example/v1?{parameter_name}=backend-a"
    second = f"https://embd.example/v1?{parameter_name}=backend-b"

    assert endpoint_contract_sha256(first) != endpoint_contract_sha256(second)


@pytest.mark.parametrize(
    "parameter_name",
    ["region_code", "routing_key", "session_timeout", "routing_session"],
)
def test_endpoint_decision_allows_noncredential_composite_names(
    parameter_name: str,
) -> None:
    endpoint = f"https://embd.example/v1?{parameter_name}=backend-a"

    assert not url_contains_credentials(endpoint)
    # 日志/显示仍采用更保守的边界规则，但不能影响保存决策或 fingerprint。
    displayed = redact_url_credentials(endpoint)
    assert displayed is not None
    assert endpoint_contract_sha256(endpoint) != endpoint_contract_sha256(
        endpoint.replace("backend-a", "backend-b")
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://embd.example/v1;jwt=alias-secret",
        "https://embd.example/v1;JSESSIONID=alias-secret",
        "https://embd.example/v1?sessionId=alias-secret",
        "https://embd.example/v1#session-id=alias-secret",
        "https://embd.example/v1?subscription-key=alias-secret",
    ],
)
def test_session_and_subscription_aliases_are_never_displayed(
    endpoint: str,
) -> None:
    rotated = endpoint.replace("alias-secret", "rotated-secret")

    assert url_contains_credentials(endpoint)
    displayed = redact_url_credentials(endpoint)
    assert displayed is not None
    assert "alias-secret" not in displayed
    assert endpoint_contract_sha256(endpoint) == endpoint_contract_sha256(rotated)


def test_endpoint_contract_credentials_are_all_covered_by_display_redaction() -> None:
    missing_display_markers = {
        parameter_name
        for parameter_name in config_module._ENDPOINT_CONTRACT_CREDENTIAL_PARAMETER_NAMES
        if not config_module._is_display_credential_parameter(parameter_name)
    }

    assert missing_display_markers == set()


def test_endpoint_contract_keeps_path_and_noncredential_fragment_values() -> None:
    assert endpoint_contract_sha256(
        "https://embd.example/v1?region=cn#route=alpha"
    ) != endpoint_contract_sha256(
        "https://embd.example/v2?region=cn#route=alpha"
    )
    assert endpoint_contract_sha256(
        "https://embd.example/v1?region=cn#route=alpha"
    ) != endpoint_contract_sha256(
        "https://embd.example/v1?region=cn#route=beta"
    )


def test_path_matrix_credentials_use_separate_display_and_contract_rules() -> None:
    endpoint = (
        "https://embd.example/v1;api_key=matrix-secret;region=north"
        "?routing=primary"
    )
    rotated_secret = endpoint.replace("matrix-secret", "rotated-secret")
    changed_region = endpoint.replace("region=north", "region=south")

    assert url_contains_credentials(endpoint)
    displayed = redact_url_credentials(endpoint)
    assert displayed is not None
    assert "matrix-secret" not in displayed
    assert "region=north" in displayed
    assert endpoint_contract_sha256(endpoint) == endpoint_contract_sha256(
        rotated_secret
    )
    assert endpoint_contract_sha256(endpoint) != endpoint_contract_sha256(
        changed_region
    )


def test_legacy_plaintext_runtime_fingerprint_is_safely_invalidated(
    tmp_path: Path, monkeypatch
) -> None:
    """旧 base_url 明文指纹不作兼容读取，只按失效缓存处理。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_dim="auto")
    runtime_path = data_dir / "runtime" / "embedding_dim.json"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(
        json.dumps(
            {
                "embedding_dim": 1536,
                "fingerprint": {
                    "base_url": "https://legacy-user:legacy-password@embd.example/v1",
                    "embedding_model": "yaml-embedding",
                },
            }
        ),
        encoding="utf-8",
    )

    assert Config(str(base_path)).embedding_dim is None
    assert not runtime_path.exists() or "legacy-password" not in runtime_path.read_text(
        encoding="utf-8"
    )


def test_config_invalidates_runtime_dim_when_model_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_model="model-a", embd_dim="auto")
    config = Config(str(base_path))
    config.set_runtime_embedding_dim(2560)

    _write_config(base_path, data_dir, embd_model="model-b", embd_dim="auto")

    assert Config(str(base_path)).embedding_dim is None


def test_config_invalidates_runtime_dim_when_base_url_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_dim="auto")
    set_yaml_config_value(local_path, "ai.embedding.base_url", "https://one.example/v1")
    config = Config(str(base_path), str(local_path))
    config.set_runtime_embedding_dim(1536)

    set_yaml_config_value(local_path, "ai.embedding.base_url", "https://two.example/v1")

    assert Config(str(base_path), str(local_path)).embedding_dim is None

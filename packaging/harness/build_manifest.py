"""Generate a strict, hash-bound W3 loopback harness package manifest."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import tempfile


def _load_loopback_provider():
    provider_path = Path(__file__).with_name("loopback_provider.py")
    if provider_path.is_symlink() or not provider_path.is_file():
        raise RuntimeError("loopback provider must be a regular sibling file")
    provider_path = provider_path.resolve(strict=True)
    module_name = "_pkv_w3_loopback_provider_contract"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("loopback provider module specification is invalid")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


_provider = _load_loopback_provider()
CONTRACT_ID = _provider.CONTRACT_ID
HARNESS_VERSION = _provider.HARNESS_VERSION
MANIFEST_SCHEMA = _provider.MANIFEST_SCHEMA
HarnessContractError = _provider.HarnessContractError
canonical_json_bytes = _provider.canonical_json_bytes
load_script = _provider.load_script
sha256_file = _provider.sha256_file


def _contained_relative(package_root: Path, path: Path, *, label: str) -> str:
    try:
        resolved_root = package_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise HarnessContractError(
            f"{label} must be inside the manifest directory"
        ) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise HarnessContractError(f"{label} must be a regular non-link file")
    return relative.as_posix()


def build_manifest(
    *,
    output: Path,
    runtime: Path,
    runtime_kind: str,
    contract: Path,
    scripts: list[Path],
    source_revision: str,
    build_fingerprint_sha256: str,
    toolchain_lock_sha256: str,
) -> dict:
    if output.exists():
        raise HarnessContractError("manifest output already exists")
    package_root = output.parent.resolve(strict=True)
    if runtime_kind not in {"source", "frozen"}:
        raise HarnessContractError("runtime kind must be source or frozen")
    for label, path in (
        ("runtime", runtime),
        ("contract", contract),
        *[("script", item) for item in scripts],
    ):
        if path.is_symlink():
            raise HarnessContractError(f"{label} must not be a symbolic link")
    if not source_revision or source_revision != source_revision.strip():
        raise HarnessContractError("source revision is invalid")
    for label, value in (
        ("build fingerprint", build_fingerprint_sha256),
        ("toolchain lock hash", toolchain_lock_sha256),
    ):
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise HarnessContractError(f"{label} is invalid")

    runtime_path = runtime.resolve(strict=True)
    contract_path = contract.resolve(strict=True)
    script_entries = []
    seen_ids: set[str] = set()
    for script_path in scripts:
        script_path = script_path.resolve(strict=True)
        script = load_script(script_path)
        if script.script_id in seen_ids:
            raise HarnessContractError("script ids must be unique")
        seen_ids.add(script.script_id)
        script_entries.append(
            {
                "script_id": script.script_id,
                "path": _contained_relative(
                    package_root,
                    script_path,
                    label="script",
                ),
                "sha256": script.sha256,
            }
        )
    if not script_entries:
        raise HarnessContractError("at least one script is required")
    script_entries.sort(key=lambda entry: entry["script_id"])

    return {
        "schema_version": MANIFEST_SCHEMA,
        "contract_id": CONTRACT_ID,
        "harness_version": HARNESS_VERSION,
        "distribution": "e2e-only",
        "release_payload_membership": "forbidden",
        "runtime": {
            "kind": runtime_kind,
            "path": _contained_relative(package_root, runtime_path, label="runtime"),
            "size": runtime_path.stat().st_size,
            "sha256": sha256_file(runtime_path),
        },
        "contract": {
            "path": _contained_relative(package_root, contract_path, label="contract"),
            "sha256": sha256_file(contract_path),
        },
        "scripts": script_entries,
        "build": {
            "source_revision": source_revision,
            "build_fingerprint_sha256": build_fingerprint_sha256,
            "toolchain_lock_sha256": toolchain_lock_sha256,
        },
    }


def atomic_publish(path: Path, payload: bytes) -> None:
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-kind", choices=("source", "frozen"), required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--script", type=Path, action="append", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--build-fingerprint-sha256", required=True)
    parser.add_argument("--toolchain-lock-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = build_manifest(
            output=args.output,
            runtime=args.runtime,
            runtime_kind=args.runtime_kind,
            contract=args.contract,
            scripts=args.script,
            source_revision=args.source_revision,
            build_fingerprint_sha256=args.build_fingerprint_sha256,
            toolchain_lock_sha256=args.toolchain_lock_sha256,
        )
        atomic_publish(args.output, canonical_json_bytes(manifest) + b"\n")
    except (HarnessContractError, OSError) as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Versioned public-contract helpers for :mod:`pkv_kernel`.

This module deliberately has no dependency on ``src``.  It is the durable
metadata surface an external Wrapper reads before constructing a Kernel.
Implementation objects remain behind the package facade.
"""

from __future__ import annotations

from dataclasses import dataclass
import platform
import re
import sys
from typing import Iterable


# ``pkv_kernel.contracts`` is one of two supported public nested modules (the
# other is ``pkv_kernel.lifecycle``).  The package-root aliases are
# intentionally convenient, while this explicit list prevents an accidental
# helper from becoming an SDK commitment merely because Python can import the
# module from an installed wheel.
__all__ = [
    "KERNEL_API_VERSION",
    "KERNEL_CAPABILITIES",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_PYTHON",
    "KernelCapabilities",
    "KernelCompatibilityError",
    "get_kernel_capabilities",
    "require_kernel_compatibility",
    "runtime_is_supported",
]


# This is the protocol version of the public Python surface, independent from
# the product build version exposed as ``pkv_kernel.__version__``.
KERNEL_API_VERSION = "1.0.0"
SUPPORTED_PYTHON = ">=3.11,<3.13"
SUPPORTED_PLATFORMS = ("Windows",)

# Capability identifiers are additive within a KERNEL_API_VERSION major line.
# A Wrapper must negotiate any identifier it requires instead of inferring
# behavior from product-version strings alone.
KERNEL_CAPABILITIES = frozenset(
    {
        "kernel.lifecycle.v1",
        "kernel.runtime-lifecycle.v1",
        "kernel.archive.v1",
        "kernel.retrieval.v1",
        "kernel.entries.v1",
        "kernel.chat-sessions.v1",
        "kernel.configuration-snapshot-reload.v1",
    }
)


@dataclass(frozen=True)
class KernelCapabilities:
    """Immutable result of a public Kernel version/capability handshake."""

    sdk_version: str
    api_version: str
    capabilities: frozenset[str]
    python_requires: str
    supported_platforms: tuple[str, ...]


class KernelCompatibilityError(ValueError):
    """A Wrapper's declared Kernel requirement is not satisfied."""


def get_kernel_capabilities(sdk_version: str) -> KernelCapabilities:
    """Return the immutable public handshake payload for this SDK build."""

    return KernelCapabilities(
        sdk_version=_parse_semver(sdk_version, label="SDK version"),
        api_version=KERNEL_API_VERSION,
        capabilities=KERNEL_CAPABILITIES,
        python_requires=SUPPORTED_PYTHON,
        supported_platforms=SUPPORTED_PLATFORMS,
    )


def require_kernel_compatibility(
    sdk_version: str,
    *,
    minimum_sdk_version: str | None = None,
    maximum_sdk_version: str | None = None,
    required_capabilities: Iterable[str] = (),
) -> KernelCapabilities:
    """Validate a Wrapper requirement and return the negotiated capabilities.

    Bounds are inclusive semantic versions.  This intentionally supports only
    release ``MAJOR.MINOR.PATCH`` values: pre-release policy is deferred until
    an actual distribution channel exists, rather than guessed here.
    """

    capabilities = get_kernel_capabilities(sdk_version)
    current = _version_tuple(capabilities.sdk_version, label="SDK version")
    if minimum_sdk_version is not None:
        minimum = _version_tuple(minimum_sdk_version, label="minimum_sdk_version")
        if current < minimum:
            raise KernelCompatibilityError(
                "Kernel SDK version is below the Wrapper minimum"
            )
    if maximum_sdk_version is not None:
        maximum = _version_tuple(maximum_sdk_version, label="maximum_sdk_version")
        if current > maximum:
            raise KernelCompatibilityError(
                "Kernel SDK version exceeds the Wrapper maximum"
            )

    required = frozenset(_validate_capability(name) for name in required_capabilities)
    missing = sorted(required - capabilities.capabilities)
    if missing:
        raise KernelCompatibilityError(
            "Kernel SDK is missing required capabilities: " + ", ".join(missing)
        )
    return capabilities


def runtime_is_supported() -> bool:
    """Return whether this host meets the published K1a runtime contract."""

    return (
        platform.system() in SUPPORTED_PLATFORMS
        and (3, 11) <= sys.version_info[:2] < (3, 13)
    )


def _parse_semver(value: str, *, label: str) -> str:
    _version_tuple(value, label=label)
    return value


def _version_tuple(value: str, *, label: str) -> tuple[int, int, int]:
    if type(value) is not str:
        raise KernelCompatibilityError(f"{label} must be MAJOR.MINOR.PATCH")
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if match is None:
        raise KernelCompatibilityError(f"{label} must be MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def _validate_capability(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.v[1-9]\d*", value):
        raise KernelCompatibilityError("required_capabilities contains an invalid identifier")
    return value

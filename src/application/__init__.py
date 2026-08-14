"""Adapter-neutral application services for Personal Knowledge Vault.

The package is deliberately below the CLI, MCP, and external-wrapper adapters. It owns
composition of stores, retrieval, providers, and workflows so an entrypoint
can remain responsible for protocol/UI concerns only.
"""

from src.application.knowledge_application import (
    KnowledgeApplication,
    configure_application,
    get_application,
    reload_application,
    reset_application,
)

__all__ = [
    "KnowledgeApplication",
    "configure_application",
    "get_application",
    "reload_application",
    "reset_application",
]

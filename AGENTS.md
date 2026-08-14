# Personal Knowledge Vault — Agent entry point

This file is the portable agent-facing entry point for the repository.
Before making a change, read the canonical project guide in
[CLAUDE.md](./CLAUDE.md) in full.

## Working rules

- Default automation must use `scripts/run-test.ps1` and an isolated
  `.data-test` root. Do not operate on a user Vault, run a real migration, or
  use real Provider credentials without explicit user authority.
- Read the module guide for every area you touch. The root index below links to
  committed `AGENTS.md` entry pages; those pages either contain the local
  guidance or point to their canonical `CLAUDE.md`.
- Treat the Developer Preview Artifact as a held test candidate. W4 functional
  verification does not authorize a release while its compliance blockers
  remain open.

## Documentation map

- [Project guide and safe commands](./CLAUDE.md)
- [CLI](./src/cli/AGENTS.md)
- [MCP](./src/mcp/AGENTS.md)
- [Headless Kernel](./src/kernel/AGENTS.md)
- [Application composition](./src/application/AGENTS.md)
- [Workflow](./src/workflow/AGENTS.md)
- [Processors](./src/processors/AGENTS.md)
- [Retrieval](./src/retrieval/AGENTS.md)
- [Storage](./src/storage/AGENTS.md)
- [AI](./src/ai/AGENTS.md)
- [Utilities](./src/utils/AGENTS.md)
- [Scripts](./scripts/AGENTS.md)
- [Tests](./tests/AGENTS.md)
- [Configuration](./config/AGENTS.md)

The default user-data-root and installation-topology discussion is intentionally
deferred; do not change that product-policy decision as part of documentation
maintenance.

The PySide6 desktop wrapper lives in the separate `pkv-GUI` repository and is
not a module, test lane, or packaged entrypoint of this headless repository.

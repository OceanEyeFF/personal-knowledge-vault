# Utilities module agent guide

This module owns configuration, logging, text utilities, and setup validation.

- Preserve RuntimeLayout as the authoritative boundary for bundled resources
  and mutable user paths.
- Do not add a configuration or environment-variable bypass without matching
  containment and offline-isolation coverage.
- Run affected tests through the repository test wrapper with an isolated
  DataRoot.

For repository-wide policy, read [CLAUDE.md](../../CLAUDE.md) first.

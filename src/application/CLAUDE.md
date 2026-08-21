# Application composition module

[Root](../../CLAUDE.md) > [src](..) > **application**

## Responsibility

`KnowledgeApplication` is the adapter-neutral composition root beneath the public
headless Kernel. It owns lazy Store/Retriever/Provider factories and operation
scoped Workflow/Processor creation for one validated runtime configuration.

## Invariants

- Never import an external wrapper, CLI, or MCP package.
- Default workflows receive the application's exact configuration, Store ports,
  coordinator, Provider factories, and vector writer factory explicitly.
- BM25 and validation rejection paths must not create a Provider.
- Missing vector artifacts are not negatively cached; a long-running process must
  observe an index created later. Successfully opened stores may be cached.
- Entry points configure the process application only after runtime bootstrap.
- `reload_application` serializes publication of a new application and legacy
  configuration identity; each Application exposes an immutable snapshot with a
  monotonic generation, and in-flight operations retain their old captured graph.
- `KnowledgeApplication.config` cannot be rebound after composition. A settings
  change writes first and reloads into a new Application; it never swaps a Store,
  Provider or VectorStore underneath an existing operation.
- A normal settings update/reload may not retarget `data_root`. A different
  `data_root_identity` fails with `data_root_switch_required` before publication;
  the lifecycle `inspect → plan → confirm → execute` path owns any future root
  switch, preservation, and rebuild work.

## Tests

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\application -Command @(
  "python", "-m", "pytest", "tests/unit/test_knowledge_application.py", "-q"
)
```

Tests must include a config-A/config-B canary proving production workflow composition
touches only B, plus absent-to-present vector-index behavior.

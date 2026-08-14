# Headless Kernel module

[Root](../../CLAUDE.md) > [src](..) > **kernel**

## Responsibility

`src.kernel` is the stable, headless product facade. It owns the public operations
needed by peripheral wrappers: archive, search, entry/list/tag/statistics access,
preview projection, deletion, chat sessions, Provider creation, and
configuration reload.

The intended relationship is like LM Studio around llama.cpp: the separately
versioned `pkv-GUI` repository is one possible desktop wrapper around the Kernel,
while the Kernel remains usable without importing PySide6 or any GUI module.

## Dependency rule

- `src.kernel` may compose `src.application` and domain/infrastructure modules.
- External wrappers may import only the public `pkv_kernel` package; `src.*` is
  implementation-private to this repository.
- `src.kernel` must never import an external wrapper.
- Store, Workflow, Retriever, Processor, and Provider implementation instances do
  not escape into wrapper code. Narrow ports and domain results are exposed instead.
- Runtime bootstrap remains an entry-point responsibility. Call
  `configure_kernel(context.config)` only after bootstrap succeeds.

## Configuration and lifecycle

One `KnowledgeKernel` wraps one `KnowledgeApplication` built from one validated
configuration identity. WorkflowEngine and every default workflow step receive
that same configuration and application-owned dependencies explicitly. A settings
reload replaces both the Kernel/application singleton and the legacy config
identity; in-flight work retains its captured dependencies.

Missing vector artifacts are not negatively cached. Long-running wrappers re-probe
until an index exists, then cache the successfully opened store.

The Kernel exposes no Qt startup contract. An external wrapper owns its own
readiness and process-lifecycle verification.

## Tests

Use the repository wrapper and an isolated `.data-test` root:

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\kernel -Command @(
  "python", "-m", "pytest",
  "tests/unit/test_knowledge_application.py",
  "tests/unit/test_kernel_wrapper_boundary.py", "-q"
)
```

The architecture test is mandatory for changes on this boundary: it AST-scans
for wrapper/Qt imports and rejects reverse dependencies.

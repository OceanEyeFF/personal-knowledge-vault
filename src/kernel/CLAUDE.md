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

## K1a public SDK contract

- External code imports only `pkv_kernel`; its frozen API-major-1 surface is
  `pkv_kernel.__all__`. `src.*`, CLI, MCP and all implementation instances are
  private and must not become Wrapper dependencies.
- Wrapper startup calls `get_kernel_capabilities()` or
  `require_kernel_compatibility(...)` to negotiate the SDK version and the
  specific capabilities it needs. Current protocol/API, Python/platform range,
  compatibility and deprecation policy are normative in
  `docs/specs/interfaces/KernelSDK公开合同.md`.
- Compatible releases may add symbols/capabilities but cannot remove or
  incompatibly change a public symbol. Removal requires a documented
  `DeprecationWarning` for at least one compatible SDK version and a new API
  major.

## Configuration and lifecycle

One `KnowledgeKernel` wraps one `KnowledgeApplication` built from one validated
configuration identity. WorkflowEngine and every default workflow step receive
that same configuration and application-owned dependencies explicitly. A settings
reload atomically replaces both the Kernel/application singleton and the legacy
config identity; the returned Kernel has a higher `configuration_generation`.
In-flight work retains its captured dependencies and generation, while subsequent
operations must use the returned/new default Kernel.

An explicit `get_kernel(config_b)` graph is isolated from the process default.
Its `update_local_config(...)` writes and reloads only Config B, returning a new
isolated B Kernel without replacing legacy/default Config A. Conversely, a
process-default Kernel may update settings only while it is the current default;
a stale former default is rejected before it can overwrite newer settings.
Both default and isolated setting updates preflight a successor Config before
writing it: a data-root change fails as `data_root_switch_required` and leaves
the user config unmodified. A manually edited root is likewise rejected during
reload; only the explicit lifecycle plan/confirmation flow may move a root.

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
for wrapper/Qt imports and rejects reverse dependencies. It also freezes the
`pkv_kernel` API-major-1 export set and handshake behavior.

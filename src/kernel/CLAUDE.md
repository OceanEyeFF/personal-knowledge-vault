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
- W3 uses local SDK build `0.9.0` and API `1.0.0`. Both package-root and
  `contracts.require_kernel_compatibility` accept optional `required_api_major`:
  `None` retains the major-1 behavior, strict integer `1` succeeds, and all other
  supplied values fail before a data root is opened. Existing SDK version bounds
  remain enforced; a maximum of `0.8.1` does not accept this build.
- Compatible releases may add symbols/capabilities but cannot remove or
  incompatibly change a public symbol. Removal requires a documented
  `DeprecationWarning` for at least one compatible SDK version and a new API
  major.
- `kernel.traceable-outcomes.v1` and `kernel.traceable-outcomes.v2` are implemented only by the narrow
  `pkv_kernel.outcomes` façade. It exposes body-free Topic/Evidence/Claim and
  revisioned `research_brief` DTOs; adapters do not receive a Store, Layout,
  writer lease, source content, raw source URL, or Provider object.
- All v1 capabilities remain present. Outcome v2 is the only new capability in
  W3; `kernel.embedding-rebuild.v2` and an API-major-2 handshake are not available.

## W3 Outcome v2 boundary

The public `pkv_kernel.outcomes` module adds five explicit functions:
`get_outcome_v2`, `get_research_brief_v2`, `render_research_brief_v2`,
`approve_research_brief_v2`, and `return_research_brief_v2`. The independent frozen
contracts are `OutcomeBundleV2`, `ResearchBriefRecordV2`,
`ResearchBriefReviewResultV2`, `ResearchBriefReviewConfirmation`, and
`ResearchBriefReturnReason`. Their closed fields and signatures are defined by the
[v2 interface contract](../../docs/specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md).
Do not expand the v1 `to_dict()` shapes to carry v2 fields.

Review passes the caller's exact outcome ID, publication revision, content SHA,
and input revision to the service; return also requires a closed reason code.
Never fetch the latest brief to complete or replace that confirmation. Source,
projection, input, pointer and review-state checks remain inside the service and
its transaction. Receipt and state commit together; exact replay succeeds only
while the same tuple and target state remain current.

V2 reads use one consistent input snapshot with no writer lease, initialization
or implicit migration. Legacy schema returns `database_upgrade_required`; a
forged current version with missing structural constraints is also rejected,
including when an explicit-config Kernel is used without bootstrap.

The old unversioned SDK approve/return signatures and success semantics remain
available and emit `DeprecationWarning` naming the v2 replacement and rejection
in API major 2. CLI/MCP provide observable replacement notices too. These old
calls remain the R-04 known gap through compatibility preparation; W3 must not
turn their W1 rejection tests green. W5 owns the major switch and old-call refusal.

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
`pkv_kernel` API-major-1 export set, including the nested `outcomes` module,
and handshake behavior.

W3 component verification ran `tests/unit/test_w3_outcome_public.py` together with
that architecture test: **84 passed, exit 0** in
[the SDK boundary log](../../.data-test/w3-sdk-20261025-boundary/pytest.log).
The separate real CLI/MCP Outcome component run passed **21 tests**, with its
scope and final-tree limitation recorded in the public SDK contract. Neither
count establishes P0, MCP coverage, installed-wheel or overall W3 acceptance.
Final frozen-tree evidence belongs to the
[W3 implementation record](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md).

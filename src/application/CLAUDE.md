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
- R4 archive input is an internal durable lifecycle: Q0 admits under a short lease
  and gives slow preparation only a claim-fenced, task-private temporary-asset
  workspace (no shared `tmp/`, no persisted raw URL body), without writing content
  or creating a Provider; Q1′ is the only Markdown/SQLite and `DerivationPatch`
  writer; Q2 rechecks policy/source, reserves budget, calls Provider outside the
  writer lease, then publishes a validated generation through pointer CAS. Public
  adapters only project the result; they do not call lifecycle stores directly.
- A Q0/Q1′ processing result is not a false successful archive: it has no claimed
  `knowledge_id` until `core_committed`. Q2 `retry_required`, `budget_paused` and
  `authorization_required` preserve the safely committed entry as an explicit
  degraded result. Read-only operations never drain these tasks.
- This is bounded work in the current Application process, not a daemon or public
  resume/rebuild API. A later public adapter must be designed and tested separately.
- P1-B traceable outcomes are Provider-free: `OutcomeService` binds only canonical
  PKV entry/chunk/relation locators plus captured revisions, keeps SQLite as the
  authority, and publishes deterministic Markdown projections under `outcomes/`.
  Reads revalidate source bindings and the pointed Markdown SHA-256 without a lease
  or write; only a directly confirmed missing/changed projection is exposed as
  `projection_state=unavailable|stale` and effective `needs_review`. Permission,
  I/O or path-state uncertainty remains a safe public failure, never a silently
  trusted approved brief. Topic/question/Claim/BM25 query and suggestion title reject
  local paths and credential-bearing URLs before they reach SQLite or Markdown.
  Explicit Topic/Evidence/Claim/render/review mutations run through the root writer
  lease and audit. No outcome operation may construct QueryRouter, an LLM, an
  Embedding provider, or silently rebind a drifted source.

## Tests

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\application -Command @(
  "python", "-m", "pytest", "tests/unit/test_knowledge_application.py", "-q"
)
```

Tests must include a config-A/config-B canary proving production workflow composition
touches only B, plus absent-to-present vector-index behavior.

R4 lifecycle evidence is split deliberately: `test_r4_ingress_lifecycle.py`,
`test_r4_content_lifecycle.py`, `test_r4_derivation_lifecycle.py` and
`test_r4_derivation_patch.py` own failure/recovery semantics, while
`tests/blackbox/test_r4_cli_fullflow.py` and `test_r4_mcp_fullflow.py` own the
real public-process proof. All run only via `run-test.ps1` and independent
`.data-test` roots.

P1-B domain coverage lives in `tests/unit/test_traceable_outcomes.py`; its CLI/MCP
public-process counterparts are `tests/blackbox/test_outcome_cli_blackbox.py` and
`tests/blackbox/test_outcome_mcp_blackbox.py`. They use only test-owned SQLite and
Markdown source fixtures, then exercise the public Kernel adapter boundary.

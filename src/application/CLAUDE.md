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
- A Q1 `repair_required` task may enter local reconciliation after Storage recovery.
  It is never reclaimed for execution merely because a later trigger arrives. Under
  the root writer lease, the original journal and SQLite commit proof must match the
  task's operation, action, target, revisions and projection path. One transaction
  rechecks the captured task/fence and advances directly to `core_committed`; absent
  or uncertain proof preserves repair/backoff and blocks dependent work. Independent
  targets retain bounded progress, and this path cannot start a Provider attempt.
- P1-B traceable outcomes are Provider-free: `OutcomeService` binds only canonical
  PKV entry/chunk/relation locators plus captured revisions, keeps SQLite as the
  authority, and publishes deterministic Markdown projections under `outcomes/`.
  Topic/question/Claim/BM25 query and suggestion title reject local paths and
  credential-bearing URLs before they reach SQLite or Markdown.
  Explicit Topic/Evidence/Claim/render/review mutations run through the root writer
  lease and audit. No outcome operation may construct QueryRouter, an LLM, an
  Embedding provider, or silently rebind a drifted source.
- W3 reserves a stable `outcome_id` at Topic creation and persists the renderer
  contract, `input_revision` and canonical `input_digest`. One SQLite transaction
  commits each actual input/edge change, advances the input binding once and marks
  an existing brief `needs_review`; no-op requests and failed transactions do not
  advance it. Topic's own revision retains its separate meaning.
- Rendering uses the persisted input and pinned renderer contract. Observed source
  states cannot change bytes under the same input digest. Persisted Evidence that
  is not `current` requires explicit detachment/reattachment before rendering, even
  if the external source has recovered. Publication identity binds `outcome_id`,
  candidate revision and input digest; identical orphan bytes may be adopted, but
  existing files cannot be overwritten. After source and candidate-file validation,
  pointer CAS compares the captured Topic input binding and complete old pointer
  `(outcome_id, revision, markdown_relative_path, content_sha256, publication_id)`.
  Only that commit advances the brief revision; a losing CAS preserves the winner's
  pointer and every published file, including one the winner already references.
- Outcome reads capture input, pointer and receipt facts in one SQLite snapshot,
  then revalidate source bindings and the pointed Markdown SHA-256 without a lease
  or write. Unknown historical bindings, input mismatch and source drift yield
  effective `needs_review`; an input mismatch does not mislabel intact file bytes.
  A directly confirmed missing/changed projection yields
  `projection_state=unavailable|stale`; permission, I/O or path-state uncertainty
  fails closed. Revalidation, including a render rejected for stale sources, does
  not persist observed Evidence states or overwrite the stored brief state.
- Outcome v2 review requires the caller's exact `outcome_id`, `expected_revision`,
  `content_sha256` and `expected_input_revision`. After source/projection checks,
  SQLite rechecks the binding and state and commits review state plus its receipt
  atomically. Replay is idempotent only while the same tuple remains in that action's
  target state; an old approve cannot undo a later return or act on a new revision.
  A post-commit terminal audit failure preserves the successful receipt and does
  not fabricate a failed review event. Audit failure while recording a rejected
  review does not replace its original business error.
- W3 is the API-major-1 compatibility stage: the old unversioned approve/return
  entrypoints retain their signatures and semantics with `DeprecationWarning`.
  Their unsafe latest-version behavior remains R-04; neither R-04 nor OC-10b is
  closed until the W5 major-2 migration. The explicit v2 protocol does not make an
  old unversioned caller safe.

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

W3 adds `test_w3_outcome_store.py`, `test_w3_outcome_service.py`,
`test_w3_outcome_process.py` and `test_w3_outcome_public.py` under `tests/unit/` for
input transactions, snapshot reads, immutable publication/CAS, exact review and
compatibility. Source/projection and pointer failures are injected at explicit
internal seams; recovery then runs in a real new OS interpreter through the
offline pytest entrypoint and public v2 calls. The original nine publication
cases inject exceptions, not external process kills. The separate OC-02 case,
`test_input_commit_survives_child_exit_before_response`, exits an owned child with
`os._exit(73)` immediately after the real input transaction returns, before the
Application can respond or unwind. Another new child reads through public v2:
input revision advances exactly once, canonical digest/statement/needs_review
appear together, the old pointer and projection bytes remain, and the read makes
no database write or Outcome writer-lease acquisition. Its isolated wrapper run
passed once in 14.96 seconds; this does not prove arbitrary-kill or power-loss
recovery.

The late component run of `69 passed, 2 failed` retained the two known R-04 old
review failures; it was neither a final run of the frozen tree nor a full-suite
result. Final combined OC-02 evidence, the subsequent full P0 run, exact commands, nodeids,
fingerprints and remaining failures are governed by the root
[W3 implementation record](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md).
Do not declare final acceptance from this component count or close OC-10b during
W3. All validation remains synthetic and wrapper-owned; no real Vault, Provider,
user migration or release is authorized by this module's test evidence.

# Embedding Generation 生命周期合同（R4 staged core）

状态：runtime-internal staged core 已实现并以隔离、fake-only 回归验证；它尚未成为
CLI、MCP 或 Kernel 的公开动作。未来适配器必须显式采用本合同，不能把历史平铺
`vectors/` 目录当作回退路径。

## 边界与前置条件

R4 只在已就绪的 R2 runtime snapshot 上工作。`<data-root>/config/local.yaml` 必须
同时满足 R2 v1 的完整无密钥 base（`schema_version`、当前数据库 schema、Embedding
provider/fingerprint）以及 R2 的当前数据库/显式 Config 比较。缺失、结构无效、未知
标量 extension、数据库或 Embedding 漂移都不能发布 generation；调用方必须先完成
R2 inspect/plan/confirm/execute。

R4 的唯一 extension 是无密钥的 `embedding_index`：

```yaml
embedding_index:
  schema_version: 2
  data_root_identity_sha256: "<sha256>"
  state: ready
  source_digest: "<sha256>"
  active_generation: g-...
  previous_generation: g-... # 可为空
  retained_generations: [g-..., g-...]
  active_manifest_sha256: "<sha256>"
  contract:
    schema_version: 1
    provider: openai_compatible
    endpoint_sha256: "<sha256>"
    model: text-embedding-3-small
    dimension: 1536
    document_pipeline_version: 1
    stored_chunk_schema_version: 1
```

它通过 `RuntimeSnapshotStore` 的 read → semantic merge → raw-byte CAS publish 写入。
R2 lifecycle 和兼容的 `Config.write_runtime_config_snapshot()` 也使用同一 primitive，
因此任何一方都不得覆盖另一方的无密钥 extension。CAS 冲突为
`runtime_plan_stale`，不是“最后写者获胜”。

## 只读与 reader 合同

`inspect_embedding_index(config)` 和 `plan_embedding_rebuild(...)` 不得创建目录、锁、
数据库、VectorStore、Provider、staging 或 audit 文件；它们只接受显式 Config，绝不读
global `get_config()`。

来源是一个固定的 readonly SQLite + Markdown projection：SQLite `knowledge_id`、正文和
已存储 `(chunk_index, chunk_text)` 与 Markdown 正文逐一一致；孤儿、不连续/空、或
`VectorStore.MAX_CHUNK_INDEX` 之外的 chunk 都是 `repair_required`，且不会调用
Provider。重建永远使用已存 chunks，不能按当前 chunk 参数重新切分。

schema v2 把 binding 状态和 ready pointer 置于同一无密钥 extension。`ready` 必须同时有
完整 pointer/manifest；`rebuild_required`、`processing`、`retry_required`、`budget_paused` 与
`authorization_required` 不可解析为 vector binding，即使它仍保留一代历史 generation。reader
必须把它们投影为相应稳定状态，绝不能使用旧 generation、平铺目录或 `no_hits`。schema v1 的
历史 ready pointer 仍可严格读取；新 generation publish 写入 schema v2 的 `ready` binding。

`resolve_embedding_index_binding(config)` 只在 `ready` 时返回一次性的
`generation_id`、`index_dir`、pointer revision 和 contract。一个逻辑读操作必须只解析
一次 binding；后续 pointer flip 只影响后续操作。检查会用
`VectorStore.open_readonly()` 验证实际 HNSW pair、transaction marker、metadata schema 和
fingerprint，且不产生 sidecar。binding 永远指向
`vectors/generations/<generation-id>`，不存在 flat-index fallback。

当前 Application 的 readonly vector port 仍需一个后续 adapter gate：它必须在每个读操作
开始时解析此 binding 并以 `VectorStore.open_readonly(binding.index_dir, ...)` 打开，不能
重新指向 writer store 或 `<data-root>/vectors`。

## 自动化授权与 token 用量（R4 P0）

自动 AI lifecycle 的 policy 只从显式 Config 的 `ai.automation` 解析：默认关闭；启用时必须
有一次性、当前 Provider/model/token quota policy hash 确认，以及至少一个日/月 token hard cap。
policy inspect 不创建数据根、Provider、网络、日志或 snapshot。可选的 price-card reference
不是启用前提；没有 card 时只记录 token，绝不估算金额。

所有后续 usage ledger 至少区分（Provider 适用且已报告时）
`uncached_input_tokens`、`cached_input_tokens`、`generated_tokens` 与
`embedding_input_tokens`。Provider 未报告的字段保存 unknown，而不是零。任务 claim、token
reservation、usage settlement 与 binding state publish 是 P1 的 lease-protected owner 写入；本
P0 只冻结 parser/DTO/reader 投影，不启动 Provider 或自动任务。

## Confirmed rebuild

```text
inspect (zero write)
  -> plan (contains opaque revision; requires confirmation + network)
  -> confirm(approved=True, allow_network=True)
  -> acquire R3 root writer lease
  -> re-inspect source/config/user-config-source revision/R2 base
  -> private staging build + strict readonly validation
  -> reserve retained generation directory without replacing an existing one
  -> audit activation_intent
  -> runtime snapshot pointer CAS
```

Plan and confirmation booleans must be actual booleans, not truthy strings. An
external edit to the loaded user `config.yaml` (including API-key rotation) changes
the opaque `Config.user_config_source_revision()` and rejects an already-approved
plan before lease/Provider/write as `runtime_plan_stale`. A key rotation by itself
does not change the stored Embedding contract; a provider/endpoint/model/dimension/
pipeline change is visible as `embedding_rebuild_required`.

The generation is assembled under `vectors/staging/.stage-*`, its four index
artifacts plus manifest are verified, then materialized under
`vectors/generations/g-*`. The target directory is reserved with no-clobber
creation; a crash or stage failure may leave an unreferenced diagnostic stage, but
cannot alter the active pointer. The pointer is changed only after validation, and
the previous generation remains in `retained_generations`; automatic deletion and a
public rollback action are deliberately not part of this phase.

`PreChunkedEmbeddingAdapter` is the explicit bridge for the existing
`src.ai.embedder.Embedder`: it calls `embed_document(text)` and the underlying
`client.embed_batch_numpy(stored_chunks)`, never historical `embed_chunks(text)`.
The caller that creates that historical embedder is a future confirmed mutation
adapter and must bind it to the captured explicit Config; R4 itself constructs no
Provider.

## Failure and audit semantics

Before pointer CAS, invalid source, Provider protocol/stage failure, config/source/
snapshot drift, or `write_busy` leave the active pointer and existing generation
bytes unchanged. The second writer is rejected before audit or Provider work.

After lease acquisition R4 records local AuditTrace events with complete article and
stored-chunk provenance, while redacting configured keys, credential fields, inline
credentials and credential-bearing URLs. Audit context contains
`embedding_contract_sha256`, `config_source_revision` and
`data_root_identity_sha256`; no field calls a hash a “generation”.

An `activation_intent` record is written before the irreversible pointer CAS. If the
final audit completion append/fsync fails *after* CAS, execution returns success
with `audit_completion_pending: true`, not a false rollback-like error. The intent
record remains for reconciliation; a future adapter must surface this warning and
must not blindly retry the stale plan. Pre-CAS audit failures still prevent
activation.

## Deliberate adapter gate

This module is not exported as a new Kernel/CLI/MCP public action in this phase.
Before exposing rebuild to a user or external wrapper, the owner must add an
explicit inspect → plan → confirm → execute adapter, inject an approved Provider
through `PreChunkedEmbeddingAdapter`, project stable errors/warnings (including
`audit_completion_pending`), and connect every vector read to the generation
binding contract above. Tests remain fake-only with isolated `.data-test` roots;
no R4 test permits real Provider credentials, real Vault data or migration.

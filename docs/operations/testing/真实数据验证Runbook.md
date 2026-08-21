# 真实数据验证 Runbook（P2：小样本真实数据验证流程）

> 文档类型：运维 / 验证 Runbook（**规划优先，授权后执行**）
> 创建日期：2026-07-31
> 状态：**CAT-0 已验证；真实数据尚未执行，等待 U1/G8 与用户授权**（migration 另需 FT5；本文件不含任何真实数据）
> 集成线：P0 主线收口
> 适用范围：archive / search / MCP evidence & citation / delete / migration 的小样本真实数据验证

---

## 0. 阅读须知与安全红线

### 0.0 当前路径合同

本 Runbook 中的 `<effective-data-root>` 一律按下列顺序解析：

```text
PKV_DATA_ROOT（若设置）
  → %USERPROFILE%\.pkv\config.yaml 中的 storage.data_root
  → %USERPROFILE%\.pkv\data
```

- `%USERPROFILE%\.pkv\config.yaml` 是唯一可编辑的用户业务配置，可能包含 Provider Key/Cookie；Agent 不读取、打印或修改。
- `<effective-data-root>/config/local.yaml` 是 PKV 管理、无敏感字段的 runtime snapshot，不是第二份用户配置、不 merge 到 `Config`，也不手工编辑。它与外部配置/当前数据库构建契约不一致时，只提示用户评估 Embedding 重建。
- checkout 中旧 `.data/`、旧 `config/local.yaml`，以及 held release candidate 的 `%LOCALAPPDATA%\PersonalKnowledgeVault` 只保留为历史用户资产/证据。不得自动探测、接管、复制、迁移或删除；只有用户走 inspect → plan → 展示影响与备份 → 确认 → execute → smoke test 后才能处理。
- `.data-test/<scenario>/` 始终是测试隔离根，不是用户数据根；它不授予 Agent 读取真实快照或用户配置的权限。

### 0.1 本 Runbook 是什么

本 Runbook 定义一套**未来在用户明确授权后**可执行的小样本真实数据验证流程，包含三个递进阶段：

| 阶段 | 名称 | 目的 | 典型样本规模 |
| --- | --- | --- | --- |
| **P0** | 小样本预演 | 用最小授权样本验证"流程本身"可执行、模板可用、门禁有效 | 3–10 条 |
| **P1** | 受控评测 | 在授权样本上测量真实数据质量指标，产出可审计结论 | 30–100 条 |
| **P2** | 定期回归 | 用固定样本清单周期性检测质量漂移 | 固定清单（≈P1 规模） |

三个阶段共享同一套授权、脱敏、隔离、判读、门禁、审计、清除与回滚机制（见第 4–14 章）。

**默认离线 + 双通道原则**：三阶段的默认步骤全部离线（不联网），但"离线"不改变执行通道——
读取真实快照、用户配置或生产 runtime snapshot 的步骤一律 user-only（0.2-7/9、8 章）。任何会触发
真实抓取、LLM 摘要或 Embedding HTTP 的命令（archive、`search --strategy vector/hybrid/auto` 等，
见 7.3）属于 **live/数据出境阶段**：必须单独授权、单列记录，同样由用户执行，**不作为当前可执行默认步骤**。

### 0.2 绝对禁止（包括授权之后）

以下禁令**不因授权而解除**，适用于所有角色（含 AI 代理 / 自动化）：

1. **AI/自动化永不直接访问生产数据**：禁止读取、复制、导出、上传、打印或修改 `<effective-data-root>/` 及其子目录；禁止读取 `%USERPROFILE%\.pkv\config.yaml` 或打印其中任何密钥；不得把 runtime snapshot 当作可编辑配置。
2. **真实数据进入验证环境只有一条通道**：用户手动执行"授权快照"（见 4.2 与 9.1-S1），把经脱敏/假名化的样本副本放入 `.data-test/<scenario>/`。除此之外的任何"取数"路径都视为违规。
3. **未经授权单签核（第 4 章），不得对真实数据副本执行任何命令**，包括只读命令。
4. **默认离线；live/数据出境必须单独授权**。`archive`（URL 或文本）与
   `search --strategy vector/hybrid` 会触发真实抓取、LLM 摘要或 Embedding HTTP；
   `auto` 可能路由到 Embedding，故同样保守按 live 管理（见 7.3 与 15.1/15.2）。这些命令归入
   **live/数据出境阶段**：必须单独授权、单独记录，且只在 `.data-test/` 快照数据根下
   执行；默认离线阶段一律不执行。`PKV_RUN_LIVE` 只是 pytest 测试收集开关（skipif
   门禁），**不是应用层网络开关**——它既不阻止也不代表应用层网络行为，应用层联网
   由用户配置（`%USERPROFILE%\.pkv\config.yaml` 的 base_url/api_key）驱动。
5. **不自动执行生产迁移、删除、恢复**：`scripts/backup-data.ps1`、`restore-data.ps1`、生产 `migrate.py --auto` 均为用户手动命令，AI 不代为执行（详见命令类别 CAT-D，第 8 章）。
6. **Agent 可读或位于工作区内的**报告、文件日志、模板、证据包和 commit 中不得出现真实数据原文、真实 URL、个人标识符或密钥；只允许出现样本清单中的**假名/占位标识**。用户终端中的原始运行输出不得复制给 Agent；如确需保存，只能进入第 12 章定义的工作区外 E9。当前产品会记录 query/URL/title，故 U1 user-only launcher 未交付前，任何真实快照命令均不得执行。
7. **双通道执行模型**：执行分两个通道（第 2、8 章）。**Agent-safe 通道**仅限不接触授权快照、不加载用户配置或生产 runtime snapshot 的静态/合成验证，且必须经过**完整离线 launcher**；**user-only 通道**覆盖所有会读取真实快照、用户配置或生产 runtime snapshot 的实际 CLI/MCP/migrate 命令——**无论离线还是 live**，一律由用户在其受控本机终端手动执行，并在 U1 交付后通过专用 launcher 禁用/净化工作区文件日志。Agent 不执行、不读取原始输出，只接收脱敏摘要（第 12 章）。
8. **G0 是按目标子进程核验的离线入口，不是全局保护**：CAT-0 G0 已交付并逐入口核验。pytest 先经 `tests/offline_entrypoint.py pytest` 在 pytest/plugin 导入前建立 G0，root `tests/conftest.py` 再维持逐用例隔离；CLI/MCP 由 `tests/offline_entrypoint.py` 保护；FT7 generic Direct Python 只接受仓库内 `python -m <module>` 或 `python <script.py>`，在同一进程内以 `runpy` 执行，并在导入产品代码前清理 live/secret/proxy 环境、绑定 base-only Config、安装网络与子进程 fail-closed guard。FT7 明确拒绝 `-c`、stdin 与解释器 flags；非 Python `-Direct` 不属于 Python G0；该机制是进程内防护，**不是 OS sandbox**。它也不改变 user-only CLI/MCP/migrate 的执行通道。不得以“G0 通过”为由让 Agent 执行真实快照命令。
9. **绝不以"默认离线"为由让 Agent 执行真实快照命令**：离线只说明该命令不联网；是否读取快照、用户配置或生产 runtime snapshot 这几个事实单独决定执行通道（0.2-7）。`PKV_RUN_LIVE` 绝不是网络开关（7.3）。

> 违反 0.2 的任何一条，立即暂停流程并按第 11 章失败分流处理。

### 0.3 术语表

| 术语 | 定义 |
| --- | --- |
| 真实数据（real data） | 用户生产 `<effective-data-root>/` 中的内容，或与之一致、未经脱敏的内容 |
| 授权快照（authorized snapshot） | 用户手动创建的真实数据副本（经选择与脱敏），存放在 `.data-test/<scenario>/`，是本流程唯一允许的真实数据载体 |
| 样本清单（sample manifest） | 版本化的选择说明：包含哪些条目、为何有代表性、脱敏规则、规模上限（模板见 T-B） |
| 合成 fixture | `tests/fixtures/`、`evals/mcp_quality/` 中的确定性离线数据；**只能证明契约正确，不能证明真实数据质量**（第 16 章） |
| dev vault | #3 任务交付的轻量开发 vault（合成但结构代表性）；P0 排练基底的两个 OR 选项之一（#3 已就绪，或合成样本预演降级，见 18.2） |
| live/数据出境阶段 | 任何会触发真实网络抓取或 LLM/Embedding HTTP 的验证步骤（archive、vector/hybrid search 等）；默认不执行，需单独授权（7.3） |
| disposable clone | 每场景从授权快照制作的独立副本（`.data-test/<scenario>/clones/<clone-id>/`）；所有破坏性演练（删除、可写迁移）只在其上进行，原快照根保持只读（7.1） |
| 离线种子（offline seed） | `scripts/setup-test-db.py` 等纯本地脚本；必须显式固定 seed/count/output 才具备确定性与场景隔离，用于 archive 的离线替代验证（15.1） |
| 判读（interpretation） | 人工对照预期结果与实际结果，给出通过/不通过/需复查结论的动作（第 10.2 章） |
| Agent-safe 通道 | 不接触授权快照、不加载用户配置或生产 runtime snapshot 的静态/合成验证（离线 pytest / 16-task 闭环、合成种子脚本、已交付的 base-only 工具）；Agent 可执行（2/8 章） |
| user-only 通道 | 所有会读取真实快照、用户配置或生产 runtime snapshot 的实际命令（离线或 live）；只能由用户在其受控本机终端手动执行，Agent 不执行也不读取原始输出（2/8 章） |
| 完整离线入口 | CAT-0 已交付的按入口 fail-closed 机制：pytest=offline pytest bootstrap + root conftest；CLI/MCP=`offline_entrypoint.py`；Direct Python=FT7 generic guard（仅仓库 `python -m` / `.py`，同进程 `runpy`）。拒绝 `-c`/stdin/flags；非 Python Direct 不在 Python G0 内；不是 OS sandbox（7.1/10.3） |
| user-only launcher（U1） | 第 19 章待交付的真实数据用户入口：验证授权/路径/只读快照与 writable clone，禁用或净化工作区文件日志；对应 G8，不进入 CI |
| 模板指纹（template fingerprint） | 记录所用模板的 ID 与版本（如 T-D v1.3），用于审计追溯；不含任何命令实参（12 章） |
| 历史 schema baseline | 用户准备的旧版本数据库/Schema 夹具（仅记录版本号与哈希），用于 migration 兼容性验证（15.5） |

---

## 1. 目标与非目标

### 1.1 目标

1. 在**授权 + 快照 + 双通道**三条安全前提下，用最小样本验证系统对真实数据的 archive、search、MCP evidence/citation、delete、migration 表现。
2. 产出一致、可审计、可复现的验证记录（授权单、样本清单、判读表、门禁结果、审计清单、清除凭证）。
3. 明确"合成 fixture 证明什么、真实数据证明什么、两者都不证明什么"（第 16 章），防止指标被过度解读为发布结论。
4. 定义自动门禁与人工判读的分工，让每次回归在 30–60 分钟内可重复执行。

### 1.2 非目标（明确不做）

- ❌ 全量真实数据评测（样本始终是小样本 + 代表性声明，见第 5 章）。
- ❌ 替代离线 CI / MCP 16-task 评测闭环（`tests/` + `evals/mcp_quality/` 仍是质量门禁的**默认来源**，真实数据验证只是补充证据，见第 16 章）。
- ❌ 自动执行生产变更（迁移、删除、恢复都由用户手动完成，且必须先备份）。
- ❌ 把真实数据样本提交进 Git，或把样本痕迹写入任何版本化文件。
- ❌ 在本 Runbook 内实现访问真实数据的脚本/工具（相关扩展列为第 19 章后续任务）。
- ❌ 默认阶段执行任何会触发真实抓取/LLM/Embedding HTTP 的命令。archive、`search --strategy vector/hybrid/auto` 归入 **live/数据出境阶段**（7.3），且 `PKV_RUN_LIVE` 不是应用层网络开关，不能作为"离线默认失效"的依据。

---

## 2. 角色与责任

| 角色 | 谁担任 | 责任 | 可自动化 |
| --- | --- | --- | --- |
| **数据所有者** | 用户 | 拥有 `<effective-data-root>/`；决定哪些数据可进入样本；确认分级与脱敏规则 | 否 |
| **授权人** | 用户（可与数据所有者同一人） | 签署/撤销授权单；批准命令类别与留存期限 | 否 |
| **用户执行者（user-only）** | 用户本人 | 手动执行所有读取真实快照、用户配置或生产 runtime snapshot 的命令（离线与 live 均如此）；不向 Agent 交付原始输出，只交付脱敏摘要（第 12 章） | 否 |
| **Agent 执行者（Agent-safe）** | AI 代理 / 自动化（CI、pytest） | 仅执行不接触授权快照、不加载用户配置或生产 runtime snapshot 的静态/合成验证与已交付的 base-only 工具（CAT-0）；整理用户交付的脱敏摘要到记录与判读辅助 | 是（受 CAT-0 约束） |
| **判读人** | 用户（或用户指定的人工） | 对真实数据相关结果给出人工结论（第 10.2 章判读表） | 否 |
| **审核人**（可选） | 用户或第二位人工 | 复核授权单与最终报告的一致性 | 否 |

责任铁律：

- Agent 不得执行或读取任何会读取授权快照、用户配置或生产 runtime snapshot 的命令——即使该命令离线、即使"默认离线"（0.2-7/8/9）。
- 用户不向 Agent 交付原始 stdout/stderr、实际 URL、真实命令实参、正文、姓名、绝对路径或密钥；只交付脱敏摘要、退出码、计数、哈希、假名 ID、时间、命令类别与模板指纹（第 12 章）。
- G0 只证明与目标命令匹配的 base-only 受控入口（10.3-G0）；未接入入口的合成 CAT-0 命令保持阻塞，真实快照命令始终 user-only，不得把预检当作全局保护（0.2-8）。
- 判读结论只能由人工给出；自动门禁（第 10.3 章）只负责"客观可机器校验"的部分，不能替代判读。

---

## 3. 数据分级与最小化

### 3.1 数据分级

| 分级 | 定义 | 允许进入样本？ | 前置条件 |
| --- | --- | --- | --- |
| **D1 公开** | 公开网页/文章等可公开访问内容 | ✅ 默认允许 | 无 |
| **D2 内部** | 非公开但低敏感（个人笔记、工作记录） | ✅ 允许 | 授权单 + 假名化（第 6 章） |
| **D3 个人** | 含可识别个人信息的条目（姓名、联系方式、私人对话） | ⚠️ 有条件 | 授权单 + 字段级脱敏 + 判读人确认，且样本规模≤P1 上限 |
| **D4 敏感** | 凭据、密钥、财务/健康信息、身份文件 | ❌ **默认禁止** | 永不进入样本；一旦发现，立即停止并清除（见 11.3） |

样本构成原则：**优先 D1，少量 D2，D3 需逐条列明于样本清单，D4 零容忍**。

### 3.2 字段级最小化

快照副本只保留验证所需字段；其余一律不复制：

| 字段 | 用途 | 快照中保留？ | 脱敏要求 |
| --- | --- | --- | --- |
| 正文 / title | archive/search/evidence 判读 | ✅ | 按 6.1 规则 |
| `source_url` | 归档与 citation 判读 | ✅（已按产品契约脱敏盘符/UNC/`file:`） | 若 URL 含个人信息则假名化 |
| `created_at` / `published_at` 等时间字段 | timeline/citation 判读 | ✅ | 无 |
| tags | 检索/对比判读 | ✅ | 无 |
| `api_key`、token、口令 | 任何用途 | ❌ 不复制 | — |
| 附件、媒体文件 | 本 Runbook 范围外 | ❌ 不复制（仅记录存在性，如需要另行授权） | — |
| 本地 `file_path` | 无需 | ❌ 不复制 | — |

### 3.3 样本规模上限

| 阶段 | 条目上限 | 附加约束 |
| --- | --- | --- |
| P0 | 10 | 单次会话内完成 |
| P1 | 100 | 分层抽样（5.1），快照体积建议 ≤ 50 MB |
| P2 | 100 | 固定清单，允许在清单内轮换 10% |

任何"为了测更多而扩样本"的诉求都必须走新的授权单，不得在原授权下扩量。

---

## 4. 授权模型

### 4.1 授权单（Authorization Record）

每个阶段执行前，授权人签署一张授权单（空白模板 T-A）。**没有签核的授权单，任何命令不得执行。** 授权单字段：

| 字段 | 说明 |
| --- | --- |
| 授权单 ID | `AUTH-<阶段>-<yyyyMMdd>-<序号>` |
| 授权人 / 签发时间 | 用户本人；时间精确到分钟 |
| 数据所有者确认 | 确认分级（第 3 章）与样本来源合法 |
| 授权范围 | 引用的样本清单 ID（T-B）、阶段、快照路径 `.data-test/<scenario>/` |
| 允许的命令类别 | 从 CAT-0/CAT-U/CAT-C/CAT-D（第 8 章）勾选；CAT-D 永不授权 |
| 允许的 live/数据出境命令 | 默认"无"；archive / vector / hybrid 等命令须在此单独勾选并给出范围与预算上限（7.3）。`PKV_RUN_LIVE` 仅是测试收集开关，不作授权依据 |
| 留存期限与清除方式 | 快照/报告的保留天数与清除命令（第 13 章） |
| 撤销条件 | 用户可随时撤销；发现 D4 或任何泄露立即自动失效 |

### 4.2 授权快照（唯一的取数通道）

1. **选择**：用户按样本清单选择条目。
2. **脱敏/假名化**：用户（或用户批准的脚本）对副本执行第 6 章规则。
3. **落盘**：用户把脱敏后的副本放入 `.data-test/<scenario>/`（数据库与 Vault 结构对齐生产布局：`db/knowledge_vault.db`、`vault/`、`vectors/`）。
4. **声明与核验**：用户核验快照路径位于 `.data-test/` 且无生产路径引用；U1 交付后由其再次验证授权、快照只读与 clone 边界（见 10.3-G4/G8），然后才开始执行命令。FT7/G0 只保护合成 CAT-0，不用于核验真实快照。

用户（或用户批准的 base-only 工具）**永远不做**从生产取数的第 1–3 步（选择/脱敏/落盘取自生产数据）。这保证真实数据到测试环境之间始终隔着"人工 + 授权"。

### 4.3 授权层级与撤销

- 授权逐级升级：**P1 授权要求 P0 已通过**；**P2 授权要求 P1 完成定义（DoD）达成**（第 17 章）。
- 授权单在以下情况自动失效：到达留存期限、用户口头/书面撤销、发现 D4 数据、任何泄露事件、对应样本被清除。

---

## 5. 样本选择与代表性

### 5.1 选择原则（分层抽样）

样本清单须说明每个维度如何覆盖，避免"只有最近归档、只有单一来源"的偏置：

| 维度 | 建议覆盖 | P0 示例（占位） |
| --- | --- | --- |
| 内容类型 | wechat / zhihu / ai_chat / generic / text_fallback | 每类 1–2 条 |
| 来源域 | 至少 2 个不同域 + 至少 1 条无 URL 纯文本 | 占位域 A、B + 文本 1 条 |
| 时间跨度 | 至少 2 个不同归档时间段（含最早与最近） | 占位时间段 |
| 体量分布 | 短文 / 长文 / 含代码块 / 含表格 | 各 1 条 |
| 语言 | 中文为主 + 至少 1 条英文（覆盖 BM25 英文回退） | 1 条 |
| 负面场景 | 空正文、特殊字符标题、超长 title、缺失时间字段 | 每类 1 条 |

### 5.2 样本规模

- P0：3–10 条（**最小可运行集合**，目标 30 分钟内跑通全部步骤）。
- P1：30–100 条（各分层至少 3 条；负面场景各 1–2 条）。
- P2：固定清单 ≈ P1 规模，允许按 10% 轮换以覆盖新增来源。

### 5.3 样本清单 manifest（模板 T-B）

每个快照必须伴随一份 YAML manifest，包含：

```yaml
sample_manifest: pkv.real_sample_manifest.v1
sample_id: SMPL-<阶段>-<yyyyMMdd>-<序号>
auth_id: AUTH-<阶段>-<yyyyMMdd>-<序号>
data_class: [D1, D2]        # 实际分级；禁止 D4
pseudonym_map_ref: <假名映射文件，不入 Git，仅存 .data-test>
strata:
  content_type: {wechat: 2, zhihu: 2, generic: 2, text_fallback: 1}
  source_domains: 2
  time_ranges: 2
  sizes: [short, long, code, table]
  languages: [zh, en]
negative_cases: [empty_body, long_title, missing_time_field]
size_limit: {entries: 10, bytes_mb: 20}
```
<!-- 以上 YAML 全部为占位值，不含真实数据。 -->

> manifest 的 `items` 部分（见模板 T-B）只允许**假名/占位 ID**，禁止填写原始 URL、正文或姓名。

### 5.4 代表性声明（结论的边界）

样本清单必须写入**代表性声明**，固定措辞模板：

> 本样本覆盖 {分层维度}，共 {N} 条，按 {选择规则} 抽取。它**只代表所覆盖的分层**；未覆盖的来源/类型/时间段的结论不得外推。本样本的指标只作为真实数据质量观测值，不作为产品发布门槛（第 16 章）。

---

## 6. 脱敏与假名化

### 6.1 脱敏规则（快照创建时应用）

| 规则 | 内容 | 示例（占位） |
| --- | --- | --- |
| R1 密钥/令牌 | 所有 key/token/口令字段不复制 | `<REDACTED>` |
| R2 个人标识 | 姓名、电话、邮箱、住址等替换为假名 | `PERSON-01` |
| R3 敏感 URL | URL 中的查询参数（若含个人信息）清除或假名化 | `https://example.test/a?id=<PID-1>` |
| R4 本地路径 | 所有 `file_path` / 盘符 / UNC 不复制或置空 | 不复制 |
| R5 附件 | 媒体/附件不复制，仅保留存在标记 | `attachment: present` |

### 6.2 假名化（Pseudonymization）

- 对 D2/D3 内容使用**稳定假名映射**：`PERSON-01 ↔ 原文标识`，映射表只保存在 `.data-test/<scenario>/`（Git 忽略区），不进任何报告。
- 判读需要回查原文时，由用户凭映射表完成；报告与证据中只出现假名。

### 6.3 脱敏验证门禁

P0/P1 报告产出前必须运行脱敏检查（后续任务 FT1 提供工具，第 19 章）：

- [ ] 报告/日志中不出现快照内已知个人标识原文（用假名映射反向检查）。
- [ ] 不出现 `api_key`/`token`/`password` 等字段值。
- [ ] 不出现 `file://`、盘符绝对路径、UNC 路径（产品层已脱敏，报告层再核一遍）。
- [ ] 不出现 `%USERPROFILE%\.pkv\config.yaml` 的任何内容，也不出现 production runtime snapshot 的内容。
- [ ] 证据只保留**脱敏摘要、退出码、计数、哈希、假名 ID、时间、命令类别与模板指纹**；不保存原始 stdout/stderr、URL、命令实参、正文、姓名（第 12 章）。

### 6.4 例外处理

- 快照中一旦发现 D4 内容：立即停止，按 11.3 清除并报告。
- 判读人若发现任何一条脱敏不彻底，该条对应的判读结论标记为"需复查"并修正脱敏后重跑。

---

## 7. 环境与凭据隔离

### 7.1 隔离强制规则（双通道）

1. **执行通道是硬边界**：CAT-0 的 Python G0 已交付并逐入口核验：pytest 先经 `tests/offline_entrypoint.py pytest` bootstrap，再由 root `tests/conftest.py` 维持逐用例隔离，CLI/MCP 由 `offline_entrypoint.py` 保护，FT7 generic Direct Python 经 wrapper 路由到同一入口。Direct Python 只接受仓库模块/脚本并用同进程 `runpy` 执行；`-c`、stdin、解释器 flags 与仓库外目标均拒绝。非 Python `-Direct` 不属于 Python G0。`scripts/migrate.py` 和所有真实快照命令仍一律 user-only，并在 U1/G8（迁移另加 FT5）交付前保持阻塞。
2. **`-DataRoot` 只能指向仓库内 `.data-test/` 下的路径**（run-test.ps1 自带校验：拒绝 junction/symlink/硬链接、拒绝生产 `<effective-data-root>` 路径，并遮蔽敏感参数）。该校验只约束命令指向，**不使命令变为 Agent 可执行**。
3. 快照与 clone 布局：

```text
DATA_DIR   -> .data-test/<scenario>                      # 快照根（只读）
DB_PATH    -> .data-test/<scenario>/db/knowledge_vault.db
VAULT_DIR  -> .data-test/<scenario>/vault
VECTOR_DIR -> .data-test/<scenario>/vectors
LOG_DIR    -> .data-test/<scenario>/logs                   # 仅合成/CAT-0；真实快照禁止写 raw log
TMP_DIR    -> .data-test/<scenario>/tmp
CLONE_ID   -> .data-test/<scenario>/clones/<clone-id>    # 破坏性演练唯一允许的数据根
```

4. 每个阶段使用**独立场景名**（`real-sample-p0` / `real-eval-p1` / `real-regression-p2`），互不复用数据根；破坏性演练的 `clone-id` 由用户生成（如 `c-<yyyyMMdd>-<序号>`）并在 T-G/T-H 记录。
5. **G0 按目标命令核验**：wrapper、root `tests/conftest.py`、`offline_runtime.py` 与 `offline_entrypoint.py` 已成套集成；pytest、CLI/MCP、16-task、合成 seed 与 dev-vault 重建/`--check-only` 已按各自入口核验。generic Direct Python 在导入产品代码前清理 live/secret/proxy、绑定 base-only Config 并安装网络/子进程 guard；它是 Python 进程内防护而非 OS sandbox。路径预检单独通过不构成 G0，非 Python Direct 也不自动获得 Python G0。预检不得用 `config show`（它可能读取用户配置或生产 runtime snapshot，7.2）。
6. **破坏性演练只能在 disposable clone 内执行**：删除/可写迁移前，用户从授权快照制作独立副本 `.data-test/<scenario>/clones/<clone-id>/`（见 15.5 制备步骤）；演练失败直接丢弃 clone 重建，快照根保持只读，不做"事后回滚"（第 14 章）。
7. **clone 制备与验证（仅用户，可审计步骤）**：① 用户复制快照根到 `.data-test/<scenario>/clones/<clone-id>/`（db/vault/vectors 齐全）；② 用户以只读命令验证 clone 可打开且条目数与快照一致，并记录条目数哈希；③ 用户记录 `clone-id`、制备时间、源快照与目录哈希（T-G/T-H）；④ 制备后快照根恢复只读，clone 成为破坏性演练的唯一操作对象。

### 7.2 凭据与 runtime snapshot 隔离

- `%USERPROFILE%\.pkv\config.yaml` 只由用户维护；**任何角色**（含 Agent）不得读取、打印、复制或上传。
- `<effective-data-root>/config/local.yaml` 是 secret-free、PKV 管理的 runtime snapshot；不作为用户配置或凭据来源，不得手工编辑。它与外部配置/数据库构建契约不一致时，只能提示用户评估重建。
- AI/自动化**不得执行任何会读取用户配置、生产 runtime snapshot 或真实快照的命令**（`config show` / `config get` / 任何未接入 base-only 入口的 CLI/MCP/migrate 命令）；预检只走 base-only 受控入口（7.1-5）。
- `run-test.ps1` 只遮蔽命令行中的凭据，不能遮蔽应用日志里的 query/URL/title。真实快照命令必须等待 U1：禁用工作区文件日志或在写入前源头脱敏；事后 G5 扫描不能替代该前置。
- 报告中禁止出现任何密钥或密钥前缀；必要时写 `<REDACTED>`。

### 7.3 网络 / 真实 API 隔离

| 模式 | 默认 | 说明 |
| --- | --- | --- |
| 纯离线 | ✅ 默认 | pytest、CLI/MCP 与受支持的 Direct Python 已分别接入 G0；16-task、合成 seed、dev-vault 重建/`--check-only` 已逐入口核验。`stats/list/show/bm25` 与 migration 虽不联网，仍因接触真实快照而等待 U1/G8、仅用户执行；migration 另需 FT5 |
| live/数据出境 | 单独授权 | `archive`（URL 或文本，触发抓取 + DeepSeek 摘要）、`search --strategy vector/hybrid` 与 MCP 非 bm25 会联网；`auto` **可能**路由至 Embedding，因此保守按 live 管理。必须由授权单单列范围/预算，并等待 U1 |

补充说明（勿混淆开关语义）：

- `PKV_RUN_LIVE` 只是 **pytest 收集开关**（`skipif os.getenv("PKV_RUN_LIVE") != "1"`），只作用于带 `network` marker 的测试用例；它**绝不是应用层网络开关**，不阻止也不代表 CLI/MCP 发起的 Provider 出站连接。这里不是指 MCP HTTP transport；M13 MCP 只发布 stdio。
- 应用层联网由 `%USERPROFILE%\.pkv\config.yaml` 的 `base_url` / `api_key` 驱动；判断"某步是否联网"应看命令本身（archive / vector / hybrid 必然联网），而不是看环境变量。
- **"纯离线"只说明不联网，不改变执行通道**：读取真实快照、用户配置或生产 runtime snapshot 的命令，无论离线还是 live，一律 user-only（0.2-7/9）。live/数据出境阶段不是默认步骤。

---

## 8. 命令类别（Command Categories）

### 8.1 类别定义（双通道）

| 类别 | 内容 | 数据接触 | 执行通道 |
| --- | --- | --- | --- |
| **CAT-0 Agent-safe 静态/合成** | 静态文档/交叉引用检查；经各自 G0 入口运行的 pytest、CLI/MCP；经 FT7 generic Direct guard 运行的 16-task、固定参数合成 seed、dev-vault 重建与 `--check-only`；已交付的 base-only 工具 | 无真实快照 | Agent + 用户 |
| **CAT-U 用户手动·快照命令** | 会读取真实快照、用户配置或生产 runtime snapshot 的实际命令，离线与 live 均含：`stats` / `list` / `show` / BM25、MCP 只读 Tool、vector/hybrid/auto、archive；**U1 user-only launcher 未交付前全部阻塞** | 快照只读；archive 仅 writable clone | **仅用户**（Agent 只接收脱敏摘要，12 章） |
| **CAT-C 迁移演练** | `scripts/migrate.py --version` / `--health-check` / `--dry-run`；可写演练 `--auto --no-backup` | 仅 `.data-test/<scenario>/clones/<clone-id>` 内 | **仅用户**（需 FT5 + U1 + 授权；G0 不适用，15.5） |
| **CAT-D 生产触碰** | `backup-data.ps1`、`restore-data.ps1`、生产库 `migrate.py --auto`、生产 `<effective-data-root>/` 任何操作 | 生产 | **仅用户** |

说明：

- 所有真实快照命令（含"离线"的 stats/list/show/bm25）都可能读取用户配置或生产 runtime snapshot，因此归 CAT-U；**不存在 Agent 可安全执行的"离线真实快照命令"**（0.2-7/9）。
- 未来工具（FT3/FT5/FT6）若以 base-only 方式交付（不加载用户配置或生产 runtime snapshot、不联网、输出仅脱敏摘要），可归 CAT-0；未交付或未证明 base-only 前一律按 user-only 处理（第 19 章）。

### 8.2 阶段 × 命令类别矩阵

| 阶段 | CAT-0 | CAT-U | CAT-C | CAT-D |
| --- | --- | --- | --- | --- |
| P0 | ✅ | ✅ 仅用户（stats/list/show/bm25） | ✅ 仅用户（clone 内，前置齐备时） | ❌ 仅用户 |
| P1 | ✅ | ✅ 仅用户（含单独授权的 live 步骤） | ✅ 仅用户（clone 内） | ❌ 仅用户 |
| P2 | ✅ | ✅ 仅用户（含单独授权的 live 步骤） | ✅ 仅用户（clone 内；每季度至少一次迁移 dry-run） | ❌ 仅用户 |

### 8.3 命令书写约定

**Agent-safe（CAT-0）示例**（CAT-0 G0 已交付；仍须通过 wrapper 使用对应入口）：

```powershell
# 离线 16-task 闭环：wrapper 将仓库模块路由到 FT7 generic Direct guard
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-quality -Command @(
  "python", "-m", "evals.mcp_quality", "--enforce-thresholds",
  "--output", ".data-test/mcp-quality/result.json"
)

# 离线 pytest 套件：offline bootstrap 在 pytest/plugin 导入前 fail-closed，root conftest 维持逐用例隔离
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\contract -Command @("python", "-m", "pytest", "-q", "-m", "not network and not manual")
```

**用户手动（CAT-U / CAT-C）目标命令模板——Agent 不执行、不读取原始输出**：

```powershell
# 以下只定义 U1 launcher 必须封装的内层意图，不是当前可复制执行命令：
snapshot-readonly: stats | list --limit 20 | search "<占位查询>" --strategy bm25
writable-clone:    archive "<占位 URL>"
writable-clone:    python scripts/migrate.py --health-check | --dry-run
```

> CAT-U/CAT-C 只有在 U1 交付后才可由用户执行。U1 必须验证授权单与 `.data-test` 路径、区分快照只读与 writable clone、禁用/源头净化工作区文件日志，并只输出可交付的脱敏摘要。archive/delete/可写 migration 永远不得指向快照根。

---

## 9. 阶段流程

### 9.0 公共前置（三阶段共用）

- [x] **G0 硬前置（CAT-0 已交付）**：pytest、CLI/MCP 与受支持的 Direct Python 均已有匹配入口，并已逐入口核验（见 10.3-G0 与 18.1）。Direct Python 仅仓库 `python -m` / `.py`，拒绝 `-c`/stdin/flags；非 Python Direct 不计入 Python G0。
- [ ] **G8 条件前置**：只要阶段包含 CAT-U/CAT-C，就必须先交付 U1 user-only launcher；未交付时只能运行 CAT-0 合成部分，真实快照步骤统一记为“剔除待前置”，不得给出可执行命令。
- [ ] G1/G2 离线门禁绿（第 10.3 章：pytest 与 MCP 16-task 使用各自已交付入口；均为合成，CAT-0）。
- [ ] 授权单已签核（4.1），样本清单已定稿（5.3）。
- [ ] P0 排练基底就绪：**（#3 dev vault 已就绪）或（合成样本预演降级）**，二选一，并明确写入执行记录（18.2）。
- [ ] 隔离核验通过（G4）：用户先核对授权单、场景/clone 路径与快照只读状态；CAT-U/C 还必须由 U1/G8 再次验证。FT7/G0 仅负责合成 CAT-0 入口。
- [ ] 备份策略确认：任何会修改快照的命令前，用户已确认生产 `<effective-data-root>/` 备份状态（`backup-data.ps1` 由用户决定执行）。
- [ ] 破坏性演练的 disposable clone 目录约定已确认（7.1-6/7.1-7：`clones/<clone-id>` 制备与验证流程）。

### 9.1 P0 小样本预演

**进入条件**：G1/G2 均在各自匹配的 G0 入口下为绿；P0 授权单 + P0 样本清单（3–10 条）已签核；场景名 `real-sample-p0`。

> **P0 的两种模式（OR）**：① **dev vault 排练**（#3 已就绪，纯合成，无需真实数据）：S1 改为准备 dev vault 快照（合成），其余步骤不变；② **最小合成样本预演降级**（仅在 dev vault 暂时不可用时）：S1 以最小合成样本跑通流程。两种模式共用同一套步骤与模板，差别只在 S1 的数据来源（18.2）。

**步骤**（每步都要有产出物；执行通道见第 2/8 章）：

| # | 步骤 | 执行人/通道 | 产出物 |
| --- | --- | --- | --- |
| S0 | 记录 P0 授权单与样本清单 ID | 用户 | 授权单/清单归档 |
| S1 | 创建授权快照（选择→脱敏→落盘 `.data-test/real-sample-p0/`）或经 FT7 wrapper Direct Python 准备 dev vault（合成） | 真实快照仅用户；dev vault 为 Agent-safe CAT-0 | 快照 + 假名映射（真实）或 dev-vault 健康摘要（合成） |
| S2 | 核验隔离（G4）与脱敏（6.3）；CAT-0 launcher 另由 G0 验证 | 仅用户（路径预检工具可辅助） | 核验记录 |
| S3 | U1 交付后，用户执行 CAT-U 只读·离线冒烟：`stats` / `list --limit 20` / 每个分层的 `show` 1 条；只向 Agent 交付脱敏摘要 | 仅用户 | 脱敏摘要 + 退出码/计数/哈希（E3）；U1 未交付则剔除 |
| S4 | U1 交付后，用户执行 CAT-U 检索冒烟：每语言 ≥1 个查询 × `search --strategy bm25` | 仅用户 | 脱敏摘要（E3）；U1 未交付则剔除 |
| S5 | 合成基底替代验证（CAT-0）：经已交付 FT7/G0，在 `.data-test/real-sample-p0/seed/` 通过第 15.1 章的固定 seed/count/output 命令生成合成条目并核对读路径一致性；`--output` 必须精确位于本次所选 `DATA_DIR` | Agent（CAT-0） | 种子输出 + 一致性记录 |
| S6 | FT5（迁移 DataRoot/断言工具）与 U1 前置齐备且有历史 baseline/pending 时，用户在 clone 内执行迁移冒烟；否则记“不适用/未覆盖 + 原因” | 仅用户 | 脱敏摘要 + 退出码，或未覆盖声明（E3） |
| S7 | 用户判读（10.2 判读表，P0 版） | 用户 | 判读表 |
| S8 | 记录门禁结果与失败（10.3 / 11 章）：G1/G2 由 Agent 直接记录，G3/G4 依据用户脱敏摘要 | Agent + 用户 | 门禁记录 |
| S9 | 决定留存/清除（13 章） | 用户 | 清除凭证 |

**P0 目标**：验证流程与模板本身可执行；发现并修正流程缺陷；不产出质量结论。

**退出条件**：S1–S9 均有状态（完成或“不适用/剔除待前置 + 原因”）；发现的流程缺陷已记录并修正；无未决 D4/泄露事件。

**P0 完成定义（DoD）**：授权单、清单、判读表、门禁记录、清除凭证五类证据齐备；流程缺陷清单为空或全部关闭。

### 9.2 P1 受控评测

**进入条件**：P0 DoD 达成；P1 授权单 + P1 样本清单（30–100 条）已签核；场景名 `real-eval-p1`。

**步骤**：

| # | 步骤 | 执行人/通道 | 产出物 |
| --- | --- | --- | --- |
| S0 | 记录 P1 授权单与样本清单 | 用户 | 归档 |
| S1 | 创建 P1 授权快照（分层抽样，脱敏/假名化） | **仅用户** | 快照 + manifest |
| S2 | 隔离 + 脱敏核验（G4）；CAT-0 launcher 另由 G0 验证 | 仅用户（路径预检工具可辅助） | 核验记录 |
| S3 | U1 交付后，用户执行 CAT-U 全量只读：逐条 `show` + BM25 矩阵；只向 Agent 交付脱敏摘要 | 仅用户 | 脱敏摘要集（E3）；U1 未交付则剔除 |
| S4 | U1 + live 单独授权后：archive 仅在 writable clone 演练；vector/hybrid 可读快照但经 U1 禁用/净化文件日志 | 仅用户 | 脱敏摘要（E3）；前置缺失则剔除 |
| S5 | delete 演练（**仅当第 19 章 FT3 工具已交付**；`clones/<clone-id>` 内，15.4） | 仅用户（FT3 若 base-only 则 CAT-0） | 脱敏摘要 + 四层一致性断言 |
| S6 | CAT-C：`--health-check` + `--dry-run`；可写迁移演练（**仅 `clones/<clone-id>` 内**，前置齐备，15.5） | 仅用户 | 脱敏摘要 + 退出码（E3） |
| S7 | MCP 真实样本证据验证（**仅当第 19 章 FT6 harness 已交付**；15.3） | 仅用户（harness 若 base-only 则 CAT-0） | 脱敏摘要（E3） |
| S8 | 人工判读（10.2 判读表） | 用户 | 判读表 |
| S9 | 自动门禁复跑（10.3 已实现项：CAT-0 直接跑，其余依据用户脱敏摘要） | Agent + 用户 | 门禁记录 |
| S10 | 失败分流（11 章）与修复/重新采样决策 | 全体 | 分流记录 |
| S11 | 留存/清除（13 章） | 用户 | 清除凭证 |

**P1 默认指标**（口径见 10.1）：search 相关性@k（BM25）、migration 兼容性、只读一致性
（stats/list/show 与 manifest 一致）。**待前置交付后纳入**：archive 成功率（live，需授权）、
向量相关性@k（live，需授权）、MCP evidence 可解析率 / citation 可引用率（FT6 harness）、
delete 完整性（FT3 工具）。未纳入指标必须在报告中以"剔除待前置"栏声明，**不得**写成 0 分或
"未测"冒充结论。

**退出条件**：S0–S11 完成（live/工具依赖步骤按剔除或授权状态处理）；默认指标已记录；DoD 达成或形成差距报告（差距报告需列出每项差距的后续任务与负责人）。

**P1 DoD**：① 可执行的默认指标有数值记录且口径可复现；受 FT5/U1、baseline 或 pending 限制的指标必须写"剔除待前置/未覆盖 + 原因"，不得伪造 0 分；② 判读表每个适用行有结论，不适用行有原因；③ 门禁结果与指标一致；④ 差距报告或通过结论由判读人确认；⑤ 证据包完整；⑥ 快照有清除/留存凭证；⑦ migration 按 15.5 记录 baseline/pending，pending=0 时标记"未覆盖"。

### 9.3 P2 定期回归

**进入条件**：P1 DoD 达成；P2 授权单 + 固定样本清单（可 10% 轮换）已签核；场景名 `real-regression-p2`。

**触发**：每月一次；或在以下事件后触发——离线门禁 G1 变红、发布前、迁移链变更、检索/证据代码变更。

**步骤**（压缩版 P1，目标 30–60 分钟）：

1. 复用固定样本清单 → 用户重建授权快照（同一 manifest，条目可轮换 10%）。
2. 复跑 S3–S11（P1 同款步骤；live/工具依赖步骤同样按剔除或授权状态处理）。
3. 对比上一轮默认指标：任一指标相对下降超过阈值（见 10.3 表内相对阈值）→ 触发失败分流（11 章）。
4. 输出"趋势表 + 判读表 + 门禁记录"。

**退出条件**：回归报告产出；趋势表与判读表归档；若指标漂移，分流记录已生成且已指派后续任务。

**P2 DoD**：固定清单版本号不变（或已走清单更新流程）；趋势表含 ≥2 个数据点；无未关闭的漂移任务。

---

## 10. 指标定义、人工判读与自动门禁

### 10.1 指标口径（在真实样本上定义）

状态图例：**✅ 默认** = 默认记录（命令均由用户执行，Agent 处理脱敏摘要）；**⚠️ live** = live/数据出境阶段（单独授权，用户执行）；**⛔ 待前置** = 依赖未交付工具（FT3/FT6）或未覆盖，纳入前须先交付。

| 指标 | 状态 | 口径 | 计算公式 | 判定方 |
| --- | --- | --- | --- | --- |
| search 相关性@k（BM25） | ✅ 默认 | 检索结果 top-k 中判读人认为相关的比例（命令 user-only） | 相关数 / (查询数 × k) | 人工（用户判读） |
| migration 兼容性 | ✅ 默认* | 见 15.5：历史 schema baseline（用户准备，仅记版本号/哈希）+ clone 制备与验证 + **至少一个 pending migration**；pending=0 时标记"未覆盖"，不宣称覆盖迁移路径 | 成功迁移数 / 迁移尝试数（或"未覆盖"） | 用户执行 + 自动断言（FT5） |
| 只读一致性 | ✅ 默认 | stats/list/show 结果与 manifest 申报一致（命令 user-only；Agent 只核对用户脱敏摘要） | 不一致项数 = 0 | 用户执行 + Agent 核对摘要 |
| archive 成功率 | ⚠️ live | 授权样本中归档命令成功完成（三重存储一致：Markdown + SQLite + 向量索引按需） | 成功数 / 尝试数 | 用户执行 + 人工抽查 |
| 向量相关性@k | ⚠️ live | vector/hybrid 检索 top-k 相关性（触发 Embedding HTTP） | 相关数 / (查询数 × k) | 人工（用户判读） |
| MCP evidence 可解析率 | ⛔ 待前置 | `collect_evidence` 返回的 `citation_locator` 可经 MCP Resource 逐条读取成功的比例 | 可读 locator / 全部 locator | 自动（FT6 harness，base-only 时 CAT-0） |
| citation 可引用率 | ⛔ 待前置 | 证据 locator 指向真实持久行/字段（entry/chunk/metadata/relation）的比例 | 持久可读 / 全部 | 自动 + 人工抽查 |
| delete 完整性 | ⛔ 待前置 | 删除后 SQLite/Markdown/向量/关系四处一致（无孤儿）；仅在 `clones/<clone-id>` 内执行 | 一致删除数 / 删除数 | 自动 + 人工抽查（FT3） |

- migration 兼容性为默认指标但受 15.5 前置约束：无 pending migration、无历史 baseline 或无 clone 制备验证时，标记"未覆盖"并从默认指标中剔除（9.2 DoD ⑦）。

### 10.2 人工判读表（Interpretation Sheet）

判读表是**人工**对真实数据结果的结论记录（空白模板 T-C）。规则：

- 每条样本 × 每个**适用**判读维度一行；结论为 `通过 / 不通过 / 需复查 / 不适用（附原因）`。
- `需复查` = 结果边界模糊、脱敏存疑或需回查原文（回查只由用户凭假名映射进行）。
- 判读依据列必须引用具体证据（输出文件 + 行号/ID），不能写"感觉不对"。
- 判读表内**只出现假名与样本 ID**，不出现真实内容。
- **证据最小化**：判读表只记录人工脱敏摘要、计数、哈希与假名 ID；**不保存原始 stdout/stderr、URL、正文或姓名**。判读依据引用证据文件的退出码/行号/ID（第 12 章）。

| 判读维度 | 判读要点（占位示例） |
| --- | --- |
| archive 正确性 | 标题/正文/来源/时间是否与样本一致 |
| search 相关性 | top-k 结果是否命中查询意图 |
| evidence 可解析 | 每条 citation locator 能否打开且内容一致 |
| delete 完整性 | 删除后无残留、无孤儿引用 |
| migration 数据完整 | 迁移前后条目数、字段值一致 |

### 10.3 自动门禁（Gates）

| 门禁 | 命令/机制 | 阈值/判定 | 生效阶段 |
| --- | --- | --- | --- |
| **G0 目标子进程离线入口** | 已交付：pytest=offline pytest bootstrap + root conftest；CLI/MCP=`offline_entrypoint.py`；FT7 Direct Python=仅仓库 `python -m` / `.py`，同进程 `runpy`。入口在产品导入前清理 live/secret/proxy、绑定 base-only Config，并安装网络 guard；Direct Python 另安装子进程 guard | 已逐入口核验；拒绝 `-c`/stdin/flags/仓库外目标；非 Python Direct 不属于 Python G0；进程内 guard 不是 OS sandbox | **CAT-0 逐命令硬前置** |
| G1 离线套件 | 默认 pytest（`-m "not network and not manual"`），合成 fixture（CAT-0） | 全绿 | P0/P1/P2 前置 |
| G2 MCP 16-task | `evals.mcp_quality --enforce-thresholds`（合成 `OfflineMcpScenario`，CAT-0） | 退出码 0（含 citability 100%） | P0/P1/P2 前置 |
| G3 快照一致性 | 快照条目数与 manifest 一致；`stats` 由用户执行，Agent 只核对用户交付的脱敏摘要/计数/哈希 | 相等 | P0/P1/P2 |
| G4 隔离核验 | 用户核对授权单、场景/clone 路径与快照只读状态；CAT-U/C 另要求 G8/U1，通过受控摘要记录，**不用 `config show`** | 无生产路径；写操作仅 clone；工作区无 raw log | P0/P1/P2 每步前 |
| G5 脱敏扫描 | 报告不含假名映射原文/密钥/盘符路径，且只含退出码/计数/哈希/假名/脱敏摘要/模板指纹（后续任务 FT1 工具） | 零命中 | P1/P2 报告产出前 |
| G6 指标漂移 | P2 趋势对比：任一**默认指标**相对下降 > 5pp（绝对值） | 触发分流 | P2 |
| G7 真实快照 MCP harness | **待实现前置（FT6）**：16-task runner 当前固定 `OfflineMcpScenario`，不能跑真实快照；G7 在 FT6 交付前**不可执行**，从 P1/P2 默认门禁剔除（离线证据契约门禁仍由 G2 承担）。FT6 交付后仅当其为 base-only（不加载用户配置或生产 runtime snapshot、不联网、输出仅脱敏摘要）才可归 CAT-0，否则 user-only | 交付后：100% 可读（否则降级记录） | P1/P2（FT6 交付后） |
| **G8 user-only launcher** | U1：授权令牌 + `.data-test`/clone 路径验证 + 不回显原始 argv + 工作区文件日志禁用/源头脱敏 + stdout/stderr 捕获后仅输出脱敏摘要；不得复用强制离线的 G0 launcher 执行 live | 原始 query/URL/title/argv 不落入工作区或 Agent 输出；快照写入为 0 | 所有 CAT-U/CAT-C 硬前置 |

> 门禁只负责客观可机器校验部分；人工判读表（10.2）不可被门禁替代。

---

## 11. 失败分流（Failure Triage）

### 11.1 失败分类

| 分类 | 判定 | 示例 | 默认处理 |
| --- | --- | --- | --- |
| **F1 流程/环境** | 快照路径、隔离、命令形式错误 | DataRoot 拼错、junction 检测 | 立即修正并重跑该步 |
| **F2 样本问题** | 样本本身导致结果不可判读 | 脱敏不彻底、样本损坏 | 修正脱敏/重新采样，重跑 |
| **F3 工具/代码缺陷** | 离线可复现的缺陷 | 某字段解析错误 | 记录缺陷，先离线修复（回到合成 fixture 验证）再重跑 |
| **F4 真实数据特性** | 仅真实数据才暴露的边界 | 罕见编码、超大正文 | 记录为"真实数据观测"，决定产品是否需适配（可开新任务） |
| **F5 安全事件** | 违反 0.2 / 发现 D4 / 泄露 | 报告出现密钥 | **立即停止全部流程**，按 11.3 处理 |

### 11.2 响应矩阵

| 分类 | 暂停？ | 谁决策 | 记录位置 |
| --- | --- | --- | --- |
| F1 | 否（原地修正） | 执行者（按通道） | 执行日志 |
| F2 | 是（样本层） | 数据所有者 | 判读表备注 + 分流记录 |
| F3 | 是（验证层） | 执行者（按通道）+ 判读人 | 分流记录 → 缺陷任务 |
| F4 | 否（记录即可） | 判读人 | 判读表 + 后续任务 |
| F5 | **是（全部）** | 数据所有者 | 安全事件记录（12 章） |

### 11.3 安全事件处理（F5 / D4）

1. 立即停止所有命令；不再产生任何输出文件。
2. 用户清除含泄露内容的快照与输出（第 13 章清除规则）。
3. 事件记录（时间、发现位置、处理、影响范围）写入审计证据包；**记录中只写字段名与假名，不写泄露原文**。
4. 流程回到 P0，重签授权单后方可继续。

---

## 12. 审计证据（Audit Evidence）

证据包存放于 `.data-test/<scenario>/evidence/`（Git 忽略），命名 `evidence-<阶段>-<日期>.zip` 或平铺目录。

**证据最小化铁律**：所有位于工作区、可被 Agent 阅读或进入版本控制的记录（含应用文件日志、模板、报告、审计证据）只允许包含**脱敏摘要、退出码、计数、哈希、假名 ID、时间、命令类别与模板指纹**；
**禁止 raw stdout/stderr、实际 URL、真实命令实参、正文、姓名、绝对路径与密钥**。如用户确实需要原始证据，只能由用户保存在**工作区之外、由用户 ACL 隔离且 Agent 不可读的位置**——**不得**使用 `.data-backup/` 或任何工作区内目录声称"Agent 不可读"（12/13 章，T-G）。Agent 不得将原始输出写入证据包或报告。

现有产品日志会记录 query/URL/title；因此 G5 事后扫描只负责验证交付物，**不能**作为执行保护。U1 必须在命令启动前禁用工作区文件日志或安装源头脱敏 formatter/filter；U1 未交付时，真实快照命令保持阻塞。

| 证据 | 来源 | 必含内容 |
| --- | --- | --- |
| E1 授权单 | 用户签发 | 授权单 ID、范围、类别、留存、授权时间与授权令牌（无签名/姓名） |
| E2 样本清单 | 用户/执行者协作 | manifest 文件（5.3；仅假名/占位 ID） |
| E3 命令日志 | 用户执行，Agent 整理 | **命令类别 + 模板指纹 + 退出码 + 输出哈希 + 时间 + 脱敏摘要**；不记录真实命令本身或实参、不含 stdout/stderr 全文（12 章） |
| E4 判读表 | 判读人 | T-C 模板填写结果（脱敏摘要 + 计数 + 假名） |
| E5 门禁记录 | 执行者 | G0–G8 已实现项的脱敏摘要与退出码 |
| E6 指标记录 | 执行者 | 10.1 默认指标数值与口径；剔除项附"剔除待前置/未覆盖"声明 |
| E7 分流记录 | 全体 | 11.1 分类、决策、后续任务 |
| E8 清除凭证 | 用户 | 清除命令退出码、目录哈希、时间戳 |
| E9 原始证据（可选） | **仅用户** | 存放于**工作区之外、用户 ACL 隔离、Agent 不可读**的位置；证据包只记录位置指针与哈希，不存内容 |

完整性约定：报告结论必须能逐条追溯到 E1–E8；E2/E4/E8 三者 ID 互相对应（授权单→清单→清除）。

---

## 13. 留存与清除（Retention & Cleanup）

| 对象 | 默认留存 | 清除方式（用户执行） | 凭证 |
| --- | --- | --- | --- |
| 授权快照（含假名映射） | 按授权单（默认 ≤ 30 天） | 用户删除 `.data-test/<scenario>/` 目录 | E8 |
| disposable clone | 演练完成后立即清除（默认 ≤ 7 天） | 用户删除 `.data-test/<scenario>/clones/<clone-id>/` | E8 |
| 报告与证据包 | 按授权单（默认 90 天），可归档至用户指定位置 | 用户移动/删除 | E8 |
| 执行日志 | 随证据包 | 同上 | E8 |
| 原始证据（如用户选择保留） | 用户自定 | **仅用户保存于工作区之外、用户 ACL 隔离、Agent 不可读的位置**（不得使用 `.data-backup/` 或任何工作区内目录）；证据包只留位置指针与哈希 | E9 |
| 样本清单（manifest） | 可长期保留（不含真实数据） | — | E2 留存 |

清除规则：

1. 只有用户执行清除；AI 不删除含真实数据副本的目录。
2. 清除后运行 `Test-Path` / `Get-ChildItem` 确认，并写入 E8。
3. 授权单到期未清除 → 提醒用户，流程暂停直到处理完成。

---

## 14. 回滚（Rollback）

本流程对生产数据**零写入**；快照根保持只读，破坏性演练一律在 disposable clone 内进行（7.1-6/7），因此"回滚"主要是 clone 重建与报告层恢复：

| 场景 | 处理 | 前置 |
| --- | --- | --- |
| clone 被演练命令写坏 | 直接丢弃 `.data-test/<scenario>/clones/<clone-id>/`，用户从授权快照重建新 clone 并重跑（7.1-7 制备流程） | 快照根保持只读 |
| clone 迁移演练出错 | 同上：丢弃 clone、重建、重跑 dry-run | 快照根在用户侧 |
| 报告/证据误删 | 从证据包恢复 | E8 前的归档 |
| 生产数据受影响（理论上不会发生） | 用户执行 `restore-data.ps1` 从 `.data-backup/` 恢复 | 必须先有备份；AI 不执行 |

铁律：**快照根永不被演练命令写入**，破坏性操作只在 disposable clone 上（7.1-6/7）；任何进入回滚流程的情况先备份再操作；生产回滚只能由用户执行。

---

## 15. 覆盖领域详解

### 15.1 archive

**状态**：**live/数据出境阶段**（需单独授权，默认不执行）。`archive` 经 `archive-url` / `archive-text` 工作流触发 `FetchStep`（URL 抓取）与 `AnalyzeStep`（DeepSeek 摘要/标签；`config/workflows/*.yaml` 固定含 `ai_analyze`），向量索引写入另触发 Embedding HTTP——这些网络行为**无法通过开关关闭**（7.3）。

**执行通道**：**user-only**（8.1 CAT-U）：`archive` 会读取用户配置并写入由快照制备的 writable clone；只能由用户经 U1 执行。Agent 不执行、不读取原始输出，只接收脱敏摘要。

**验证内容（授权后，用户执行）**：真实内容归档后三重存储一致（Markdown 主存储 + SQLite 索引 + 向量索引按需）、Front Matter 字段完整、处理器选择正确（wechat/zhihu/chat/generic/text_fallback）。

**目标命令意图（用户手动；U1 + 单独授权后）**：

```text
U1 launcher -> DataRoot=.data-test/real-eval-p1/clones/<archive-clone-id>
             -> archive "<占位 URL>"
U1 launcher -> DataRoot=.data-test/real-eval-p1/clones/<archive-clone-id>
             -> archive "<占位文本文件路径>" --type chat
```

`<archive-clone-id>` 是从只读授权快照制备的独立 writable clone；archive 后直接核对 clone 的 Markdown/SQLite/vector，不把结果回写快照。U1 未交付前，上述仅是接口契约，不是可执行命令。

**离线替代验证（Agent-safe，CAT-0）**：脚本依赖项目 requirements（bs4/frontmatter/存储模块）。它已要求 FT7 运行时 attestation，必须经 wrapper 路由到 generic Direct Python 入口；入口在同一进程内通过 `runpy` 执行并安装 base-only Config、网络/子进程 guard。以下命令已按 G0 核验，同时仍须显式固定所有可变参数：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\real-sample-p0\seed -Command @(
  "python", "scripts\setup-test-db.py",
  "--seed", "20260731", "--count", "20",
  "--wechat-count", "3", "--zhihu-count", "3",
  "--output", ".data-test/real-sample-p0/seed/db/knowledge_vault.db"
)
```

`setup-test-db.py` 的 `--output` 已精确锁定到本次 wrapper 选定的 `DATA_DIR`，不能写入其他 `.data-test` 场景或仓库外路径。该命令只允许清理 `.data-test/real-sample-p0/seed/` 派生的 db/vault/vectors；不得省略 `--seed` 或 `--output`。它只验证**读路径与存储一致性**，不能证明 archive 写入路径或真实抓取/LLM 行为。

**判读点**：标题/正文/来源/时间字段与样本一致；`list` 中出现新条目；`show <id>` 可回读（以上均为 live 授权后、用户执行并交付脱敏摘要的判读；离线替代只判读一致性）。

### 15.2 search

**验证内容**：真实文本上的检索相关性与排序；中文分词效果（jieba）；英文回退。

- **默认（离线）**：`search --strategy bm25`（FTS5，无 Embedder）与 MCP `search_knowledge` strategy=bm25——**user-only**（命令会读取用户配置/生产 runtime snapshot 并读取快照，8.1）。
- **live/数据出境（单独授权后）**：`--strategy vector/hybrid`、路由到 Hybrid 的 `auto`（当前为 ≥5 tokens）及 MCP 对应语义检索路径会触发 Embedding Provider——**user-only**。短查询 `auto` 路由到 BM25，不提前构造 Provider，但读取真实快照仍是 user-only。

**目标命令意图（全部由用户经 U1 执行；Agent 只接收脱敏摘要）**：

```text
# 默认离线：BM25（不触发 Embedding HTTP）
U1 launcher -> readonly snapshot -> search "<占位查询>" --strategy bm25 --limit 10
# live/数据出境（单独授权后）：向量/混合
U1 launcher -> readonly snapshot -> search "<占位查询>" --strategy vector --limit 10
U1 launcher -> readonly snapshot -> search "<占位查询>" --strategy hybrid --limit 10
```

**判读点**：top-k 相关性（10.1；默认只计 BM25，live 指标单独计）；不同策略结果差异是否可解释（live 授权后）。

### 15.3 MCP evidence / citation

**现状**：16-task 评测 runner（`evals/mcp_quality/runner.py`）固定 `OfflineMcpScenario`（`evals/mcp_quality/scenario.py`），只能跑确定性离线 fixture，**不能**挂接真实快照库；stdio MCP server 绑定启动进程的 stdin/stdout，**无法**被另一会话附着调用。因此"在真实快照上经 MCP 验证 evidence/citation"目前**不可执行**，属**待实现前置**（FT6 受控 harness，参照 `tests/e2e/conftest.py` 的 `TestEnv` 子进程模式与 16-task runner 的 scenario 模式）。

**目标离线能力（已逐入口核验）**：G2 16-task 闭环经 FT7 generic Direct guard 运行（证据契约门禁，CAT-0）；MCP 只读 bm25 工具冒烟经受保护的 `offline_entrypoint.py` 运行，可覆盖 `search_knowledge` strategy=bm25 / `get_entry` / `query_subgraph` / `explain_relation` / `collect_evidence`(文档级) / `find_bridges` / `timeline_of` / `contrast`。后者只要作用于真实快照即 user-only，仍需 U1/G8。

**FT6 交付后的验证内容**：`collect_evidence` / `explain_relation` / `query_subgraph` / `timeline_of` / `contrast` 在真实样本上的证据质量与 citation 可解析性；`pkv://entries/{id}/chunks/{chunk_id}` 等 locator 可经 MCP Resource 读取。FT6 交付后仅当其以 base-only 方式实现（不加载用户配置或生产 runtime snapshot、不联网、输出仅脱敏摘要）才可归 CAT-0；否则仍 user-only（8.1、10.3-G7）。

**判读点**：证据相关性（人工）、citation 可解析率（FT6 交付后自动，G7）、partial Tool 的 `implementation_level/limitation_notes/evidence_sources` 是否诚实公开、无本机绝对路径泄漏。

### 15.4 delete

**验证内容**：删除条目后 SQLite（含 chunk）、Markdown、向量索引、关系四层一致，无孤儿引用。

**执行边界**：只能在 **disposable clone**（`.data-test/<scenario>/clones/<clone-id>/`）内执行（7.1-6/7）；快照根保持只读，不做"事后回滚"。

> **现状缺口与门禁**：当前无 CLI delete 子命令，delete 位于存储层（`SQLiteStore.delete_entry` / `MarkdownStore.delete`），且没有在 clone 上执行删除并核验四层一致的安全执行器。因此 **delete 完整性指标默认从 P1/P2 指标剔除**（10.1）；若纳入，**FT3 工具是其硬前置**——FT3 未交付前 delete 不进入 P1 DoD，也不计分（9.2）。执行通道：FT3 交付后若为 base-only（不加载用户配置或生产 runtime snapshot、不联网、输出仅脱敏摘要）可归 CAT-0，否则 user-only（8.1）。

**判读点**：删除后 `show <id>` 返回不存在；`list` 计数减少；chunk/向量/关系无孤儿（FT3 提供断言）。

### 15.5 migration

**验证内容**：快照库在 disposable clone 上从历史 schema baseline 迁移到目标版本成功；条目数/字段值在迁移前后一致；`schema_version` 更新。

**执行通道**：**user-only**（CAT-C）：`scripts/migrate.py` 用 `Config()` 解析用户配置和有效数据根，且未接入 base-only 受控入口；迁移可能创建备份。历史 checkout 的 `.data-backup/` 语义不构成当前运行布局，且不得自动接管。`scripts/run-test.ps1` 当前会明确拒绝 `migrate.py` 并返回 exit 2，因此不得提供或使用任何 wrapper migrate 命令。迁移只能在 FT5 + U1/G8 + 用户授权齐备后由专用 user-only launcher 执行；Agent 不执行、不读取原始输出（0.2-7、8.1）。

**可审计步骤（全部由用户执行）**：

1. **历史 schema baseline 来源**：用户准备旧版本数据库/Schema 夹具（脱敏），**仅记录版本号与目录/文件哈希**（不记录路径内容、不提交 Git）；baseline 缺失时迁移路径不可测。
2. **clone 制备与验证**（7.1-7）：用户把基线夹具（或授权快照）复制到 `.data-test/<scenario>/clones/<clone-id>/`（db/vault/vectors 齐全），以只读命令验证可打开且条目数与基线一致，记录 `clone-id`、制备时间与哈希（T-G/T-H）。
3. **pending migration 检查**：`--dry-run` 输出待迁移脚本数。**若 pending = 0，不得宣称覆盖迁移路径**，只能记录"当前版本健康检查"，并把 migration 兼容性指标标记为"未覆盖"（10.1、9.2 DoD ⑦）。
4. **执行迁移**（破坏性，前置缺一不可）：user-only + FT5 DataRoot/断言支持 + U1 user-only launcher + clone-only（`clones/<clone-id>`）+ 明确授权；必须加 `--no-backup`。G0 是 CAT-0 离线入口，不是 user-only migration 的前置或保护器。
5. **验证**：迁移前后条目数一致、抽样 `show` 字段一致、`schema_version` 更新；记录脱敏摘要与退出码（E3）。

**目标命令意图（FT5/U1 交付后由用户执行；DataRoot 必须指向 clone）**：

> **场景绑定（7.1-4）**：`<scenario>` 必须替换为当前阶段——P0=`real-sample-p0`、P1=`real-eval-p1`、P2=`real-regression-p2`；clone 命令只允许在当前阶段的 `clones/<clone-id>` 下执行，**不得跨场景**使用其他阶段的数据根。§8.3 的 CAT-C 示例同样适用此绑定。

```text
U1 launcher -> DataRoot=.data-test/<scenario>/clones/<clone-id>
             -> python scripts/migrate.py --version | --health-check | --dry-run
U1 launcher -> DataRoot=.data-test/<scenario>/clones/<clone-id>
             -> python scripts/migrate.py --auto --no-backup
```

> **DataRoot 支持前置**：`migrate.py` 当前没有显式 `--db-path`，需 FT5 提供可审计的 clone 寻址与前后一致性断言。它作为 user-only 命令可以读取用户配置/生产 runtime snapshot，但必须由 U1 将 DB_PATH 锁定到 clone、禁用/净化工作区日志并验证授权；不得声称 FT7/G0 base-only 预检保护了 migration。

**判读点**：迁移前条目数 = 迁移后条目数；抽样 3 条 `show` 字段一致；迁移日志无异常（均为用户执行的脱敏摘要）。生产迁移永远由用户按[数据库迁移指南](../数据库迁移指南.md)执行。

---

## 16. 合成 fixture 与真实数据的边界

### 16.1 各自能证明什么

| 证据来源 | 能证明 | 不能证明 |
| --- | --- | --- |
| 合成 fixture（`tests/fixtures/`、`evals/mcp_quality/`、dev vault #3） | 契约正确性、代码路径、离线可复现性、16-task 推理契约（119 项检查） | 真实数据质量、真实分布、真实长尾 |
| 授权快照（本 Runbook） | 在**所覆盖分层**上真实数据的可观测表现（由用户执行命令并交付脱敏摘要，12 章） | 全量数据质量、未覆盖分层的表现、生产安全性 |
| 两者都不能 | — | 产品发布结论（见 16.3） |

### 16.2 边界规则

1. 合成 fixture 是**门禁默认来源**（G1/G2）；真实快照是**补充观测**，两者互不替代。
2. 真实快照指标只对样本清单声明的分层有效（5.4 代表性声明），不得外推。
3. 合成 fixture 中发现的缺陷修复后，必须先在合成 fixture 上复绿，才允许重跑真实样本（防"为过真实样本而改测试"）。
4. 真实样本中发现的缺陷，必须先尝试在合成 fixture 上构造最小复现；构造不出的（真实数据特性 F4）单独记录并评估产品适配。

### 16.3 不可作为发布结论的指标（明确禁止的解读）

以下解读**一律不允许**出现在报告或 release 判定中：

- ❌ "16-task 闭环 119/119，因此真实数据上的证据一定可引用" —— 离线闭环只证明契约，不证明真实数据。
- ❌ "P1 样本相关性 @k 达到 X%，因此产品检索质量达标" —— 小样本 + 人工判读，仅观测值。
- ❌ "快照迁移演练成功，因此生产迁移一定成功" —— 生产迁移必须由用户按既有流程（备份→授权→执行→验证）完成。
- ❌ "合成 fixture 全部通过 = 发布可以" —— 发布还需要 16.3 之外的完整证据链（离线套件 + 用户人工验收）。
- ❌ 把任何样本级数值作为 CI 硬门禁/发布门槛；G8 是用户侧执行安全门，不进入 CI，G0/G7 也只在对应工具交付后启用。

---

## 17. 进入/退出条件、风险、成本、完成定义（汇总）

### 17.1 三阶段条件汇总

| | P0 预演 | P1 受控评测 | P2 定期回归 |
| --- | --- | --- | --- |
| **进入** | G0+G1+G2 绿；P0 授权单+清单；合成基底 OR；执行 CAT-U/C 另需 U1/G8 | P0 DoD；P1 授权单+清单；CAT-U/C 需 U1/G8 | P1 DoD；P2 授权单+固定清单；CAT-U/C 需 U1/G8 |
| **退出** | 五类证据齐备；流程缺陷清零 | 默认指标有记录（剔除项带声明）；判读确认；证据包完整 | 趋势表≥2 点；无未关闭漂移任务 |
| **DoD** | 9.1 | 9.2 | 9.3 |

### 17.2 风险登记

| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| 真实数据泄露（报告/日志含原文） | 高 | U1 写入前禁用/脱敏日志 + 不回显 argv + G5 交付物扫描；G5 不能替代 U1 |
| D4 数据混入样本 | 高 | 3.1 分级 + 授权单审查 + 11.3 事件流程 |
| G0 被误当全局保护（预检通过即让 Agent 执行快照命令） | 高 | G0 只保护完整离线 CAT-0 入口；真实快照必须 user-only 且等待 U1/G8 |
| 指标被过度解读为发布结论 | 中 | 16.3 禁令 + 报告固定措辞 |
| 快照污染（演练命令写坏快照） | 中 | 快照根只读 + 破坏性演练只在 disposable clone（7.1-6/7） |
| 原始证据误存于工作区（如 `.data-backup/`） | 中 | 12/13 章：仅工作区之外、用户 ACL 隔离、Agent 不可读的位置 |
| 迁移指标虚报（pending=0 仍宣称覆盖） | 中 | 15.5：pending=0 标记"未覆盖"，不宣称覆盖迁移路径 |
| 样本偏置（代表性不足） | 中 | 5.1 分层 + 5.4 代表性声明 |
| 迁移演练破坏快照库 | 中 | 仅 clone 内可写迁移 + `--no-backup` + 丢弃重建 |
| live 数据出境（archive/vector 误当默认步骤执行） | 中 | 7.3 边界 + 授权单单列 + U1/G8 + CAT-U 仅用户 |
| dev vault 暂时不可用或健康检查失败 | 低 | P0 走最小合成样本预演降级，并记录 dev-vault 修复任务（18.2） |

### 17.3 成本估计

| 项 | 估算 | 说明 |
| --- | --- | --- |
| P0 时间 | 45–90 分钟 | U1 交付后的估算；未交付时仅运行 CAT-0 合成排练 |
| P1 时间 | 3–6 小时 | U1 交付后的真实快照执行 + 人工判读（最耗时） |
| P2 时间 | 45–90 分钟 | 固定清单复用 |
| API/网络费用 | 默认 ¥0 | 纯离线默认；live/数据出境（archive/vector 等）授权执行时按授权单预算上限计费并单列记录（7.3） |
| 存储 | 每场景 ≤ 100 MB | 快照 + 证据包 + clone，均在 `.data-test/` |

### 17.4 总完成定义（Runbook 级 DoD）

- [ ] 三阶段文档与模板齐备（本文件 + T-A…T-H）。
- [x] 每个当前 CAT-0 Python 命令均有匹配的 G0 入口：pytest=offline pytest bootstrap + root conftest；CLI/MCP=`offline_entrypoint.py`；16-task/seed/dev-vault=`FT7 generic Direct Python`。Direct Python 只支持仓库 `python -m` / `.py` 并同进程 `runpy`；非 Python Direct 不属于 Python G0。
- [ ] G8/U1 被所有真实快照步骤引用；U1 未交付时这些步骤明确剔除，不存在当前可复制执行的真实数据命令。
- [ ] 双通道表述一致：无"Agent 可执行离线真实快照命令"的表述（0.2-7/9、2、8）。
- [ ] P0 已实际执行一次并验证流程（**待用户授权后执行**；本任务只交付文档）。
- [ ] 交付状态与后续任务清单（第 19 章）已登记：FT7/#3 为已交付，FT1–FT6/U1 为后续；阶段步骤继续使用 S0–S11，两套编号无冲突。
- [ ] 文档一致性检查通过（第 20 章 + `git diff --check`）。

---

## 18. 与其他任务/分支的接口与依赖

### 18.1 CAT-0 G0（已交付并逐入口核验）

- `codex/review-testcase-repair@5c14caa` 的安全/测试修复已成套集成到当前 P0 收口线，当前实现基线如下。
- **pytest**：`tests/offline_entrypoint.py pytest` 在 pytest/plugin 导入前安装 base-only Config 与网络 fail-closed，root `tests/conftest.py` 再维持逐用例隔离。
- **CLI/MCP**：`tests/offline_entrypoint.py` 在产品导入前安装同类门禁。
- **FT7 generic Direct Python**：wrapper 只接受仓库内 `python -m <module>` 或 `python <script.py>`，转交 `offline_entrypoint.py` 后在同一进程以 `runpy` 执行；入口清理 live/secret/proxy 环境、绑定 base-only Config，并安装网络与子进程 guard。`-c`、stdin、解释器 flags、仓库外模块/脚本均 fail-closed。
- **边界**：非 Python `-Direct` 不属于 Python G0；上述 guard 是 Python 进程内防护，不是 OS sandbox。真实快照仍是 user-only，另依赖 U1/G8；migration 还依赖 FT5，且 wrapper 对 `scripts/migrate.py` 明确返回 exit 2。

### 18.2 #3 开发 vault 轻量重建（已交付）—— P0 合成排练基底

| 接口 | 定义 |
| --- | --- |
| 交付物 | 已交付的轻量开发 vault：合成但结构代表性（内容类型/时间/体量分层），可重复重建 |
| 消费方 | 本 Runbook P0 的排练基底之一；`tests/` 离线用例的补充 fixture |
| 契约 | dev vault 必须是**合成数据**（不得含任何真实数据）；`rebuild-dev-vault.py` 要求 FT7 runtime attestation（`process_guarded=True`），且 `--root` 必须位于本次 wrapper 选定的 `DATA_DIR` 内 |
| 依赖方向 | P0 默认使用已交付 dev vault；必要时仍可使用最小合成样本预演。两者都只属于 CAT-0，不代表真实数据验证 |

重建与健康检查均必须经 FT7 Direct Python 入口运行，例如 `run-test.ps1 -Direct -DataRoot <selected> -Command @("python", "scripts/rebuild-dev-vault.py", "--root", "<selected>", "--json")`，以及同一 selected `DATA_DIR` 下的 `--check-only --json`。脚本要求 `process_guarded=True` 的 runtime attestation；裸 Python 会在产品 import 前失败，`--root` 指向任意 `.data-test` sibling 或 selected `DATA_DIR` 之外也会被拒绝。两条受保护路径已核验。dev vault 完全由合成数据构成，不改变“真实数据尚未执行”的状态。

### 18.3 依赖顺序图

```text
当前 P0 收口线
├─ CAT-0 G0（pytest / CLI / MCP / FT7 Direct Python）──▶ 已交付、已逐入口核验
├─ dev vault 重建 + --check-only ─────────────────────▶ 已经 FT7 wrapper Direct Python 核验
├─ U1 user-only launcher ─────────────────────────────▶ 未交付；G8 阻塞 CAT-U/CAT-C
├─ FT5 migration DataRoot/断言 ───────────────────────▶ 未交付；额外阻塞 migration
└─ 真实数据验证 ──────────────────────────────────────▶ 尚未执行，等待用户授权 + U1/G8（migration 再加 FT5）
```

---

## 19. 交付状态与后续任务清单

> CAT-0 FT7 已交付；本次仍**未执行真实数据验证，也未实现 U1/真实数据专用 launcher**。其余工具/扩展继续作为后续任务，完成时回填本 Runbook 对应步骤。

| ID | 任务 | 类型 | 解除的阻塞 | 依赖 |
| --- | --- | --- | --- | --- |
| FT1 | 脱敏扫描器（RedactionScanner）：基于假名映射反向扫描报告/日志，产出零命中报告 | 安全工具 | G5 自动化 | 无（可纯离线开发） |
| FT2 | 样本清单校验器（SampleManifestValidator）：校验 manifest 与快照一致、分层覆盖、规模上限 | 测试工具 | G3 自动化 | 无 |
| FT3 | 快照 delete 安全执行器：在 `clones/<clone-id>` 内调用存储层 `delete_entry` 并核验 SQLite/Markdown/向量/关系四层一致 | TestCase 扩展 | **15.4 delete 指标硬前置**（未交付则 P1/P2 剔除该指标） | 无 |
| FT4 | 授权快照制备向导（用户侧脚本，非 AI 执行）：选择→脱敏→落盘的引导式帮助 | 用户工具 | 降低人工准备成本 | 需用户审阅 |
| FT5 | 迁移前后一致性断言扩展（条目数/字段抽样，clone 内）+ 显式 `--db-path`/DataRoot 支持 | TestCase 扩展 | **15.5 迁移指标自动断言 + DataRoot 支持前置**（未交付则迁移命令不可执行，登记前置） | 无 |
| FT6 | 真实快照 MCP 证据 harness：参照 `tests/e2e/conftest.py` 的 `TestEnv` 子进程模式与 16-task runner 的 scenario 模式，在受控 harness 内对真实快照执行 evidence/citation 验证 | 测试工具 | **G7 / 15.3 硬前置**（未交付则 P1/P2 剔除 MCP evidence/citation 指标；G2 离线闭环不受影响） | 无 |
| FT7（已交付） | 完整离线 launcher 扩展：pytest、CLI/MCP 与 generic Direct Python 均有匹配入口；Direct Python 仅仓库 `python -m` / `.py`、同进程 `runpy`，清理 live/secret/proxy、绑定 base-only Config、安装网络/子进程 guard，拒绝 `-c`/stdin/flags | 安全工具 | **已解除受支持 Direct Python 的 G0 / CAT-0 阻塞**；非 Python Direct 不在 Python G0 内，guard 不是 OS sandbox | 已完成逐入口核验 |
| U1 | user-only 真实数据 launcher：验证授权令牌与 `.data-test` 边界、快照只读/clone 可写策略、live 授权与预算；不回显原始 argv，禁用工作区文件日志或源头脱敏，捕获输出后只生成退出码/计数/哈希/假名摘要 | 用户安全工具 | **G8；所有 CAT-U/CAT-C 硬前置**，解决 raw query/URL/title/argv 落盘与 G0 强制离线不适用于 live 的问题 | FT1 脱敏规则；用户审阅 |

所有后续任务必须满足：只在 `.data-test/` 下运行；不得读取 `<effective-data-root>/`、用户配置或生产 runtime snapshot；除 U1 明确授权的 live 命令外不得联网。FT1–FT6 只有以 base-only、无网络、**不读取真实快照**、仅脱敏输出交付才可进入 CAT-0；任何读取真实快照的实现仍为 user-only。已交付 FT7 维持同一合同。U1 明确为 user-only，可加载用户凭据但不得把凭据或原始数据写入工作区/交给 Agent。进入默认 CI 前先过 F1–F5 分流（U1 不进入 CI）。

---

## 20. 文档一致性检查与相关文档

### 20.1 一致性检查（本任务已完成并记录）

- [x] 双通道模型一致：CAT-0（Agent-safe）vs CAT-U/CAT-C/CAT-D（user-only）；全文无"Agent 可执行离线真实快照命令"的表述（0.2-7/9、2、8、9）。
- [x] G0 语义正确：pytest、CLI/MCP 与 FT7 Direct Python 已按目标子进程逐入口验证；FT7 只支持仓库 `python -m` / `.py`，拒绝 `-c`/stdin/flags，非 Python Direct 不属于 Python G0，且进程内 guard 不是 OS sandbox；user-only 命令另由 U1/G8 保护。
- [x] 证据契约统一：T-D/E3 无"原样记录"；版本化/Agent 可读记录仅含脱敏摘要、退出码、计数、哈希、假名 ID、时间、命令类别与模板指纹；无 raw stdout/stderr、实际 URL、真实命令实参、正文、姓名、绝对路径、密钥（6.3、10.2、12 章、模板）。
- [x] 原始证据仅存于**工作区之外、用户 ACL 隔离、Agent 不可读**的位置；未将历史 checkout `.data-backup/` 或任何工作区内目录称为 Agent 不可读（12/13 章、T-G）。
- [x] clone/migration 步骤可审计：`clones/<clone-id>` 路径、制备与验证（7.1-7）、历史 schema baseline 与 pending migration 要求（15.5）。
- [x] 全文未宣称 `PKV_RUN_LIVE` 为网络开关（0.2-9、7.3）。
- [x] 仅 CAT-0 提供当前可复制命令，且明确依赖完整 G0；真实快照仅保留 U1 目标接口，不提供可误执行的裸 `run-test.ps1` 命令；文中不出现已解析的真实生产绝对路径。
- [x] `git diff --check` 无空白错误；本文件不含任何真实数据。

### 20.2 相关文档

- [测试环境隔离指南](./测试环境隔离指南.md) —— 隔离机制与命令约定（本 Runbook 第 7 章依据）
- [MCP 最小评测闭环](../../operations/MCP最小评测闭环.md) —— 16-task 闭环与 G2 门禁
- [MCP 最小评测基线（2026-07-29）](../../operations/MCP最小评测基线-2026-07-29.md) —— 119/119 基线与契约
- [AI安全与数据库升级完整方案](./AI安全与数据库升级完整方案.md) —— live 测试开关与安全边界
- [数据库迁移指南](../数据库迁移指南.md) —— CAT-C / 15.5 依据
- [关系回填质量验证指南](../../operations/关系回填质量验证指南.md) —— 测试副本库验证先例
- [tests/CLAUDE.md](../../../tests/CLAUDE.md) —— 测试层次与 marker 契约
- [.ai-safety-rules.md](../../../.ai-safety-rules.md) —— 安全红线（0.2 依据）
- [docs/README.md](../../README.md) —— 文档结构约定

---

**文档版本**: v1.5（2026-08-21：路径合同切换到用户 profile / effective data root；CAT-0、真实数据门禁不变）
**创建日期**: 2026-07-31
**状态**: CAT-0 G0 已按 pytest、CLI/MCP、FT7 Direct Python 逐入口交付并核验；真实数据尚未执行，CAT-U/CAT-C 仍等待 U1/G8 与用户授权，migration 另需 FT5。旧 checkout 路径与 held release artifacts 仅历史保留，不触发自动迁移。
**作者**: AI Agent（P2 任务）

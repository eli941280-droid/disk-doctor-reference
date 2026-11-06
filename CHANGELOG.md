# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.3] — 2026-11-06

两项发布阻断修复。都不是"少写了一个检查"，而是**检查发生的位置不对**。

### 安全修复

- **崩溃恢复在锁外写状态**：`reconcile_interrupted()` 原本是
  `read_state → _lock_is_free（取锁后立刻释放）→ _write_state(INTERRUPTED)`。
  活着的执行者可以在"释放锁"和"写入"之间完成并写 `COMPLETED`，
  然后被覆盖成 `INTERRUPTED`——文件没事，审计记录撒谎。
  改为 `_recover_if_orphaned()`：线程锁 → 文件锁 → **锁内重读** →
  仍是 `IN_PROGRESS` 才写。任一锁拿不到就不写。删除 `_lock_is_free()`。
- **扫描入口跟随链接根目录**：`is_symlink_or_reparse()` 此前只用于过滤**树内**
  条目。`os.walk(..., followlinks=False)` 不跟随子目录，但根目录是不是链接它不管。
  若 `--root` 本身是 junction，`shutil.disk_usage()` 报告目标卷的容量，
  扫描/去重/缓存候选全部长在目标目录上。
  现在 `DiskScanner.volume` / `directory_sizes` / `top_files` / `scan` 在
  `is_dir` / `disk_usage` / `scandir` / `walk` **之前**抛 `UnsafeScanRoot`；
  `DuplicateFinder._collect` 与 `CacheAnalyzer.discover` 跳过该 root；
  `collect_files` 返回 `((), 0)`；`path_size_report` 返回 `skipped_redirects=1`。

### 新增

- `core/utils.UnsafeScanRoot`：根路径被拒绝时的显式异常。
  抛异常而不是返回零值，因为"被拒绝"和"空目录"是两件不同的事实。
- `tests/test_security_v023.py`（15 项，A/B 两组，零 skip）。
- `docs/SECURITY_AUDIT_v0.2.2.md`、`docs/WORKBUDDY_EXECUTION_LOG_v0.2.3.md`。

### 变更

- `DiskScanner` 的四个公开入口现在可能抛 `UnsafeScanRoot`。
  调用方（诊断、`scan` 子命令）在传入链接根时会得到明确错误，而不是错误的数字。
- `_lock_is_free()` 移除：它是 P1-1 缺陷的载体，保留只会被再次误用。

### 测试

- 测试总数 200 → 215（通过 215，跳过 1，跳过项仍是 `test_policy.py` 的真实符号链接）。
- 既有 200 项一条未删、未放宽。
- 变异检验：同时回退两处修复后，10 项按预期失败（含核心用例
  `test_reconcile_rechecks_state_under_lock`），去掉变异后全部恢复通过。
- `ruff check src tests` 零告警；`compileall` 退出码 0。

### 未改变

- TOCTOU 仍未消除。
- 仍不实现永久删除；回收站不可用即失败关闭。
- 回收站 ≠ 释放空间的三指标口径不变。

---

## [0.2.2] — 2026-11-05

收敛执行路径的补丁版本。v0.2.1 解决了"确认哪一份计划"，本版解决"谁在执行、
执行几次、崩溃后怎么办"。全部改动附回归测试，明细见
`docs/SECURITY_AUDIT_v0.2.1.md`。

### 安全修复

- **TrashManager 是绕过闸门的写入入口**：删除公开的 `move()` 与 `quarantine()`，
  改为只读观测（`is_available()` / `backend_info()`）。`ToolRegistry` 在
  `llm_facing=True` 时**拒绝注册**写工具，`safe_executor.execute` 从注册表移除。
  执行只剩 `DiskDoctorAgent.execute_plan` 一个入口。
- **审批凭证可并发重放**：`ConfirmationAuthority.verify_and_consume()` 把校验与
  作废放进同一临界区；`SafeExecutor` 在第一次移动之前消费凭证，
  不再有 verify → move → consume 的窗口。
- **同一计划可被多进程同时执行**：`PlanStore.claim_for_execution()` 用
  进程内 `threading.Lock` + 跨进程 `portalocker` 锁，`LOCK_NB`，拿不到立即
  `plan_locked`。锁文件释放后不删除（避免同名不同 inode）。
- **崩溃后的计划可重放**：新增持久化状态机 `READY / IN_PROGRESS / COMPLETED /
  FAILED / INTERRUPTED`。`IN_PROGRESS` 在首次移动前落盘；下次领取时若无人持锁，
  标记 `INTERRUPTED` 并拒绝，**不自动重试**。
- **扫描器各自实现链接判断**：`core/utils.is_symlink_or_reparse()` 成为唯一实现，
  `DiskScanner` / `DuplicateFinder` / `CacheAnalyzer` / `path_size` /
  `collect_files` / `policy_engine` 全部复用。此前扫描器只判断 `islink`，
  一个 Windows junction 就能让它遍历到别的卷。
- **隔离区同名文件互相覆盖**：唯一名（时间戳 + 随机后缀）+
  `O_CREAT|O_EXCL` 预留 + `os.replace` 原子替换，全程持锁；跨卷退化为
  copy → fsync → replace → unlink，失败保留源文件。

### 变更

- `SCHEMA_VERSION` 3 → 4。v0.2.1 的计划无法执行，需重新生成。
- 新增运行时依赖 `portalocker>=2.8`；缺失时执行器**失败关闭**
  （`lock_backend_unavailable`），不退化为无锁运行。
- `SafeExecutor.execute(plan, receipt)` 内部改为 `verify_and_consume`；
  `Approver` 协议不再提供分离的 `verify` / `consume`。
- `TrashManager.describe()` → `backend_info()`。
- `capabilities()` 增加 `write_tools`（恒为空）与 `execution_entry`。
- 隔离区的锁文件放在隔离目录**同级**（`<dir>.lock`），不污染目录内容。

### 测试

- 新增 `tests/test_security_v022.py`（37 项，A–F 六组），含 `threading`
  与 `multiprocessing(spawn)` 的真实并发用例；无 skip，CI 会因出现 skip 而失败。
- 新增 `tests/v022_worker.py`（子进程 worker，spawn 可导入）。
- 测试总数 163 → 200（通过 200，跳过 1）。
- 三个断言旧注册表契约的测试被改写为断言新契约（零写工具 + 唯一执行入口）。
- `ruff check src tests` 零告警；`compileall` 退出码 0。

### 文档

- 新增 `docs/SECURITY_AUDIT_v0.2.1.md`（六项缺陷的触发条件、修复、测试、残余风险）。
- 新增 `docs/WORKBUDDY_EXECUTION_LOG_v0.2.2.md`。
- README 的"四道闸门 / 五条硬约束"更新为 v0.2.2 实际实现；
  SECURITY.md 威胁模型新增 T13–T17。

---

## [0.2.1] — 2026-11-05

安全补丁版本。修复四项 P0 与两项 P1，全部附回归测试。详见
`docs/SECURITY_AUDIT_v0.2.0.md`。

### 安全修复（P0）

- **父目录链接逃逸**：新增 `PolicyEngine.validate_path_chain()`，逐级检查
  `required_root` 到目标之间的每个目录组件，要求
  `realpath(target) ∈ realpath(plan.root)`；`verdict()` 增加 `required_root` 参数。
  执行器新增整份计划的 `preflight`，任一文件不合格即在首次移动前阻断。
- **Temp/tmp 泛化识别**：删除按名称匹配 `Temp` / `temp` / `tmp` 的 SAFE 规则。
  仅保留 `__pycache__`（且只接受 `.pyc`）与带 `CACHEDIR.TAG` 注册签名的
  `.pytest_cache` / `.ruff_cache`；其他类别默认不产生候选。
- **plan_id 确认未绑定**：`CleanPlan` 移除 `dry_run`，`SCHEMA_VERSION` 升到 `3`，
  旧计划拒绝加载。新增 `PlanStore`（原子写入、内容寻址、过期与消费校验）与
  `ConfirmationAuthority`（HMAC + nonce + 有效期，必须输入完整
  `EXECUTE <plan_id>` / `SECOND <plan_id>`）。`SafeExecutor.execute(plan, receipt)`
  取消 `confirm` 参数。CLI 改为 `plan` / `execute` 两阶段，非交互终端拒绝执行。
- **Quarantine 探测覆盖**：改用 `tempfile.mkstemp` 独占创建随机文件名，
  只清理本次创建的文件。

### 变更（P1）

- **空间统计**：拆分为 `estimated_bytes` / `bytes_moved_to_trash` /
  `available_space_change_bytes`；不再把移入回收站说成"释放空间"；
  成就措辞改为"累计移入回收站"。
- **Windows 路径解析**：改用 `ntpath` / `posixpath`，解析结果与宿主平台无关。

### 兼容性

- **破坏性变更**：v0.2.0 的计划无法执行（schema 版本不匹配），必须重新生成。
- **破坏性变更**：`SafeExecutor.execute()` 签名变化；`clean --no-dry-run --yes`
  不再具备执行能力。
- 新增 CLI：`execute --plan-id`；`plan` 新增 `--allow-optional-cache`。

### 测试

- 新增 `tests/test_security_v021.py`（50 项，覆盖 A–J 六组）。
- 测试总数 115 → 163（通过 163，跳过 1）。
- `ruff check src tests` 零告警；`compileall` 退出码 0。

---

## [0.2.0] — 2026-11-05

安全加固版本。上一版存在范围校验与计划完整性方面的缺陷，本版逐项修复，
并把每项修复固化为一个会失败的回归测试。

### 安全修复

- **BUG-01 扫描范围越界**：分析器按 `scope_root` 绑定，计划构建阶段再校验
  每个候选与每个文件都在 root 之内。新增 `outside_scope_root` 拒绝规则。
- **BUG-02 执行阶段缺少 root 约束**：执行前逐文件复检——在 `plan.root` 内、
  在条目目录内、父目录仍匹配缓存签名、在授权根内、存在、是普通文件、
  非符号链接、非重解析点，且 `size_bytes`/`mtime_ns`/`device`/`inode` 与计划一致。
- **BUG-03 预览不完整**：计划改为文件级，预览逐行列出行号、完整路径、大小、
  风险等级、拟执行动作、当前状态；执行前再打印一次完整清单。
- **BUG-04 计划 ID 过短**：改为对规范投影做 SHA-256（64 位十六进制），
  覆盖 schema/policy 版本、root、dry_run、总量、确认要求、每个文件的元数据。
- **BUG-05 计划无有效期**：默认 15 分钟 TTL，过期拒绝执行，需重新生成。
- **BUG-06 路径字符串前缀比较**：改为路径组件级判断（`relpath`），
  `D:\work` 不再授权 `D:\workshop`；跨盘符失败关闭。
- **确认语义收紧**：会话内同一时刻只允许一个有效确认；执行后清空；
  计划内容变化即导致 ID 变化，旧确认自动失效。
- **模型权限固化**：新增 `agent/llm_contract.py`，意图枚举封闭、
  `extra="forbid"`、响应校验失败即抛错；模型不持有任何文件句柄。

### 变更

- 计划单位从"目录"改为"文件"；空目录保留不删。
- 受保护后缀扩展：压缩包、视频、常见源码后缀默认不清理。
- `plan` 子命令需要 `--allow-root`（与 `clean` 一致）。
- 新增 `SafeExecutor(cleanable=...)` 注入点，执行期可复核签名匹配。

### 测试

- 新增 `tests/test_security_regression.py`，16 项安全回归用例逐条对应审计项。
- 测试总数 94 → 115（通过 115，跳过 1）。
- 引入 Ruff，`ruff check src tests` 零告警。

### 文档

- 新增 `SECURITY.md`（威胁模型、权限范围、TOCTOU 限制、残余风险）。
- 新增 `environment.yml`（Python 3.11，依赖以 pyproject.toml 为准）。
- README 增补安全机制、Conda 安装、计划有效期与已知限制。

### 未改变

- 仍不实现永久删除；回收站不可用时失败关闭。
- 仍不给出空间耗尽预测（无历史采样）。

---

## [0.1.0] — 2026-11-05

首个可运行版本。功能完整，测试通过，但**不声明生产可用**。

### 新增

**核心**
- `PolicyEngine`：路径白名单。规范化在判断之前，符号链接在解析之前拦截，受保护组件/后缀/文件名三层过滤。
- `CleanPlan` + `PlanBuilder`：过滤、去重、排序、截断，被拦截项保留在 `rejections` 中可审计。
- `SafeExecutor`：唯一写入口。四道闸门（授权 / 确认 / 阈值 / 回收站可用性）+ 逐项复检。
- `Validator`：计划与实测对账，残留扣除，异常放大时主动报警。
- `AuditLogger`：JSONL 审计，移动前后各一条，懒创建目录。

**能力模块**
- `DiskScanner`：卷用量、目录体积、大文件 Top N，只读。
- `CacheAnalyzer`：签名匹配识别缓存，深度 ≤ 4，未识别不产出。
- `DuplicateFinder`：大小预筛 → 64 KiB 哈希 → 全量 SHA-256。
- `TrashManager` + 双后端（`send2trash` / `QuarantineBackend`）。
- `AchievementStore`：四档里程碑，写失败静默忽略。

**Agent**
- 规则式 `IntentRouter`：12 条优先级规则，破坏性与越界排最前。
- `ContextManager`：槽位、否决台账、**确认绑定到 plan_id**。
- `Orchestrator`：状态机，越界三段式拒绝，部分失败不回滚。
- `MockLLM` + `OpenAICompatibleClient`（默认 DeepSeek）。

**报告**
- 诊断、预览、完成三套模板，强制区分实测与估算。
- 明确不给出空间耗尽预测，并在报告中说明原因。

**工程**
- CLI 8 个子命令，安全标志在参数层校验。
- 93 个测试通过，1 个跳过（Windows 符号链接权限）。
- GitHub Actions：Ubuntu/Windows × Python 3.11/3.12/3.13，含硬编码密钥检测。

**文档**
- 架构恢复方法与证据分级（OBSERVED / SELF-REPORTED / INFERRED / SUBSTITUTED）。
- 8 篇架构文档 + 运行记录。
- `known_unknowns.md`：9 项 UNKNOWN、6 项替代设计、5 个已知缺陷。

### 已知问题

- 硬链接重复计数，释放量可能偏高（D1）。
- 稀疏文件按表观大小计算（D2）。
- 扫描深度固定 4 层（D3）。
- 计划与执行之间存在并发修改窗口（D4）。
- Windows 真实符号链接测试因权限被跳过，由 monkeypatch 测试覆盖分支（D5）。

### 未实现

- 跨会话记忆（隐私成本高于收益）。
- 空间耗尽预测（无历史采样，不编造）。
- 自动重试失败的删除（会破坏幂等性）。

---

## 版本约定

| 变更类型 | 版本位 |
|---------|--------|
| 安全行为变化（即使更宽松） | MAJOR |
| 新增能力模块、CLI 参数 | MINOR |
| 修复、文档、测试 | PATCH |

**安全行为变化一律升 MAJOR**，包括"放宽"的情况。使用者需要能通过版本号判断是否需要重新评估。

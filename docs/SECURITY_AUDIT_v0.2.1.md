# 安全审计 —— v0.2.1 遗留问题与 v0.2.2 修复

审计对象：`disk-doctor-reference` v0.2.1（提交时的 73 文件状态）
审计方式：源码审查 + 针对每项缺陷编写可失败的回归测试
审计范围：从「用户确认」到「文件离开原位置」的整条路径

本文只记录**已经在 v0.2.2 中改掉的**问题。没有改的、以及无法在本项目层面
消除的风险，单列在 §7，不假装已经解决。

---

## 1. TrashManager 是一个绕过全部闸门的写入入口（P0）

**原始问题**

`tools/trash_manager.py` 对外暴露 `TrashManager.move(path)`，内部直接调用
`TrashBackend.move()`。它不经过 `PolicyEngine`、不读 `PlanStore`、不需要
`ConfirmationAuthority` 的凭证、也不触发 `SafeExecutor` 的任何预检。

同时 `TrashManager.quarantine(directory)` 是一个公开类方法，可以凭空构造一个
隔离区后端。

**触发条件**

任何持有 `TrashManager` 实例的代码路径（插件、后续新增的子命令、一次不小心的
重构）都可以直接移动用户文件。这条路径不需要用户输入 plan_id，也不需要终端。

**修复方法**

- `TrashManager` 只保留 `backend_name` / `is_available()` / `backend_info()`；
  `move()` 与 `quarantine()` 被删除，不是改成私有方法再留着。
- `ToolRegistry` 增加 `llm_facing` 约束：面向模型的注册表**拒绝注册**写工具
  （`read_only=False` 直接抛 `ValueError`）。
- `safe_executor.execute` 从注册表移除。执行不再是"一个工具"，而是
  `DiskDoctorAgent.execute_plan` 这个需要已存计划 + 一次性凭证的状态转换。
- `TrashBackend` 仍然有 `move()`，但它是 `SafeExecutor` 的构造依赖，全包只有
  `core/safe_executor.py` 调用它（由静态扫描测试守住）。

**回归测试**

`tests/test_security_v022.py::test_trash_manager_has_no_public_move`
`::test_trash_manager_cannot_quarantine_directly`
`::test_only_safe_executor_can_write_user_data`
`::test_llm_registry_contains_no_write_tools`
`::test_trash_backend_not_exposed_as_agent_tool`
`::test_registry_rejects_write_tools_at_registration`

**残余风险**

Python 没有真正的私有成员。同进程内持有 backend 对象的代码可以直接调用
`move()`。这里获得的是**架构层**保证：不存在意外的调用点、不存在注册表入口、
不存在模型可触发的动作。它不是对抗本机恶意代码的沙箱，文档不再暗示相反结论。

---

## 2. 审批凭证可被并发重放（P0）

**原始问题**

v0.2.1 的执行顺序是：

```
verify(receipt) → preflight → 逐文件移动 → consume(receipt)
```

凭证在文件操作**之后**才作废。两个并发执行请求可以同时通过 `verify`：
两个都看到 nonce 未使用，两个都进入移动阶段。

**触发条件**

- 同一进程两个线程同时调用 `execute(plan, receipt)`；
- 两个进程同时执行同一 plan_id（例如两次 `disk-doctor execute`）。

`PlanStore` 在 v0.2.1 只有 `.consumed` 标记，且是在执行**结束后**才写。

**修复方法**

四层，缺一层都不成立：

1. **进程内互斥**：`threading.Lock` per plan_id，`acquire(blocking=False)`，
   拿不到立刻 `plan_locked`。
2. **跨进程锁**：`portalocker` 独占锁 `plans/locks/<plan_id>.lock`，
   `LOCK_NB`，拿不到立刻 `plan_locked`。锁文件释放后**不删除**——删了会让两个
   进程锁住同名的不同 inode。
3. **持久化状态机**：`READY → IN_PROGRESS → COMPLETED | FAILED`，
   状态写在 `plans/state/<plan_id>.state`，与计划本体分离（计划 digest 不变）。
   `IN_PROGRESS` 在**第一次移动之前**落盘。
4. **凭证原子消费**：`ConfirmationAuthority.verify_and_consume()` 在同一个
   临界区里完成"校验签名/有效期/二次确认"和"把 nonce 记为已用"。执行器改成
   `verify_and_consume` 之后才继续，不存在 verify 与 consume 之间的窗口。

锁的获取顺序固定为：`reconcile → thread lock → file lock → load → 状态检查
→ 写 IN_PROGRESS → 消费 nonce → 才允许移动`。

**回归测试**

`::test_same_receipt_concurrent_execution`（threading，断言移动次数 = 1）
`::test_same_plan_concurrent_execution`（multiprocessing spawn，4 进程，
断言成功领取 = 1）
`::test_plan_lock_rejects_second_process`（断言错误码恰为 `plan_locked`）
`::test_plan_claim_marks_in_progress_before_first_move`
`::test_receipt_consumed_before_first_move`
`::test_completed_plan_cannot_be_claimed_again`
`::test_failed_execution_cannot_reuse_receipt`

**残余风险**

- `portalocker` 不可用时执行器**拒绝执行**（`lock_backend_unavailable`），
  不会退化为"无锁运行"。
- 锁只能约束协作进程。一个不经过 `PlanStore` 的进程仍然可以自己写文件。
- 状态文件与锁文件在同一个应用数据目录；能改写该目录的攻击者可以伪造状态。

---

## 3. 崩溃后的 IN_PROGRESS 计划（P0）

**原始问题**

v0.2.1 没有执行状态，进程在执行中途被杀掉后，计划看起来仍然"未消费"，
可以再次执行——而没人知道第一次已经移动了多少文件。

**触发条件**

执行中崩溃、断电、任务管理器结束进程、CI 超时。

**修复方法**

- `IN_PROGRESS` 是持久化状态，且**不是终态**。
- 下一次领取时，若状态为 `IN_PROGRESS` 而锁是空闲的，说明持有者已死：
  标记为 `INTERRUPTED` 并抛出 `plan_interrupted`，**不自动重放**。
- `INTERRUPTED` 是终态，再次领取得到 `plan_consumed`。
- 主动调用 `PlanStore.reconcile_interrupted()` 可以批量清理孤儿状态。

**回归测试**

`::test_plan_in_progress_cannot_be_replayed`
`::test_interrupted_plan_rejected`
`::test_reconcile_interrupted_marks_orphans`

**残余风险**

"锁空闲"是判断持有者已死的唯一依据。如果持有者只是被挂起（调试器断点、
系统休眠）而锁句柄仍被内核保留，该计划不会被误判；反之若锁被操作系统强制回收，
计划会被判为中断——这个方向的误判是安全的（拒绝执行），不是危险的。

---

## 4. 扫描模块各自实现链接判断（P0）

**原始问题**

`is_reparse_point()` 只存在于 `policy_engine.py`。`DiskScanner` 与
`DuplicateFinder` 用的是 `os.path.islink` / `entry.is_symlink()`，
**不检查 Windows junction**。一个名为 `cache` 的 junction 可以指向任意卷，
扫描器会把它当成普通目录遍历。

`path_size()` 同样只判断 `is_symlink`，因此 junction 目标的体积会被算进来，
预估值与实际处理量对不上。

**触发条件**

`D:\data\cache` 是指向 `E:\` 的 junction，扫描根为 `D:\data`。

**修复方法**

- `core/utils.py` 提供唯一实现：`is_reparse_point()` 与
  `is_symlink_or_reparse()`（POSIX 看 `S_ISLNK`，Windows 看
  `FILE_ATTRIBUTE_REPARSE_POINT`）。
- `policy_engine` 改为从 `utils` 导入并 re-export，不再有第二份。
- `DiskScanner.directory_sizes` / `top_files`、`DuplicateFinder._collect`、
  `CacheAnalyzer.discover` / `has_valid_cachedir_tag`、`collect_files`、
  `path_size` 全部改用同一个判断。
- `path_size_report()` 返回 `skipped_redirects`，让"少算了多少"可见，
  而不是悄悄返回 0。

**回归测试**

`::test_disk_scanner_skips_symlink`
`::test_disk_scanner_skips_reparse_directory`
`::test_path_size_skips_reparse_directory`
`::test_duplicate_finder_skips_symlink`
`::test_duplicate_finder_skips_reparse_directory`
`::test_cache_analyzer_uses_shared_path_guard`
`::test_policy_rejects_reparse_root`

**残余风险**

本机没有 `SeCreateSymbolicLinkPrivilege`，无法创建原生符号链接，也无法创建
junction。相关用例在无法原生创建时改用 monkeypatch 命中同一分支，并在测试名
与注释中标明"模拟"。**这些用例不能作为"真实 Windows junction 已验证"的证据**，
只证明分支逻辑正确。

---

## 5. 隔离区文件名竞争导致覆盖（P1）

**原始问题**

```python
while target.exists():
    counter += 1
shutil.move(source, target)
```

`exists()` 与 `move()` 之间是典型的 check-then-act。两个执行者（或两个线程）
可以选中同一个空闲名字，后一个覆盖前一个——被覆盖的是**已经移入隔离区的用户文件**。

**触发条件**

两个同名文件在相近时间被移入同一隔离目录。

**修复方法**

- 目标名 = `原名__<UTC 时间戳>-<8 位随机十六进制><后缀>`。
- 用 `os.open(..., O_CREAT | O_EXCL)` **预留**目标名：创建成功才算拿到名字，
  失败就换一个。预留出来的空文件是自己创建的，随后用 `os.replace` 原子替换它。
- 整个「选名 + 占位 + 替换」在一把锁内完成：进程内 `threading.Lock`
  + 跨进程 `portalocker`（锁文件是隔离目录的**同级**文件，不污染隔离目录内容）。
- 跨卷时退化为「复制到临时文件 → fsync → 原子替换 → 删除源文件」；任一步失败
  都保留源文件，并删除本次创建的占位。
- 只为普通文件服务；目录与链接直接拒绝。
- 删除源文件失败时抛错（由执行器记为 FAILED 并写审计），而不是静默留下两份。

**回归测试**

`::test_quarantine_preserves_existing_target`
`::test_quarantine_collision_does_not_overwrite`
`::test_quarantine_concurrent_same_filename`
`::test_quarantine_copy_failure_preserves_source`
`::test_quarantine_partial_failure_logged`
`::test_quarantine_rejects_directories`

**残余风险**

锁只约束协作实例。不经过 `QuarantineBackend` 的第三方进程仍可往该目录写文件；
`os.replace` 保留占位名期间的窗口由 `O_EXCL` 与锁共同覆盖，但不是内核级事务。
Windows 上索引/杀毒服务可能短暂持有刚创建的文件句柄，`os.replace` 因此加了
有界重试（5 次，指数退避），重试失败仍按失败处理。

---

## 6. 工具注册表与实际接口不一致（P1）

**原始问题**

注册表里 `safe_executor.execute` 声明的输入是
`{"plan": "CleanPlan", "confirm": "bool", "second_confirm": "bool"}`，
而 v0.2.1 的 `SafeExecutor.execute(plan, receipt)` 早就不接受 `confirm`。
`planner.plan` 也还声明着 `dry_run`。

一个把模型输出映射到注册表的适配层，会按声明构造 `confirm=True` 并得到
`TypeError`——或者更糟，被后来的人"修好"成真的接受布尔确认。

**修复方法**

- 删除 `safe_executor.execute` 条目（见 §1）。
- `planner.plan` 的声明改为真实参数。
- 新增测试：注册表声明的每个输入名都必须出现在真实签名里（或有 `**kwargs`）。
- 新增测试：用 `ast` 遍历源码，禁止出现名为 `confirm` / `second_confirm` /
  `require_confirmation` 的**形参或实参**。用 AST 而不是 grep，是因为文档里
  正当地讨论着这个已被删除的布尔值；文本扫描会逼人删掉解释。

**回归测试**

`::test_tool_registry_matches_real_signatures`
`::test_no_stale_confirm_boolean_api`
`::test_llm_has_no_execution_tools`
`::test_llm_cannot_access_confirmation_authority`

---

## 7. 本轮未解决 / 无法在本项目层面解决

1. **TOCTOU 仍然存在。** 预检（整份计划）+ 复检（每个文件移动前）把窗口
   缩到"最后一次 `lstat` 到 `os.replace` 之间"，但两者不是原子操作。
   有能力在该窗口内替换文件的本地攻击者仍然可以得手。
2. **`plan_id` 是内容指纹，不是数字签名。** 能改写计划并重算 SHA-256 的本地
   攻击者可以绕过摘要校验。它防的是意外改动与半懂不懂的篡改。
3. **HMAC 凭证不防本机恶意代码。** 密钥在进程内存里，能执行同进程代码就能读到。
   它防的是凭证被误用、被重放、被跨计划挪用。
4. **回收站 ≠ 释放空间。** 文件进入回收站后仍占用磁盘。本项目把三个数字分开
   报告：预计处理大小 / 实际移入回收站 / 磁盘可用空间实测变化（可为负）。
5. **硬链接与稀疏文件。** 同一 inode 的多个路径会分别计入 `FileRef`，
   稀疏文件按表观大小统计。释放量因此可能偏高。未修。
6. **真实回收站未做端到端验证。** 所有测试使用 `QuarantineBackend` 或
   Fake 后端，刻意不污染用户回收站。
7. **macOS 未验证。** 未进入 CI 矩阵，文档也不声称支持。
8. **本项目不是经过独立渗透测试的生产级安全软件。** 它是可审查、可测试的
   参考实现；在受控环境之外使用前，请自行评估。

---

## 8. 一句话结论

v0.2.2 把「移动用户文件」这件事收敛为**一个入口、一把锁、一个终态**：
入口是 `SafeExecutor`，锁是 `PlanStore` 的跨进程执行锁，终态是
`COMPLETED / FAILED / INTERRUPTED` 三者之一且不可回退。
剩下的风险集中在本地同进程攻击与 TOCTOU，这两项在本项目的能力边界之外，
已在 §7 明示。

# v0.2.2 执行日志

执行时间：2026-11-05
执行范围：`D:\githubproject\disk-doctor-reference`
基线上限：v0.2.1（ZIP SHA-256 `10d8264f…f57ca81`）

本日志只记录**实际执行过**的内容。没有执行的项标注 NOT VERIFIED，不补写。

---

## 第一步：只读前置检查

```
PROJECT    : True
SOURCE_ZIP : True（SHA-256 = 10D8264F…AF57CA81，与预期一致）
ENV_PATH   : True（D:\conda_envs\disk-doctor-reference）
BACKUP_EXISTS (must be False): False
Python 3.11.18 / pytest 9.2.0 / ruff 0.17.3 / pydantic 2.14.3
portalocker: 未安装（本步只读，不改环境）
D 盘可用: 62.5 GB
项目文件: 73
```

结论：全部满足，进入备份。

---

## 第二步：备份

```
robocopy D:\githubproject\disk-doctor-reference \
         D:\githubproject\_backups\disk-doctor-reference-pre-v0.2.2 \
         /E /COPY:DAT /R:1 /W:1
退出码: 1（0–7 为成功区间）
源文件（统一过滤后）: 73
备份文件            : 73
差异                : 0
Python 文件哈希核对 : 46 个，0 不一致
```

备份目录此前不存在，未覆盖任何已有备份。原始 v0.2.1 ZIP 未修改。

---

## 第三步：P0-1 —— 关闭 TrashManager 独立写入入口

**修改文件**

| 文件 | 改动 |
|---|---|
| `src/disk_doctor/tools/trash_manager.py` | 重写为只读；删除 `move()` / `quarantine()`；保留 `backend_name` / `is_available()` / `backend_info()` |
| `src/disk_doctor/tools/registry.py` | 增加 `llm_facing`；面向模型时拒绝注册 `read_only=False` 的工具；新增 `llm_tools()` / `write_tools()` / `assert_read_only()` |
| `src/disk_doctor/agent/orchestrator.py` | 移除 `safe_executor.execute` 注册；`capabilities()` 增加 `write_tools` / `execution_entry`；`_trash_info` 改用 `TrashManager` |
| `src/disk_doctor/cli.py` | `trash` 子命令改用 `backend_info()` |

**测试**

新增 6 项（见 `tests/test_security_v022.py` §A）。原有 3 个断言"注册表里恰好有一个写工具"的测试被改写为"注册表里零个写工具 + 唯一执行入口"——这是契约变更，不是放宽：
- `test_agent.py::test_only_one_tool_writes` → `test_registry_exposes_no_write_tools`
- `test_cli.py::test_tools_lists_capabilities` → 改为断言 `safe_executor.execute` **不**出现
- `test_security_regression.py::test_only_one_user_data_writer` → 改为"注册表零写工具 + 全包只有 `core/safe_executor.py` 调用 `trash.move`"

---

## 第四步：P0-2 —— 跨进程执行锁与持久化状态

**修改文件**

| 文件 | 改动 |
|---|---|
| `src/disk_doctor/core/schemas.py` | 新增 `ExecutionState`（READY/IN_PROGRESS/COMPLETED/FAILED/INTERRUPTED）；`SCHEMA_VERSION` 3 → 4 |
| `src/disk_doctor/core/clean_plan.py` | `PlanStore` 增加 `state/` 与 `locks/` 目录、`read_state` / `_write_state` / `reconcile_interrupted` / `claim_for_execution`；新增 `PlanClaim` |
| `src/disk_doctor/agent/orchestrator.py` | `execute_plan` 改为 `with plan_store.claim_for_execution(...)`，按结果置 COMPLETED / FAILED |
| `pyproject.toml` | 新增运行时依赖 `portalocker>=2.8` |

**顺序（`claim_for_execution` 内部）**

```
validate_id → reconcile_interrupted → thread lock → file lock
→ load（schema/digest/过期/已消费）→ 状态检查（IN_PROGRESS → INTERRUPTED）
→ 写 IN_PROGRESS → 写 .consumed → yield
```

**环境操作**

```
pip install --index-url https://pypi.org/simple portalocker==4.4.2
（只装进 D:\conda_envs\disk-doctor-reference，未改全局配置）
```

**测试**：8 项（§B），含 threading 与 multiprocessing(spawn)。

---

## 第五步：P0-3 —— 凭证原子消费

**修改文件**

| 文件 | 改动 |
|---|---|
| `src/disk_doctor/core/clean_plan.py` | `ConfirmationAuthority` 增加 `threading.Lock`；新增 `verify_and_consume()` / `is_consumed()` |
| `src/disk_doctor/core/safe_executor.py` | `Approver` 协议改为只要求 `verify_and_consume`；`execute()` 在第 5 步消费凭证，删除末尾的 `consume()` |

**测试**：`test_receipt_consumed_before_first_move`、`test_same_receipt_concurrent_execution`、`test_failed_execution_cannot_reuse_receipt`。

---

## 第六步：P1-1 —— 统一 Reparse Point 检查

**修改文件**

| 文件 | 改动 |
|---|---|
| `src/disk_doctor/core/utils.py` | 新增 `is_reparse_point` / `is_symlink_or_reparse` / `SizeReport` / `path_size_report` |
| `src/disk_doctor/core/policy_engine.py` | 删除本地实现，改为从 `utils` 导入并 re-export |
| `src/disk_doctor/tools/disk_scanner.py` | `directory_sizes` / `top_files` 改用统一判断 |
| `src/disk_doctor/tools/duplicate_finder.py` | `_collect` 改用统一判断 |
| `src/disk_doctor/tools/cache_analyzer.py` | 改用 `is_symlink_or_reparse`，不再单独依赖 `is_reparse_point` |

**测试**：7 项（§C）。

---

## 第七步：P1-2 —— 隔离区命名冲突

**修改文件**：`src/disk_doctor/core/safe_executor.py`

**做法**

- 目标名带时间戳与 8 位随机十六进制；
- `os.open(O_CREAT|O_EXCL)` 预留，`os.replace` 原子替换自己的占位；
- 进程内 `threading.Lock` + 跨进程 `portalocker`（锁文件为隔离目录的同级文件）；
- 跨卷退化为 copy → fsync → replace → unlink；
- `os.replace` 对 Windows 瞬时 `PermissionError` 做 5 次指数退避重试。

**测试**：6 项（§D）。

---

## 第八步：P1-3 —— 注册表与接口一致

见第三步（注册表移除执行工具）与 §E 的 4 项测试。`planner.plan` 的 `dry_run` 声明一并删除。

---

## 开发过程中由测试抓出的真实缺陷

这三个都不是"测试写错了"，是实现的缺陷：

### 缺陷 1：Windows 上第二个线程打开锁文件直接 EACCES

`QuarantineBackend._exclusive()` 原本只用 `portalocker.Lock`。Windows 上
`msvcrt.locking` 锁定字节区间后，同进程另一个线程 `open()` 同一个锁文件会直接
抛 `PermissionError`，portalocker 连"已被锁定"都来不及报。

表现：`test_quarantine_concurrent_same_filename` 4 线程中 3 个抛
`PermissionError`，隔离区只进了 1 个文件。

修复：进程内 `threading.Lock`（按锁文件路径注册）+ 文件锁，两层并用。

### 缺陷 2：`PlanStore._acquire_file_lock` 的 `open()` 在 try 之外

第二个进程 `open()` 锁文件时同样可能拿到 `PermissionError`，而这一行不在
`try` 里，异常会原样抛给调用方（子进程报告 `("error", "PermissionError: …")`），
而不是干净的 `plan_locked`。

表现：`test_same_plan_concurrent_execution` 偶发失败，4 个子进程中有一个不是
"rejected"。

修复：把 `open()` 纳入 `try`，任何 `OSError` 都转成
`PlanStoreError("plan_locked")`。

### 缺陷 3：`state/` 目录只在首次写入时创建

`PlanStore.state_path()` 在 `state/` 不存在时返回一个不可写的路径，测试里
直接写状态会 `FileNotFoundError`。虽然产品路径不会遇到（`_write_state` 会建目录），
但这是一个"读状态前必须保证目录存在"的隐含契约。

修复：`PlanStore.__init__` 显式创建 `state/` 与 `locks/`。

---

## 变异检验（证明并发测试有效，非摆设）

用一个临时 pytest 插件把 `PlanStore._acquire_file_lock` 换成空操作：

```
PYTHONPATH=D:\tmp\disk-doctor-v0.2.2 pytest tests/test_security_v022.py -p mutate_lock -k lock_rejects
→ FAILED test_plan_lock_rejects_second_process（子进程得到 plan_interrupted 而非 plan_locked）
```

去掉变异后该用例恢复通过。说明它确实在检验锁，而不是在检验"函数被调用了"。
插件未进入仓库（位于 `D:\tmp`，用后即弃）。

---

## 最终验证

```
$ python -m pytest -q
200 passed, 1 skipped in 3.41s

$ python -m ruff check src tests
All checks passed!

$ python -m compileall -q src tests examples
exit code 0

$ python -m disk_doctor --version
disk-doctor 0.2.2
```

连续 5 轮全量回归均为 `200 passed, 1 skipped`，用于确认并发用例不再偶发失败。

唯一跳过项：`tests/test_policy.py:69`（当前 Windows 账户无
`SeCreateSymbolicLinkPrivilege`）。该分支的扫描行为由 v0.2.2 的 monkeypatch
用例覆盖，且测试名/注释标明是模拟。

---

## 遗留

| 项 | 状态 |
|---|---|
| TOCTOU 残余窗口 | 未消除，`SECURITY.md` §6 与审计文档 §7 明示 |
| 真实系统回收站端到端 | NOT VERIFIED（刻意不污染用户回收站） |
| Windows 原生 junction / symlink 创建 | NOT VERIFIED（无权限），分支由 monkeypatch 覆盖 |
| macOS | NOT VERIFIED，未进 CI 矩阵 |
| 真实 DeepSeek 调用 | NOT VERIFIED（无 Key，全程 mock） |
| GitHub Actions 实际运行 | NOT VERIFIED（无 `.git`，未推送） |
| 硬链接重复计数、稀疏文件表观大小 | 未修（v0.2.1 遗留 D1/D2） |

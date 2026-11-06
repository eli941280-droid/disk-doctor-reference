# v0.2.3 执行日志

执行时间：2026-11-05
执行范围：`D:\githubproject\disk-doctor-reference`
基线上限：v0.2.2（ZIP SHA-256 `ddcb94ae…8068f85`）

只记录实际执行过的内容。未执行的标注 NOT VERIFIED，不补写。

---

## 第一步：只读前置检查

```
PROJECT    : True
SOURCE_ZIP : True（SHA-256 = DDCB94AE…8068F85，与预期一致）
ENV_PATH   : True（D:\conda_envs\disk-doctor-reference）
BACKUP_EXISTS (must be False): False
Python 3.11.18 / pytest 9.2.0 / ruff 0.17.3
portalocker 4.4.2 / pydantic 2.14.3
D 盘可用: 61.4 GB
项目文件: 77
```

全部满足，进入备份。

---

## 第二步：备份

```
robocopy D:\githubproject\disk-doctor-reference \
         D:\githubproject\_backups\disk-doctor-reference-pre-v0.2.3 \
         /E /COPY:DAT /R:1 /W:1
退出码: 1（0–7 为成功区间）
源文件（统一过滤后）: 77
备份文件            : 77
差异                : 0
Python 文件哈希核对 : 48 个，0 不一致
```

备份目录此前不存在。v0.2.2 ZIP 未修改。

---

## 第三步：P1-1 —— reconcile_interrupted 状态竞争

**修改文件**：`src/disk_doctor/core/clean_plan.py`

**改动**

- `reconcile_interrupted()` 保留一次读状态作为"哪些计划值得加锁"的筛选，
  真正的判断移入新的 `_recover_if_orphaned()`。
- `_recover_if_orphaned()`：线程锁（非阻塞）→ 文件锁（`LOCK_EX | LOCK_NB`）
  → **锁内重读** → 仍为 `IN_PROGRESS` 才写 `INTERRUPTED`。
- 任一锁获取失败立即返回 `False`，不写任何状态。
- 删除 `_lock_is_free()`：它是缺陷的载体（取锁后立刻释放），留着会被再次误用。

**锁顺序**与 `claim_for_execution` 一致（先线程锁、后文件锁），两处都是非阻塞，
不会互相死锁。

---

## 第四步：P1-2 —— 扫描入口拒绝链接根

**修改文件**

| 文件 | 改动 |
|---|---|
| `src/disk_doctor/core/utils.py` | 新增 `UnsafeScanRoot`；`collect_files()` 在 `os.walk` 前拒绝链接根，返回 `((), 0)` |
| `src/disk_doctor/tools/disk_scanner.py` | 新增 `require_safe_root()`；`volume` / `directory_sizes` / `top_files` / `scan` 在 `is_dir` / `disk_usage` / `scandir` / `walk` 之前调用 |
| `src/disk_doctor/tools/duplicate_finder.py` | `_collect()` 对每个 root 先判断，命中则跳过该 root |
| `src/disk_doctor/tools/cache_analyzer.py` | `discover()` 对每个 root 先判断，命中则跳过该 root |

`path_size_report()` 原有根检查保持不变（返回 `skipped_redirects=1`），本轮只补回归。

选择"抛异常"而不是"返回零值"的原因：零字节会让"被拒绝"看起来像"空目录"，
两者是完全不同的事实。`DuplicateFinder.find()` 与 `CacheAnalyzer.discover()`
接受多根，因此对单个 root 采取跳过而不是整体失败。

---

## 第五步：新增测试

`tests/test_security_v023.py`，15 项：

**A. 恢复逻辑（7 项）**

- `test_reconcile_does_not_overwrite_completed_state`
- `test_reconcile_does_not_overwrite_failed_state`
- `test_reconcile_rechecks_state_under_lock` ← 核心
- `test_reconcile_does_not_change_active_plan`（同进程持锁 + 另一实例文件锁）
- `test_reconcile_marks_only_orphaned_in_progress`
- `test_reconcile_is_fail_closed_without_lock_backend`

**B. 链接根（8 项）**

- `test_disk_scanner_rejects_symlink_root`
- `test_disk_scanner_rejects_reparse_root`
- `test_duplicate_finder_rejects_symlink_root`
- `test_duplicate_finder_rejects_reparse_root`
- `test_cache_analyzer_rejects_symlink_root`
- `test_cache_analyzer_rejects_reparse_root`
- `test_collect_files_rejects_symlink_root`
- `test_path_size_rejects_symlink_root`
- `test_linked_root_detection_uses_shared_guard`

（B 组 9 项，合计 15 项。）

**关于模拟**：本机没有 `SeCreateSymbolicLinkPrivilege`，无法创建真实符号链接或
junction。`_linked_root()` 先尝试 `Path.symlink_to()`；失败时创建一个**带内容的**
普通目录并把它标记为重解析点，命中的是同一条分支。测试在断言消息里带上
`mode=native|simulated`，不把模拟写成"原生已验证"。

**一处自我修正**：最初的模拟目录是空的。空目录下"返回空结果"在没有检查时也成立，
测试会对着有缺陷的代码通过。改为在伪根目录里放 `secret.bin` / 两份相同文件 /
`__pycache__`，使"拒绝"成为断言成立的**必要条件**。

---

## 第六步：变异检验

用临时 pytest 插件同时回退两处修复（`D:\tmp\disk-doctor-v0.2.3\mutate_v023.py`）：

```python
PlanStore._recover_if_orphaned = unlocked_recover      # 取锁→释放→裸写
disk_scanner.require_safe_root = lambda p: Path(p)
duplicate_finder.is_symlink_or_reparse = lambda p: False
cache_analyzer.is_symlink_or_reparse = lambda p: False
utils.is_symlink_or_reparse = lambda p: False
```

第一次变异**没有抓住** `test_reconcile_rechecks_state_under_lock`：我的"回退版"
里多留了一次 `read_state`，恰好起到了锁内重读的作用。改成忠实的 v0.2.2 序列
（锁只用来探测，不做重读）后：

```
FAILED test_reconcile_rechecks_state_under_lock
FAILED test_disk_scanner_rejects_symlink_root
FAILED test_disk_scanner_rejects_reparse_root
FAILED test_duplicate_finder_rejects_symlink_root
FAILED test_duplicate_finder_rejects_reparse_root
FAILED test_cache_analyzer_rejects_symlink_root
FAILED test_cache_analyzer_rejects_reparse_root
FAILED test_collect_files_rejects_symlink_root
FAILED test_path_size_rejects_symlink_root
FAILED test_linked_root_detection_uses_shared_guard
10 failed, 5 passed
```

剩下 5 项在变异下仍通过是预期的：它们检验的是"不覆盖终态""活着的计划不动"
"只标记孤儿""缺锁时失败关闭"，这些行为在 v0.2.2 里本来也成立，
本轮的缺陷只出现在"读与写之间"那一个交错上。

去掉变异后全部恢复通过。插件位于 `D:\tmp`，用后即删，未进入仓库。

---

## 第七步：验证

```
$ python -m pytest -q
SKIPPED [1] tests/test_policy.py:69: 当前环境不允许创建符号链接
215 passed, 1 skipped in 3.43s

$ python -m ruff check src tests
All checks passed!

$ python -m compileall -q src tests examples
exit code 0

$ python -m disk_doctor --version
disk-doctor 0.2.3
```

测试 200 → **215**。既有 200 项一条未删、未放宽。唯一 skip 与 v0.2.2 相同
（`tests/test_policy.py` 的真实符号链接创建），v0.2.3 新增套件**零 skip**。

示例：`examples/mock_demo.py` 与 `examples/basic_usage.py` 均正常输出。

---

## 遗留

| 项 | 状态 |
|---|---|
| TOCTOU（检查 → `os.replace` 窗口） | 未消除 |
| Windows 原生 symlink / junction | NOT VERIFIED（无权限），分支由模拟标记覆盖 |
| 真实系统回收站端到端 | NOT VERIFIED（刻意不污染用户回收站） |
| macOS | NOT VERIFIED，未进 CI |
| 真实 DeepSeek 调用 | NOT VERIFIED（无 Key，全程 mock） |
| GitHub Actions 实际运行 | NOT VERIFIED（无 `.git`，未推送） |
| 硬链接重复计数、稀疏文件表观大小 | 未修（v0.2.1 遗留） |
| 许可证 / 作者 / 仓库地址 | 仍为占位符 |

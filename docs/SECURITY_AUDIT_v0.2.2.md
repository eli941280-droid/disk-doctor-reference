# 安全审计 —— v0.2.2 遗留的两项发布阻断问题

审计对象：`disk-doctor-reference` v0.2.2（77 文件状态）
审计方式：源码审查 + 可失败的回归测试 + 变异检验
审计范围：计划恢复路径、扫描入口路径

这两项都不是"少写了一个检查"，而是**检查的位置不对**：一个在锁外做决定，
一个只检查了树内条目却放过了树根本身。

---

## 1. `reconcile_interrupted` 在锁外写状态（P1-1）

### 原始问题

v0.2.2 的恢复逻辑是三步分离：

```python
if self.read_state(plan_id) is not ExecutionState.IN_PROGRESS:
    continue
if self._lock_is_free(plan_id):          # 取锁 → 立刻释放
    self._write_state(plan_id, ExecutionState.INTERRUPTED)   # 裸写
```

`_lock_is_free()` 的设计本身就把锁释放了：它只回答"现在有没有人"，不负责
"接下来这段时间有没有人"。于是从释放锁到写入 `INTERRUPTED` 之间存在窗口。

### 触发条件

1. 计划状态为 `IN_PROGRESS`（可能是上一轮崩溃留下的，也可能**正在执行**）；
2. 恢复程序读到 `IN_PROGRESS`，试探锁，发现空闲，释放；
3. 真正的执行进程恰好在此刻完成，写入 `COMPLETED`；
4. 恢复程序把 `COMPLETED` 覆盖成 `INTERRUPTED`。

后果不是数据损坏——文件已经按计划移动完了——而是**审计记录撒谎**：
一次成功执行被记成"状态不可知"。之后任何依赖状态的判断都建立在这个假事实上。

另一种更糟的方向：如果第 3 步是执行进程正在写 `IN_PROGRESS` 并准备开始移动，
恢复程序可能把一个**活着**的执行者标记为中断，进而阻塞它。

### 修复方法

把"读—判—写"收进一个持锁事务，锁顺序与 `claim_for_execution` 完全一致：

```python
thread lock (non-blocking)
  → file lock (EX | NB)
    → 重新读取状态
      → 仍为 IN_PROGRESS ? 写 INTERRUPTED : 原样返回
  → 释放 file lock
→ 释放 thread lock
```

要点：

- **锁内重读**。函数开头那次 `read_state` 只用于筛掉明显不是 IN_PROGRESS 的计划，
  属于优化，不是决策依据。
- 任一锁获取失败即返回 `False`，**不写任何东西**。活着的执行者持有锁，恢复程序
  自然退让。
- 只可能写 `INTERRUPTED`，且仅在重读仍是 `IN_PROGRESS` 时。`COMPLETED` /
  `FAILED` / `READY` / `INTERRUPTED` 一律不动。
- 锁顺序固定（先线程锁、后文件锁），两处获取都是非阻塞，不会与
  `claim_for_execution` 形成死锁。
- `portalocker` 不可用时 `reconcile_interrupted` 直接返回 `[]`，失败关闭。
- `_lock_is_free()` 已删除——它是这个缺陷的载体，留着只会被再次误用。

### 回归测试

`tests/test_security_v023.py`：

- `test_reconcile_rechecks_state_under_lock` —— **核心用例**。用一个"第一次读返回
  陈旧值、同时把磁盘写成 COMPLETED"的读函数，精确复现上述交错；断言
  `recovered == []` 且终态为 `COMPLETED`。
- `test_reconcile_does_not_overwrite_completed_state`
- `test_reconcile_does_not_overwrite_failed_state`
- `test_reconcile_does_not_change_active_plan` —— 分别覆盖"同进程持锁"与
  "另一实例持文件锁"两条退让路径。
- `test_reconcile_marks_only_orphaned_in_progress` —— 五个计划一起扫，只有孤儿被改。
- `test_reconcile_is_fail_closed_without_lock_backend`

### 残余风险

- 判断"持有者已死"的唯一依据仍是"锁空闲"。持有者被调试器挂起时锁仍在内核手里，
  不会被误判；锁被操作系统强制回收时会被判为中断——误判方向是拒绝执行，安全。
- 恢复程序本身在写入时若被强杀，状态文件仍是上一个值（写入走 mkstemp +
  `os.replace`，不会写半个文件），下次扫描会再试一次，结果一致。

---

## 2. 扫描入口跟随链接根目录（P1-2）

### 原始问题

v0.2.2 已经统一了 `is_symlink_or_reparse()`，但只在**遍历过程中**使用：
`os.walk` 的 `dirnames` 过滤、文件条目的 `lstat` 判断。
**传入的 root 本身从未被检查。**

```python
def top_files(self, root, ...):
    base = Path(root)          # ← 就是链接，也照单全收
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
```

`os.walk(..., followlinks=False)` 不跟随的是**子目录**；根目录是不是链接，它不管。

受影响入口：

| 模块 | 入口 | 后果 |
|---|---|---|
| `DiskScanner` | `volume()` | `shutil.disk_usage()` 报告**目标卷**的容量，不是用户所指位置的卷 |
| `DiskScanner` | `directory_sizes()` / `top_files()` / `scan()` | 列出目标目录的文件 |
| `DuplicateFinder` | `_collect()` | 在目标目录里找重复文件 |
| `CacheAnalyzer` | `discover()` | 在目标目录里生成缓存候选 |
| `utils.collect_files()` | — | 为目标目录生成 `FileRef`，进而进入清理计划 |
| `utils.path_size_report()` | — | 已有根检查，行为正确（本次仅回归确认） |

### 触发条件

`D:\data\cache` 是指向 `E:\elsewhere` 的 junction，或 `~/work` 是指向另一块盘的
符号链接，而用户把它当作 `--root`。

危险之处不是"多读了一点"，而是**计划会以错误的路径身份生成**：
`PlanBuilder` 拿到的候选路径在链接之下，`policy.verdict` 的 `realpath` 检查
会发现真实路径落在授权根之外从而拒绝——但那是第二道闸门在兜底，
第一道（扫描）本就不该产生这些候选。

### 修复方法

在四个 ScanRoot 入口最前面检查，位于 `is_dir()` / `disk_usage()` / `scandir()` /
`walk()` **之前**：

- `DiskScanner.volume` / `directory_sizes` / `top_files` / `scan`：
  抛出 `UnsafeScanRoot`（定义在 `core/utils.py`，`RuntimeError` 子类）。
  返回零值会让"被拒绝"看起来像"空目录"，这是两种完全不同的事实。
- `DuplicateFinder._collect`：跳过该 root，继续处理其余 root（`find()` 接受多根）。
- `CacheAnalyzer.discover`：跳过该 root。
- `utils.collect_files`：返回 `((), 0)`，不进入 `os.walk`。
- `utils.path_size_report`：保留原有根检查，返回 `skipped_redirects=1`。

### 回归测试

`tests/test_security_v023.py`：

- `test_disk_scanner_rejects_symlink_root` —— 四个入口逐一断言抛异常；并断言目标
  文件仍在（拒绝不等于删除）。
- `test_disk_scanner_rejects_reparse_root` —— monkeypatch `shutil.disk_usage`
  为"被调用即失败"，证明检查发生在任何测量之前。
- `test_duplicate_finder_rejects_symlink_root` / `..._reparse_root`
- `test_cache_analyzer_rejects_symlink_root` / `..._reparse_root`
- `test_collect_files_rejects_symlink_root`
- `test_path_size_rejects_symlink_root`
- `test_linked_root_detection_uses_shared_guard` —— 确认用的是共享判断，不是又写一份。

每个用例都同时断言"夹具本身有内容"（直接扫目标目录能得到结果），
否则"返回空"在缺少检查时也成立，测试就没有意义。

### 残余风险

- 本机没有 `SeCreateSymbolicLinkPrivilege`，无法创建真实符号链接或 junction。
  相关用例在无法原生创建时，改用"把普通目录标记为重解析点 + 目录内放假文件"
  的方式命中同一分支。**这些是模拟测试，不是"真实 Windows junction 已验证"的证据。**
- 根检查与后续遍历之间仍有 TOCTOU：检查通过后、`os.walk` 开始前，路径可以被替换。
  这与 §3 的 TOCTOU 是同一类窗口，本轮未消除。
- 父目录被换成链接的情形由 `PolicyEngine.validate_path_chain` 在计划/执行阶段处理，
  不在扫描阶段。

---

## 3. 本轮未消除的风险（与 v0.2.2 相同，未改善也未恶化）

1. **TOCTOU**：预检与 `os.replace` 之间仍有窗口，加锁解决的是并发执行，
   不是"做的时候东西被换了"。
2. **`plan_id` 是内容指纹，不是数字签名**：能改写计划并重算 SHA-256 的本地攻击者
   可以绕过摘要校验。
3. **HMAC 凭证不防同进程恶意代码**：密钥在内存里。
4. **回收站 ≠ 释放空间**：三个数字分开报告，最后一项可为负。
5. **硬链接重复计数、稀疏文件按表观大小**：未修。
6. **真实回收站未做端到端验证**：全程 `QuarantineBackend` / Fake 后端。
7. **macOS 未验证**：不在 CI 矩阵，文档不声称支持。
8. **真实 DeepSeek 调用未验证**：无 Key，全程 mock。
9. **本项目未达到生产级安全标准**：未做独立渗透测试与供应链校验。

---

## 4. 结论

两项修复都只改了"检查发生的位置"，没有新增能力、没有放宽任何既有约束：

- 恢复逻辑从"读—探—写"变成"锁—读—写"，且只可能写一个终态；
- 扫描从"过滤树内链接"变成"先拒绝链接根，再过滤树内链接"。

两条都有可失败的回归测试，并经过变异检验：把修复回退后，
`test_reconcile_rechecks_state_under_lock` 与全部链接根用例都会失败。

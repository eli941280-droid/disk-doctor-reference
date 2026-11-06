# v0.2.0 安全审计报告

> 本文记录进入 v0.2.1 前发现的四个 P0 问题与两个 P1 问题。
> 每项包含：原始问题、触发条件、修复方法、回归测试、残余风险。
> 审计对象：`disk-doctor-reference` v0.2.0（commit 无 Git 历史，以 v0.2.0 ZIP 为基线，
> SHA-256 `95502033c2dc5ab60ec3b58e164f70f5f927bf98446f1aee27866c66dfce7f13`）。

---

## P0-1 父目录符号链接 / Junction 逃逸

### 原始问题

v0.2.0 的 `PolicyEngine.verdict()` 只检查**目标文件本身**是不是符号链接或重解析点。
如果目标文件的**父目录**被替换为指向另一个授权根的链接，校验仍然通过。

### 触发条件

```
D:\sandbox\A\        （授权根 A）
    __pycache__\a.pyc

D:\sandbox\B\        （授权根 B）
    __pycache__\b.pyc

攻击：把 A\__pycache__ 替换为指向 B\__pycache__ 的符号链接或 Junction
```

此时针对 A 的计划里，`A\__pycache__\a.pyc` 的路径字符串仍在 A 之内，
但真实落点是 B。若 B 也在 `allowed_roots` 中，旧实现还会认为"属于某个授权根"而放行。

### 修复方法

新增 `PolicyEngine.validate_path_chain(path, required_root)`：

1. 校验 `required_root` 存在、是目录、本身不是链接/重解析点；
2. 逐级遍历 `required_root` 到 `path` 之间的**每一个**目录组件，
   检查 `lstat`、`os.path.islink`、`FILE_ATTRIBUTE_REPARSE_POINT`；
3. 中间目录必须存在（目标本身允许暂时不存在，由执行器另行检查）；
4. 最终要求 `realpath(target) ∈ realpath(required_root)`，
   **不是**"属于任意 allowed_root"。

`verdict()` 新增 `required_root` 参数；`PlanBuilder.build()` 与
`SafeExecutor._execute_file()` 都传入 `plan.root`。

新增**两遍校验**：

- 第一遍 `SafeExecutor.preflight()`：整份计划的所有文件检查完，任何一项失败则**整份计划在首次移动前阻断**；
- 第二遍：每个文件移动前重新校验自身与父目录链；若中途检测到链变化，立即放弃剩余文件并记录部分完成。

### 回归测试

```
test_parent_symlink_to_other_allowed_root_rejected
test_nested_symlink_parent_rejected
test_symlink_root_rejected
test_reparse_parent_rejected
test_resolved_path_must_remain_in_plan_root
test_all_files_prevalidated_before_first_move
```

Windows 无法在无特权会话中创建真实符号链接，因此原生创建分支被跳过，
但**逻辑分支由 monkeypatch 完整覆盖**，不存在"未测即通过"。

### 残余风险

- `required_root` 自身的**祖先目录**不在校验范围内。若用户授权了一个位于重定向
  用户配置目录（如被 Junction 重定向的 `AppData`）之下的根，该重定向被视为调用方上下文，
  而非本工具发现的逃逸。这一取舍已写入 `policy_engine.py` 模块文档。
- TOCTOU 未消除：检查与 `move` 之间存在窗口（见 §残余风险总述）。

---

## P0-2 Temp / tmp 泛化缓存识别

### 原始问题

v0.2.0 的 `CacheAnalyzer` 把任何名为 `Temp` 或 `tmp` 的目录当作 SAFE 缓存清空：

```python
Signature("temp", "临时文件", RiskLevel.SAFE, "...", _names("temp", "tmp"))
```

目录名叫 `Temp` 完全不能证明里面的内容可重建。用户把笔记、导出文件放在 `Temp` 里是常见做法。

### 触发条件

```
<授权根>\Temp\
    notes.txt
    data.csv
```

v0.2.0 会把这些文件纳入清理计划。

### 修复方法

重写为**正向识别**，只保留三类，且必须由名称 + 内容共同证明：

| 类别 | 条件 |
|------|------|
| `__pycache__` | 目录名为 `__pycache__`，**且仅接受 `.pyc` 文件**；`.py` / `.txt` 等一律排除 |
| `.pytest_cache` | 目录名匹配，**且存在含注册签名的 `CACHEDIR.TAG`** |
| `.ruff_cache` | 同上 |

`CACHEDIR.TAG` 必须包含 `Signature: 8a477f597d28d172789f06886806bc55`，且该文件本身不能是链接。
标签无效则整个目录不被识别。

其他类别（pip / npm / browser / node_modules / Gradle / 任意叫 `cache` 的目录）
**默认完全不产生候选**，只有显式传入 `allow_optional=True`（CLI 为 `--allow-optional-cache`）
才出现，且恒为 `CAUTION`。

**没有**通过给 `.txt` / `.csv` 增加受保护后缀来"解决"这个问题——那只是把失败点后移。

### 回归测试

```
test_generic_temp_directory_not_cleanable
test_generic_tmp_directory_not_cleanable
test_temp_with_user_txt_not_cleanable
test_unknown_cache_directory_not_cleanable
test_pycache_only_accepts_pyc
test_pytest_cache_requires_valid_tag
test_invalid_cache_tag_rejected
test_cache_symlink_rejected
```

### 残余风险

- 合法 `.pytest_cache` 的 `CACHEDIR.TAG` 若被删除，将不再被识别 —— 方向是**少清理**，安全。
- 可选类别一旦被显式启用，就回到"名称匹配"的弱证据状态。这是用户主动选择，
  报告中始终标记 `CAUTION`，且不进入默认计划。

---

## P0-3 plan_id 确认未真正绑定

### 原始问题

v0.2.0 的执行接口是 `execute(plan, confirm=True)`，CLI 是 `--yes`。两个问题：

1. **布尔值无法表达"批准了哪一份文件清单"**；
2. `CleanPlan` 带有 `dry_run` 字段，且该字段参与摘要计算 —— 预览与执行是两份**不同 plan_id** 的计划。

结果是：用户看到的摘要与最终执行的摘要不是同一个对象，`plan_id` 的绑定形同虚设。

### 触发条件

正常使用即可触发：`plan` 显示 ID-A（`dry_run=True`），`execute` 重新构建得到 ID-B（`dry_run=False`）。
任何"用预览 ID 去执行"的尝试都会失败，说明绑定关系没有建立起来。

### 修复方法

**架构固定为两阶段：**

```
plan → 持久化 PlanStore → 完整文件清单 → 用户输入 EXECUTE <完整 plan_id>
     → ConfirmationAuthority 签发一次性凭证 → SafeExecutor 执行 → 计划标记已消费
```

具体改动：

1. **`CleanPlan` 移除 `dry_run`**，摘要不再包含执行意图。`SCHEMA_VERSION` 升到 `"3"`，
   v0.2.0 的计划一律拒绝加载（`plan_schema_outdated`），不做迁移。
2. **`PlanStore`**（`core/clean_plan.py`）：
   - 目录 `<app-data>/plans/`，文件名 `f"{plan_id}.json"`；
   - 只接受 `^[0-9a-f]{64}$`，调用方无法指定任意路径；
   - 临时文件 + `os.replace` 原子写入；
   - 加载时重新校验摘要、schema 版本、有效期、是否已消费。
3. **`ConfirmationAuthority`**：
   - 进程启动生成随机 HMAC 密钥；
   - 必须精确匹配 `EXECUTE <64位 plan_id>`（大小写与空格敏感）；
   - 大计划额外要求 `SECOND <64位 plan_id>`；
   - 凭证绑定 `plan_id` + 一次性 `nonce` + 有效期（默认 300 秒）；
   - 执行后 `consume()`，重复使用报 `approval_already_used`。
4. **`SafeExecutor.execute(plan, receipt)`**：删除 `confirm` 参数与
   `require_confirmation` 开关；无凭证即 `approval:approval_missing`。
5. **CLI**：`plan` / `execute` 两条命令；`execute` 在 `sys.stdin.isatty()` 为假时直接拒绝，
   **不提供非交互绕过**；旧 `clean --no-dry-run --yes` 降级为"只生成计划"；
   `ask --execute` 不再具备执行能力。

### 回归测试

```
test_plan_id_same_between_preview_and_execute
test_plan_store_roundtrip
test_plan_store_rejects_invalid_id            (6 个参数化用例)
test_plan_store_rejects_expired_plan
test_plan_store_rejects_consumed_plan
test_wrong_plan_id_confirmation_rejected
test_partial_plan_id_confirmation_rejected
test_old_approval_cannot_authorize_new_plan
test_approval_is_single_use
test_second_confirmation_required
test_noninteractive_execution_rejected
test_yes_flag_cannot_bypass_plan_confirmation
test_llm_cannot_issue_confirmation
test_llm_cannot_call_safe_executor
```

### 残余风险

- **HMAC 不是本机恶意代码防护。** 能在此进程中执行代码的攻击者可以读密钥、伪造凭证。
  它防的是凭证被意外复用、错配与跨计划泄漏。
- `plan_id` 是内容指纹，不是签名（见下）。
- 计划存储目录与应用数据同域，未加密。计划只含路径与元数据，不含文件内容。

---

## P0-4 Quarantine 探测文件覆盖

### 原始问题

v0.2.0 `QuarantineBackend.is_available()` 使用固定文件名：

```python
probe = self.directory / ".write_probe"
probe.write_text("ok")
probe.unlink()
```

如果隔离目录里本来就有用户自己的 `.write_probe` 文件，它会被**覆盖并删除**。

### 触发条件

隔离目录中存在同名文件（用户自建、其他工具创建、或上一次中断残留）。

### 修复方法

改用 `tempfile.mkstemp(prefix=".disk-doctor-probe-", dir=...)`：

- 独占创建，文件名随机，不可能与既有文件冲突；
- 只清理本次创建的那一个文件；
- 异常路径同样只删自己的临时文件；
- 绝不触碰目录中预先存在的任何文件。

### 回归测试

```
test_quarantine_probe_preserves_existing_write_probe
test_quarantine_probe_uses_unique_filename
test_quarantine_probe_cleans_only_its_own_file
test_quarantine_probe_failure_preserves_existing_files
```

### 残余风险

无已知残余。该函数只做"能否写入"探测，不承担其他安全职责。

---

## P1-1 把"移入回收站"当成"释放空间"

### 原始问题

v0.2.0 的报告写"📦 实测释放：7.8 GB"，但文件只是进了回收站，磁盘可用空间没有变化。
成就系统同样把文件大小累加为"已释放"。

### 修复方法

拆成三个互不替代的指标：

| 指标 | 来源 |
|------|------|
| `estimated_bytes` | 计划中文件大小之和 |
| `bytes_moved_to_trash` | 实际成功移动的文件大小之和 |
| `available_space_change_bytes` | `shutil.disk_usage(root).free` 前后实测差值 |

差值允许为负（其他进程在写盘），原样呈现，不钳制。
报告措辞固定为"预计处理文件大小 / 实际移入回收站 / 磁盘可用空间变化"，
禁止在未验证时输出"实际释放空间"。成就措辞改为"累计移入回收站"。

### 回归测试

```
test_moved_bytes_not_reported_as_freed_space
test_recycle_bin_does_not_assume_free_space_increase
test_available_space_delta_is_measured_separately
test_negative_free_space_delta_preserved
```

### 残余风险

- 可用空间差值是**采样**，受并发写盘影响，不能精确归因于本次操作。
- 未做硬链接去重：同一 inode 多个路径仍会被重复计入 `bytes_moved_to_trash`（沿用 D1）。

---

## P1-2 Windows 路径解析依赖宿主 `os.sep`

### 原始问题

```python
return f"{letter}:{os.sep}{tail}"
```

在 Linux 上解析 `D:\work\temp` 会得到 `D:/work/temp`，与 Windows 行为不一致，
导致测试在 CI 不同平台上结论不同。

### 修复方法

- Windows 盘符路径使用 `ntpath.normpath`；
- Unix 路径使用 `posixpath.normpath`；
- 输入约定决定输出形式，与运行平台无关。

`D:\work\temp`、`D:/work/temp` → `D:\work\temp`；
`D:/`、`D盘` → `D:\`。

### 回归测试

```
test_windows_drive_path_parsing
test_windows_forward_slash_parsing
test_windows_drive_root_parsing
test_chinese_drive_name_parsing
test_unix_path_parsing
```

### 残余风险

- 含空格的未加引号路径仍会在空格处截断。CLI 通过 `--root` 显式传参规避；
  自然语言路径仍需用户加引号。

---

## 残余风险总述（必须与代码一起阅读）

1. **`plan_id` 是内容指纹，不是数字签名。** 能改写计划文件并重算哈希的本地攻击者可以绕过。
   本项目不声称抵抗此类攻击。
2. **TOCTOU 未消除。** 预检 + 移动前复检把窗口压到最小，但检查与 `move` 之间仍非原子。
   已移入回收站的文件可恢复，是最后一道缓冲。
3. **HMAC 凭证不是本机恶意代码防护。**
4. **回收站中的文件仍占用磁盘。** 报告如实呈现，但不代为清空。
5. **本项目未达到生产级安全标准。** 它是研究性的参考实现，安全设计可审查、可测试，
   但未经过独立渗透测试，也未做供应链校验（依赖投毒不在威胁模型内）。

# WorkBuddy 执行日志 — v0.2.1 安全补丁

> 记录本次定向修复的**实际执行内容**，含成功项、返工项与未完成项。
> 命令与输出为真实记录。

时间：2026-11-05 20:59 — 21:40 (GMT+8)
环境：Windows 11 / PowerShell 5.1 / Git Bash
Python：`D:\conda_envs\disk-doctor-reference\python.exe` = **3.11.18**

---

## 1. 前置检查（只读）

| 检查项 | 实际值 | 结论 |
|--------|--------|------|
| `$PROJECT` | `D:\githubproject\disk-doctor-reference` | 存在 |
| `$SOURCE_ZIP` | `...-v0.2.0.zip` | 存在，SHA-256 `95502033…ce7f13` |
| `$CONDA_EXE` | `D:\conda\Scripts\conda.exe` | 存在（未加入 PATH，用绝对路径调用）|
| `$ENV_PATH` | `D:\conda_envs\disk-doctor-reference` | 存在，Python 3.11.18 |
| pytest | 9.2.0 | 可用 |
| ruff | 0.17.3 | 可用 |
| build | 缺失 | 第 12 阶段在环境内安装 |
| D 盘可用 | 62.5 GB | 充足 |
| `$BACKUP` 是否已存在 | False | 允许备份 |

全部通过，进入备份。

---

## 2. 备份

```powershell
robocopy $PROJECT $BACKUP /E /COPY:DAT /R:1 /W:1 /XD __pycache__ .pytest_cache ...
```

| 项 | 值 |
|----|-----|
| 备份路径 | `D:\githubproject\_backups\disk-doctor-reference-pre-v0.2.1` |
| robocopy 退出码 | 1（0–7 为成功区间）|
| 记录文件数（统一过滤规则）| 源 70 / 备份 70 |
| 差异 | 0 |
| Python 文件哈希比对 | 45 个文件，0 处不一致 |
| 原 ZIP SHA-256 | `95502033C2DC5AB60EC3B58E164F70F5F927BF98446F1AEE27866C66DFCE7F13` |

**一次计数误报**：初次比对显示"源 70 / 备份 76"。原因是两侧过滤规则不一致——
源计数排除了 `.egg-info`，而 robocopy 的 `/XD` 未排除该目录，导致 6 个构建元数据文件被复制。
改用同一套过滤规则重算后 **70 vs 70，零差异**。备份本身完整。

---

## 3. 源码审查与修改范围

审查了 `src/disk_doctor/`（6 个子包）、`tests/`、`prompts/`、`docs/`、`pyproject.toml`。

**范围外的必要改动**（按第 3 阶段要求记录原因）：

| 文件 | 原因 | 影响 |
|------|------|------|
| `tests/conftest.py` | 执行 API 从 `confirm=True` 改为凭证制，fixture 必须提供 `authority` / `approve` | 所有测试依赖 |
| `tests/test_planner.py` | 断言了已被 P0-3 删除的 `CleanPlan.dry_run` 字段 | 4 个用例 |
| `tests/test_validator.py` | 同上，且 `Verification` 字段重命名 | 全文件 |
| `examples/mock_demo.py` | 使用 `handle(execute=True)`，该参数已移除 | CI 会运行它 |
| `examples/basic_usage.py` | 同上；且沙箱使用 `Temp`，已不再是缓存签名 | 同上 |

**整合**：`disk-doctor-fixed-core` 不存在，本轮按要求不再查找、不尝试整合。

---

## 4. P0-1 父目录链接逃逸

**修改**：`core/policy_engine.py`

- 新增 `validate_path_chain(path, required_root) -> ChainVerdict`：
  检查 root 本身、逐级检查中间目录的 `lstat` / `islink` / 重解析点、
  要求 `realpath(target) ∈ realpath(root)`。
- `verdict(path, *, required_root=None)` 新增参数；给出 required_root 时，
  包含性判断改为"仅针对该 root"，而非"任意 allowed_root"。
- 新增 `is_within()` 模块级函数（`relpath` 组件比较，跨卷返回 False）。

**修改**：`core/clean_plan.py` / `core/safe_executor.py`

- `PlanBuilder` 对候选与每个文件都传 `required_root=scope`。
- `SafeExecutor.preflight()`：整份计划先全部校验，任一失败 → `preflight_failed:<rule>`，
  **零次移动**。
- 逐文件移动前再校验一次；若检测到链变化（`symlink_in_path` 等）→ 放弃剩余文件并标记跳过。

**测试**：6 项全部通过。

```
test_parent_symlink_to_other_allowed_root_rejected    PASSED
test_nested_symlink_parent_rejected                   PASSED
test_symlink_root_rejected                            PASSED
test_reparse_parent_rejected                          PASSED
test_resolved_path_must_remain_in_plan_root           PASSED
test_all_files_prevalidated_before_first_move         PASSED
```

**返工记录**：首次实现中，链校验要求**目标文件本身必须存在**，
导致计划阶段拒绝任何尚未落盘的条目，并使 `test_plan_planner` 系列全红。
修正为"中间目录必须存在，目标允许暂缺"，由执行器的 `inspect_file` 承担存在性检查。
这是实现缺陷，不是测试放宽。

---

## 5. P0-2 Temp/tmp 泛化识别

**修改**：`tools/cache_analyzer.py`（重写）

| 旧规则 | 新规则 |
|--------|--------|
| `_names("temp","tmp")` → SAFE | **删除** |
| pip / npm / browser / node_modules / Gradle → SAFE/CAUTION | 默认**不产生候选**，需 `allow_optional=True` |
| `__pycache__` 按目录大小整体清理 | 仅接受 `.pyc`，其他扩展名逐个排除 |
| `.pytest_cache` / `.ruff_cache` 按名称 | 必须存在含 `Signature: 8a477f597d28d172789f06886806bc55` 的 `CACHEDIR.TAG` |

**没有**通过增加 `.txt` / `.csv` 受保护后缀来绕开问题——初版曾短暂加入，随即删除。
核心修复是正向识别。

**测试**：8 项全部通过。

```
test_generic_temp_directory_not_cleanable             PASSED
test_generic_tmp_directory_not_cleanable              PASSED
test_temp_with_user_txt_not_cleanable                 PASSED
test_unknown_cache_directory_not_cleanable            PASSED
test_pycache_only_accepts_pyc                         PASSED
test_pytest_cache_requires_valid_tag                  PASSED
test_invalid_cache_tag_rejected                       PASSED
test_cache_symlink_rejected                           PASSED
```

---

## 6. P0-3 plan_id 确认绑定

**修改**：`core/schemas.py`

- 删除 `CleanPlan.dry_run` 与规范化载荷中的该字段；`SCHEMA_VERSION` `"2"` → `"3"`；
  `POLICY_VERSION` `"1"` → `"2"`；
- 新增 `ChainVerdict`、`ApprovalReceipt`；
- `ExecutionResult` 拆出 `estimated_bytes` / `bytes_moved_to_trash` /
  `free_space_before` / `free_space_after`；
- `Verification` 同步改名。

**修改**：`core/clean_plan.py`

- 新增 `PlanStore`：`<app-data>/plans/<plan_id>.json`，`^[0-9a-f]{64}$` 校验、
  `mkstemp` + `os.replace` 原子写入、加载时重算摘要/Schema/过期/消费状态。
- 新增 `ConfirmationAuthority`：进程随机 HMAC 密钥、`EXECUTE <id>` / `SECOND <id>`
  精确匹配、nonce 一次性、300 秒有效期。

**修改**：`core/safe_executor.py`

- `execute(plan, receipt)` —— 移除 `confirm` 与 `require_confirmation`；
  无凭证 → `approval:approval_missing`；凭证无效/过期/已用 → 对应 `approval:*` 码。

**修改**：`agent/orchestrator.py` / `cli.py`

- `handle()` 不再接受 `execute/confirm`，只生成并持久化计划；
- 新增 `execute_plan(plan_id, confirmation, second_confirmation)`；
- CLI 拆为 `plan` / `execute`；`execute` 在 `sys.stdin.isatty()` 为假时拒绝；
- `clean` 降级为"只生成计划"；`ask` 不再具备执行能力。

**测试**：14 项全部通过（含 6 个参数化的无效 ID 用例，实际 19 个断言）。

```
test_plan_id_same_between_preview_and_execute         PASSED
test_plan_store_roundtrip                             PASSED
test_plan_store_rejects_invalid_id[6 参数]            PASSED
test_plan_store_rejects_expired_plan                  PASSED
test_plan_store_rejects_consumed_plan                 PASSED
test_wrong_plan_id_confirmation_rejected              PASSED
test_partial_plan_id_confirmation_rejected            PASSED
test_old_approval_cannot_authorize_new_plan           PASSED
test_approval_is_single_use                           PASSED
test_second_confirmation_required                     PASSED
test_noninteractive_execution_rejected                PASSED
test_yes_flag_cannot_bypass_plan_confirmation         PASSED
test_llm_cannot_issue_confirmation                    PASSED
test_llm_cannot_call_safe_executor                    PASSED
```

**关键验证**：预览与执行使用**同一个 plan_id**。
`test_plan_id_same_between_preview_and_execute` 直接比较 `plan_store.load(id)` 与
会话内计划的 `canonical_json()`，两者字节相同。

---

## 7. P0-4 Quarantine 探测

**修改**：`core/safe_executor.py`

```python
fd, probe_name = tempfile.mkstemp(prefix=".disk-doctor-probe-", dir=str(self.directory))
```

只删除本次创建的文件；异常路径同样只删自己的临时文件。

**测试**：4 项全部通过。

```
test_quarantine_probe_preserves_existing_write_probe   PASSED
test_quarantine_probe_uses_unique_filename             PASSED
test_quarantine_probe_cleans_only_its_own_file         PASSED
test_quarantine_probe_failure_preserves_existing_files PASSED
```

---

## 8. P1-1 空间统计

**修改**：`core/validator.py`、`core/schemas.py`、`reporting/completion.py`、`tools/achievements.py`

- 三个指标分开：计划估算 / 实际移入回收站 / 磁盘可用空间实测差；
- 差值可负，原样呈现；
- 报告措辞改为"预计处理文件大小 / 实际移入回收站 / 磁盘可用空间变化"，
  并固定附加一句"移入回收站不等于释放磁盘空间"；
- 成就字段 `total_freed_*` → `total_moved_*`，旧文件通过兼容分支读取。

**测试**：4 项通过（`test_security_v021.py`）+ 7 项（`test_validator.py`）。

---

## 9. P1-2 Windows 路径解析

**修改**：`agent/intent_router.py`

- `ntpath.normpath` 处理 Windows 盘符路径，`posixpath.normpath` 处理 Unix 路径；
- 新增 `normalize_windows_path()`，输出与宿主平台无关。

**测试**：

```
test_windows_drive_path_parsing      D:\work\temp
test_windows_forward_slash_parsing   D:/work/temp  -> D:\work\temp
test_windows_drive_root_parsing      D:/           -> D:\
test_chinese_drive_name_parsing      D盘           -> D:\
test_unix_path_parsing               /var/tmp/x
```

---

## 10. 自动化测试

```
$ python -m pytest -q
........................................................................ [ 87%]
....................                                                     [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests/test_policy.py:69: 当前环境不允许创建符号链接
163 passed, 1 skipped in 1.88s
```

```
$ python -m ruff check src tests
All checks passed!

$ python -m compileall -q src tests examples
exit code 0

$ python -m disk_doctor --version
disk-doctor 0.2.1
```

### 开发过程中由测试抓出的真实缺陷

| # | 缺陷 | 发现方式 | 修复 |
|---|------|---------|------|
| 1 | 路径链校验强制目标必须存在，导致计划阶段拒绝合成条目 | `test_planner` 系列 6 项同时变红 | 改为"中间目录必须存在，目标可暂缺" |
| 2 | 成就字段重命名后 `orchestrator._achievements` 仍读旧键 | `test_achievements_command` KeyError | 同步键名与文案 |
| 3 | `test_security_regression` 中 `Temp` 不再是缓存签名 | 4 项断言失败 | 改用 `__pycache__` + `.pyc` |
| 4 | 旧测试用位置参数构造 `FileRef` | pydantic `TypeError` | 改关键字参数 |

第 1 项是**实现缺陷**（会误杀真实场景），后三项是测试适配。

---

## 11. 构建

```
$ python -m build --outdir D:\tmp\disk-doctor-v0.2.1-tests\dist
Successfully built disk_doctor_reference-0.2.1.tar.gz
                    and disk_doctor_reference-0.2.1-py3-none-any.whl

wheel 74,274 bytes / sdist 84,288 bytes
```

---

## 12. 未完成 / 未验证

| 项 | 状态 |
|----|------|
| Git 仓库 | 不存在，未初始化（本轮明确禁止） |
| 真实系统回收站端到端 | NOT VERIFIED（测试统一使用 FakeQuarantine） |
| Windows 原生符号链接创建 | 环境无特权，分支由 monkeypatch 覆盖 |
| macOS | 未运行，未纳入 CI |
| 真实 DeepSeek 调用 | 无 Key，全程 mock |
| 硬链接去重 / 稀疏文件 | 沿用 D1/D2，本轮未修 |
| TOCTOU | 缩小未消除 |

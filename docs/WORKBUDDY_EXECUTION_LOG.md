# WorkBuddy 执行日志 — v0.2.0

> 记录本次加固任务的**实际执行内容**，包含成功的、失败的、以及没做成的。
> 命令与输出为真实记录，未做修饰。

时间：2026-11-05 19:04 — 19:30 (GMT+8)
执行环境：Windows / Git Bash + PowerShell 5.1

---

## 步骤 0：前置检查

```bash
$ ls -d disk-doctor-reference disk-doctor-fixed-core
disk-doctor-reference
ls: cannot access 'disk-doctor-fixed-core': No such file or directory

$ find /d/githubproject -maxdepth 3 -iname "*fixed*core*"
(空)

$ conda --version
bash: conda: command not found

$ cd disk-doctor-reference && git status
fatal: not a git repository
```

**结论**

| 检查项 | 结果 | 处理 |
|--------|------|------|
| 主项目目录 | ✅ 存在 | 继续 |
| `disk-doctor-fixed-core` | ❌ **不存在**（三层内无匹配） | 不声称已整合；在报告中标为缺失依赖 |
| Git 仓库 | ❌ 无 `.git` | 无法检查未提交修改；备份改为整目录复制 |
| conda 在 PATH | ❌ | 继续查找 |

PowerShell 中找到 `D:\conda\Scripts\conda.exe`（conda 26.9.4，未加入 PATH）。
`D:\conda_envs` 不存在。**未使用 venv 替代**。

---

## 步骤 1：备份

```powershell
$BACKUP = D:\githubproject\_backups\disk-doctor-reference-pre-v0.2-20261105_190417
robocopy $PROJECT $BACKUP /E /COPY:DAT /R:1 /W:1 /XD __pycache__ .pytest_cache ...
```

| 项 | 值 |
|----|-----|
| 备份路径 | `D:\githubproject\_backups\disk-doctor-reference-pre-v0.2-20261105_190417` |
| robocopy 退出码 | 1（0–7 为成功） |
| 源文件数 | 64 |
| 备份文件数 | 64 |
| 一致性 | True |
| 原始 ZIP SHA-256 | `BE3A2396585D07C0E99EC124C5D2CE027B1B68BB1D4C9EF8C325BA07D91E6515` |

备份成功，开始修改。

---

## 步骤 2：Conda 环境

```powershell
$env:CONDA_PKGS_DIRS = "D:\conda_pkgs"
& "D:\conda\Scripts\conda.exe" create --prefix "D:\conda_envs\disk-doctor-reference" `
    python=3.11 pip -y --override-channels -c conda-forge
```

| 项 | 值 |
|----|-----|
| 环境路径 | `D:\conda_envs\disk-doctor-reference` |
| Python | **3.11.18** |
| pip | 26.2.2 |
| 包缓存 | `D:\conda_pkgs` |
| pip 缓存 | `D:\pip_cache` |
| 临时目录 | `D:\tmp\disk-doctor-reference` |

依赖（直连 PyPI，未改全局镜像配置）：

```
pydantic 2.14.3 / pytest 9.2.0 / ruff 0.17.3 / send2trash 2.1.0
python-dotenv 1.1.1 / openai 3.46.0
```

**遇到的问题**：conda 创建成功，但 PowerShell 把 conda 的 stderr 警告
（conda-pypi 提示）当作 `NativeCommandError`，任务状态误报失败。
实际环境存在且可用。

---

## 步骤 3：源码审查与整合

```bash
$ find src -name "*.py" | wc -l
$ wc -l src/disk_doctor/**/*.py
```

审查范围：`src/disk_doctor/`（7 个包）、`tests/`、`prompts/`、`docs/`、`pyproject.toml`。

**整合结论：无 Fixed Core 可整合。**

`disk-doctor-fixed-core` 目录不存在，三层内无同名文件。
因此**没有执行**任何"两套安全内核合并"的工作，主项目保持单一实现：

```
src/disk_doctor/          ← 唯一包
  core/safe_executor.py   ← 唯一写入口
```

不声称"已完成整合"。该项在最终验收中标 **NOT VERIFIED**。

---

## 步骤 4：六项安全缺陷修复

### BUG-01 扫描范围越界

**改动**

- `CacheAnalyzer.discover(roots)` 只遍历传入的 roots，不跨根。
- `PlanBuilder.build()` 新增守卫：每个候选与每个文件都必须
  `_is_within(path, scope)`，否则记 `outside_scope_root` 拒绝。
- `_is_within` 用 `relpath` + 前缀校验，跨盘返回 False。

**回归测试**：`test_scan_only_selected_root`（两个授权根 A/B，扫描 A 不得出现 B 的文件）。

### BUG-02 执行阶段缺少 root 约束

**改动**：`SafeExecutor._execute_file()` 逐文件复检 9 项：

```
outside_plan_root → outside_item_dir → not_cleanable → policy.verdict
→ inspect_file(symlink/reparse/regular) → lstat 存在性
→ size_changed → mtime_changed → identity_changed(device/inode)
```

**回归测试**：`test_executor_rejects_item_outside_plan_root`、`test_file_changed_after_plan`、
`test_symlink_rejected`、`test_reparse_point_rejected`、`test_protected_suffix_rejected`。

### BUG-03 预览不完整

**改动**：计划改为**文件级**。`FileRef` 携带 path/size/mtime_ns/device/inode；
`render_preview` 逐行输出「序号 / 大小 / 风险 / 动作 / 状态 / 完整路径」，
并给出总文件数、预计释放、被保护数、被排除数、二次确认标记、完整 plan_id。
执行前再打印一次 `render_confirmation_summary`。

**回归测试**：`test_preview_contains_full_file_paths`。

### BUG-04 Plan ID 完整性

**改动**：`plan_id = sha256(canonical_json)`，64 位十六进制。
摘要覆盖 schema/policy 版本、created_at、ttl、root、dry_run、总量、
确认要求，以及每个 item 与每个文件的元数据。
`verify_integrity()` 重算并比对，不符则 `plan_digest_mismatch` 阻断。

**回归测试**：`test_plan_id_full_sha256`、`test_modified_plan_rejected`。

**明确不声称**：这是内容指纹，不是数字签名，**不能**抵抗有本地写权限并重算哈希的攻击者。

### BUG-05 计划过期

**改动**：`CleanPlan.ttl_seconds` 默认 900 秒；`is_expired()` 比对 `created_at`；
执行前检查，过期则 `plan_expired` 阻断。TTL 只由 `PlanBuilder` 写入，模型无权修改。

**回归测试**：`test_expired_plan_rejected`。

### BUG-06 路径比较

**改动**：`PolicyEngine.contains()` 从字符串前缀改为 `os.path.relpath` 组件判断，
跨盘 `ValueError` → 视为不包含（失败关闭）。CLI `_is_authorized()` 同步改为
`base + os.sep` 前缀。

**回归测试**：`test_path_boundary_work_vs_workshop`。

---

## 步骤 5：LLM 权限固定

**新增** `src/disk_doctor/agent/llm_contract.py`：

```python
class IntentType(str, Enum): DIAGNOSE SCAN PLAN PREVIEW HELP UNKNOWN
class LLMIntentResult(BaseModel): model_config = ConfigDict(extra="forbid")
def parse_llm_intent(payload) -> LLMIntentResult   # 违约即 LLMContractError
```

模型不持有任何文件句柄；`_narrate()` 只把已渲染文本作为输入、做追加，不替换。

**回归测试**：`test_no_llm_execution_authority`（恶意 LLM 返回 `{"command":"rm -rf /"}`
→ 契约报错，文件未被触碰）、`test_llm_contract_rejects_unknown_intent`、
`test_llm_contract_accepts_minimal_payload`。

---

## 步骤 6：测试

新增 `tests/test_security_regression.py`（16 项）+ 改造 3 个旧测试文件为文件级模型。

```bash
$ python -m pytest -q
........................................................................ [ 62%]
...s........................................                             [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests/test_policy.py:69: 当前环境不允许创建符号链接
115 passed, 1 skipped in 1.14s
```

开发中由测试抓出的**真实缺陷**（已修复，非测试放宽）：

| # | 缺陷 | 表现 | 修复 |
|---|------|------|------|
| 1 | 确认集合残留旧计划 id | `is_confirmed(旧id)` 执行后仍为 True | `mark_confirmed` 改为"先清空再写入"；执行后 `clear_confirmations()` |
| 2 | `python -m disk_doctor` 不可用 | `No module named disk_doctor.__main__` | 新增 `__main__.py`（v0.1.0 的 CI 写了这条命令但从未真正跑通） |
| 3 | PlanItem 冻结导致篡改测试无法写入 | pydantic ValidationError | 测试改用 `model_copy(update=...)` 替换条目 |

---

## 步骤 7：静态检查

```bash
$ python -m ruff check src tests
All checks passed!

$ python -m compileall -q src tests examples
(无输出，退出码 0)
```

首次检查 37 项告警（主要是未使用 import、`timezone.utc` 过时写法、导入排序）。
`ruff --fix` 修 47 项（含连带），手工修 4 项（BLE001 ×1、F841 ×1、ISC004 ×2）。
随后收紧 `select`，又出现 19 项（E501 ×13、UP042 ×6），通过
`line-length = 120` 与忽略 `UP042`（保留 `(str, Enum)` 以稳定 pydantic 序列化）收敛到 0。

---

## 步骤 8：文档与 CI

| 文件 | 变更 |
|------|------|
| `pyproject.toml` | 版本 0.2.0；新增 ruff 配置；dev extra 加 ruff |
| `README.md` | 版本/测试数、文件级计划、安全机制章节、Conda 安装、计划有效期、局限补充 |
| `SECURITY.md` | **新增**：威胁模型、权限范围、确认机制、TOCTOU 限制、残余风险 |
| `environment.yml` | **新增**：python 3.11 + pip 依赖，声明 pyproject 为唯一依赖来源 |
| `CHANGELOG.md` | 新增 0.2.0 段 |
| `.github/workflows/ci.yml` | 重写：3 OS × 3 Python、ruff、compileall、pytest、wheel 构建、仓库卫生检查 |
| `src/disk_doctor/__init__.py` | `__version__ = "0.2.0"` |
| `src/disk_doctor/__main__.py` | **新增** |

---

## 步骤 9：CLI 实测

```bash
$ python -m disk_doctor --version
disk-doctor 0.2.0

$ python -m disk_doctor plan --root D:\githubproject\.smoke2 --allow-root D:\githubproject\.smoke2
🧹 清理预览
扫描范围：D:\githubproject\.smoke2
计划编号：eae7f8f9e4931d3459d21b846d42871e7f0556c54f6522291e6e9ebbeea9a256
计划创建：2026-11-05T11:13:09+00:00　有效期 900 秒

  序号  大小        风险      动作          状态      完整路径
     1     4.0 KB  🟢 安全    移入回收站        仅演练       ...\Temp\a.tmp
     2     2.0 KB  🟢 安全    移入回收站        仅演练       ...\Temp\b.tmp
     3     1.0 KB  🟢 安全    移入回收站        仅演练       ...\__pycache__\m.pyc

  总文件数量：3
  总预计释放：约 7.0 KB
  被保护文件：0（类型受保护，未纳入）
  被排除文件：0（范围或策略不通过）
  需要二次确认：否
```

执行 + 独立核对：

```bash
$ python -m disk_doctor clean --root ... --allow-root ... --no-dry-run --yes --quarantine ...
plan_id：2b418416c8fae13777292c1887204a8b1aff5c71fe0107b86988093d00817c38
文件数：3　预计释放：约 7.0 KB
     1  ...\Temp\a.tmp
     2  ...\Temp\b.tmp
     3  ...\__pycache__\m.pyc
✨ 清理完成
✅ 成功移入回收站：3 项

$ 核对
thesis.docx 仍在 : True
a.tmp 仍在       : False
b.tmp 仍在       : False
m.pyc 仍在       : False
隔离区            : ['a.tmp', 'b.tmp', 'm.pyc']
```

注意：预览的 plan_id 与执行的 plan_id **不同**。这是正确行为——
预览 `dry_run=True`、执行 `dry_run=False`，摘要覆盖了该字段。
也正因如此，若有人试图"用预览的 id 去执行"，会被 `plan_digest_mismatch` 拦下。

---

## 步骤 10：打包

见最终报告 H 节。

---

## 遗留问题

| # | 问题 | 状态 |
|---|------|------|
| 1 | `disk-doctor-fixed-core` 不存在，未整合 | NOT VERIFIED（缺失依赖） |
| 2 | 无 Git 仓库，无法检查未提交修改 | NOT VERIFIED（不可用） |
| 3 | macOS 未纳入 CI 矩阵 | 未做（代码未在 macOS 验证） |
| 4 | Windows 真实符号链接测试被跳过 | 分支已有 monkeypatch 测试覆盖 |
| 5 | TOCTOU 未消除 | 已知限制，文档声明 |
| 6 | 硬链接重复计数 / 稀疏文件大小 | 沿用 v0.1.0 缺陷 D1/D2，本版未修 |
| 7 | 真实 DeepSeek 调用未验证 | 无 Key，全程 mock |

> **版本说明**：本文记录的是 **v0.1.0** 的实际运行结果（94 passed）。
> v0.2.1 的结果（163 passed）见 `WORKBUDDY_EXECUTION_LOG_v0.2.1.md`。
> v0.2.2 的结果（200 passed）见 `WORKBUDDY_EXECUTION_LOG_v0.2.2.md`。
> v0.2.3 的结果（215 passed）见 `WORKBUDDY_EXECUTION_LOG_v0.2.3.md`。
> v0.2.0 的加固改动、新增测试与实测结果见
> [`WORKBUDDY_EXECUTION_LOG.md`](WORKBUDDY_EXECUTION_LOG.md)（115 passed）。
> 两版数字不同是正常的，本文保留原始记录，未做覆盖。

---

# 实际运行与测试记录

> 本文件记录的是**真实执行过的命令与真实输出**，不是设计意图。
> 环境：Windows 11 / Python 3.13.14 / pydantic 2.14.3 / pytest 9.2.0
> 日期：2026-11-05

---

## 1. 环境准备

```
$ python --version
Python 3.13.14

$ python -m venv .venv
$ .venv/Scripts/python.exe -m pip install -e ".[dev,llm]"
Successfully installed disk-doctor-reference-0.1.0 pydantic-2.14.3
                    pytest-9.2.0 send2trash-2.1.0 python-dotenv-1.1.1 openai-3.46.0
```

> 备注：本次安装需显式指定 `--index-url https://pypi.org/simple`，
> 因为环境里配置的清华镜像返回了不可解析的索引。这是环境问题，不是项目问题。

---

## 2. 模块导入完整性

```
$ python -c "import importlib, pkgutil, disk_doctor; ..."
modules: 31
import failures: none
version: 0.1.0
```

31 个模块全部可导入，无循环依赖。

---

## 3. 测试套件

```
$ pytest -q
........................................................................ [ 75%]
s......................                                                  [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests/test_policy.py:69: 当前环境不允许创建符号链接
94 passed, 1 skipped in 1.02s
```

| 项 | 值 |
|----|-----|
| 通过 | 94 |
| 跳过 | 1 |
| 失败 | 0 |
| 耗时 | 1.02 s |

**关于那 1 个跳过**：`os.symlink` 在 Windows 上需要 `SeCreateSymbolicLinkPrivilege`，当前账户没有。
该分支另有 `test_symlink_branch_is_covered_without_privileges` 通过 monkeypatch 覆盖，
所以"符号链接必须被拒绝"这条保证仍然是被测过的，只是没有做真实链接的端到端验证。
这条限制记录在 `known_unknowns.md` 的 D5。

### 开发过程中由测试抓出的真实缺陷

| # | 缺陷 | 发现方式 | 修复 |
|---|------|---------|------|
| 1 | 成就统计把"计划条目数"当成"文件数" | 跑 `mock_demo.py` 时发现"累计文件 2 个"，而实际是 2 个目录 | `_execute` 改为按 `PlanItem.file_count` 汇总成功项 |
| 2 | CLI 授权前缀判断未加分隔符 | 审查 `startswith` 时发现 | 抽 `_is_authorized()`，改为 `base + os.sep`，并补测试 |
| 3 | `plan` 子命令缺少 `--allow-root`，导致对任意目录都返回"不在授权范围" | 冒烟测试实际执行时暴露 | 补参数并复用同一授权检查 |
| 4 | 两处测试自身写错（跨盘 `relpath`、文件未创建就期望移动失败） | pytest 报错 | 修正测试，未改实现 |

前三条是实现问题，第四条是测试问题。都记录在此，不做隐去。

---

## 4. CLI 冒烟测试

沙箱目录 `D:\githubproject\.smoke` 内构造：`Temp/a.tmp`(4 KB)、`__pycache__/m.pyc`(1 KB)、
`Chrome/User Data/Default/Cache/d0`(2 KB)、`Documents/thesis.docx`(512 B)、两个 8 KB 重复文件。

### 4.1 `tools` —— 能力清单必须真实

```
$ disk-doctor tools
可用能力
━━━━━━━━━━━━━━━━━━
  achievements.summary             [只读]  读取累计清理统计
  cache_analyzer.discover          [只读]  识别已知缓存目录并估算可释放空间（只读）
  disk_scanner.scan                [只读]  读取磁盘用量、目录体积与大文件排名（只读）
  disk_scanner.top_files           [只读]  找出指定目录下最大的 N 个文件（只读）
  duplicate_finder.find            [只读]  按大小预筛 + SHA-256 查找重复文件（只读）
  planner.plan                     [只读]  生成清理计划（不写盘）
  safe_executor.execute            [写操作/moves files to trash]  执行清理计划（唯一会写盘的路径）
```

7 个能力中只有 1 个写操作。这是架构约束，由 `test_only_one_tool_writes` 守住。

### 4.2 `scan` —— 只读测量

```
$ disk-doctor scan --root D:\githubproject\.smoke --top 3
位置：D:\githubproject\.smoke
总 250.5 GB / 已用 185.2 GB (74%) / 剩余 65.4 GB

目录占用：
      4.0 KB  ...\.smoke\Temp
      2.0 KB  ...\.smoke\Chrome
      1.0 KB  ...\.smoke\__pycache__
       512 B  ...\.smoke\Documents

最大文件：
      8.0 KB  ...\.smoke\dup2.bin
      8.0 KB  ...\.smoke\dup1.bin
      4.0 KB  ...\.smoke\Temp\a.tmp
```

### 4.3 `dupes` —— 三级筛选生效

```
$ disk-doctor dupes --root D:\githubproject\.smoke --min-size 1024
发现 1 组重复，冗余约 8.0 KB
  1. 8.0 KB × 2
       D:\githubproject\.smoke\dup1.bin
       D:\githubproject\.smoke\dup2.bin
```

只报告，不删除。行为符合设计。

### 4.4 `plan --mode deep` —— 风险分级可见

```
$ disk-doctor plan --root ... --allow-root ... --mode deep
🧹 清理预览
━━━━━━━━━━━━━━━━━━

扫描位置：D:\githubproject\.smoke
计划编号：aba2e09d5120
模式：演练（不会改动磁盘）

即将处理以下项目：
  I001      4.0 KB  🟢 安全  ...\.smoke\Temp
  I002      1.0 KB  🟢 安全  ...\.smoke\__pycache__
  I003      2.0 KB  🟡 注意  ...\.smoke\Chrome\User Data\Default\Cache
          注意：重建需要重新联网，会丢失离线内容

━━━━━━━━━━━━━━━━━━
  预计释放：约 7.0 KB
  项目数量：3 项 / 约 3 个文件
━━━━━━━━━━━━━━━━━━

说明：模式：深度（含需注意项）

当前为演练模式，未做任何改动。加 --no-dry-run --yes 才会真正执行。
```

同一个目录在 `quick` 模式下只出现 2 项（I003 是 CAUTION 被排除）。分级不是装饰。

### 4.5 三条拒绝路径

```
$ disk-doctor clean --root ...                       # 无 --allow-root
拒绝执行：目标目录不在授权范围内。
  目标：D:\githubproject\.smoke
  请用 --allow-root 显式授权该目录后再试。
exit=2

$ disk-doctor clean --root ... --allow-root ... --no-dry-run    # 无 --yes
拒绝执行：--no-dry-run 必须同时提供 --yes。
exit=2
```

第三条是默认演练：

```
$ disk-doctor clean --root ... --allow-root ...
  预计释放：约 5.0 KB
  项目数量：2 项 / 约 2 个文件
当前为演练模式，未做任何改动。加 --no-dry-run --yes 才会真正执行。
exit=0
```

三步递进：授权 → 确认 → 演练默认。缺任一步都进不到写操作。

### 4.6 真实执行与事后核对

```
$ disk-doctor clean --root ... --allow-root ... --no-dry-run --yes --quarantine .\.quarantine
✨ 清理完成
━━━━━━━━━━━━━━━━━━

✅ 成功移入回收站：2 项
⏭️ 跳过：0 项
❌ 失败：0 项

📦 实测释放：5.0 KB
   计划估算：约 5.0 KB

🗑️ 已移入回收站，需要的话可以随时恢复。
```

清理后独立核对：

```
thesis.docx 仍存在 : True      ← 受保护类型，未被触碰
Temp 仍存在        : False     ← 已移走
__pycache__ 仍存在 : False     ← 已移走
隔离区内容         : ['Temp', '__pycache__']
```

**这是本次验证最关键的一组数字**：`thesis.docx` 在计划阶段就被 `protected_suffix` 规则拦下，
即使它在扫描范围内、即使属于"看起来可以清理的目录"，也没有进入计划。

### 4.7 `achievements`

```
🏆 清理成就
━━━━━━━━━━━━━━━━━━

累计释放：5.0 KB
累计文件：2 个
清理次数：1 次
```

"累计文件 2 个"是真实的文件数（Temp 里 1 个 + `__pycache__` 里 1 个），不是条目数（2 项）。
这正是缺陷 #1 修复后的结果。

---

## 5. 示例程序

### 5.1 `examples/mock_demo.py`（无 API Key）

六个阶段全部跑通：

| 阶段 | 结果 |
|------|------|
| 1 诊断 | 输出健康评分 54/100，明确写出"数据不足，未做预测" |
| 2 计划 | 2 项，约 6.5 MB，Chrome 缓存因 quick 模式被排除 |
| 3 未确认执行 | `🛡️ 操作已阻断 / not_confirmed / 未改动任何文件` |
| 4 确认执行 | `✨ 清理完成 / 实测释放 6.5 MB` |
| 5 核对 | `thesis.docx 仍存在：True`，隔离区 2 项 |
| 6 成就 | 累计 6.5 MB，未达 1 GB 里程碑 |

全程在 `tempfile.mkdtemp()` 内，退出时 `shutil.rmtree`。不接触真实目录。

### 5.2 `examples/basic_usage.py`

```
范围：C:\...\Temp\disk-doctor-basic-39hc7be0
模型：{'provider': 'mock', 'model': 'deterministic-v1'}

健康评分：54/100
计划条目：1，预计释放 8192 字节
  I001  ...\Temp  (临时文件/safe)

实测释放：8192 字节（估算 8192 字节）
```

展示了库用法：`handle()` 返回结构化 `turn.data`，不需要解析文本。

---

## 6. 未验证的部分

明确列出来，避免读者误以为全部都测过：

| 项 | 状态 | 原因 |
|----|------|------|
| 真实系统回收站 | ⚠️ 未端到端验证 | 冒烟测试用 `QuarantineBackend`，避免污染用户回收站 |
| 真实大目录（>10 万文件）性能 | ❌ 未测 | 沙箱只有 6 个文件 |
| Windows 真实符号链接 | ⚠️ 分支已测，端到端未测 | 缺少 `SeCreateSymbolicLinkPrivilege` |
| macOS / Linux | ❌ 未运行 | 仅在 Windows 上执行过 |
| 真实 DeepSeek API 调用 | ❌ 未运行 | 无 API Key，全程 mock |
| 并发修改（TOCTOU） | ❌ 未测 | 需要竞态注入 |

第 1 项是刻意回避的：在用户机器上真实清空回收站不可接受。
第 5 项需要一个 Key，逻辑上已经通过 `LLMClient` 接口隔离，但没有实际验证过网络调用路径。

---

## 7. 结论

| 验收项 | 状态 |
|--------|------|
| 所有 Python 文件可导入 | ✅ 31/31 |
| 依赖安装无冲突 | ✅ `pip install -e ".[dev,llm]"` 成功 |
| 基础功能可运行 | ✅ 6 个 CLI 子命令实测 |
| Mock 模式独立可运行 | ✅ 两个示例均无 Key 跑通 |
| 单元测试全绿 | ✅ 94 passed / 1 skipped / 0 failed |
| 示例代码可运行 | ✅ |
| README 命令与实际一致 | ✅ 逐条对照执行过 |
| 无硬编码密钥 | ✅ CI 有 grep 检查；仓库内无 `sk-` 开头字符串 |
| 无空模块 | ✅ 31 个模块均有实际逻辑 |
| 文档功能有代码支撑 | ✅ 架构文档中的每个模块均可在 `src/` 找到 |

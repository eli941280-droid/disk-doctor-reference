# 决策引擎

所有决策都是确定性的 Python 函数。没有一处依赖模型输出。

---

## 1. 意图路由决策表

按顺序匹配，先命中先返回。**破坏性与越界规则排在最前**，防止被后面的"清理"关键词遮住。

| 优先级 | 意图 | 关键词（部分） | 依据 |
|-------|------|--------------|------|
| 1 | OUT_OF_SCOPE | 格式化、分区、diskpart | 破坏性，超出能力 |
| 2 | OUT_OF_SCOPE | 恢复、找回、误删 | 超出能力 |
| 3 | OUT_OF_SCOPE | 隐私、私密、身份证 | 内容审查越界 |
| 4 | OUT_OF_SCOPE | 提权、sudo、管理员权限 | 最小权限 |
| 5 | ACHIEVEMENTS | 成就、里程碑、统计 | — |
| 6 | TRASH_INFO | 回收站、撤销删除 | — |
| 7 | FIND_DUPLICATE | 重复、duplicate | — |
| 8 | FIND_LARGE | 大文件、最占、top | — |
| 9 | DEV_CLEAN | 开发者、node_modules、gradle | — |
| 10 | CLEAN_DEEP | 深度清理、彻底 | — |
| 11 | CLEAN_QUICK | 清理、瘦身、腾空间 | 默认清理 |
| 12 | DIAGNOSE | 看看、诊断、健康 | 最宽泛，排最后 |
| — | UNKNOWN | 无命中 | 不猜，返回引导 |

**为什么 DIAGNOSE 排最后**："清理一下看看"应该走清理，而不是诊断。
宽泛词排后面的代价是偶尔漏判，收益是不会把清理请求误判成只读诊断——后者会导致用户以为清理了但什么都没发生。

---

## 2. 范围解析优先级

```
显式参数（CLI --root）
  > 文本中解析出的路径（D:\work\temp）
  > 文本中解析出的盘符（C 盘 → C:\）
  > 会话中上次使用的范围
  > 当前工作目录
```

显式参数最高，是因为文本解析会被空格截断：`D:\my work\temp` 只能解析出 `D:\my`。
截断后的路径如果恰好通过白名单（比如 `D:\my` 也被授权了），就会清理错目录。

---

## 3. 执行准入决策

四个闸门，任一不通过即终止。顺序不可调换。

```python
def may_execute(plan, root_verdict, confirm, second_confirm, trash_ok):
    if not root_verdict.allowed:                  # 闸门 1：授权
        return BLOCK("outside_allowed_roots")
    if not confirm:                               # 闸门 2：确认
        return BLOCK("not_confirmed")
    if plan.requires_second_confirm and not second_confirm:   # 闸门 3：阈值
        return BLOCK("second_confirmation_required")
    if not trash_ok:                              # 闸门 4：可逆性
        return BLOCK("trash_unavailable")
    return ALLOW
```

**闸门 4 放在最后是有意的**：它必须在前三个都通过后才被检查，否则用户会看到"回收站不可用"而误以为自己没授权。
错误信息的准确性影响用户能否自行修复问题。

---

## 4. 风险分级决策

| 条件 | 风险等级 | 是否进入快速模式 | 是否进入深度模式 |
|------|---------|---------------|---------------|
| 匹配 SAFE 签名（Temp、`__pycache__`、pip/npm 缓存） | SAFE | ✅ | ✅ |
| 匹配 CAUTION 签名（浏览器缓存、node_modules、Gradle） | CAUTION | ❌ | ✅（需二次确认） |
| 路径含受保护组件 | FORBIDDEN | ❌ | ❌ |
| 后缀属受保护类型 | FORBIDDEN | ❌ | ❌ |
| 未匹配任何签名 | — | ❌ | ❌ |

最后一行是白名单思维的核心：**不认识 = 不处理**。

---

## 5. 排序决策

```
第 1 键：风险等级（SAFE 先于 CAUTION）
第 2 键：体积降序
第 3 键：路径字典序（保证同样输入产出同样顺序）
```

第 3 键看着多余，但它让计划可复现：同样的磁盘状态跑两次，`I001` 指向同一个路径。
测试和审计都依赖这个性质。

---

## 6. 失败决策

| 场景 | 决策 | 不这样做的后果 |
|------|------|--------------|
| 单项移动失败 | 记录 FAILED，继续下一项 | 整批中止会留下做了一半的状态 |
| 路径已不存在 | SKIPPED（不是 FAILED） | 误报失败会让用户以为出了问题 |
| 复检未通过 | SKIPPED，记录 rule | 静默跳过会掩盖策略问题 |
| 回收站不可用 | 整体 BLOCKED | 部分执行 = 部分可逆，最难解释 |
| 释放量超估算 2 倍 | 标记 `ok=False` | 可能是越界，必须先怀疑自己 |

最后一条对应 `Validator` 里的合理性检查：

```python
if plan.total_bytes and observed > plan.total_bytes * 2:
    notes.append("实测释放量超过计划估算两倍，建议人工核查是否越界")
    ok = False
```

宁可误报，不可漏报。

---

## 7. 完成判定

任务算完成，需要同时满足：

1. 每个 item 有终态（`MOVED_TO_TRASH` / `SKIPPED` / `FAILED` / `DRY_RUN`）
2. `Validator` 对账完成，`observed_bytes` 来自实测
3. 失败与跳过项在报告里逐条列出
4. 可撤销路径已告知

不满足第 3 条时，报告标题会变成"⚠️ 清理部分完成"，而不是"✨ 清理完成"。

```python
_STATUS_TITLE = {
    OutcomeStatus.COMPLETED: "✨ 清理完成",
    OutcomeStatus.PARTIAL:   "⚠️ 清理部分完成",
    OutcomeStatus.FAILED:    "❌ 清理未成功",
    OutcomeStatus.BLOCKED:   "🛡️ 操作已阻断",
    OutcomeStatus.PREVIEW_ONLY: "🔍 演练结束",
}
```

标题就承载了判定结果，不靠正文找补。

---

## 8. 模型在这个决策体系里的位置

| 可以做 | 不可以做 |
|--------|---------|
| 把结构化结果写得通顺 | 决定处理哪些路径 |
| 解释某个目录为什么大 | 决定风险等级 |
| 在报告后追加一段说明 | 修改任何数字 |
| — | 触发执行 |

`_narrate()` 的实现保证这一点：它把已经渲染好的 `turn.message` 作为输入，只做追加，不替换。

```python
return turn.message + f"\n\n🤖 说明（{self.llm.name}）：{comment}"
```

即使模型返回了完全错误的内容，前面的事实部分也不会被覆盖。

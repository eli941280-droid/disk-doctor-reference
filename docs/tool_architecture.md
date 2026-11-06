# 工具架构

## 1. 三层分离

产品界面上写的是"6 个技能"。照抄这 6 个名字建 6 个模块，会得到一个好看但错误的架构。
本项目按**权限**重新切分，并把"声明"与"绑定"分开：

| 层 | 含义 | 本项目对应 |
|----|------|-----------|
| Capability Declaration | 对用户的承诺（界面上那 6 个名字） | `registry.py` 的 `summary` 字段 |
| Tool Binding | 真实的函数绑定 | `Tool.handler` |
| Invocation Policy | 何时可以调用 | `read_only` 标志 + 编排层分支 |

三者可以不一致，而这**不是缺陷**。不一致本身是一种设计选择：对外承诺稳定，对内实现可演进。

## 2. 注册表

```python
registry.register(
    name="safe_executor.execute",
    summary="执行清理计划（唯一会写盘的路径）",
    handler=executor.execute,
    read_only=False,
    danger="moves files to trash",
)
```

`read_only` 不只是文档字段，它被测试直接校验：

```python
def test_only_one_tool_writes(agent):
    writers = [t for t in agent.capabilities()["tools"] if not t["read_only"]]
    assert len(writers) == 1
```

如果将来有人加了第二个写工具，这个测试会失败。**架构约束变成可执行的断言，而不是文档里的一句话。**

## 3. 六个模块

| 模块 | 权限 | 输入 | 输出 | 关键实现细节 |
|------|------|------|------|------------|
| `disk_scanner` | 只读 | path | volume / dirs / files | 先拒绝链接根（`UnsafeScanRoot`）；`os.walk` 关闭 `followlinks`；遍历中的 `OSError` 吞掉不中断 |
| `cache_analyzer` | 只读 | roots | `Candidate[]` | 深度 ≤ 4；命中签名后不再下探；未识别不产出 |
| `duplicate_finder` | 只读 | roots | `DuplicateGroup[]` | 三级筛选，见 §4 |
| `safe_executor` | **写** | `CleanPlan` + 凭证 | `ExecutionResult` | 五道闸门 + 预检 + 逐项复检 |
| `trash_manager` | 探测 | — | availability | v0.2.2 起只读：没有 `move()`，只报告后端状态 |
| `achievements` | 写自身统计 | freed / files | 新解锁列表 | 写失败静默忽略，不影响主流程 |

### 为什么 cache_analyzer 不负责删除

产品概念里 `cache-cleaner` 是"清理器"。如果照着实现，它会同时具备"发现"和"删除"两个能力，
那么"先预览后执行"就只能靠它自觉。

拆开之后：

- `cache_analyzer` 产出 `Candidate`
- `plan_builder` 过滤成 `PlanItem`
- `safe_executor` 执行

**预览变成结构性保证**：中间隔着两个对象，绕不过去。

## 4. 重复检测的三级筛选

| 级别 | 判据 | 成本 | 过滤效果 |
|------|------|------|---------|
| 1 | 文件大小 | 一次 `stat` | 消除绝大多数 |
| 2 | 首 64 KiB 的 SHA-256 | 读 64 KiB | 分离同大小不同内容 |
| 3 | 全文件 SHA-256 | 全读 | 确认 |

三级的意义在成本：10000 个文件里可能只有几十个需要全量读取。
直接对全部文件做全量哈希，在机械硬盘上能把一次扫描从 30 秒拖到 30 分钟。

## 5. 工具调用策略

| 场景 | 调用 | 理由 |
|------|------|------|
| 用户问"磁盘怎么样" | `disk_scanner.scan` | 只读，成本低，先做 |
| 用户问"能清多少" | `cache_analyzer.discover` | 只需要估算，不需要执行能力 |
| 用户问"有重复吗" | `duplicate_finder.find` | 只报告，不删除 |
| 用户说"清理" | `planner.plan`（不执行） | 默认停在预览 |
| 用户明确确认 | `safe_executor.execute` | 唯一写路径 |
| 模型想调用删除 | **不提供此能力** | 模型没有工具调用权限 |

最后一行是架构决定，不是遗漏：`_narrate()` 只接收已渲染文本，不接收工具句柄。

## 6. 错误传播

```
工具内部异常  →  工具吞掉并降级（返回空/0）
编排层异常    →  返回 AgentTurn，message 说明原因
执行层异常    →  单项 FAILED，批次继续
```

三条规则对应三种粒度。最容易出错的是第一层：扫描时一个目录没权限，不应该让整个诊断失败。

```python
except OSError:
    continue   # 单个子目录读不到，跳过
```

## 7. 可替换性

`TrashBackend` 是 `Protocol`，不是基类：

```python
@runtime_checkable
class TrashBackend(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def move(self, path: Path) -> None: ...
```

两个实现：

- `Send2TrashBackend` —— 真实回收站
- `QuarantineBackend` —— 本地隔离目录

第二个不是为了测试方便才存在的。在没有回收站的环境（容器、网络盘、CI）里，它让"可逆"这件事仍然成立。

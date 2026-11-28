# kubelet-hugepage-reservation-validator

离线验证 hugepage 预留、NUMA 布局与 Pod 真实可调度性的工具。

## 背景

节点预先预留了 hugepage 后，调度器只检查节点级别的**总量**，而 kubelet
实际分配时必须满足 **page size + NUMA 节点** 两个维度。总量够、但对应
page size 或 NUMA 节点上没有连续可用的页时，Pod 会被调度器放行、却被
kubelet 拒绝。本工具按 kubelet 的视角重新计算每个 Pod 的真实可调度性。

## 重要说明：不连接 kubelet

本工具是**纯离线计算**：不连接 kubelet、不访问 API server、不读取宿主机
的 `/sys/kernel/mm/hugepages` 或 cgroup 文件系统。所有输入（节点 page
size、NUMA 布局、预留量、Pod request、当前分配、cgroup 限制、节点状态）
都以 Python 对象的形式显式传入，便于在测试和仿真环境中复现问题。

## 环境

- Python 3.8.2+（仅使用标准库：`dataclasses`、`typing`、`unittest`）

## 输入模型

- `HugePagePool`：某一 page size（KiB）在某一 NUMA 节点上的页池，含
  `total_pages` / `reserved_pages` / `allocated_pages`
- `CgroupLimit`：hugetlb cgroup 对某一 page size 的页数上限
- `PodRequest`：Pod 的 page size、页数和可选的 NUMA 亲和列表
- `Node`：页池集合 + cgroup 限制 + Ready 状态

## 判定逻辑

对每个 Pod 依次检查，输出不可调度原因（可多个并存）：

- `NODE_NOT_READY`：节点 NotReady
- `PAGE_SIZE_MISMATCH`：节点没有该 page size 的页池
- `CGROUP_LIMIT_EXCEEDED`：请求超过 hugetlb cgroup 上限
- `NUMA_INSUFFICIENT`：节点总量够，但 Pod 可用的 NUMA 节点上不够
  （跨 NUMA 碎片，或 NUMA 亲和限制了可用页池）
- `INSUFFICIENT_PAGES`：节点全局总量也不够

## 预留审计与回滚

`HugePageValidator.check_reservations()` 检测错误预留并就地回滚：

- `NEGATIVE_PAGES`：负计数，钳制为 0
- `RESERVED_EXCEEDS_TOTAL`：预留超过总量
- `OVER_COMMIT`：`reserved + allocated > total`，将 `reserved` 回滚到
  `total - allocated`（`allocated` 是真实用量，不回滚）

## 输出

`validate(pods)` 返回报告字典：

- `available_pages`：按 page size → NUMA 节点分组的可用页数
- `error_reservations`：检测到的错误预留
- `rollback_actions`：执行的回滚动作
- `pods`：每个 Pod 的 `schedulable`、`reasons`、`available_pages`

## 用法

```python
from hugepage_validator import (
    HugePagePool, HugePageValidator, Node, PodRequest,
)

node = Node("node-0", [
    HugePagePool(2048, numa_node=0, total_pages=1024,
                 reserved_pages=512, allocated_pages=256),
    HugePagePool(2048, numa_node=1, total_pages=1024,
                 reserved_pages=512, allocated_pages=256),
])
report = HugePageValidator(node).validate([
    PodRequest("demo", page_size_kb=2048, pages=300),
])
# report["pods"][0]["reasons"] == ["NUMA_INSUFFICIENT"]（碎片：单个
# NUMA 节点只有 256 页可用，无法满足 300 页）
```

## 测试

```bash
python -m unittest
```

测试覆盖：NUMA 不足（碎片 / 亲和耗尽 / 亲和到无该 page size 的节点）、
page size 不匹配（含"调度器总量够但 kubelet 分不出"的场景）、cgroup
上限、错误预留检测与回滚、节点 NotReady 及恢复后重新可调度。

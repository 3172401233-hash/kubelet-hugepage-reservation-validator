"""Offline hugepage reservation / schedulability validator.

Simulates the gap between the Kubernetes scheduler (which only checks the
node-level total of pre-reserved hugepages) and the kubelet (which must
actually allocate pages of a specific size on a specific NUMA node).

This module is a pure, offline computation. It never talks to a kubelet,
an API server, or reads /sys/kernel/mm/hugepages from the host.

Compatible with Python 3.8.2+.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Unschedulable reason codes.
REASON_NODE_NOT_READY = "NODE_NOT_READY"
REASON_PAGE_SIZE_MISMATCH = "PAGE_SIZE_MISMATCH"
REASON_CGROUP_LIMIT_EXCEEDED = "CGROUP_LIMIT_EXCEEDED"
REASON_NUMA_INSUFFICIENT = "NUMA_INSUFFICIENT"
REASON_INSUFFICIENT_PAGES = "INSUFFICIENT_PAGES"

# Reservation error codes.
ERR_NEGATIVE_PAGES = "NEGATIVE_PAGES"
ERR_RESERVED_EXCEEDS_TOTAL = "RESERVED_EXCEEDS_TOTAL"
ERR_OVER_COMMIT = "OVER_COMMIT"


@dataclass
class HugePagePool:
    """Pages of one page size on one NUMA node."""

    page_size_kb: int
    numa_node: int
    total_pages: int
    reserved_pages: int = 0
    allocated_pages: int = 0

    @property
    def available_pages(self) -> int:
        return max(
            0, self.total_pages - self.reserved_pages - self.allocated_pages
        )


@dataclass
class CgroupLimit:
    """hugetlb cgroup limit for one page size (expressed in pages)."""

    page_size_kb: int
    max_pages: int


@dataclass
class PodRequest:
    """A pod's hugepage request.

    numa_affinity: list of NUMA node ids the pod is pinned to, or None
    when the pod may land on any single NUMA node.
    """

    name: str
    page_size_kb: int
    pages: int
    numa_affinity: Optional[List[int]] = None


@dataclass
class PodValidationResult:
    pod_name: str
    schedulable: bool
    reasons: List[str]
    page_size_kb: int
    requested_pages: int
    available_pages: Dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pod": self.pod_name,
            "schedulable": self.schedulable,
            "reasons": list(self.reasons),
            "page_size_kb": self.page_size_kb,
            "requested_pages": self.requested_pages,
            "available_pages": dict(self.available_pages),
        }


class Node:
    """A node with hugepage pools, cgroup limits and a ready condition."""

    def __init__(self, name, pools, cgroup_limits=None, ready=True):
        self.name = name
        self.pools = list(pools)
        self.cgroup_limits = list(cgroup_limits or [])
        self.ready = ready

    def pools_for(self, page_size_kb, numa_affinity=None):
        result = []
        for pool in self.pools:
            if pool.page_size_kb != page_size_kb:
                continue
            if numa_affinity is not None and pool.numa_node not in numa_affinity:
                continue
            result.append(pool)
        return result

    def cgroup_limit_for(self, page_size_kb):
        for limit in self.cgroup_limits:
            if limit.page_size_kb == page_size_kb:
                return limit
        return None


class HugePageValidator:
    """Validates pod schedulability against a node's real hugepage state."""

    def __init__(self, node):
        self.node = node
        self.error_reservations = []
        self.rollback_actions = []

    def check_reservations(self):
        """Detect erroneous reservations and roll them back in place.

        Allocated pages reflect real usage and are never rolled back;
        only the reserved (bookkeeping) counters are corrected.
        """
        self.error_reservations = []
        self.rollback_actions = []
        for pool in self.node.pools:
            label = "page_size={}KiB numa={}".format(
                pool.page_size_kb, pool.numa_node
            )
            if pool.reserved_pages < 0 or pool.allocated_pages < 0:
                self.error_reservations.append(
                    "{}: {} (reserved={}, allocated={})".format(
                        label,
                        ERR_NEGATIVE_PAGES,
                        pool.reserved_pages,
                        pool.allocated_pages,
                    )
                )
                old_reserved = pool.reserved_pages
                old_allocated = pool.allocated_pages
                pool.reserved_pages = max(0, pool.reserved_pages)
                pool.allocated_pages = max(0, pool.allocated_pages)
                self.rollback_actions.append(
                    "{}: clamped negative counters "
                    "(reserved {} -> {}, allocated {} -> {})".format(
                        label,
                        old_reserved,
                        pool.reserved_pages,
                        old_allocated,
                        pool.allocated_pages,
                    )
                )
            if pool.reserved_pages > pool.total_pages:
                self.error_reservations.append(
                    "{}: {} (reserved={} > total={})".format(
                        label,
                        ERR_RESERVED_EXCEEDS_TOTAL,
                        pool.reserved_pages,
                        pool.total_pages,
                    )
                )
            if pool.reserved_pages + pool.allocated_pages > pool.total_pages:
                self.error_reservations.append(
                    "{}: {} (reserved={} + allocated={} > total={})".format(
                        label,
                        ERR_OVER_COMMIT,
                        pool.reserved_pages,
                        pool.allocated_pages,
                        pool.total_pages,
                    )
                )
                old_reserved = pool.reserved_pages
                pool.reserved_pages = max(
                    0, pool.total_pages - pool.allocated_pages
                )
                self.rollback_actions.append(
                    "{}: rolled back reserved {} -> {} "
                    "(allocated={} is real usage)".format(
                        label,
                        old_reserved,
                        pool.reserved_pages,
                        pool.allocated_pages,
                    )
                )
        return self.error_reservations

    def available_pages(self):
        """Available pages grouped by page size then NUMA node."""
        result = {}
        for pool in self.node.pools:
            per_numa = result.setdefault(pool.page_size_kb, {})
            per_numa[pool.numa_node] = (
                per_numa.get(pool.numa_node, 0) + pool.available_pages
            )
        return result

    def validate_pod(self, pod):
        reasons = []
        matching = self.node.pools_for(pod.page_size_kb)
        candidates = self.node.pools_for(pod.page_size_kb, pod.numa_affinity)
        available = {p.numa_node: p.available_pages for p in candidates}

        if not self.node.ready:
            reasons.append(REASON_NODE_NOT_READY)

        if not matching:
            reasons.append(REASON_PAGE_SIZE_MISMATCH)
        else:
            limit = self.node.cgroup_limit_for(pod.page_size_kb)
            if limit is not None and pod.pages > limit.max_pages:
                reasons.append(REASON_CGROUP_LIMIT_EXCEEDED)

            if not candidates:
                # Page size exists, but not on any NUMA node the pod
                # is allowed to use.
                reasons.append(REASON_NUMA_INSUFFICIENT)
            else:
                best = max(p.available_pages for p in candidates)
                if best >= pod.pages:
                    pass  # a single NUMA node can satisfy the request
                else:
                    global_total = sum(
                        p.available_pages for p in matching
                    )
                    if global_total >= pod.pages:
                        # The node has enough pages overall, but the
                        # NUMA nodes the pod may use cannot satisfy it
                        # (fragmentation or NUMA affinity).
                        reasons.append(REASON_NUMA_INSUFFICIENT)
                    else:
                        reasons.append(REASON_INSUFFICIENT_PAGES)

        return PodValidationResult(
            pod_name=pod.name,
            schedulable=not reasons,
            reasons=reasons,
            page_size_kb=pod.page_size_kb,
            requested_pages=pod.pages,
            available_pages=available,
        )

    def validate(self, pods):
        """Full report: reservation audit + per-pod schedulability."""
        self.check_reservations()
        results = [self.validate_pod(pod) for pod in pods]
        return {
            "node": self.node.name,
            "node_ready": self.node.ready,
            "available_pages": self.available_pages(),
            "error_reservations": list(self.error_reservations),
            "rollback_actions": list(self.rollback_actions),
            "pods": [r.to_dict() for r in results],
        }

"""Unit tests for the offline hugepage reservation validator.

Run with: python -m unittest
Compatible with Python 3.8.2+.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hugepage_validator import (
    CgroupLimit,
    ERR_OVER_COMMIT,
    ERR_RESERVED_EXCEEDS_TOTAL,
    HugePagePool,
    HugePageValidator,
    Node,
    PodRequest,
    REASON_CGROUP_LIMIT_EXCEEDED,
    REASON_INSUFFICIENT_PAGES,
    REASON_NODE_NOT_READY,
    REASON_NUMA_INSUFFICIENT,
    REASON_PAGE_SIZE_MISMATCH,
)

KB_2M = 2048
KB_1G = 1048576


def make_node(ready=True):
    """Two NUMA nodes, each with 2MB and 1GB pools."""
    pools = [
        HugePagePool(KB_2M, numa_node=0, total_pages=1024,
                     reserved_pages=512, allocated_pages=256),
        HugePagePool(KB_2M, numa_node=1, total_pages=1024,
                     reserved_pages=512, allocated_pages=256),
        HugePagePool(KB_1G, numa_node=0, total_pages=8,
                     reserved_pages=4, allocated_pages=2),
        HugePagePool(KB_1G, numa_node=1, total_pages=8,
                     reserved_pages=4, allocated_pages=2),
    ]
    limits = [
        CgroupLimit(KB_2M, max_pages=512),
        CgroupLimit(KB_1G, max_pages=4),
    ]
    return Node("node-0", pools, cgroup_limits=limits, ready=ready)


class TestSchedulablePods(unittest.TestCase):
    def setUp(self):
        self.node = make_node()
        self.validator = HugePageValidator(self.node)

    def test_pod_fits_on_single_numa(self):
        pod = PodRequest("ok-2m", KB_2M, pages=100)
        result = self.validator.validate_pod(pod)
        self.assertTrue(result.schedulable)
        self.assertEqual(result.reasons, [])
        self.assertEqual(result.available_pages, {0: 256, 1: 256})

    def test_multiple_page_sizes_are_independent(self):
        # 1GB request must not be satisfied by 2MB pages and vice versa.
        pod_1g = PodRequest("ok-1g", KB_1G, pages=2, numa_affinity=[0])
        pod_2m = PodRequest("ok-2m", KB_2M, pages=200, numa_affinity=[1])
        self.assertTrue(self.validator.validate_pod(pod_1g).schedulable)
        self.assertTrue(self.validator.validate_pod(pod_2m).schedulable)

    def test_numa_affinity_selects_pool(self):
        pod = PodRequest("pinned", KB_2M, pages=10, numa_affinity=[1])
        result = self.validator.validate_pod(pod)
        self.assertTrue(result.schedulable)
        self.assertEqual(result.available_pages, {1: 256})


class TestPageSizeMismatch(unittest.TestCase):
    def test_unsupported_page_size(self):
        node = make_node()
        validator = HugePageValidator(node)
        pod = PodRequest("bad-size", page_size_kb=16, pages=4)
        result = validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_PAGE_SIZE_MISMATCH, result.reasons)

    def test_scheduler_total_would_pass_but_kubelet_fails(self):
        # Node advertises 2048 x 2MB pages (= 4GiB) in total. A pod
        # asking for 4 x 1GB pages (= 4GiB) matches the aggregate bytes
        # the scheduler looks at, but only 2 free 1GB pages exist per
        # NUMA node, so the kubelet cannot actually allocate them.
        node = make_node()
        validator = HugePageValidator(node)
        pod = PodRequest("greedy-1g", KB_1G, pages=4)
        result = validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_NUMA_INSUFFICIENT, result.reasons)


class TestNumaInsufficiency(unittest.TestCase):
    def setUp(self):
        self.node = make_node()
        self.validator = HugePageValidator(self.node)

    def test_fragmentation_across_numa(self):
        # 256 free pages on each NUMA node (512 total), but the pod
        # needs 300 on one node: fragmented, unschedulable.
        pod = PodRequest("fragmented", KB_2M, pages=300)
        result = self.validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_NUMA_INSUFFICIENT, result.reasons)

    def test_affinity_to_exhausted_numa(self):
        pod = PodRequest("pinned-full", KB_2M, pages=300, numa_affinity=[0])
        result = self.validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_NUMA_INSUFFICIENT, result.reasons)

    def test_affinity_to_numa_without_page_size(self):
        node = Node(
            "node-single",
            [HugePagePool(KB_2M, numa_node=0, total_pages=64)],
        )
        validator = HugePageValidator(node)
        pod = PodRequest("wrong-numa", KB_2M, pages=4, numa_affinity=[1])
        result = validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_NUMA_INSUFFICIENT, result.reasons)
        self.assertNotIn(REASON_PAGE_SIZE_MISMATCH, result.reasons)

    def test_aggregate_insufficient(self):
        pod = PodRequest("too-big", KB_2M, pages=600)
        result = self.validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_INSUFFICIENT_PAGES, result.reasons)


class TestCgroupLimit(unittest.TestCase):
    def test_cgroup_limit_exceeded(self):
        # Plenty of free 1GB pages, but the hugetlb cgroup caps the
        # pod at 2 pages.
        node = Node(
            "node-cgroup",
            [HugePagePool(KB_1G, numa_node=0, total_pages=8)],
            cgroup_limits=[CgroupLimit(KB_1G, max_pages=2)],
        )
        validator = HugePageValidator(node)
        pod = PodRequest("cgroup-bust", KB_1G, pages=4)
        result = validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_CGROUP_LIMIT_EXCEEDED, result.reasons)
        self.assertEqual(result.reasons, [REASON_CGROUP_LIMIT_EXCEEDED])


class TestReservationRollback(unittest.TestCase):
    def test_over_commit_detected_and_rolled_back(self):
        pool = HugePagePool(KB_2M, numa_node=0, total_pages=100,
                            reserved_pages=90, allocated_pages=50)
        node = Node("node-oc", [pool])
        validator = HugePageValidator(node)
        errors = validator.check_reservations()
        self.assertTrue(any(ERR_OVER_COMMIT in e for e in errors))
        # Allocated pages are real usage; reserved rolls back to fit.
        self.assertEqual(pool.allocated_pages, 50)
        self.assertEqual(pool.reserved_pages, 50)
        self.assertEqual(pool.available_pages, 0)
        self.assertEqual(len(validator.rollback_actions), 1)

    def test_reserved_exceeds_total(self):
        pool = HugePagePool(KB_2M, numa_node=0, total_pages=64,
                            reserved_pages=128)
        node = Node("node-over", [pool])
        validator = HugePageValidator(node)
        errors = validator.check_reservations()
        self.assertTrue(any(ERR_RESERVED_EXCEEDS_TOTAL in e for e in errors))
        # Over-commit rollback caps reserved at total.
        self.assertEqual(pool.reserved_pages, 64)
        self.assertEqual(pool.available_pages, 0)

    def test_negative_counters_clamped(self):
        pool = HugePagePool(KB_2M, numa_node=0, total_pages=64,
                            reserved_pages=-5, allocated_pages=-2)
        node = Node("node-neg", [pool])
        validator = HugePageValidator(node)
        errors = validator.check_reservations()
        self.assertEqual(len(errors), 1)
        self.assertEqual(pool.reserved_pages, 0)
        self.assertEqual(pool.allocated_pages, 0)
        self.assertEqual(pool.available_pages, 64)

    def test_legitimate_reservation_is_not_rolled_back(self):
        # reserved(95) + allocated(0) <= total(100): the reservation is
        # legitimate, so nothing is rolled back and the pod that needs
        # 50 pages stays unschedulable (only 5 pages are free).
        pool = HugePagePool(KB_2M, numa_node=0, total_pages=100,
                            reserved_pages=95, allocated_pages=0)
        node = Node("node-fix", [pool])
        pod = PodRequest("blocked", KB_2M, pages=50)
        validator = HugePageValidator(node)
        self.assertEqual(validator.check_reservations(), [])
        self.assertEqual(validator.rollback_actions, [])
        result = validator.validate_pod(pod)
        self.assertFalse(result.schedulable)
        self.assertIn(REASON_INSUFFICIENT_PAGES, result.reasons)


class TestNodeRecovery(unittest.TestCase):
    def test_not_ready_then_recovered(self):
        node = make_node(ready=False)
        validator = HugePageValidator(node)
        pod = PodRequest("waiting", KB_2M, pages=10)

        down = validator.validate_pod(pod)
        self.assertFalse(down.schedulable)
        self.assertIn(REASON_NODE_NOT_READY, down.reasons)

        # Node recovers: the same pod becomes schedulable.
        node.ready = True
        up = validator.validate_pod(pod)
        self.assertTrue(up.schedulable)
        self.assertEqual(up.reasons, [])

    def test_not_ready_does_not_mask_other_reasons(self):
        node = make_node(ready=False)
        validator = HugePageValidator(node)
        pod = PodRequest("double-trouble", page_size_kb=16, pages=1)
        result = validator.validate_pod(pod)
        self.assertIn(REASON_NODE_NOT_READY, result.reasons)
        self.assertIn(REASON_PAGE_SIZE_MISMATCH, result.reasons)


class TestFullReport(unittest.TestCase):
    def test_report_shape(self):
        pool = HugePagePool(KB_2M, numa_node=0, total_pages=100,
                            reserved_pages=150, allocated_pages=10)
        node = Node("node-report", [pool])
        validator = HugePageValidator(node)
        pods = [
            PodRequest("fits", KB_2M, pages=50),
            PodRequest("bad-size", 16, pages=1),
        ]
        report = validator.validate(pods)

        self.assertEqual(report["node"], "node-report")
        self.assertTrue(report["node_ready"])
        # reserved 150 + allocated 10 > total 100 -> rolled back to 90.
        self.assertTrue(report["error_reservations"])
        self.assertTrue(report["rollback_actions"])
        self.assertEqual(report["available_pages"], {KB_2M: {0: 0}})

        by_name = {p["pod"]: p for p in report["pods"]}
        self.assertFalse(by_name["fits"]["schedulable"])
        self.assertIn(REASON_INSUFFICIENT_PAGES, by_name["fits"]["reasons"])
        self.assertIn(REASON_PAGE_SIZE_MISMATCH,
                      by_name["bad-size"]["reasons"])


if __name__ == "__main__":
    unittest.main()

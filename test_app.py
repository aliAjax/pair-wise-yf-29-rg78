import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _tail(self, user, evidence_id):
        return self.store.get_evidence(user, evidence_id)["chain_tail"]["event_hash"]

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱", expected_tail=self._tail("custodian1", item["id"]))
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交", expected_tail=self._tail("custodian2", item["id"]))
        # 释放需两名保管员分别见证
        r1 = self.store.release("custodian1", item["id"], "检察机关", "按调取令释放原件", expected_tail=self._tail("custodian1", item["id"]))
        self.assertEqual(r1["status"], "pending_release")
        self.assertEqual(r1["witness_count"], 1)
        r2 = self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件", expected_tail=self._tail("custodian2", item["id"]))
        self.assertEqual(r2["status"], "released")
        self.assertEqual(r2["witnesses"], ["custodian1", "custodian2"])
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        self.assertEqual(report["issues"], [])
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertTrue(original["witness_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_chain_conflict_when_position_occupied(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        tail = self._tail("custodian1", item["id"])
        # 第一名保管员先提交，占用下一个链位
        self.store.open_evidence("custodian1", item["id"], "A 区证物室", expected_tail=tail)
        # 第二名保管员仍持旧链尾提交，应被退回并提示重新查看
        with self.assertRaises(BusinessError) as ctx:
            self.store.open_evidence("custodian2", item["id"], "B 区证物室", expected_tail=tail)
        self.assertEqual(ctx.exception.code, "chain_conflict")
        # 重新查看后链尾已更新、状态为已开箱，不再重复提交
        reloaded = self.store.get_evidence("custodian2", item["id"])
        self.assertEqual(reloaded["status"], "opened")
        self.assertNotEqual(reloaded["chain_tail"]["event_hash"], tail)

    def test_duplicate_witness_and_missing_witness_flagged(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-003", "dup.bin",
            base64.b64encode(b"dup").decode(), self.retention,
        )
        self.store.release("custodian1", item["id"], "接收方", expected_tail=self._tail("custodian1", item["id"]))
        # 同一名保管员不能重复见证
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "接收方", expected_tail=self._tail("custodian1", item["id"]))
        self.assertEqual(ctx.exception.code, "duplicate_witness")
        # 报告中点名未释放证据的待见证状态
        report = self.store.report("auditor1", self.case["id"])
        ev = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertTrue(ev["pending_release"])
        self.assertTrue(ev["witness_valid"])
        self.assertEqual(report["issues"], [])

    def test_retention_change_requires_reconfirm_before_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-004", "ret.bin",
            base64.b64encode(b"ret").decode(), self.retention,
        )
        new_retention = (date.today() + timedelta(days=365)).isoformat()
        self.store.set_retention("custodian1", item["id"], new_retention, "保留期限届满续期")
        # 保留期限一变，未释放证据须重新确认保管结论
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "接收方", expected_tail=self._tail("custodian1", item["id"]))
        self.assertEqual(ctx.exception.code, "conclusion_required")
        self.store.confirm_conclusion("custodian1", item["id"], "重新确认继续保管")
        r1 = self.store.release("custodian1", item["id"], "接收方", expected_tail=self._tail("custodian1", item["id"]))
        self.assertEqual(r1["status"], "pending_release")
        self.store.release("custodian2", item["id"], "接收方", expected_tail=self._tail("custodian2", item["id"]))
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-005", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")


if __name__ == "__main__":
    unittest.main()

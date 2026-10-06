import base64
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

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

    def _ingest(self, label="E-001"):
        return self.store.ingest_evidence(
            "custodian1", self.case["id"], label, f"{label}.bin",
            base64.b64encode(b"evidence bytes").decode(), self.retention, "custodian1",
        )

    def _tail(self, evidence_id):
        return self.store.get_evidence("custodian1", evidence_id)["chain_tail"]

    def test_full_custody_analysis_release_and_integrity_report(self):
        item = self._ingest()
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱", item["chain_tail"])
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(), opened["chain_tail"],
        )
        moved = self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交", child["parent_chain_tail"])
        first = self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件", moved["chain_tail"])
        self.assertEqual(first["status"], "awaiting_witness")
        second = self.store.release("custodian1", item["id"], "检察机关", "第二保管员见证", first["chain_tail"])
        self.assertEqual(second["status"], "released")
        self.assertEqual(second["witnesses"], ["custodian1", "custodian2"])
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["chain_issues"], [])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self._ingest("E-002")
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构", expected_tail=item["chain_tail"])
        self.assertEqual(ctx.exception.status, 403)
        held = self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求", item["chain_tail"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构", expected_tail=held["chain_tail"])
        self.assertEqual(ctx.exception.code, "legal_hold_active")

    def test_stale_tail_rejected_and_tail_required(self):
        item = self._ingest("E-003")
        self.store.transfer("custodian1", item["id"], "custodian2", "A 区证物室", expected_tail=item["chain_tail"])
        # 第二名保管员仍拿着旧链尾提交，链位已被占用
        with self.assertRaises(BusinessError) as ctx:
            self.store.transfer("custodian2", item["id"], "custodian2", "B 区证物室", expected_tail=item["chain_tail"])
        self.assertEqual(ctx.exception.code, "chain_conflict")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(BusinessError) as ctx:
            self.store.open_evidence("custodian2", item["id"], "B 区证物室")
        self.assertEqual(ctx.exception.code, "missing_chain_tail")
        # 重新查看后拿到新链尾即可提交
        tail = self._tail(item["id"])
        moved = self.store.transfer("custodian2", item["id"], "custodian2", "B 区证物室", expected_tail=tail)
        self.assertEqual(moved["current_custodian"], "custodian2")

    def test_concurrent_submissions_conflict_on_chain_position(self):
        item = self._ingest("E-004")
        results, errors = [], []
        barrier = threading.Barrier(2)

        def work(user, location):
            try:
                barrier.wait(timeout=5)
                results.append(self.store.transfer(user, item["id"], user, location, expected_tail=item["chain_tail"]))
            except BusinessError as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=work, args=("custodian1", "A 区证物室")),
            threading.Thread(target=work, args=("custodian2", "B 区证物室")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "chain_conflict")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["chain_length"], 2)  # INGEST + 唯一一次成功的 TRANSFER

    def test_write_failure_rolls_back_and_retry_reuses_same_tail(self):
        item = self._ingest("E-005")
        original = self.store._append_event
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "_append_event", side_effect=flaky):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.transfer("custodian1", item["id"], "custodian2", "证物室", expected_tail=item["chain_tail"])
        # 写盘失败不占用链位，链尾保持原样
        self.assertEqual(self._tail(item["id"]), item["chain_tail"])
        retried = self.store.transfer("custodian1", item["id"], "custodian2", "证物室", expected_tail=item["chain_tail"])
        self.assertEqual(retried["current_custodian"], "custodian2")

    def test_release_requires_two_distinct_custodian_witnesses(self):
        item = self._ingest("E-006")
        first = self.store.release("custodian1", item["id"], "检察机关", expected_tail=item["chain_tail"])
        self.assertEqual(first["status"], "awaiting_witness")
        self.assertEqual(first["witnesses"], ["custodian1"])
        # 同一保管员不能重复见证
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "检察机关", expected_tail=first["chain_tail"])
        self.assertEqual(ctx.exception.code, "duplicate_witness")
        # 接收方必须与已有见证一致
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian2", item["id"], "其他机关", expected_tail=first["chain_tail"])
        self.assertEqual(ctx.exception.code, "recipient_mismatch")
        second = self.store.release("custodian2", item["id"], "检察机关", expected_tail=first["chain_tail"])
        self.assertEqual(second["status"], "released")
        detail = self.store.get_evidence("custodian1", item["id"])
        types = [e["event_type"] for e in detail["events"]]
        self.assertEqual(types.count("RELEASE_WITNESS"), 2)
        self.assertEqual(types.count("RELEASE"), 1)
        self.assertEqual(len(detail["release_witnesses"]), 2)

    def test_retention_change_requires_reconfirm_before_release(self):
        item = self._ingest("E-007")
        new_deadline = (date.today() + timedelta(days=365)).isoformat()
        changed = self.store.set_retention("custodian1", item["id"], new_deadline, "法院裁定延长", item["chain_tail"])
        self.assertTrue(changed["needs_reconfirm"])
        # 保留期限一变，未释放证据必须先重新确认保管结论
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "检察机关", expected_tail=changed["chain_tail"])
        self.assertEqual(ctx.exception.code, "reconfirm_required")
        confirmed = self.store.reconfirm("custodian1", item["id"], "核对无误，继续保管", changed["chain_tail"])
        self.assertFalse(confirmed["needs_reconfirm"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.reconfirm("custodian1", item["id"], "重复确认无意义", confirmed["chain_tail"])
        self.assertEqual(ctx.exception.code, "reconfirm_not_required")
        first = self.store.release("custodian1", item["id"], "检察机关", expected_tail=confirmed["chain_tail"])
        self.assertEqual(first["status"], "awaiting_witness")

    def test_report_names_evidence_on_broken_chain_and_duplicate_witness(self):
        broken = self._ingest("E-008")
        dup = self._ingest("E-009")
        # 篡改：弄断 E-008 的链
        with self.store.connect() as conn:
            conn.execute("UPDATE custody_events SET previous_hash='TAMPERED' WHERE evidence_id=? AND sequence=1", (broken["id"],))
        # 篡改：给 E-009 追加同一保管员的重复见证（绕过接口直写，模拟异常数据）
        first = self.store.release("custodian1", dup["id"], "检察机关", expected_tail=dup["chain_tail"])
        self.assertEqual(first["status"], "awaiting_witness")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.store._append_event(conn, dup["id"], "RELEASE_WITNESS", "custodian1", to_person="检察机关")
        report = self.store.report("auditor1", self.case["id"])
        self.assertFalse(report["overall_integrity_valid"])
        broken_hits = [i["evidence_id"] for i in report["chain_issues"] if i["issue"] == "broken_chain"]
        dup_hits = [i["evidence_id"] for i in report["chain_issues"] if i["issue"] == "duplicate_witness"]
        self.assertEqual(broken_hits, [broken["id"]])
        self.assertEqual(dup_hits, [dup["id"]])
        messages = " ".join(i["message"] for i in report["chain_issues"])
        self.assertIn(f"#{broken['id']}", messages)
        self.assertIn(f"#{dup['id']}", messages)


if __name__ == "__main__":
    unittest.main()

"""法律证据保管与流转后台。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "custody.db"
MEMBER_ROLES = {"custodian", "analyst", "auditor"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CustodyStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_members(
                    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
                    PRIMARY KEY(case_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
                        CHECK(status IN ('custody','opened','released','derivative')),
                    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
                    retention_until TEXT NOT NULL,
                    needs_reconfirm INTEGER NOT NULL DEFAULT 0 CHECK(needs_reconfirm IN (0,1)),
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(case_id,label)
                );
                CREATE TABLE IF NOT EXISTS custody_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','RELEASE_WITNESS','HOLD_SET','HOLD_CLEARED','RETENTION_CHANGE','RECONFIRM')),
                    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
                    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS release_witnesses(
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    actor_id TEXT NOT NULL REFERENCES users(id),
                    recipient TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    PRIMARY KEY(evidence_id,actor_id)
                );
                CREATE TABLE IF NOT EXISTS derivatives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
                    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name) VALUES(?,?)",
                [
                    ("custodian1", "证据保管员甲"), ("custodian2", "证据保管员乙"),
                    ("analyst1", "电子数据分析员"), ("auditor1", "案件审计员"), ("outsider", "外部人员"),
                ],
            )

    def _user(self, conn, user_id):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        return user

    def _case(self, conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("案件不存在", 404, "not_found")
        return row

    def _member(self, conn, case_id, user_id, roles=None):
        user = self._user(conn, user_id)
        self._case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM case_members WHERE case_id=? AND user_id=? AND active=1", (case_id, user_id)
        ).fetchone()
        if not row:
            raise BusinessError("不是案件有效成员", 403, "forbidden")
        if roles and row["role"] not in roles:
            raise BusinessError("当前案件角色无权执行此操作", 403, "forbidden")
        return user, row

    def _audit(self, conn, case_id, actor_id, action, detail):
        conn.execute(
            "INSERT INTO audit_log(case_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (case_id, actor_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def create_case(self, user_id, case_number, title):
        if not case_number.strip() or len(title.strip()) < 2:
            raise BusinessError("案件编号和标题不能为空", 422, "invalid_case")
        with self.connect() as conn:
            self._user(conn, user_id)
            try:
                cur = conn.execute(
                    "INSERT INTO cases(case_number,title,created_by,created_at) VALUES(?,?,?,?)",
                    (case_number.strip(), title.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("案件编号已存在", 409, "case_exists")
            case_id = cur.lastrowid
            conn.execute(
                "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",
                (case_id, user_id, "custodian", user_id, now()),
            )
            self._audit(conn, case_id, user_id, "case.create", {"case_number": case_number.strip()})
            return {"id": case_id, "case_number": case_number.strip(), "title": title.strip()}

    def add_member(self, user_id, case_id, member_id, role):
        if role not in MEMBER_ROLES:
            raise BusinessError("案件角色必须是 custodian、analyst 或 auditor", 422, "invalid_role")
        with self.connect() as conn:
            case = self._case(conn, case_id)
            if case["created_by"] != user_id:
                raise BusinessError("只有案件创建人可以授权成员", 403, "forbidden")
            self._user(conn, member_id)
            conn.execute(
                """INSERT INTO case_members(case_id,user_id,role,active,granted_by,granted_at) VALUES(?,?,?,1,?,?)
                   ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role,active=1,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
                (case_id, member_id, role, user_id, now()),
            )
            self._audit(conn, case_id, user_id, "member.grant", {"member_id": member_id, "role": role})
            return {"case_id": case_id, "member_id": member_id, "role": role}

    @staticmethod
    def _event_hash(event):
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _chain_tail(self, conn, evidence_id):
        row = conn.execute(
            "SELECT event_hash,sequence FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (evidence_id,)
        ).fetchone()
        return (row["event_hash"], row["sequence"]) if row else ("GENESIS", 0)

    def _append_event(self, conn, evidence_id, event_type, actor, from_person="", to_person="", location="", note="",
                      expected_tail=None, check_tail=False):
        previous_hash, sequence = self._chain_tail(conn, evidence_id)
        if check_tail:
            if expected_tail is None or not str(expected_tail).strip():
                raise BusinessError("每次提交必须携带当前链尾 expected_tail", 422, "missing_chain_tail")
            if str(expected_tail).strip() != previous_hash:
                raise BusinessError("链位已被占用，保管链已变化，请重新查看最新链尾后再提交", 409, "chain_conflict")
        sequence += 1
        payload = {
            "evidence_id": evidence_id, "sequence": sequence, "event_type": event_type,
            "actor_id": actor, "from_person": from_person or None, "to_person": to_person or None,
            "location": location, "note": note, "previous_hash": previous_hash, "created_at": now(),
        }
        digest = self._event_hash(payload)
        cur = conn.execute(
            """INSERT INTO custody_events(evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, sequence, event_type, actor, from_person or None, to_person or None, location, note, previous_hash, digest, payload["created_at"]),
        )
        return cur.lastrowid, digest

    def ingest_evidence(self, user_id, case_id, label, filename, content_b64, retention_until, custodian=None):
        label, filename = label.strip(), filename.strip()
        if not label or not filename:
            raise BusinessError("证据标签和文件名不能为空", 422, "invalid_evidence")
        try:
            content = base64.b64decode(content_b64, validate=True)
            deadline = date.fromisoformat(retention_until)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("证据内容 Base64 或保留期限格式错误", 422, "invalid_evidence")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "invalid_retention")
        digest = hashlib.sha256(content).hexdigest()
        custodian = (custodian or user_id).strip()
        with self.connect() as conn:
            self._member(conn, case_id, user_id, {"custodian"})
            if not custodian:
                raise BusinessError("保管人不能为空", 422, "invalid_custodian")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,current_custodian,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (case_id, label, filename, digest, len(content), content, custodian, retention_until, user_id, now()),
                )
                evidence_id = cur.lastrowid
                _, tail = self._append_event(conn, evidence_id, "INGEST", user_id, to_person=custodian, note=f"入册 SHA-256 {digest}")
                self._audit(conn, case_id, user_id, "evidence.ingest", {"evidence_id": evidence_id, "sha256": digest, "label": label})
                return {"id": evidence_id, "label": label, "sha256": digest, "size": len(content), "status": "custody",
                        "current_custodian": custodian, "chain_tail": tail}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该案件中的证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def _evidence(self, conn, evidence_id):
        row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise BusinessError("证据不存在", 404, "not_found")
        return row

    def get_evidence(self, user_id, evidence_id, include_content=False):
        with self.connect() as conn:
            row = self._evidence(conn, evidence_id)
            self._member(conn, row["case_id"], user_id)
            result = {k: row[k] for k in row.keys() if k != "content"}
            result["legal_hold"] = bool(row["legal_hold"])
            result["needs_reconfirm"] = bool(row["needs_reconfirm"])
            result["integrity_valid"] = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
            events = conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (evidence_id,)).fetchall()
            result["events"] = [dict(x) for x in events]
            tail, _ = self._chain_tail(conn, evidence_id)
            result["chain_tail"] = tail
            result["chain_length"] = len(events)
            result["release_witnesses"] = [dict(x) for x in conn.execute(
                "SELECT actor_id,recipient,note,created_at FROM release_witnesses WHERE evidence_id=? ORDER BY created_at,rowid", (evidence_id,)).fetchall()]
            result["derived_children"] = [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (evidence_id,)).fetchall()]
            if include_content:
                result["content_b64"] = base64.b64encode(row["content"]).decode()
            return result

    def transfer(self, user_id, evidence_id, to_person, location, note="", expected_tail=None):
        if not to_person.strip() or not location.strip():
            raise BusinessError("接收人和保管位置不能为空", 422, "invalid_transfer")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据不能再移交", 409, "evidence_released")
                _, tail = self._append_event(conn, evidence_id, "TRANSFER", user_id, from_person=row["current_custodian"],
                                             to_person=to_person.strip(), location=location.strip(), note=note.strip(),
                                             expected_tail=expected_tail, check_tail=True)
                conn.execute("UPDATE evidence SET current_custodian=? WHERE id=?", (to_person.strip(), evidence_id))
                self._audit(conn, row["case_id"], user_id, "custody.transfer", {"evidence_id": evidence_id, "to": to_person.strip(), "location": location.strip()})
                return {"id": evidence_id, "current_custodian": to_person.strip(), "location": location.strip(), "chain_tail": tail}
            except Exception:
                conn.rollback()
                raise

    def open_evidence(self, user_id, evidence_id, location, note="", expected_tail=None):
        if not location.strip():
            raise BusinessError("开箱地点不能为空", 422, "location_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] != "custody":
                    raise BusinessError("只有处于封存保管状态的证据可以开箱", 409, "invalid_status")
                _, tail = self._append_event(conn, evidence_id, "OPEN", user_id, from_person=row["current_custodian"],
                                             location=location.strip(), note=note.strip(),
                                             expected_tail=expected_tail, check_tail=True)
                conn.execute("UPDATE evidence SET status='opened' WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "evidence.open", {"evidence_id": evidence_id, "location": location.strip()})
                return {"id": evidence_id, "status": "opened", "location": location.strip(), "chain_tail": tail}
            except Exception:
                conn.rollback()
                raise

    def derive(self, user_id, evidence_id, method, label, filename, content_b64, expected_tail=None):
        if len(method.strip()) < 3 or not label.strip() or not filename.strip():
            raise BusinessError("分析方法、子证据标签和文件名不能为空", 422, "invalid_derivative")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                parent = self._evidence(conn, evidence_id)
                self._member(conn, parent["case_id"], user_id, {"analyst"})
                if parent["status"] != "opened":
                    raise BusinessError("原始证据必须先开箱才能分析", 409, "evidence_not_opened")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,status,current_custodian,legal_hold,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (parent["case_id"], label.strip(), filename.strip(), digest, len(content), content, "derivative", user_id, 0, parent["retention_until"], user_id, now()),
                )
                child_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO derivatives(parent_evidence_id,child_evidence_id,method,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (evidence_id, child_id, method.strip(), user_id, now()),
                )
                _, parent_tail = self._append_event(conn, evidence_id, "ANALYZE", user_id, from_person=parent["current_custodian"],
                                                    note=f"生成衍生证据 #{child_id}: {method.strip()}",
                                                    expected_tail=expected_tail, check_tail=True)
                _, child_tail = self._append_event(conn, child_id, "INGEST", user_id, from_person=parent["current_custodian"],
                                                   to_person=user_id, note=f"由证据 #{evidence_id} 派生，SHA-256 {digest}")
                self._audit(conn, parent["case_id"], user_id, "evidence.derive", {"parent_id": evidence_id, "child_id": child_id, "method": method.strip(), "sha256": digest})
                return {"id": child_id, "parent_id": evidence_id, "label": label.strip(), "sha256": digest,
                        "status": "derivative", "parent_chain_tail": parent_tail, "chain_tail": child_tail}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("衍生证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def set_hold(self, user_id, evidence_id, hold, reason, expected_tail=None):
        if len(reason.strip()) < 5:
            raise BusinessError("法律保留原因至少 5 字", 422, "reason_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                case = self._case(conn, row["case_id"])
                if user_id != case["created_by"]:
                    self._member(conn, row["case_id"], user_id, {"auditor"})
                event = "HOLD_SET" if hold else "HOLD_CLEARED"
                _, tail = self._append_event(conn, evidence_id, event, user_id, note=reason.strip(),
                                             expected_tail=expected_tail, check_tail=True)
                conn.execute("UPDATE evidence SET legal_hold=? WHERE id=?", (int(bool(hold)), evidence_id))
                self._audit(conn, row["case_id"], user_id, "evidence.hold", {"evidence_id": evidence_id, "hold": bool(hold), "reason": reason.strip()})
                return {"id": evidence_id, "legal_hold": bool(hold), "chain_tail": tail}
            except Exception:
                conn.rollback()
                raise

    def set_retention(self, user_id, evidence_id, retention_until, reason="", expected_tail=None):
        try:
            deadline = date.fromisoformat(retention_until)
        except (ValueError, TypeError):
            raise BusinessError("保留期限格式错误", 422, "invalid_retention")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "invalid_retention")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian", "auditor"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据无需调整保留期限", 409, "evidence_released")
                note = f"保留期限 {row['retention_until']} → {retention_until}"
                if reason.strip():
                    note += f"；{reason.strip()}"
                _, tail = self._append_event(conn, evidence_id, "RETENTION_CHANGE", user_id, note=note,
                                             expected_tail=expected_tail, check_tail=True)
                conn.execute("UPDATE evidence SET retention_until=?, needs_reconfirm=1 WHERE id=?", (retention_until, evidence_id))
                self._audit(conn, row["case_id"], user_id, "evidence.retention",
                            {"evidence_id": evidence_id, "retention_until": retention_until, "reason": reason.strip()})
                return {"id": evidence_id, "retention_until": retention_until, "needs_reconfirm": True, "chain_tail": tail}
            except Exception:
                conn.rollback()
                raise

    def reconfirm(self, user_id, evidence_id, conclusion, expected_tail=None):
        if len(conclusion.strip()) < 5:
            raise BusinessError("保管结论至少 5 字", 422, "conclusion_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据无需重新确认保管结论", 409, "evidence_released")
                if not row["needs_reconfirm"]:
                    raise BusinessError("当前无需重新确认保管结论", 409, "reconfirm_not_required")
                _, tail = self._append_event(conn, evidence_id, "RECONFIRM", user_id, from_person=row["current_custodian"],
                                             note=conclusion.strip(), expected_tail=expected_tail, check_tail=True)
                conn.execute("UPDATE evidence SET needs_reconfirm=0 WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "evidence.reconfirm", {"evidence_id": evidence_id, "conclusion": conclusion.strip()})
                return {"id": evidence_id, "needs_reconfirm": False, "chain_tail": tail}
            except Exception:
                conn.rollback()
                raise

    def release(self, user_id, evidence_id, recipient, note="", expected_tail=None):
        if not recipient.strip():
            raise BusinessError("接收方不能为空", 422, "recipient_required")
        recipient = recipient.strip()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["legal_hold"]:
                    raise BusinessError("存在法律保留，禁止释放证据", 409, "legal_hold_active")
                if row["status"] == "released":
                    raise BusinessError("证据已经释放", 409, "already_released")
                if row["needs_reconfirm"]:
                    raise BusinessError("保留期限已变更，需先重新确认保管结论再释放", 409, "reconfirm_required")
                witnesses = conn.execute(
                    "SELECT * FROM release_witnesses WHERE evidence_id=? ORDER BY created_at,rowid", (evidence_id,)).fetchall()
                if any(w["actor_id"] == user_id for w in witnesses):
                    raise BusinessError("同一保管员不能重复见证同一次释放", 409, "duplicate_witness")
                if witnesses and witnesses[0]["recipient"] != recipient:
                    raise BusinessError("释放接收方与已有见证不一致", 409, "recipient_mismatch")
                _, witness_tail = self._append_event(conn, evidence_id, "RELEASE_WITNESS", user_id,
                                                     from_person=row["current_custodian"], to_person=recipient,
                                                     note=note.strip() or "释放见证",
                                                     expected_tail=expected_tail, check_tail=True)
                conn.execute(
                    "INSERT INTO release_witnesses(evidence_id,actor_id,recipient,note,created_at) VALUES(?,?,?,?,?)",
                    (evidence_id, user_id, recipient, note.strip(), now()),
                )
                distinct = sorted({w["actor_id"] for w in witnesses} | {user_id})
                if len(distinct) >= 2:
                    _, tail = self._append_event(conn, evidence_id, "RELEASE", user_id,
                                                 from_person=row["current_custodian"], to_person=recipient,
                                                 note=f"两名保管员分别见证（{'、'.join(distinct)}），执行释放")
                    conn.execute("UPDATE evidence SET status='released' WHERE id=?", (evidence_id,))
                    self._audit(conn, row["case_id"], user_id, "evidence.release",
                                {"evidence_id": evidence_id, "recipient": recipient, "witnesses": distinct})
                    return {"id": evidence_id, "status": "released", "recipient": recipient,
                            "witnesses": distinct, "chain_tail": tail}
                self._audit(conn, row["case_id"], user_id, "evidence.release_witness",
                            {"evidence_id": evidence_id, "recipient": recipient, "witnesses": distinct})
                return {"id": evidence_id, "status": "awaiting_witness", "recipient": recipient,
                        "witnesses": distinct, "chain_tail": witness_tail}
            except Exception:
                conn.rollback()
                raise

    def report(self, user_id, case_id):
        with self.connect() as conn:
            self._member(conn, case_id, user_id)
            case = self._case(conn, case_id)
            items, chain_issues, all_valid = [], [], True
            for row in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                hash_valid = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
                events = conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (row["id"],)).fetchall()
                expected_prev, expected_seq, chain_valid = "GENESIS", 1, True
                for e in events:
                    payload = {
                        "evidence_id": e["evidence_id"], "sequence": e["sequence"], "event_type": e["event_type"],
                        "actor_id": e["actor_id"], "from_person": e["from_person"], "to_person": e["to_person"],
                        "location": e["location"], "note": e["note"], "previous_hash": e["previous_hash"], "created_at": e["created_at"],
                    }
                    if e["sequence"] != expected_seq or e["previous_hash"] != expected_prev or self._event_hash(payload) != e["event_hash"]:
                        chain_valid = False
                    expected_prev, expected_seq = e["event_hash"], e["sequence"] + 1
                witness_actors = [e["actor_id"] for e in events if e["event_type"] == "RELEASE_WITNESS"]
                seen, duplicate_witnesses = set(), set()
                for actor in witness_actors:
                    if actor in seen:
                        duplicate_witnesses.add(actor)
                    seen.add(actor)
                issues = []
                if not chain_valid:
                    issues.append({"issue": "broken_chain",
                                   "message": f"证据 #{row['id']}（{row['label']}）保管链断裂：序号不连续、前序哈希或事件哈希不匹配"})
                if duplicate_witnesses:
                    issues.append({"issue": "duplicate_witness", "actors": sorted(duplicate_witnesses),
                                   "message": f"证据 #{row['id']}（{row['label']}）存在重复见证：{'、'.join(sorted(duplicate_witnesses))}"})
                if any(e["event_type"] == "RELEASE" for e in events) and len(set(witness_actors)) < 2:
                    issues.append({"issue": "release_witness_shortage",
                                   "message": f"证据 #{row['id']}（{row['label']}）释放缺少两名保管员分别见证"})
                for issue in issues:
                    chain_issues.append({"evidence_id": row["id"], "label": row["label"], **issue})
                all_valid = all_valid and hash_valid and chain_valid and not issues
                items.append({
                    "id": row["id"], "label": row["label"], "filename": row["filename"], "sha256": row["sha256"],
                    "size": row["size"], "status": row["status"], "current_custodian": row["current_custodian"],
                    "legal_hold": bool(row["legal_hold"]), "retention_until": row["retention_until"],
                    "needs_reconfirm": bool(row["needs_reconfirm"]),
                    "hash_valid": hash_valid, "chain_valid": chain_valid, "issues": issues,
                    "events": [dict(e) for e in events],
                    "derivatives": [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (row["id"],)).fetchall()],
                })
            audit = conn.execute("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
            return {
                "case": dict(case), "generated_at": now(), "overall_integrity_valid": all_valid,
                "evidence_count": len(items), "evidence": items, "chain_issues": chain_issues,
                "audit": [dict(a) | {"detail": json.loads(a["detail"])} for a in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceCustody/1.0"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path=urlparse(self.path).path.rstrip("/") or "/"; parts=[p for p in path.split("/") if p]
        user=self.headers.get("X-User-Id",""); store=self._store()
        if method=="GET" and path=="/":
            body=(BASE_DIR/"web"/"index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method=="GET" and path=="/health": return self._send(200,{"ok":True})
        if parts==["api","cases"] and method=="POST":
            d=self._body(); return self._send(201,store.create_case(user,d.get("case_number",""),d.get("title","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="members" and method=="POST":
            d=self._body(); return self._send(201,store.add_member(user,int(parts[2]),d.get("user_id",""),d.get("role","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="evidence" and method=="POST":
            d=self._body(); return self._send(201,store.ingest_evidence(user,int(parts[2]),d.get("label",""),d.get("filename",""),d.get("content_b64",""),d.get("retention_until",""),d.get("custodian")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="report" and method=="GET":
            return self._send(200,store.report(user,int(parts[2])))
        if len(parts)>=3 and parts[:2]==["api","evidence"]:
            evidence_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200,store.get_evidence(user,evidence_id,bool(urlparse(self.path).query)))
            if len(parts)==4 and method=="POST":
                d=self._body()
                if parts[3]=="transfer": return self._send(200,store.transfer(user,evidence_id,d.get("to_person",""),d.get("location",""),d.get("note",""),d.get("expected_tail")))
                if parts[3]=="open": return self._send(200,store.open_evidence(user,evidence_id,d.get("location",""),d.get("note",""),d.get("expected_tail")))
                if parts[3]=="derive": return self._send(201,store.derive(user,evidence_id,d.get("method",""),d.get("label",""),d.get("filename",""),d.get("content_b64",""),d.get("expected_tail")))
                if parts[3]=="release": return self._send(200,store.release(user,evidence_id,d.get("recipient",""),d.get("note",""),d.get("expected_tail")))
                if parts[3]=="hold": return self._send(200,store.set_hold(user,evidence_id,bool(d.get("hold")),d.get("reason",""),d.get("expected_tail")))
                if parts[3]=="retention": return self._send(200,store.set_retention(user,evidence_id,d.get("retention_until",""),d.get("reason",""),d.get("expected_tail")))
                if parts[3]=="reconfirm": return self._send(200,store.reconfirm(user,evidence_id,d.get("conclusion",""),d.get("expected_tail")))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._send(405,{"error":{"code":"immutable_audit","message":"证据和保管记录不提供删除接口"}})
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class CustodyServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="法律证据保管与流转后台")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8105)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=CustodyStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=CustodyServer(("127.0.0.1",args.port),store); print(f"证据保管系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()

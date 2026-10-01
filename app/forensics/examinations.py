from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.custody import CustodyService
from app.forensics.repository import ForensicRepository, record, records


class ExaminationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)
        self.custody = CustodyService(connection, self.clock)

    def create_protocol(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.repository.protocol_latest(data["protocol_code"])
        version = int(latest["version"]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        if latest:
            self.connection.execute("UPDATE examination_protocols SET active=0 WHERE id=?", (latest["id"],))
        cursor = self.connection.execute(
            "INSERT INTO examination_protocols(protocol_code,version,discipline,observation_target,checkpoint_count,reference_value,"
            "turnaround_days,conclusion_rule,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (
                data["protocol_code"], version, data["discipline"], data["observation_target"], data["checkpoint_count"],
                data["reference_value"], data["turnaround_days"], data["conclusion_rule"], data["created_by"], timestamp,
            ),
        )
        return self.repository.require_protocol(int(cursor.lastrowid))

    def schedule_examination(self, data: dict[str, Any]) -> dict[str, Any]:
        replay = self.connection.execute(
            "SELECT response_json FROM idempotency_records WHERE scope='examinations.schedule' AND idempotency_key=?",
            (data["idempotency_key"],),
        ).fetchone()
        if replay:
            import json
            return self.repository.examination_detail(int(json.loads(replay[0])["examination_id"]))
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        protocol = self.repository.require_protocol(int(data["protocol_id"]))
        forensic_case = self.repository.require_forensic_case(int(specimen["case_id"]))
        if protocol["discipline"] != forensic_case["discipline"]:
            raise ValidationError("检验规程与案件鉴定专业不匹配")
        if specimen["status"] in {"depleted", "disposed"}:
            raise ConflictError("耗尽或销毁的检材不能安排检验")
        held_quantity = self.repository.held_quantity(specimen["id"])
        if float(data["sample_quantity"]) > float(specimen["available_quantity"]) - held_quantity + 1e-9:
            raise ConflictError(
                "检验取样数量超过检材可分配数量（需扣除尚未履行的批准占用）",
                context={"available_quantity": specimen["available_quantity"], "held_quantity": held_quantity},
            )
        active = self.connection.execute(
            "SELECT id FROM examinations WHERE specimen_id=? AND status IN ('scheduled','running')",
            (specimen["id"],),
        ).fetchone()
        if active:
            raise ConflictError("该检材已有未完成的检验", context={"examination_id": active[0]})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO examinations(examination_no,specimen_id,protocol_id,examination_type,sample_quantity,scheduled_for,status,"
                "requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'scheduled',?,?,?)",
                (
                    data["examination_no"], specimen["id"], protocol["id"], data["examination_type"], data["sample_quantity"],
                    data["scheduled_for"], data["requested_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("检验编号已经存在") from exc
        examination_id = int(cursor.lastrowid)
        import json
        self.connection.execute(
            "INSERT INTO idempotency_records(scope,idempotency_key,request_hash,response_json,status_code,created_at) "
            "VALUES('examinations.schedule',?,? ,?,201,?)",
            (data["idempotency_key"], data["examination_no"], json.dumps({"examination_id": examination_id}), timestamp),
        )
        schedule = self.connection.execute(
            "SELECT id FROM review_schedules WHERE specimen_id=? AND status IN ('pending','notified') ORDER BY due_on LIMIT 1",
            (specimen["id"],),
        ).fetchone()
        if schedule:
            self.connection.execute(
                "UPDATE review_schedules SET status='scheduled',updated_at=? WHERE id=?", (timestamp, schedule[0])
            )
        return self.repository.examination_detail(examination_id)

    def start_examination(self, examination_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_examination(examination_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检验任务版本冲突", context={"current_version": test["version"]})
        if test["status"] != "scheduled":
            raise ConflictError("只有待执行检验可以开始")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE examinations SET status='running',started_at=?,performed_by=?,version=version+1,updated_at=? "
            "WHERE id=? AND version=? AND status='scheduled'",
            (timestamp, data["performed_by"], timestamp, examination_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("检验任务版本冲突")
        sample_key = f"examination-sample-{examination_id}"
        self.custody.withdraw({
            "specimen_id": test["specimen_id"],
            "quantity": test["sample_quantity"],
            "movement_type": "取样",
            "idempotency_key": sample_key,
            "actor": data["performed_by"],
            "reason": f"检验任务 {test['examination_no']} 取样",
        })
        return self.repository.examination_detail(examination_id)

    def add_observation(self, examination_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_examination(examination_id)
        if test["status"] != "running":
            raise ConflictError("只有执行中的检验可以录入计数")
        protocol = self.repository.require_protocol(int(test["protocol_id"]))
        if int(data["checkpoint_no"]) > int(protocol["checkpoint_count"]):
            raise ValidationError("检查点编号超过规程规定数量")
        if int(data["items_checked"]) > int(protocol["observation_target"]):
            raise ValidationError("单个检查点的项目数超过规程上限")
        if int(data["sequence_no"]) > int(protocol["turnaround_days"]):
            raise ValidationError("观察日超过规程持续天数")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO examination_observations(examination_id,checkpoint_no,items_checked,conforming_count,exception_count,unusable_count,"
                "pending_count,sequence_no,observed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    examination_id, data["checkpoint_no"], data["items_checked"], data["conforming_count"],
                    data["exception_count"], data["unusable_count"], data.get("pending_count", 0),
                    data["sequence_no"], data["observed_by"], timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该检查点在当前观察序次已经录入") from exc
        return record(self.connection.execute("SELECT * FROM examination_observations WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def replace_observation(self, count_id: int, data: dict[str, Any]) -> dict[str, Any]:
        existing = record(self.connection.execute("SELECT * FROM examination_observations WHERE id=?", (count_id,)).fetchone())
        if not existing:
            raise ValidationError("计数记录不存在")
        test = self.repository.require_examination(int(existing["examination_id"]))
        if test["status"] != "running":
            raise ConflictError("已经结束的检验不能修改计数")
        total = int(data["conforming_count"]) + int(data["exception_count"]) + int(data["unusable_count"]) + int(data.get("pending_count", 0))
        if total != int(data["items_checked"]):
            raise ValidationError("分类计数之和必须等于检验项数")
        self.connection.execute(
            "UPDATE examination_observations SET items_checked=?,conforming_count=?,exception_count=?,unusable_count=?,pending_count=?,"
            "sequence_no=?,observed_by=? WHERE id=?",
            (
                data["items_checked"], data["conforming_count"], data["exception_count"], data["unusable_count"],
                data.get("pending_count", 0), data["sequence_no"], data["observed_by"], count_id,
            ),
        )
        return record(self.connection.execute("SELECT * FROM examination_observations WHERE id=?", (count_id,)).fetchone()) or {}

    def complete_examination(self, examination_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_examination(examination_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检验任务版本冲突", context={"current_version": test["version"]})
        if test["status"] != "running":
            raise ConflictError("只有执行中的检验可以完成")
        protocol = self.repository.require_protocol(int(test["protocol_id"]))
        latest_counts = self._latest_observations(examination_id)
        if len(latest_counts) != int(protocol["checkpoint_count"]):
            raise ValidationError("每个规程检查点都必须有最终观察记录", context={
                "expected": protocol["checkpoint_count"], "actual": len(latest_counts)
            })
        percentages = [100.0 * int(item["conforming_count"]) / int(item["items_checked"]) for item in latest_counts]
        conformity = round(sum(percentages) / len(percentages), 2)
        reliability_parts = [
            100.0 * (int(item["conforming_count"]) + 0.5 * int(item["pending_count"])) / int(item["items_checked"])
            for item in latest_counts
        ]
        reliability = round(sum(reliability_parts) / len(reliability_parts), 2)
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE examinations SET status='completed',conformity_percent=?,reliability_index=?,completed_at=?,"
            "performed_by=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status='running'",
            (conformity, reliability, timestamp, data["performed_by"], timestamp, examination_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("检验任务版本冲突")
        self._schedule_next(examination_id, conformity, timestamp)
        if conformity < 50:
            self._create_low_examination_alert(examination_id, conformity, timestamp)
        return self.repository.examination_detail(examination_id)

    def invalidate_examination(self, examination_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_examination(examination_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检验任务版本冲突", context={"current_version": test["version"]})
        if test["status"] not in {"running", "completed"}:
            raise ConflictError("当前检验状态不能作废")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE examinations SET status='invalidated',invalid_reason=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (data["reason"], timestamp, examination_id, data["expected_version"]),
        )
        self.connection.execute(
            "UPDATE review_schedules SET status='superseded',updated_at=? WHERE source_examination_id=? AND status IN ('pending','notified')",
            (timestamp, examination_id),
        )
        return self.repository.examination_detail(examination_id)

    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.connection.execute(
            "SELECT version FROM review_policies WHERE discipline=? AND risk_level=? ORDER BY version DESC LIMIT 1",
            (data["discipline"], data["risk_level"]),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO review_policies(discipline,risk_level,interval_months,warning_days,minimum_conformity_percent,"
            "effective_from,effective_to,version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                data["discipline"], data["risk_level"], data["interval_months"], data["warning_days"],
                data["minimum_conformity_percent"], data["effective_from"], data.get("effective_to"), version,
                data["created_by"], timestamp,
            ),
        )
        return self.repository.require_policy(int(cursor.lastrowid))

    def due_schedules(self, before: date, limit: int = 100) -> list[dict[str, Any]]:
        return records(self.connection.execute(
            "SELECT s.*,l.specimen_no,a.case_no,a.discipline FROM review_schedules s "
            "JOIN specimens l ON l.id=s.specimen_id JOIN forensic_cases a ON a.id=l.case_id "
            "WHERE s.status IN ('pending','notified') AND s.due_on<=? ORDER BY s.due_on,l.specimen_no LIMIT ?",
            (before.isoformat(), limit),
        ).fetchall())

    def mark_notifications(self, schedule_ids: list[int]) -> int:
        if not schedule_ids:
            return 0
        placeholders = ",".join("?" for _ in schedule_ids)
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            f"UPDATE review_schedules SET status='notified',updated_at=? WHERE id IN ({placeholders}) AND status='pending'",
            (timestamp, *schedule_ids),
        )
        return int(cursor.rowcount)

    def _latest_observations(self, examination_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT c.* FROM examination_observations c JOIN (SELECT checkpoint_no,MAX(sequence_no) AS day "
            "FROM examination_observations WHERE examination_id=? GROUP BY checkpoint_no) latest "
            "ON latest.checkpoint_no=c.checkpoint_no AND latest.day=c.sequence_no WHERE c.examination_id=? "
            "ORDER BY c.checkpoint_no",
            (examination_id, examination_id),
        ).fetchall()
        return records(rows)

    def _schedule_next(self, examination_id: int, conformity: float, timestamp: str) -> None:
        test = self.repository.require_examination(examination_id)
        specimen = self.repository.require_specimen(int(test["specimen_id"]))
        forensic_case = self.repository.require_forensic_case(int(specimen["case_id"]))
        risk = "high" if conformity < 70 else ("medium" if conformity < 85 else "low")
        completed_date = datetime.fromisoformat(timestamp).date()
        policy = self.repository.applicable_policy(forensic_case["discipline"], risk, completed_date.isoformat())
        if policy is None:
            return
        due = add_months(completed_date, int(policy["interval_months"]))
        self.connection.execute(
            "UPDATE review_schedules SET status='superseded',updated_at=? WHERE specimen_id=? AND status IN ('pending','notified')",
            (timestamp, specimen["id"]),
        )
        self.connection.execute(
            "INSERT INTO review_schedules(specimen_id,source_examination_id,policy_id,due_on,status,reason,created_at,updated_at) "
            "VALUES(?,?,?,?,'pending',?,?,?)",
            (specimen["id"], examination_id, policy["id"], due.isoformat(), f"检验结果 {conformity:.2f}% 对应 {risk} 风险", timestamp, timestamp),
        )

    def _create_low_examination_alert(self, examination_id: int, conformity: float, timestamp: str) -> None:
        test = self.repository.require_examination(examination_id)
        key = f"low-examination-{examination_id}"
        import json
        self.connection.execute(
            "INSERT OR IGNORE INTO quality_alerts(alert_key,alert_type,severity,specimen_id,message,detail_json,created_at,updated_at) "
            "VALUES(?,'low_examination','critical',?,?,?,?,?)",
            (key, test["specimen_id"], f"检材检验符合率降至 {conformity:.2f}%", json.dumps({"examination_id": examination_id, "value": conformity}), timestamp, timestamp),
        )


def add_months(value: date, months: int) -> date:
    target_month = value.month - 1 + months
    year = value.year + target_month // 12
    month = target_month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

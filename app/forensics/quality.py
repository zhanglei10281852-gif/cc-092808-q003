from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from datetime import timedelta
from functools import wraps
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record, records


_savepoint_counter = itertools.count()


def atomic(method):
    """在共享事务连接上以 SAVEPOINT 包裹业务操作：失败仅回滚本操作，不留半成品。"""

    @wraps(method)
    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        marker = f"sp_{method.__name__}_{next(_savepoint_counter)}"
        self.connection.execute(f"SAVEPOINT {marker}")
        try:
            result = method(self, *args, **kwargs)
            self.connection.execute(f"RELEASE SAVEPOINT {marker}")
            return result
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
            self.connection.execute(f"RELEASE SAVEPOINT {marker}")
            raise

    return wrapper



class QualityService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def add_reading(self, data: dict[str, Any]) -> dict[str, Any]:
        location = self.repository.require_location(int(data["location_id"]))
        observed_at = data["observed_at"]
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO storage_readings(location_id,observed_at,reference_value,humidity_percent,source_key,imported_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    location["id"], to_storage(observed_at), data["reference_value"], data["humidity_percent"],
                    data["source_key"], timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = record(self.connection.execute(
                "SELECT * FROM storage_readings WHERE location_id=? AND source_key=?",
                (location["id"], data["source_key"]),
            ).fetchone())
            if existing and (
                float(existing["reference_value"]) != float(data["reference_value"])
                or float(existing["humidity_percent"]) != float(data["humidity_percent"])
            ):
                raise ConflictError("同一来源读数键对应了不同数据")
            return {"reading": existing, "replayed": True, "alerts": []}
        reading = record(self.connection.execute("SELECT * FROM storage_readings WHERE id=?", (cursor.lastrowid,)).fetchone())
        alerts = self._evaluate_reading(location, reading or {}, timestamp)
        return {"reading": reading, "replayed": False, "alerts": alerts}

    def import_readings(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(rows, start=1):
            marker = f"reading_import_{index}"
            self.connection.execute(f"SAVEPOINT {marker}")
            try:
                result = self.add_reading(item)
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                accepted.append({"row": index, "id": result["reading"]["id"], "replayed": result["replayed"]})
            except Exception as exc:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                self.connection.execute(f"RELEASE SAVEPOINT {marker}")
                rejected.append({"row": index, "reason": str(exc)})
        return {"accepted": accepted, "rejected": rejected, "accepted_count": len(accepted), "rejected_count": len(rejected)}

    def decide_alert(self, alert_id: int, data: dict[str, Any]) -> dict[str, Any]:
        alert = self.repository.require_alert(alert_id)
        action = data["action"]
        timestamp = to_storage(self.clock.now())
        if action == "acknowledge":
            if alert["status"] != "open":
                raise ConflictError("只有未处理告警可以确认")
            self.connection.execute(
                "UPDATE quality_alerts SET status='acknowledged',acknowledged_by=?,acknowledged_at=?,updated_at=? WHERE id=?",
                (data["actor"], timestamp, timestamp, alert_id),
            )
        elif action == "resolve":
            if alert["status"] not in {"open", "acknowledged"}:
                raise ConflictError("当前告警状态不能关闭")
            self.connection.execute(
                "UPDATE quality_alerts SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, alert_id),
            )
        else:
            if alert["status"] != "open":
                raise ConflictError("只有未处理告警可以忽略")
            if not data.get("reason"):
                raise ValidationError("忽略告警时必须填写原因")
            details = alert.get("detail", {})
            details["dismiss_reason"] = data["reason"]
            details["dismissed_by"] = data["actor"]
            self.connection.execute(
                "UPDATE quality_alerts SET status='dismissed',detail_json=?,updated_at=? WHERE id=?",
                (json.dumps(details, ensure_ascii=False), timestamp, alert_id),
            )
        return self.repository.require_alert(alert_id)

    def open_alerts(self, severity: str | None = None) -> list[dict[str, Any]]:
        if severity:
            rows = self.connection.execute(
                "SELECT * FROM quality_alerts WHERE status IN ('open','acknowledged') AND severity=? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,created_at",
                (severity,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM quality_alerts WHERE status IN ('open','acknowledged') "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,created_at"
            ).fetchall()
        return records(rows)

    def location_summary(self, location_id: int, hours: int = 24) -> dict[str, Any]:
        location = self.repository.location_detail(location_id)
        cutoff = to_storage(self.clock.now() - timedelta(hours=hours))
        row = self.connection.execute(
            "SELECT COUNT(*) AS samples,MIN(reference_value) AS min_temperature,MAX(reference_value) AS max_temperature,"
            "AVG(reference_value) AS avg_temperature,MIN(humidity_percent) AS min_humidity,"
            "MAX(humidity_percent) AS max_humidity,AVG(humidity_percent) AS avg_humidity "
            "FROM storage_readings WHERE location_id=? AND observed_at>=?",
            (location_id, cutoff),
        ).fetchone()
        alerts = self.connection.execute(
            "SELECT COUNT(*) FROM quality_alerts WHERE location_id=? AND status IN ('open','acknowledged')",
            (location_id,),
        ).fetchone()[0]
        return {"location": location, "window_hours": hours, "readings": dict(row), "open_alerts": int(alerts)}

    def _evaluate_reading(self, location: dict[str, Any], reading: dict[str, Any], timestamp: str) -> list[dict[str, Any]]:
        deviations: list[tuple[str, float, float]] = []
        temperature_delta = abs(float(reading["reference_value"]) - float(location["reference_value"]))
        humidity_delta = abs(float(reading["humidity_percent"]) - float(location["humidity_percent"]))
        if temperature_delta > 5:
            deviations.append(("temperature", float(reading["reference_value"]), float(location["reference_value"])))
        if humidity_delta > 15:
            deviations.append(("humidity", float(reading["humidity_percent"]), float(location["humidity_percent"])))
        alerts: list[dict[str, Any]] = []
        for metric, observed, target in deviations:
            digest = hashlib.sha256(f"{location['id']}:{reading['id']}:{metric}".encode()).hexdigest()[:20]
            severity = "critical" if abs(observed - target) > (10 if metric == "temperature" else 30) else "warning"
            message = f"库位 {location['location_code']} 的{('温度' if metric == 'temperature' else '湿度')}偏离设定值"
            detail = {"reading_id": reading["id"], "metric": metric, "observed": observed, "target": target}
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO quality_alerts(alert_key,alert_type,severity,location_id,message,detail_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"environment-{digest}", "environment_excursion", severity, location["id"], message, json.dumps(detail), timestamp, timestamp),
            )
            if cursor.rowcount:
                alerts.append(self.repository.require_alert(int(cursor.lastrowid)))
        return alerts


class ReleaseService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def create_request(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        seen: set[int] = set()
        normalized: list[tuple[int, float]] = []
        for raw in data["items"]:
            case_id = int(raw.get("case_id", 0))
            quantity = float(raw.get("quantity", 0))
            if case_id <= 0 or quantity <= 0:
                raise ValidationError("领用明细必须包含有效案件和正数数量")
            if case_id in seen:
                raise ValidationError("同一案件不能在申请中重复出现")
            seen.add(case_id)
            forensic_case = self.repository.require_forensic_case(case_id)
            if forensic_case["status"] != "accepted":
                raise ConflictError("仅正式接收的案件可以申请领用", context={"case_id": case_id})
            normalized.append((case_id, quantity))
        try:
            cursor = self.connection.execute(
                "INSERT INTO release_requests(request_no,requester,purpose,status,requested_at) VALUES(?,?,?,'draft',?)",
                (data["request_no"], data["requester"], data["purpose"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("领用申请编号已经存在") from exc
        request_id = int(cursor.lastrowid)
        for case_id, quantity in normalized:
            self.connection.execute(
                "INSERT INTO release_items(request_id,case_id,quantity) VALUES(?,?,?)",
                (request_id, case_id, quantity),
            )
        return self.repository.release_detail(request_id)

    def submit(self, request_id: int, expected_version: int) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        if request["status"] != "draft":
            raise ConflictError("只有草稿申请可以提交")
        updated = self.connection.execute(
            "UPDATE release_requests SET status='submitted',version=version+1 WHERE id=? AND version=? AND status='draft'",
            (request_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请版本冲突", context={"current_version": request["version"]})
        return self.repository.release_detail(request_id)

    @atomic
    def decide(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        if request["status"] != "submitted":
            raise ConflictError("只有已提交申请可以审批")
        timestamp = to_storage(self.clock.now())
        if not data["approve"]:
            if not data.get("reason"):
                raise ValidationError("拒绝申请时必须填写原因")
            updated = self.connection.execute(
                "UPDATE release_requests SET status='rejected',reviewed_by=?,reviewed_at=?,decision_reason=?,"
                "version=version+1 WHERE id=? AND version=? AND status='submitted'",
                (data["actor"], timestamp, data["reason"], request_id, data["expected_version"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("领用申请版本冲突或已被处理", context={"current_version": request["version"]})
            self.connection.execute(
                "UPDATE release_items SET status='cancelled' WHERE request_id=? AND status='requested'",
                (request_id,),
            )
            return self.repository.release_detail(request_id)
        # 版本+状态双重条件更新：版本过期或并发审批只有一方能成功
        updated = self.connection.execute(
            "UPDATE release_requests SET status='approved',reviewed_by=?,reviewed_at=?,decision_reason=?,"
            "version=version+1 WHERE id=? AND version=? AND status='submitted'",
            (data["actor"], timestamp, data.get("reason", ""), request_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请版本冲突或已被处理", context={"current_version": request["version"]})
        items = self.repository.release_detail(request_id)["items"]
        for item in items:
            specimen = self._choose_specimen(int(item["case_id"]), float(item["quantity"]))
            if specimen is None:
                raise ConflictError("没有满足数量与检验条件的可领用检材", context={"case_id": item["case_id"]})
            # 原子占用：仅当“物理可用 - 既存未履行占用”仍足够时才能推进版本，
            # 与插入占用记录处于同一事务，任何一条失败整体回滚，不留孤立明细。
            guarded = self.connection.execute(
                "UPDATE specimens SET version=version+1,updated_at=? WHERE id=? AND "
                "available_quantity-(SELECT COALESCE(SUM(quantity),0) FROM release_allocations "
                "WHERE specimen_id=? AND status='held')>=?-1e-9",
                (timestamp, specimen["id"], specimen["id"], float(item["quantity"])),
            )
            if guarded.rowcount != 1:
                raise ConflictError(
                    "检材可分配量不足，无法建立领用占用",
                    context={"specimen_id": specimen["id"], "case_id": item["case_id"]},
                )
            self.connection.execute(
                "INSERT INTO release_allocations(request_id,item_id,specimen_id,quantity,status,created_by,created_at) "
                "VALUES(?,?,?,?,'held',?,?)",
                (request_id, item["id"], specimen["id"], float(item["quantity"]), data["actor"], timestamp),
            )
            self.connection.execute(
                "UPDATE release_items SET allocated_specimen_id=?,status='allocated' WHERE id=? AND status='requested'",
                (specimen["id"], item["id"]),
            )
        return self.repository.release_detail(request_id)

    @atomic
    def cancel(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        if request["status"] not in {"submitted", "approved"}:
            raise ConflictError("只有已提交或已批准的申请可以取消")
        timestamp = to_storage(self.clock.now())
        updated = self.connection.execute(
            "UPDATE release_requests SET status='cancelled',decision_reason=?,version=version+1 "
            "WHERE id=? AND version=? AND status IN ('submitted','approved')",
            (data.get("reason", ""), request_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请版本冲突或状态已变化", context={"current_version": request["version"]})
        self._release_holds(request_id, data["actor"], data.get("reason", "申请取消"), timestamp)
        self.connection.execute(
            "UPDATE release_items SET status='cancelled' WHERE request_id=? AND status IN ('requested','allocated')",
            (request_id,),
        )
        return self.repository.release_detail(request_id)

    @atomic
    def fulfill(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        if request["status"] != "approved":
            raise ConflictError("只有已批准申请可以履行出库")
        timestamp = to_storage(self.clock.now())
        held = records(self.connection.execute(
            "SELECT * FROM release_allocations WHERE request_id=? AND status='held' ORDER BY id",
            (request_id,),
        ).fetchall())
        if not held:
            raise ConflictError("申请没有待履行的批准占用")
        updated = self.connection.execute(
            "UPDATE release_requests SET status='fulfilled',version=version+1 "
            "WHERE id=? AND version=? AND status='approved'",
            (request_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请版本冲突或状态已变化", context={"current_version": request["version"]})
        consumed_by_specimen: dict[int, float] = {}
        for allocation in held:
            specimen = self.repository.require_specimen(int(allocation["specimen_id"]))
            if self.repository.active_holds(specimen["id"]):
                raise ConflictError("检材存在未解除冻结，不能出库", context={"specimen_id": specimen["id"]})
            quantity = float(allocation["quantity"])
            already_consumed = consumed_by_specimen.get(specimen["id"], 0.0)
            if float(specimen["available_quantity"]) - already_consumed + 1e-9 < quantity:
                raise ConflictError("检材物理可用数量不足", context={"specimen_id": specimen["id"]})
            event_cursor = self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                "VALUES(?,'领用',?,?,?,?,?)",
                (
                    specimen["id"], -quantity, f"release-fulfill-{int(allocation['id'])}",
                    data["actor"], f"履行领用申请 {request['request_no']}：{request['purpose']}", timestamp,
                ),
            )
            self.connection.execute(
                "UPDATE release_allocations SET status='consumed',consumed_at=?,consumed_event_id=? "
                "WHERE id=? AND status='held'",
                (timestamp, int(event_cursor.lastrowid), allocation["id"]),
            )
            self.connection.execute(
                "UPDATE release_items SET status='fulfilled' WHERE id=? AND status='allocated'",
                (int(allocation["item_id"]),),
            )
            consumed_by_specimen[specimen["id"]] = round(already_consumed + quantity, 6)
        for specimen_id, quantity in consumed_by_specimen.items():
            self.connection.execute(
                "UPDATE specimens SET available_quantity=ROUND(available_quantity-?,6),"
                "status=CASE WHEN ROUND(available_quantity-?,6)<=1e-9 THEN 'depleted' ELSE status END,"
                "version=version+1,updated_at=? WHERE id=?",
                (quantity, quantity, timestamp, specimen_id),
            )
        return self.repository.release_detail(request_id)

    def _release_holds(self, request_id: int, actor: str, reason: str, timestamp: str) -> None:
        held = records(self.connection.execute(
            "SELECT * FROM release_allocations WHERE request_id=? AND status='held' ORDER BY id",
            (request_id,),
        ).fetchall())
        for allocation in held:
            updated = self.connection.execute(
                "UPDATE release_allocations SET status='released',released_at=?,released_by=?,release_reason=? "
                "WHERE id=? AND status='held'",
                (timestamp, actor, reason, int(allocation["id"])),
            )
            if updated.rowcount != 1:
                raise ConflictError("领用占用状态已变化", context={"allocation_id": allocation["id"]})
            self.connection.execute(
                "UPDATE specimens SET version=version+1,updated_at=? WHERE id=?",
                (timestamp, int(allocation["specimen_id"])),
            )

    def _choose_specimen(self, case_id: int, quantity: float) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT l.*,v.conformity_percent,v.completed_at,"
            "(l.available_quantity-(SELECT COALESCE(SUM(a.quantity),0) FROM release_allocations a "
            "WHERE a.specimen_id=l.id AND a.status='held')) AS allocatable_quantity "
            "FROM specimens l "
            "LEFT JOIN examinations v ON v.id=(SELECT id FROM examinations WHERE specimen_id=l.id AND status='completed' "
            "ORDER BY completed_at DESC,id DESC LIMIT 1) WHERE l.case_id=? AND l.status='stored' "
            "AND l.available_quantity-(SELECT COALESCE(SUM(a.quantity),0) FROM release_allocations a "
            "WHERE a.specimen_id=l.id AND a.status='held')>=?-1e-9 "
            "AND NOT EXISTS(SELECT 1 FROM specimen_holds h WHERE h.specimen_id=l.id AND h.released_at IS NULL) "
            "ORDER BY CASE WHEN v.conformity_percent IS NULL THEN 1 ELSE 0 END,v.completed_at,l.received_year,l.specimen_no LIMIT 1",
            (case_id, quantity),
        ).fetchone()
        return record(row)

    # ------------------------------------------------------------------
    # 既有数据核对与修复
    # ------------------------------------------------------------------

    def doctor(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        legacy_rows = self.connection.execute(
            "SELECT i.id AS item_id,i.request_id,i.case_id,i.quantity,i.allocated_specimen_id,r.status AS request_status "
            "FROM release_items i JOIN release_requests r ON r.id=i.request_id "
            "WHERE i.status='allocated' AND r.status IN ('approved','fulfilled') "
            "AND NOT EXISTS(SELECT 1 FROM release_allocations a WHERE a.item_id=i.id) ORDER BY i.id"
        ).fetchall()
        for row in legacy_rows:
            issues.append({
                "type": "missing_allocation",
                "request_id": row["request_id"],
                "item_id": row["item_id"],
                "case_id": row["case_id"],
                "specimen_id": row["allocated_specimen_id"],
                "quantity": row["quantity"],
                "request_status": row["request_status"],
                "message": "已批准明细缺少占用记录（历史数据）",
            })
        orphan_rows = self.connection.execute(
            "SELECT a.id,a.request_id,a.item_id,a.specimen_id,a.quantity,r.status AS request_status "
            "FROM release_allocations a JOIN release_requests r ON r.id=a.request_id "
            "WHERE a.status='held' AND r.status NOT IN ('approved') ORDER BY a.id"
        ).fetchall()
        for row in orphan_rows:
            issues.append({
                "type": "orphan_hold",
                "request_id": row["request_id"],
                "allocation_id": row["id"],
                "item_id": row["item_id"],
                "specimen_id": row["specimen_id"],
                "quantity": row["quantity"],
                "message": "占用未随申请终结而释放",
            })
        over_rows = self.connection.execute(
            "SELECT * FROM (SELECT s.id AS specimen_id,s.specimen_no,s.available_quantity,"
            "(SELECT COALESCE(SUM(a.quantity),0) FROM release_allocations a WHERE a.specimen_id=s.id AND a.status='held') AS held_quantity "
            "FROM specimens s) WHERE held_quantity>available_quantity+1e-9 ORDER BY specimen_id"
        ).fetchall()
        for row in over_rows:
            issues.append({
                "type": "oversubscribed",
                "specimen_id": row["specimen_id"],
                "specimen_no": row["specimen_no"],
                "available_quantity": row["available_quantity"],
                "held_quantity": row["held_quantity"],
                "message": "未履行占用总量超过检材物理可用量",
            })
        drift_rows = self.connection.execute(
            "SELECT * FROM (SELECT s.id,s.specimen_no,s.available_quantity,"
            "ROUND(s.initial_quantity+(SELECT COALESCE(SUM(quantity),0) FROM custody_events e WHERE e.specimen_id=s.id "
            "AND e.movement_type IN ('取样','领用','报废','归还','盘点调整')),6) AS expected_quantity "
            "FROM specimens s) WHERE ABS(available_quantity-expected_quantity)>1e-6 ORDER BY id"
        ).fetchall()
        for row in drift_rows:
            issues.append({
                "type": "ledger_drift",
                "specimen_id": row["id"],
                "specimen_no": row["specimen_no"],
                "available_quantity": row["available_quantity"],
                "expected_quantity": row["expected_quantity"],
                "message": "检材可用量与出入库台账不一致",
            })
        return {
            "healthy": not issues,
            "issue_count": len(issues),
            "issues": [dict(row) for row in issues],
        }

    @atomic
    def repair(self, actor: str = "数据修复") -> dict[str, Any]:
        before = self.doctor()
        timestamp = to_storage(self.clock.now())
        actions: list[dict[str, Any]] = []

        # 1. 台账漂移优先按台账真值复位物理可用量
        for issue in before["issues"]:
            if issue["type"] != "ledger_drift":
                continue
            expected = float(issue["expected_quantity"])
            if expected < -1e-9:
                continue
            self.connection.execute(
                "UPDATE specimens SET available_quantity=?,version=version+1,updated_at=? WHERE id=?",
                (round(expected, 6), timestamp, issue["specimen_id"]),
            )
            actions.append({"type": "reset_available", "specimen_id": issue["specimen_id"], "quantity": round(expected, 6)})

        # 2. 释放依附在非批准申请上的孤立占用
        for issue in before["issues"]:
            if issue["type"] != "orphan_hold":
                continue
            self.connection.execute(
                "UPDATE release_allocations SET status='released',released_at=?,released_by=?,release_reason=? "
                "WHERE id=? AND status='held'",
                (timestamp, actor, "修复：申请未处于批准状态，释放孤立占用", issue["allocation_id"]),
            )
            self.connection.execute(
                "UPDATE release_items SET status='cancelled' WHERE id=? AND status='allocated'",
                (issue["item_id"],),
            )
            actions.append({"type": "release_orphan_hold", "allocation_id": issue["allocation_id"]})

        # 3. 历史已批准/已履行明细补建占用（按明细编号确定先来后到）
        for issue in before["issues"]:
            if issue["type"] != "missing_allocation":
                continue
            specimen_id = issue["specimen_id"]
            quantity = float(issue["quantity"])
            if specimen_id is None:
                self._mark_unavailable(issue["item_id"], issue["request_id"], "修复：历史明细未指定检材，无法补建占用", timestamp, actions)
                continue
            if issue["request_status"] == "fulfilled":
                specimen = self.repository.require_specimen(specimen_id)
                physical = max(float(specimen["available_quantity"]), 0.0)
                take_now = min(quantity, physical)
                event_cursor = self.connection.execute(
                    "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                    "VALUES(?,'领用',?,?,?,?,?)",
                    (specimen_id, -round(take_now, 6), f"repair-release-{issue['item_id']}", actor, "历史履行数据补账", timestamp),
                )
                note = ""
                gap = round(quantity - take_now, 6)
                if gap > 1e-9:
                    # 历史脏数据：申请记录的消耗量超过现存物理量，差额无法追溯，只在占用上备注，不伪造台账
                    note = f"修复时现存物理量仅 {round(take_now, 6)}，差额 {gap} 无历史台账可追溯"
                self.connection.execute(
                    "INSERT INTO release_allocations(request_id,item_id,specimen_id,quantity,status,created_by,created_at,"
                    "consumed_at,consumed_event_id,release_reason) VALUES(?,?,?,?,'consumed',?,?,?,?,?)",
                    (issue["request_id"], issue["item_id"], specimen_id, quantity, actor, timestamp, timestamp,
                     event_cursor.lastrowid, note),
                )
                self.connection.execute(
                    "UPDATE specimens SET available_quantity=ROUND(available_quantity-?,6),"
                    "status=CASE WHEN ROUND(available_quantity-?,6)<=1e-9 THEN 'depleted' ELSE status END,"
                    "version=version+1,updated_at=? WHERE id=?",
                    (round(take_now, 6), round(take_now, 6), timestamp, specimen_id),
                )
                actions.append({"type": "backfill_consumed", "item_id": issue["item_id"], "specimen_id": specimen_id,
                                "quantity": quantity, "deducted": round(take_now, 6), "adjusted_gap": gap})
                continue
            guarded = self.connection.execute(
                "UPDATE specimens SET version=version+1,updated_at=? WHERE id=? AND "
                "available_quantity-(SELECT COALESCE(SUM(quantity),0) FROM release_allocations "
                "WHERE specimen_id=? AND status='held')>=?-1e-9",
                (timestamp, specimen_id, specimen_id, quantity),
            )
            if guarded.rowcount != 1:
                self._mark_unavailable(issue["item_id"], issue["request_id"], "修复：检材可分配量不足，该笔历史批准无法补建占用", timestamp, actions)
                continue
            self.connection.execute(
                "INSERT INTO release_allocations(request_id,item_id,specimen_id,quantity,status,created_by,created_at) "
                "VALUES(?,?,?,?,'held',?,?)",
                (issue["request_id"], issue["item_id"], specimen_id, quantity, actor, timestamp),
            )
            actions.append({"type": "backfill_hold", "item_id": issue["item_id"], "specimen_id": specimen_id, "quantity": quantity})

        # 4. 兜底：仍超出物理可用量的占用，按占用编号倒序释放最新超出部分
        over_rows = self.connection.execute(
            "SELECT * FROM (SELECT s.id AS specimen_id,s.available_quantity,"
            "(SELECT COALESCE(SUM(a.quantity),0) FROM release_allocations a WHERE a.specimen_id=s.id AND a.status='held') AS held_quantity "
            "FROM specimens s) WHERE held_quantity>available_quantity+1e-9"
        ).fetchall()
        for row in over_rows:
            excess = float(row["held_quantity"]) - float(row["available_quantity"])
            allocations = records(self.connection.execute(
                "SELECT * FROM release_allocations WHERE specimen_id=? AND status='held' ORDER BY id DESC",
                (row["specimen_id"],),
            ).fetchall())
            for allocation in allocations:
                if excess <= 1e-9:
                    break
                self.connection.execute(
                    "UPDATE release_allocations SET status='released',released_at=?,released_by=?,release_reason=? "
                    "WHERE id=? AND status='held'",
                    (timestamp, actor, "修复：超出检材可分配量，释放最新占用", int(allocation["id"])),
                )
                self._mark_unavailable(int(allocation["item_id"]), int(allocation["request_id"]), "修复：占用超卖，转不可履行", timestamp, actions)
                excess -= float(allocation["quantity"])

        return {"before": before, "actions": actions, "after": self.doctor()}

    def _mark_unavailable(self, item_id: int, request_id: int, note: str, timestamp: str, actions: list[dict[str, Any]]) -> None:
        self.connection.execute(
            "UPDATE release_items SET status='unavailable' WHERE id=? AND status IN ('requested','allocated')",
            (item_id,),
        )
        self.connection.execute(
            "UPDATE release_requests SET decision_reason=TRIM(COALESCE(decision_reason,'')||CHAR(10)||?) WHERE id=?",
            (f"[{timestamp}] {note}", request_id),
        )
        actions.append({"type": "mark_unavailable", "item_id": item_id, "request_id": request_id, "reason": note})


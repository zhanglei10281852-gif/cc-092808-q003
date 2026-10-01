from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record, records


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
    """领用申请审批。

    批准动作在单个事务内原子完成：状态翻转（版本谓词）、可分配量条件扣减、
    占用记录建立三者同生共死。可分配量按“初始量 - 实际耗用 - 未履行占用”计算，
    冻结检材完全不参与分配。
    """

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
        if int(request["version"]) != expected_version:
            raise ConflictError("领用申请版本冲突", context={"current_version": request["version"]})
        updated = self.connection.execute(
            "UPDATE release_requests SET status='submitted',version=version+1 WHERE id=? AND version=?",
            (request_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请版本冲突", context={"current_version": request["version"]})
        return self.repository.release_detail(request_id)

    def decide(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        expected_version = int(data["expected_version"])
        if int(request["version"]) != expected_version:
            raise ConflictError("领用申请版本冲突", context={"current_version": request["version"]})
        if request["status"] != "submitted":
            raise ConflictError("只有已提交申请可以审批", context={"status": request["status"]})
        timestamp = to_storage(self.clock.now())
        if not data["approve"]:
            if not data.get("reason"):
                raise ValidationError("拒绝申请时必须填写原因")
            # 拒绝不建立任何占用，自然无物可释放
            updated = self.connection.execute(
                "UPDATE release_requests SET status='rejected',reviewed_by=?,reviewed_at=?,decision_reason=?,"
                "version=version+1 WHERE id=? AND version=? AND status='submitted'",
                (data["actor"], timestamp, data["reason"], request_id, expected_version),
            )
            if updated.rowcount != 1:
                raise ConflictError("领用申请状态已变更，请刷新后重试")
            return self.repository.release_detail(request_id)

        detail = self.repository.release_detail(request_id)
        plan = self._build_allocation_plan(detail["items"])
        # 先做带版本与状态谓词的状态翻转：并发审批只有一方 rowcount=1
        flipped = self.connection.execute(
            "UPDATE release_requests SET status='approved',reviewed_by=?,reviewed_at=?,decision_reason=?,"
            "version=version+1 WHERE id=? AND version=? AND status='submitted'",
            (data["actor"], timestamp, data.get("reason", ""), request_id, expected_version),
        )
        if flipped.rowcount != 1:
            raise ConflictError("领用申请状态已变更，请刷新后重试")
        # 此后任一笔扣减失败都会由外层事务整体回滚：状态翻转、版本推进、占用、扣减同生共死
        for item, specimen in plan:
            self._reserve(item, specimen, float(item["quantity"]), data["actor"], timestamp)
        return self.repository.release_detail(request_id)

    def cancel(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        request = self.repository.require_release(request_id)
        if request["status"] not in {"draft", "submitted", "approved"}:
            raise ConflictError("当前申请状态不能取消", context={"status": request["status"]})
        expected_version = int(data["expected_version"])
        if int(request["version"]) != expected_version:
            raise ConflictError("领用申请版本冲突", context={"current_version": request["version"]})
        timestamp = to_storage(self.clock.now())
        updated = self.connection.execute(
            "UPDATE release_requests SET status='cancelled',decision_reason=?,version=version+1 "
            "WHERE id=? AND version=? AND status IN ('draft','submitted','approved')",
            (data.get("reason", "申请取消"), request_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ConflictError("领用申请状态已变更，请刷新后重试")
        # 已批准的取消：释放全部未履行占用，数量回到可分配量
        self._release_holds(request_id, timestamp, actor=data.get("actor", request.get("requester") or ""),
                            reason="申请取消")
        self.connection.execute(
            "UPDATE release_items SET status='unavailable',allocated_specimen_id=NULL "
            "WHERE request_id=? AND status='allocated'",
            (request_id,),
        )
        return self.repository.release_detail(request_id)

    def fulfill(self, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """实际发料履行：逐笔把未履行占用转为真实耗用并写出库流水。

        任一笔失败整体回滚：不会出现部分履行后仍挂着占用的中间态。
        """
        request = self.repository.require_release(request_id)
        if request["status"] != "approved":
            raise ConflictError("只有已批准申请可以履行", context={"status": request["status"]})
        timestamp = to_storage(self.clock.now())
        actor = data.get("actor") or request.get("reviewed_by") or ""
        holds = self.repository.held_allocations_for_request(request_id)
        if not holds:
            raise ConflictError("该申请没有待履行的占用", context={"request_id": request_id})
        for allocation in holds:
            specimen_id = int(allocation["specimen_id"])
            quantity = float(allocation["quantity"])
            specimen = self.repository.require_specimen(specimen_id)
            if self.repository.active_holds(specimen_id):
                raise ConflictError("检材存在未解除冻结，不能出库", context={"specimen_id": specimen_id})
            # 账面不变量：available = initial - 实际耗用 - held。履行把本笔 held 转为耗用，
            # held 与耗用一减一增、总额不变，因此 available 数值不再变动，只补出库流水。
            physical = round(float(specimen["available_quantity"]) + quantity, 6)
            if physical < -1e-9 or specimen["status"] in {"depleted", "disposed"}:
                raise ConflictError("检材实体已不足或已处置，不能履行", context={"specimen_id": specimen_id})
            self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                "VALUES(?, '领用', ?, ?, ?, ?, ?)",
                (
                    specimen_id, -quantity, f"release-{request_id}-item-{allocation['release_item_id']}",
                    actor, f"领用申请 {request['request_no']} 实际发料", timestamp,
                ),
            )
            updated = self.connection.execute(
                "UPDATE specimen_allocations SET status='consumed',consumed_at=? WHERE id=? AND status='held'",
                (timestamp, allocation["id"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("占用已被释放或履行，状态冲突", context={"allocation_id": allocation["id"]})
            self.connection.execute(
                "UPDATE release_items SET status='fulfilled' WHERE id=?",
                (allocation["release_item_id"],),
            )
        # 全部占用处理完后按真实存量（初始 - 流水耗用）统一刷新耗尽状态与版本
        for specimen_id in {int(item["specimen_id"]) for item in holds}:
            consumed = self.repository.consumed_quantity(specimen_id)
            initial = float(self.repository.require_specimen(specimen_id)["initial_quantity"])
            target_status = "depleted" if round(initial - consumed, 6) <= 1e-9 else "stored"
            self.connection.execute(
                "UPDATE specimens SET status=?,version=version+1,updated_at=? WHERE id=? AND status!='disposed'",
                (target_status, timestamp, specimen_id),
            )
        self.connection.execute(
            "UPDATE release_requests SET status='fulfilled',version=version+1 WHERE id=? AND status='approved'",
            (request_id,),
        )
        return self.repository.release_detail(request_id)

    def audit_allocations(self, specimen_id: int | None = None) -> dict[str, Any]:
        """核对占用与账面：只读检查，给出确定的修复入口数据。"""
        clause = ""
        params: tuple[Any, ...] = ()
        if specimen_id is not None:
            clause, params = " WHERE l.id=?", (specimen_id,)
        rows = self.connection.execute(
            f"SELECT l.id AS specimen_id,l.specimen_no,l.initial_quantity,l.available_quantity,l.status "
            f"FROM specimens l{clause} ORDER BY l.id", params,
        ).fetchall()
        findings: list[dict[str, Any]] = []
        for row in rows:
            lot = dict(row)
            consumed = self.repository.consumed_quantity(int(lot["specimen_id"]))
            held = self.repository.held_quantity(int(lot["specimen_id"]))
            physical = round(float(lot["initial_quantity"]) - consumed, 6)
            expected_recorded = round(physical - held, 6)
            recorded = round(float(lot["available_quantity"]), 6)
            frozen = bool(self.repository.active_holds(int(lot["specimen_id"])))
            issues: list[str] = []
            if abs(recorded - expected_recorded) > 1e-6:
                issues.append("账面可用量与流水耗用及未履行占用不符")
            if physical + 1e-9 < held:
                issues.append("未履行占用超过实际存量（超分）")
            if held > 0 and frozen:
                # 占用在批准后被冻结：属于待处置状态而非硬错误，但必须暴露
                issues.append("已占用检材当前被冻结")
            if issues:
                findings.append({
                    "specimen_id": int(lot["specimen_id"]),
                    "specimen_no": lot["specimen_no"],
                    "initial_quantity": float(lot["initial_quantity"]),
                    "recorded_available_quantity": recorded,
                    "expected_available_quantity": max(expected_recorded, 0.0),
                    "physical_quantity": max(physical, 0.0),
                    "consumed_quantity": consumed,
                    "held_quantity": held,
                    "allocatable_quantity": max(round(physical - held, 6), 0.0),
                    "frozen": frozen,
                    "issues": issues,
                })
        # 孤立占用检查：占用存在但其明细/申请缺失（失败事务遗留）
        orphan_rows = self.connection.execute(
            "SELECT c.id,c.specimen_id,c.release_item_id,c.request_id,c.status FROM specimen_allocations c "
            "LEFT JOIN release_items i ON i.id=c.release_item_id "
            "LEFT JOIN release_requests r ON r.id=c.request_id "
            "WHERE i.id IS NULL OR r.id IS NULL"
        ).fetchall()
        orphans = [dict(row) for row in orphan_rows]
        # 已批准/已履行明细却没有对应占用记录（旧版“只标记不扣量”数据）
        unbacked_rows = self.connection.execute(
            "SELECT i.id AS item_id,i.request_id,i.quantity,i.allocated_specimen_id,r.request_no,r.status AS request_status "
            "FROM release_items i JOIN release_requests r ON r.id=i.request_id "
            "WHERE i.status IN ('allocated','fulfilled') AND i.allocated_specimen_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM specimen_allocations c WHERE c.release_item_id=i.id)"
        ).fetchall()
        unbacked = [dict(row) for row in unbacked_rows]
        return {
            "specimens_checked": len(rows),
            "findings": findings,
            "orphan_allocations": orphans,
            "unbacked_items": unbacked,
            "healthy": not findings and not orphans and not unbacked,
        }

    def repair_allocations(self, specimen_id: int | None = None, *, actor: str = "system-repair") -> dict[str, Any]:
        """遗留已批准数据的确定修复入口。

        修复规则（全部在同一事务内）：
        1. 删除无主孤立占用（失败事务残留）；
        2. 为旧版已批准但缺占用记录的明细补建 held 占用并同步扣减账面可用量，
           数量不足时将该明细置为 unavailable、申请回退 submitted 等待重新审批；
        3. 按流水耗用重算 available_quantity，并标记整体超分的占用。
        """
        timestamp = to_storage(self.clock.now())
        repaired: dict[str, Any] = {"orphans_removed": [], "occupancies_backfilled": [],
                                   "oversubscribed_items": [], "specimens_rebased": []}

        orphans = self.connection.execute(
            "SELECT c.id,c.specimen_id,c.release_item_id FROM specimen_allocations c "
            "LEFT JOIN release_items i ON i.id=c.release_item_id "
            "WHERE i.id IS NULL"
        ).fetchall()
        for orphan in orphans:
            self.connection.execute("DELETE FROM specimen_allocations WHERE id=?", (orphan["id"],))
            repaired["orphans_removed"].append(dict(orphan))

        # 旧数据：已批准/已分配明细但没有占用记录。按申请分组，任一笔无法补建
        # 占用则整单回退为 submitted、明细全部退回待分配，等待按新规则重新审批。
        legacy_items = self.connection.execute(
            "SELECT i.id AS item_id,i.request_id,i.case_id,i.quantity,i.allocated_specimen_id,r.status AS request_status "
            "FROM release_items i JOIN release_requests r ON r.id=i.request_id "
            "WHERE i.status='allocated' AND i.allocated_specimen_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM specimen_allocations c WHERE c.release_item_id=i.id) "
            "ORDER BY i.request_id,i.id"
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for line in legacy_items:
            grouped.setdefault(int(line["request_id"]), []).append(line)
        for legacy_request_id, lines in grouped.items():
            demand: dict[int, float] = {}
            frozen = False
            for line in lines:
                lot_id = int(line["allocated_specimen_id"])
                demand[lot_id] = demand.get(lot_id, 0.0) + float(line["quantity"])
                if self.repository.active_holds(lot_id):
                    frozen = True
            short = any(
                self.repository.availability(lot_id)["allocatable_quantity"] + 1e-9 < qty
                for lot_id, qty in demand.items()
            ) or frozen
            if short:
                for line in lines:
                    # 退回待分配，便于重新审批时按新规则重新选袋
                    self.connection.execute(
                        "UPDATE release_items SET status='requested',allocated_specimen_id=NULL WHERE id=?",
                        (line["item_id"],),
                    )
                    repaired["oversubscribed_items"].append(
                        {"item_id": line["item_id"], "request_id": legacy_request_id,
                         "quantity": float(line["quantity"])}
                    )
                self.connection.execute(
                    "UPDATE release_requests SET status='submitted',version=version+1 "
                    "WHERE id=? AND status='approved'",
                    (legacy_request_id,),
                )
                continue
            for line in lines:
                lot = self.repository.require_specimen(int(line["allocated_specimen_id"]))
                self._reserve(
                    {"id": line["item_id"]}, lot, float(line["quantity"]), actor, timestamp,
                    request_id=legacy_request_id,
                )
                repaired["occupancies_backfilled"].append(
                    {"item_id": line["item_id"], "request_id": legacy_request_id,
                     "specimen_id": lot["id"], "quantity": float(line["quantity"])}
                )

        # 账面重校：available_quantity = initial - 流水耗用净额 - 未履行占用
        clause = ""
        params: tuple[Any, ...] = ()
        if specimen_id is not None:
            clause, params = " WHERE l.id=?", (specimen_id,)
        lots = self.connection.execute(f"SELECT l.id,l.initial_quantity FROM specimens l{clause}", params).fetchall()
        for lot in lots:
            lot_id = int(lot["id"])
            consumed = self.repository.consumed_quantity(lot_id)
            held = self.repository.held_quantity(lot_id)
            expected = max(round(float(lot["initial_quantity"]) - consumed - held, 6), 0.0)
            current = float(self.repository.require_specimen(lot_id)["available_quantity"])
            if abs(current - expected) > 1e-6:
                self.connection.execute(
                    "UPDATE specimens SET available_quantity=?,version=version+1,updated_at=? WHERE id=?",
                    (expected, timestamp, lot_id),
                )
                repaired["specimens_rebased"].append(
                    {"specimen_id": lot_id, "from": current, "to": expected}
                )
        repaired["audit_after"] = self.audit_allocations(specimen_id)
        return repaired

    # ---- 内部辅助 ----------------------------------------------------------

    def _build_allocation_plan(self, items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """为全部明细选择检材；任一明细无法满足即整体拒绝（尚未写库）。"""
        plan: list[tuple[dict[str, Any], dict[str, Any]]] = []
        reserved_in_request: dict[int, float] = {}
        for item in items:
            quantity = float(item["quantity"])
            specimen = self._choose_specimen(int(item["case_id"]), quantity, reserved_in_request)
            if specimen is None:
                raise ConflictError("没有满足数量与检验条件的可领用检材", context={
                    "case_id": item["case_id"], "required_quantity": quantity,
                })
            plan.append((item, specimen))
            reserved_in_request[int(specimen["id"])] = (
                reserved_in_request.get(int(specimen["id"]), 0.0) + quantity
            )
        return plan

    def _reserve(self, item: dict[str, Any], specimen: dict[str, Any], quantity: float,
                 actor: str, timestamp: str, *, request_id: int | None = None) -> None:
        """原子建立一笔占用：条件扣减账面可用量并写入占用记录。

        UPDATE 带数量/状态/冻结三重谓词，rowcount=0 即数量不足或被并发改动，
        调用方事务整体回滚；release_item_id 唯一约束保证重试不重复占用。
        """
        updated = self.connection.execute(
            "UPDATE specimens SET available_quantity=MAX(ROUND(available_quantity-?,6),0),version=version+1,updated_at=? "
            "WHERE id=? AND status='stored' AND available_quantity-?>=-1e-9 "
            "AND NOT EXISTS(SELECT 1 FROM specimen_holds h WHERE h.specimen_id=specimens.id AND h.released_at IS NULL)",
            (quantity, timestamp, specimen["id"], quantity),
        )
        if updated.rowcount != 1:
            raise ConflictError("检材可分配量不足或已被冻结，请重新提交", context={
                "specimen_id": specimen["id"], "required_quantity": quantity,
            })
        actual_request_id = request_id if request_id is not None else int(item["request_id"])
        self.connection.execute(
            "INSERT INTO specimen_allocations(specimen_id,release_item_id,request_id,quantity,status,"
            "created_by,created_at) VALUES(?,?,?,?,'held',?,?)",
            (specimen["id"], int(item["id"]), actual_request_id, quantity, actor, timestamp),
        )
        self.connection.execute(
            "UPDATE release_items SET allocated_specimen_id=?,status='allocated' WHERE id=?",
            (specimen["id"], int(item["id"])),
        )

    def _release_holds(self, request_id: int, timestamp: str, *, actor: str, reason: str) -> None:
        holds = self.repository.held_allocations_for_request(request_id)
        for allocation in holds:
            self.connection.execute(
                "UPDATE specimens SET available_quantity=available_quantity+?,version=version+1,updated_at=? WHERE id=?",
                (float(allocation["quantity"]), timestamp, allocation["specimen_id"]),
            )
            self.connection.execute(
                "UPDATE specimen_allocations SET status='released',released_at=?,release_reason=? WHERE id=? AND status='held'",
                (timestamp, reason, allocation["id"]),
            )

    def _choose_specimen(self, case_id: int, quantity: float,
                         extra_holds: dict[int, float] | None = None) -> dict[str, Any] | None:
        """选择可领用检材，同时考虑实际耗用、未履行占用、冻结状态与本申请内预留。"""
        extra_holds = extra_holds or {}
        rows = self.connection.execute(
            "SELECT l.*,v.conformity_percent,v.completed_at FROM specimens l "
            "LEFT JOIN examinations v ON v.id=(SELECT id FROM examinations WHERE specimen_id=l.id AND status='completed' "
            "ORDER BY completed_at DESC,id DESC LIMIT 1) WHERE l.case_id=? AND l.status='stored' "
            "AND NOT EXISTS(SELECT 1 FROM specimen_holds h WHERE h.specimen_id=l.id AND h.released_at IS NULL) "
            "ORDER BY CASE WHEN v.conformity_percent IS NULL THEN 1 ELSE 0 END,v.completed_at,l.received_year,l.specimen_no",
            (case_id,),
        ).fetchall()
        for row in rows:
            candidate = record(row)
            assert candidate is not None
            availability = self.repository.availability(int(candidate["id"]))
            room = availability["allocatable_quantity"] - extra_holds.get(int(candidate["id"]), 0.0)
            if room + 1e-9 >= quantity:
                return candidate
        return None


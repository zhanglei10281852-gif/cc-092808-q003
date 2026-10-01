from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from app.core.errors import NotFoundError


JSON_COLUMNS = {
    "case_profile_json": "passport",
    "contact_json": "restrictions",
    "detail_json": "detail",
    "payload_json": "payload",
}


def record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for column, target in JSON_COLUMNS.items():
        if column in data:
            raw = data.pop(column)
            try:
                data[target] = json.loads(raw or "{}")
            except json.JSONDecodeError:
                data[target] = {}
    return data


def records(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [record(row) or {} for row in rows]


class ForensicRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def require_agency(self, agency_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM submitting_agencies WHERE id=?", (agency_id,)).fetchone())
        if item is None:
            raise NotFoundError("来源记录不存在")
        return item

    def require_forensic_case(self, case_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM forensic_cases WHERE id=?", (case_id,)).fetchone())
        if item is None:
            raise NotFoundError("鉴定案件不存在")
        return item

    def forensic_case_by_number(self, case_no: str) -> dict[str, Any] | None:
        return record(self.connection.execute("SELECT * FROM forensic_cases WHERE case_no=?", (case_no,)).fetchone())

    def forensic_case_detail(self, case_id: int) -> dict[str, Any]:
        item = self.require_forensic_case(case_id)
        if item.get("agency_id"):
            item["source"] = self.require_agency(int(item["agency_id"]))
        item["lots"] = records(self.connection.execute(
            "SELECT * FROM specimens WHERE case_id=? ORDER BY created_at,specimen_no", (case_id,)
        ).fetchall())
        item["events"] = records(self.connection.execute(
            "SELECT * FROM case_events WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall())
        return item

    def list_forensic_cases(self, *, status: str | None, crop: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status=?")
            params.append(status)
        if crop:
            where.append("discipline LIKE ?")
            params.append(f"%{crop.strip()}%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM forensic_cases{clause}", params).fetchone()[0])
        params.extend([limit, offset])
        rows = self.connection.execute(
            f"SELECT * FROM forensic_cases{clause} ORDER BY accepted_on DESC,case_no LIMIT ? OFFSET ?", params
        ).fetchall()
        return records(rows), total

    def require_location(self, location_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM storage_locations WHERE id=?", (location_id,)).fetchone())
        if item is None:
            raise NotFoundError("库位不存在")
        return item

    def location_usage(self, location_id: int) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM specimen_placements WHERE location_id=? AND removed_at IS NULL",
            (location_id,),
        ).fetchone()
        return float(row[0])

    def location_detail(self, location_id: int) -> dict[str, Any]:
        item = self.require_location(location_id)
        item["used_grams"] = self.location_usage(location_id)
        item["available_grams"] = round(float(item["capacity_units"]) - item["used_grams"], 6)
        item["placements"] = records(self.connection.execute(
            "SELECT p.*,l.specimen_no FROM specimen_placements p JOIN specimens l ON l.id=p.specimen_id "
            "WHERE p.location_id=? AND p.removed_at IS NULL ORDER BY p.container_code", (location_id,)
        ).fetchall())
        return item

    def require_specimen(self, specimen_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM specimens WHERE id=?", (specimen_id,)).fetchone())
        if item is None:
            raise NotFoundError("检材不存在")
        return item

    def active_holds(self, specimen_id: int) -> list[dict[str, Any]]:
        return records(self.connection.execute(
            "SELECT * FROM specimen_holds WHERE specimen_id=? AND released_at IS NULL ORDER BY id", (specimen_id,)
        ).fetchall())

    def held_quantity(self, specimen_id: int) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM release_allocations WHERE specimen_id=? AND status='held'",
            (specimen_id,),
        ).fetchone()
        return float(row[0])

    def specimen_allocations(self, specimen_id: int) -> list[dict[str, Any]]:
        return records(self.connection.execute(
            "SELECT a.*,r.request_no,r.status AS request_status,i.case_id,c.case_no "
            "FROM release_allocations a "
            "JOIN release_requests r ON r.id=a.request_id "
            "JOIN release_items i ON i.id=a.item_id "
            "JOIN forensic_cases c ON c.id=i.case_id "
            "WHERE a.specimen_id=? ORDER BY a.id",
            (specimen_id,),
        ).fetchall())

    def specimen_detail(self, specimen_id: int) -> dict[str, Any]:
        item = self.require_specimen(specimen_id)
        item["forensic_case"] = self.require_forensic_case(int(item["case_id"]))
        item["placements"] = records(self.connection.execute(
            "SELECT p.*,s.location_code FROM specimen_placements p JOIN storage_locations s ON s.id=p.location_id "
            "WHERE p.specimen_id=? ORDER BY p.id", (specimen_id,)
        ).fetchall())
        item["movements"] = records(self.connection.execute(
            "SELECT * FROM custody_events WHERE specimen_id=? ORDER BY id", (specimen_id,)
        ).fetchall())
        item["holds"] = records(self.connection.execute(
            "SELECT * FROM specimen_holds WHERE specimen_id=? ORDER BY id", (specimen_id,)
        ).fetchall())
        item["allocations"] = self.specimen_allocations(specimen_id)
        held_quantity = round(sum(float(a["quantity"]) for a in item["allocations"] if a["status"] == "held"), 6)
        consumed_quantity = round(sum(float(a["quantity"]) for a in item["allocations"] if a["status"] == "consumed"), 6)
        item["held_quantity"] = held_quantity
        item["consumed_quantity"] = consumed_quantity
        item["allocatable_quantity"] = round(float(item["available_quantity"]) - held_quantity, 6)
        item["frozen"] = bool(item["holds"] and any(not h["released_at"] for h in item["holds"]))
        item["latest_examination"] = record(self.connection.execute(
            "SELECT * FROM examinations WHERE specimen_id=? AND status='completed' ORDER BY completed_at DESC,id DESC LIMIT 1",
            (specimen_id,),
        ).fetchone())
        return item

    def require_placement(self, placement_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM specimen_placements WHERE id=?", (placement_id,)).fetchone())
        if item is None:
            raise NotFoundError("容器摆放记录不存在")
        return item

    def custody_event_by_key(self, key: str) -> dict[str, Any] | None:
        return record(self.connection.execute("SELECT * FROM custody_events WHERE idempotency_key=?", (key,)).fetchone())

    def require_protocol(self, protocol_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM examination_protocols WHERE id=?", (protocol_id,)).fetchone())
        if item is None:
            raise NotFoundError("检验规程不存在")
        return item

    def protocol_latest(self, code: str) -> dict[str, Any] | None:
        return record(self.connection.execute(
            "SELECT * FROM examination_protocols WHERE protocol_code=? ORDER BY version DESC LIMIT 1", (code,)
        ).fetchone())

    def require_examination(self, examination_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM examinations WHERE id=?", (examination_id,)).fetchone())
        if item is None:
            raise NotFoundError("检验任务不存在")
        return item

    def examination_detail(self, examination_id: int) -> dict[str, Any]:
        item = self.require_examination(examination_id)
        item["specimen"] = self.require_specimen(int(item["specimen_id"]))
        item["protocol"] = self.require_protocol(int(item["protocol_id"]))
        item["counts"] = records(self.connection.execute(
            "SELECT * FROM examination_observations WHERE examination_id=? ORDER BY checkpoint_no,sequence_no", (examination_id,)
        ).fetchall())
        return item

    def require_policy(self, policy_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM review_policies WHERE id=?", (policy_id,)).fetchone())
        if item is None:
            raise NotFoundError("复核策略不存在")
        return item

    def applicable_policy(self, discipline: str, risk_level: str, on_date: str) -> dict[str, Any] | None:
        return record(self.connection.execute(
            "SELECT * FROM review_policies WHERE discipline=? AND risk_level=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>=?) ORDER BY version DESC LIMIT 1",
            (discipline, risk_level, on_date, on_date),
        ).fetchone())

    def require_alert(self, alert_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM quality_alerts WHERE id=?", (alert_id,)).fetchone())
        if item is None:
            raise NotFoundError("质量告警不存在")
        return item

    def require_release(self, request_id: int) -> dict[str, Any]:
        item = record(self.connection.execute("SELECT * FROM release_requests WHERE id=?", (request_id,)).fetchone())
        if item is None:
            raise NotFoundError("领用申请不存在")
        return item

    def release_detail(self, request_id: int) -> dict[str, Any]:
        item = self.require_release(request_id)
        item["items"] = records(self.connection.execute(
            "SELECT i.*,a.case_no,a.discipline FROM release_items i "
            "JOIN forensic_cases a ON a.id=i.case_id WHERE i.request_id=? ORDER BY i.id", (request_id,)
        ).fetchall())
        allocations = records(self.connection.execute(
            "SELECT * FROM release_allocations WHERE request_id=? ORDER BY id", (request_id,)
        ).fetchall())
        for line in item["items"]:
            line["allocations"] = [alloc for alloc in allocations if int(alloc["item_id"]) == int(line["id"])]
        return item

    def count_table(self, table: str) -> int:
        allowed = {
            "forensic_cases", "specimens", "storage_locations", "examinations",
            "review_schedules", "quality_alerts", "release_requests",
        }
        if table not in allowed:
            raise ValueError("不允许统计该数据表")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

from __future__ import annotations

import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record


class CustodyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def create_location(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO storage_locations(location_code,facility,room,rack,shelf,capacity_units,reference_value,"
                "humidity_percent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    data["location_code"], data["facility"], data["room"], data["rack"], data["shelf"],
                    data["capacity_units"], data["reference_value"], data["humidity_percent"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("库位编码已经存在") from exc
        return self.repository.location_detail(int(cursor.lastrowid))

    def change_location_status(self, location_id: int, status: str, expected_version: int) -> dict[str, Any]:
        if status not in {"active", "maintenance", "closed"}:
            raise ValidationError("库位状态无效")
        before = self.repository.require_location(location_id)
        if int(before["version"]) != expected_version:
            raise ConflictError("库位版本冲突", context={"current_version": before["version"]})
        if status == "closed" and self.repository.location_usage(location_id) > 0:
            raise ConflictError("库位中仍有检材容器，不能关闭")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE storage_locations SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (status, timestamp, location_id, expected_version),
        )
        return self.repository.location_detail(location_id)

    def create_specimen(self, data: dict[str, Any]) -> dict[str, Any]:
        forensic_case = self.repository.require_forensic_case(int(data["case_id"]))
        if forensic_case["status"] not in {"accepted", "restricted", "quarantine"}:
            raise ConflictError("案件尚未受理，不能登记检材")
        parent = None
        if data.get("parent_specimen_id"):
            parent = self.repository.require_specimen(int(data["parent_specimen_id"]))
            if int(parent["case_id"]) != int(data["case_id"]):
                raise ValidationError("子检材必须与来源检材属于同一案件")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO specimens(specimen_no,case_id,parent_specimen_id,received_year,initial_quantity,"
                "available_quantity,integrity_percent,packaging,sealed_on,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    data["specimen_no"], data["case_id"], data.get("parent_specimen_id"), data["received_year"],
                    data["initial_quantity"], data["initial_quantity"], data.get("integrity_percent"),
                    data.get("packaging", ""), data.get("sealed_on"), data["created_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("检材编号已经存在") from exc
        specimen_id = int(cursor.lastrowid)
        if parent:
            self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                "VALUES(?,'盘点调整',0,?,?,?,?)",
                (specimen_id, f"lineage-{specimen_id}", data["created_by"], f"由来源检材 {parent['specimen_no']} 分取", timestamp),
            )
        return self.repository.specimen_detail(specimen_id)

    def place_specimen(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.custody_event_by_key(data["idempotency_key"])
        if previous:
            placement = self.repository.require_placement(int(previous["placement_id"]))
            return {"placement": placement, "replayed": True}
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        location = self.repository.require_location(int(data["location_id"]))
        if specimen["status"] in {"depleted", "disposed"}:
            raise ConflictError("检材已经耗尽或销毁")
        if location["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        active_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL",
            (specimen["id"],),
        ).fetchone()[0])
        if active_weight + float(data["quantity"]) > float(specimen["available_quantity"]) + 1e-9:
            raise ValidationError("摆放数量超过检材可用数量")
        used = self.repository.location_usage(int(location["id"]))
        if used + float(data["quantity"]) > float(location["capacity_units"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": location["capacity_units"] - used})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
                (specimen["id"], location["id"], data["quantity"], data["container_code"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("容器编码与入库时间冲突") from exc
        placement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO custody_events(specimen_id,placement_id,movement_type,quantity,to_location_id,idempotency_key,"
            "actor,reason,created_at) VALUES(?,?,'入库',?,?,?,?,?,?)",
            (specimen["id"], placement_id, data["quantity"], location["id"], data["idempotency_key"], data["actor"], "首次入库", timestamp),
        )
        self.connection.execute(
            "UPDATE specimens SET status='stored',version=version+1,updated_at=? WHERE id=?",
            (timestamp, specimen["id"]),
        )
        return {"placement": self.repository.require_placement(placement_id), "replayed": False}

    def move_placement(self, placement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.custody_event_by_key(data["idempotency_key"])
        if previous:
            return {"placement": self.repository.require_placement(int(previous["placement_id"])), "replayed": True}
        placement = self.repository.require_placement(placement_id)
        if placement["removed_at"]:
            raise ConflictError("容器已经移出原库位")
        if int(placement["version"]) != int(data["expected_version"]):
            raise ConflictError("容器摆放版本冲突", context={"current_version": placement["version"]})
        target = self.repository.require_location(int(data["target_location_id"]))
        if target["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        used = self.repository.location_usage(int(target["id"]))
        if used + float(placement["quantity"]) > float(target["capacity_units"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": target["capacity_units"] - used})
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
            (placement["specimen_id"], target["id"], placement["quantity"], placement["container_code"], timestamp),
        )
        new_id = int(cursor.lastrowid)
        updated = self.connection.execute(
            "UPDATE specimen_placements SET removed_at=?,version=version+1 WHERE id=? AND version=? AND removed_at IS NULL",
            (timestamp, placement_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("容器摆放版本冲突")
        self.connection.execute(
            "INSERT INTO custody_events(specimen_id,placement_id,movement_type,quantity,from_location_id,to_location_id,"
            "idempotency_key,actor,reason,created_at) VALUES(?,?,'移库',?,?,?,?,?,?,?)",
            (
                placement["specimen_id"], new_id, placement["quantity"], placement["location_id"], target["id"],
                data["idempotency_key"], data["actor"], data["reason"], timestamp,
            ),
        )
        return {"placement": self.repository.require_placement(new_id), "replayed": False}

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.custody_event_by_key(data["idempotency_key"])
        if previous:
            return {"specimen": self.repository.specimen_detail(int(previous["specimen_id"])), "movement": previous, "replayed": True}
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        holds = self.repository.active_holds(int(specimen["id"]))
        if holds:
            raise ConflictError("检材存在未解除的保全、质量或权限冻结", context={"holds": [item["id"] for item in holds]})
        quantity = float(data["quantity"])
        held_quantity = self.repository.held_quantity(int(specimen["id"]))
        allocatable = round(float(specimen["available_quantity"]) - held_quantity, 6)
        if quantity > allocatable + 1e-9:
            raise ConflictError(
                "检材可分配数量不足（需同时扣除尚未履行的批准占用）",
                context={"available_quantity": specimen["available_quantity"], "held_quantity": held_quantity},
            )
        timestamp = to_storage(self.clock.now())
        remaining = round(float(specimen["available_quantity"]) - quantity, 6)
        status = "depleted" if remaining <= 1e-9 else specimen["status"]
        self.connection.execute(
            "UPDATE specimens SET available_quantity=?,status=?,version=version+1,updated_at=? WHERE id=?",
            (remaining, status, timestamp, specimen["id"]),
        )
        cursor = self.connection.execute(
            "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (specimen["id"], data["movement_type"], -quantity, data["idempotency_key"], data["actor"], data["reason"], timestamp),
        )
        return {
            "specimen": self.repository.specimen_detail(int(specimen["id"])),
            "movement": record(self.connection.execute("SELECT * FROM custody_events WHERE id=?", (cursor.lastrowid,)).fetchone()),
            "replayed": False,
        }

    def impose_hold(self, data: dict[str, Any]) -> dict[str, Any]:
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        existing = self.connection.execute(
            "SELECT * FROM specimen_holds WHERE specimen_id=? AND hold_type=? AND released_at IS NULL",
            (specimen["id"], data["hold_type"]),
        ).fetchone()
        if existing:
            raise ConflictError("该类型冻结已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO specimen_holds(specimen_id,hold_type,reason,imposed_by,imposed_at) VALUES(?,?,?,?,?)",
            (specimen["id"], data["hold_type"], data["reason"], data["actor"], timestamp),
        )
        self.connection.execute(
            "UPDATE specimens SET status='held',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('depleted','disposed')",
            (timestamp, specimen["id"]),
        )
        return record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def release_hold(self, hold_id: int, actor: str, reason: str) -> dict[str, Any]:
        hold = record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (hold_id,)).fetchone())
        if not hold:
            raise ValidationError("冻结记录不存在")
        if hold["released_at"]:
            raise ConflictError("冻结记录已经解除")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE specimen_holds SET released_by=?,released_at=?,release_reason=? WHERE id=? AND released_at IS NULL",
            (actor, timestamp, reason, hold_id),
        )
        remaining = self.repository.active_holds(int(hold["specimen_id"]))
        if not remaining:
            self.connection.execute(
                "UPDATE specimens SET status=CASE WHEN available_quantity<=0 THEN 'depleted' ELSE 'stored' END,"
                "version=version+1,updated_at=? WHERE id=? AND status='held'",
                (timestamp, hold["specimen_id"]),
            )
        return record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (hold_id,)).fetchone()) or {}

    def reconcile(self, specimen_id: int) -> dict[str, Any]:
        specimen = self.repository.require_specimen(specimen_id)
        movement_total = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM custody_events WHERE specimen_id=? AND movement_type IN ('取样','领用','报废','归还','盘点调整')",
            (specimen_id,),
        ).fetchone()[0])
        expected_available = round(float(specimen["initial_quantity"]) + movement_total, 6)
        placed_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL", (specimen_id,)
        ).fetchone()[0])
        held_quantity = self.repository.held_quantity(specimen_id)
        return {
            "specimen_id": specimen_id,
            "recorded_available_grams": specimen["available_quantity"],
            "expected_available_grams": expected_available,
            "active_placement_grams": placed_weight,
            "held_grams": round(held_quantity, 6),
            "allocatable_grams": round(float(specimen["available_quantity"]) - held_quantity, 6),
            "available_matches_ledger": abs(float(specimen["available_quantity"]) - expected_available) < 1e-6,
            "placements_within_available": placed_weight <= float(specimen["available_quantity"]) + 1e-6,
            "holds_within_available": held_quantity <= float(specimen["available_quantity"]) + 1e-6,
        }

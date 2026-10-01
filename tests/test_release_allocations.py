from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app.core.errors import ConflictError
from app.database import transaction
from app.forensics.service import ForensicService
from tests.test_forensics_workflow import create_accepted_forensic_case

LEGACY_TS = datetime(2026, 9, 20, tzinfo=UTC).isoformat(timespec="seconds")


def create_lot(service: ForensicService, suffix: str, quantity: float = 5.0) -> tuple[dict, dict, dict]:
    forensic_case = create_accepted_forensic_case(service, suffix)
    location = service.custody.create_location({
        "location_code": f"VAULT-{suffix}", "facility": "检材保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    })
    specimen = service.custody.create_specimen({
        "specimen_no": f"SP-{suffix}", "case_id": forensic_case["id"], "parent_specimen_id": None,
        "received_year": 2026, "initial_quantity": quantity, "integrity_percent": 100,
        "packaging": "防拆封袋，封识完整", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    service.custody.place_specimen({
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": quantity,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    return forensic_case, service.repository.specimen_detail(specimen["id"]), location


def submit_request(service: ForensicService, no: str, case_id: int, quantity: float) -> dict:
    request = service.release.create_request({
        "request_no": no, "requester": "法医物证实验室", "purpose": "补充检验取样",
        "items": [{"case_id": case_id, "quantity": quantity}],
    })
    return service.release.submit(request["id"], 1)


def approve(service: ForensicService, request_id: int, version: int, actor: str = "审核员甲") -> dict:
    return service.release.decide(request_id, {
        "approve": True, "expected_version": version, "actor": actor, "reason": "材料充足",
    })


def test_second_approval_over_same_lot_is_rejected(client):
    """两份各需 4 份的申请面对仅 5 份的同一袋检材：只能批准一份。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "101", quantity=5)
        first = submit_request(service, "REL-101", forensic_case["id"], 4)
        second = submit_request(service, "REL-102", forensic_case["id"], 4)

        approved = approve(service, first["id"], first["version"])
        assert approved["status"] == "approved"
        assert approved["items"][0]["allocations"][0]["status"] == "held"

        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 5
        assert detail["held_quantity"] == 4
        assert detail["allocatable_quantity"] == 1
        assert len(detail["allocations"]) == 1

        with pytest.raises(ConflictError):
            approve(service, second["id"], second["version"], actor="审核员乙")

        # 失败的审批不允许改变申请状态，也不允许留下任何占用明细
        assert service.repository.require_release(second["id"])["status"] == "submitted"
        assert connection.execute("SELECT COUNT(*) FROM release_allocations").fetchone()[0] == 1
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["held_quantity"] == 4
        assert detail["allocatable_quantity"] == 1


def test_retrying_same_approval_does_not_double_hold(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "102", quantity=5)
        request = submit_request(service, "REL-201", forensic_case["id"], 2)

        approved = approve(service, request["id"], request["version"])
        assert approved["version"] == request["version"] + 1
        with pytest.raises(ConflictError):
            approve(service, request["id"], request["version"])
        detail = service.repository.specimen_detail(specimen["id"])
        assert len(detail["allocations"]) == 1
        assert detail["held_quantity"] == 2


def test_concurrent_approvals_only_one_wins(client):
    """两个连接并发批准不同申请，BEGIN IMMEDIATE + 条件更新保证只有一方成功。"""
    db_path = None
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, _, _ = create_lot(service, "103", quantity=5)
        case_id = forensic_case["id"]
        db_path = connection.execute("PRAGMA database_list").fetchone()[2]
        first = submit_request(service, "REL-301", case_id, 4)
        second = submit_request(service, "REL-302", case_id, 4)
        first_id, first_version = first["id"], first["version"]
        second_id, second_version = second["id"], second["version"]

    results: dict[str, str] = {}

    def worker(name: str, request_id: int, version: int) -> None:
        other = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        other.row_factory = sqlite3.Row
        other.execute("PRAGMA foreign_keys=ON")
        other.execute("PRAGMA busy_timeout=30000")
        try:
            other.execute("BEGIN IMMEDIATE")
            svc = ForensicService(other)
            approve(svc, request_id, version, actor=name)
            other.commit()
            results[name] = "approved"
        except ConflictError:
            other.rollback()
            results[name] = "rejected"
        finally:
            other.close()

    t1 = threading.Thread(target=worker, args=("审核员甲", first_id, first_version))
    t2 = threading.Thread(target=worker, args=("审核员乙", second_id, second_version))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == ["approved", "rejected"]
    with transaction(immediate=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM release_allocations").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM release_allocations WHERE status='held'"
        ).fetchone()[0] == 4


def test_concurrent_decision_on_same_request_single_winner(client):
    db_path = None
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, _, _ = create_lot(service, "104", quantity=10)
        request = submit_request(service, "REL-401", forensic_case["id"], 3)
        db_path = connection.execute("PRAGMA database_list").fetchone()[2]
        request_id, version = request["id"], request["version"]

    results: dict[str, str] = {}

    def worker(name: str) -> None:
        other = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        other.row_factory = sqlite3.Row
        other.execute("PRAGMA foreign_keys=ON")
        other.execute("PRAGMA busy_timeout=30000")
        try:
            other.execute("BEGIN IMMEDIATE")
            approve(ForensicService(other), request_id, version, actor=name)
            other.commit()
            results[name] = "approved"
        except ConflictError:
            other.rollback()
            results[name] = "rejected"
        finally:
            other.close()

    t1 = threading.Thread(target=worker, args=("审核员甲",))
    t2 = threading.Thread(target=worker, args=("审核员乙",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == ["approved", "rejected"]
    with transaction(immediate=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM release_allocations").fetchone()[0] == 1


def test_reject_cancel_and_fulfill_lifecycle(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "105", quantity=5)

        rejected = submit_request(service, "REL-501", forensic_case["id"], 4)
        service.release.decide(rejected["id"], {
            "approve": False, "expected_version": rejected["version"], "actor": "审核员甲", "reason": "用途不清",
        })
        assert service.repository.specimen_detail(specimen["id"])["allocatable_quantity"] == 5
        assert service.repository.release_detail(rejected["id"])["items"][0]["status"] == "cancelled"

        approved_req = submit_request(service, "REL-502", forensic_case["id"], 4)
        approved = approve(service, approved_req["id"], approved_req["version"])
        assert service.repository.specimen_detail(specimen["id"])["allocatable_quantity"] == 1

        cancelled = service.release.cancel(approved["id"], {
            "expected_version": approved["version"], "actor": "审核员甲", "reason": "申请单位撤回",
        })
        assert cancelled["status"] == "cancelled"
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["held_quantity"] == 0
        assert detail["allocatable_quantity"] == 5
        allocation = detail["allocations"][0]
        assert allocation["status"] == "released"
        assert allocation["released_by"] == "审核员甲"

        # 取消后释放回池的检材可再次被批准
        other = submit_request(service, "REL-503", forensic_case["id"], 5)
        reapproved = approve(service, other["id"], other["version"])
        fulfilled = service.release.fulfill(reapproved["id"], {
            "expected_version": reapproved["version"], "actor": "保管员",
        })
        assert fulfilled["status"] == "fulfilled"
        assert fulfilled["items"][0]["status"] == "fulfilled"
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 0
        assert detail["status"] == "depleted"
        assert detail["held_quantity"] == 0
        assert detail["consumed_quantity"] == 5
        final = detail["allocations"][-1]
        assert final["status"] == "consumed"
        assert final["consumed_event_id"] is not None
        movement = next(m for m in detail["movements"] if m["id"] == final["consumed_event_id"])
        assert movement["movement_type"] == "领用" and movement["quantity"] == -5
        assert service.custody.reconcile(specimen["id"])["available_matches_ledger"] is True


def test_failed_multi_item_approval_leaves_no_orphans(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        case_a, specimen_a, _ = create_lot(service, "106a", quantity=5)
        case_b, _, _ = create_lot(service, "106b", quantity=5)
        request = service.release.create_request({
            "request_no": "REL-601", "requester": "实验室", "purpose": "跨案取样",
            "items": [{"case_id": case_a["id"], "quantity": 4}, {"case_id": case_b["id"], "quantity": 4}],
        })
        submitted = service.release.submit(request["id"], 1)
        first = approve(service, submitted["id"], submitted["version"])
        assert first["status"] == "approved"

        second = service.release.create_request({
            "request_no": "REL-602", "requester": "实验室", "purpose": "跨案取样",
            "items": [{"case_id": case_a["id"], "quantity": 4}, {"case_id": case_b["id"], "quantity": 4}],
        })
        second = service.release.submit(second["id"], 1)
        # 两个案件各只剩 1 可分配，任一明细都不够；第二份整体失败
        with pytest.raises(ConflictError):
            approve(service, second["id"], second["version"], actor="审核员乙")
        assert service.repository.require_release(second["id"])["status"] == "submitted"
        assert connection.execute(
            "SELECT COUNT(*) FROM release_allocations WHERE request_id=?", (second["id"],)
        ).fetchone()[0] == 0
        assert service.repository.specimen_detail(specimen_a["id"])["held_quantity"] == 4


def test_hold_freeze_blocks_approval_and_fulfillment(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "107", quantity=5)
        service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "争议", "reason": "待法院裁定", "actor": "审核员甲",
        })
        request = submit_request(service, "REL-701", forensic_case["id"], 1)
        with pytest.raises(ConflictError):
            approve(service, request["id"], request["version"])
        assert service.repository.specimen_detail(specimen["id"])["frozen"] is True

        service.custody.release_hold(
            connection.execute("SELECT id FROM specimen_holds WHERE released_at IS NULL").fetchone()[0],
            "审核员甲", "裁定结束",
        )
        approved = approve(service, request["id"], request["version"])
        service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "保全", "reason": "突发异议", "actor": "审核员甲",
        })
        with pytest.raises(ConflictError):
            service.release.fulfill(approved["id"], {"expected_version": approved["version"], "actor": "保管员"})
        # 履行失败不能把占用转成耗用
        assert service.repository.specimen_detail(specimen["id"])["held_quantity"] == 1


def test_withdraw_respects_held_quantity(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "108", quantity=5)
        request = submit_request(service, "REL-801", forensic_case["id"], 4)
        approve(service, request["id"], request["version"])
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 2, "movement_type": "取样",
                "idempotency_key": "withdraw-108-1", "actor": "技术员", "reason": "试验",
            })
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 1, "movement_type": "取样",
            "idempotency_key": "withdraw-108-2", "actor": "技术员", "reason": "试验",
        })
        assert result["specimen"]["available_quantity"] == 4
        assert result["specimen"]["held_quantity"] == 4
        assert result["specimen"]["allocatable_quantity"] == 0


def _insert_legacy_approved(connection: sqlite3.Connection, request_no: str, case_id: int,
                            specimen_id: int, quantity: float, status: str) -> int:
    cursor = connection.execute(
        "INSERT INTO release_requests(request_no,requester,purpose,status,requested_at) VALUES(?,?,?,?,?)",
        (request_no, "实验室", "历史申请", status, LEGACY_TS),
    )
    request_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO release_items(request_id,case_id,quantity,allocated_specimen_id,status) VALUES(?,?,?,?,'allocated')",
        (request_id, case_id, quantity, specimen_id),
    )
    return request_id


def test_doctor_detects_and_repairs_legacy_approved_data(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "109", quantity=5)

        # 历史已批准但从未建立占用的数据
        legacy_approved = _insert_legacy_approved(
            connection, "REL-901", forensic_case["id"], specimen["id"], 4, "approved"
        )
        # 历史已履行但既无占用也无领用台账的数据
        legacy_fulfilled = _insert_legacy_approved(
            connection, "REL-902", forensic_case["id"], specimen["id"], 1, "fulfilled"
        )

        report = service.release.doctor()
        kinds = {issue["type"] for issue in report["issues"]}
        assert "missing_allocation" in kinds
        assert report["healthy"] is False

        repaired = service.release.repair()
        assert {action["type"] for action in repaired["actions"]} >= {"backfill_hold", "backfill_consumed"}
        assert repaired["after"]["healthy"] is True

        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 4          # 履行的 1 已转真实耗用
        assert detail["held_quantity"] == 4               # 批准的 4 补建为占用
        assert detail["allocatable_quantity"] == 0
        assert detail["consumed_quantity"] == 1
        approved_detail = service.repository.release_detail(legacy_approved)
        assert approved_detail["items"][0]["allocations"][0]["status"] == "held"
        fulfilled_detail = service.repository.release_detail(legacy_fulfilled)
        consumed = fulfilled_detail["items"][0]["allocations"][0]
        assert consumed["status"] == "consumed" and consumed["consumed_event_id"] is not None
        assert service.custody.reconcile(specimen["id"])["available_matches_ledger"] is True


def test_doctor_repair_oversubscribed_legacy(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "110", quantity=5)
        _insert_legacy_approved(connection, "REL-A01", forensic_case["id"], specimen["id"], 4, "approved")
        _insert_legacy_approved(connection, "REL-A02", forensic_case["id"], specimen["id"], 4, "approved")

        report = service.release.doctor()
        assert any(issue["type"] == "missing_allocation" for issue in report["issues"])
        repaired = service.release.repair()
        assert repaired["after"]["healthy"] is True
        types = [action["type"] for action in repaired["actions"]]
        assert "backfill_hold" in types and "mark_unavailable" in types
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["held_quantity"] == 4
        assert detail["allocatable_quantity"] == 1


def test_doctor_repair_fulfilled_legacy_exceeding_physical(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, _ = create_lot(service, "111", quantity=3)
        _insert_legacy_approved(connection, "REL-B01", forensic_case["id"], specimen["id"], 5, "fulfilled")

        repaired = service.release.repair()
        assert repaired["after"]["healthy"] is True
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 0
        assert detail["consumed_quantity"] == 5
        action = next(a for a in repaired["actions"] if a["type"] == "backfill_consumed")
        assert action["deducted"] == 3 and action["adjusted_gap"] == 2
        reconcile = service.custody.reconcile(specimen["id"])
        assert reconcile["available_matches_ledger"] is True


def test_release_doctor_api(client, admin):
    response = client.get("/api/forensics/releases/doctor/check", headers=admin["headers"])
    assert response.status_code == 200
    assert response.json()["healthy"] is True

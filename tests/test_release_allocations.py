from __future__ import annotations

import threading

from app.core.errors import ConflictError
from app.database import database_path, get_connection, transaction
from app.forensics.service import ForensicService

from tests.test_forensics_workflow import create_accepted_forensic_case


def prepare_lot(service: ForensicService, suffix: str, quantity: float = 5.0) -> tuple[dict, dict]:
    """建立一个已受理案件与一袋指定数量、已上架的检材。"""
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
    return forensic_case, service.repository.specimen_detail(specimen["id"])


def submit_request(service: ForensicService, request_no: str, case_id: int, quantity: float) -> dict:
    request = service.release.create_request({
        "request_no": request_no, "requester": "法医物证实验室", "purpose": "补充检验取样",
        "items": [{"case_id": case_id, "quantity": quantity}],
    })
    return service.release.submit(request["id"], 1)


def approve(service: ForensicService, request_id: int, version: int, actor: str = "审核员甲") -> dict:
    return service.release.decide(request_id, {
        "approve": True, "expected_version": version, "actor": actor, "reason": "材料充足",
    })


def test_two_approvals_against_undersized_bag_second_is_rejected(client):
    """复现缺陷：一袋 5 份，两份各 4 份的申请不能都批准。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A1", quantity=5)
        first = submit_request(service, "REL-A1-1", forensic_case["id"], 4)
        second = submit_request(service, "REL-A1-2", forensic_case["id"], 4)

        approved = approve(service, first["id"], first["version"])
        assert approved["status"] == "approved"

        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 1
        assert view["held_quantity"] == 4
        assert view["allocatable_quantity"] == 1

        try:
            approve(service, second["id"], second["version"], actor="审核员乙")
        except ConflictError:
            pass
        else:
            raise AssertionError("第二份申请超出可分配量，必须拒绝")

        # 失败的批准不得留下任何痕迹
        second_view = service.repository.release_detail(second["id"])
        assert second_view["status"] == "submitted"
        assert second_view["items"][0]["allocation"] is None
        assert service.connection.execute("SELECT COUNT(*) FROM specimen_allocations").fetchone()[0] == 1
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 1 and view["held_quantity"] == 4


def test_approval_retry_does_not_double_reserve(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A2", quantity=5)
        request = submit_request(service, "REL-A2", forensic_case["id"], 4)
        approve(service, request["id"], request["version"])
        for _ in range(2):
            try:
                approve(service, request["id"], request["version"])
            except ConflictError:
                pass
            else:
                raise AssertionError("同版本重试必须失败")
        allocations = service.connection.execute(
            "SELECT COUNT(*) FROM specimen_allocations WHERE release_item_id IN "
            "(SELECT id FROM release_items WHERE request_id=?)", (request["id"],)
        ).fetchone()[0]
        assert allocations == 1
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 1


def test_concurrent_approvals_only_one_wins(client):
    """两个线程并发批准同一申请：版本/状态谓词保证只有一方成功。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A3", quantity=5)
        request = submit_request(service, "REL-A3", forensic_case["id"], 4)
        request_id, version = request["id"], request["version"]

    outcomes: list[str] = []

    def worker() -> None:
        conn = get_connection()
        local_service = ForensicService(conn)
        try:
            with transaction(immediate=True):
                approve(local_service, request_id, version)
            outcomes.append("approved")
        except ConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["approved", "conflict"]
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 1 and view["held_quantity"] == 4
        assert service.connection.execute(
            "SELECT COUNT(*) FROM specimen_allocations WHERE status='held'"
        ).fetchone()[0] == 1


def test_concurrent_approvals_different_requests_share_bag(client):
    """两个线程并发批准不同申请但争用同一袋：条件扣减只有一方成功。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A4", quantity=5)
        first = submit_request(service, "REL-A4-1", forensic_case["id"], 4)
        second = submit_request(service, "REL-A4-2", forensic_case["id"], 4)
        first_id, second_id = first["id"], second["id"]
        first_ver, second_ver = first["version"], second["version"]

    outcomes: list[str] = []

    def worker(request_id: int, version: int) -> None:
        local_service = ForensicService(get_connection())
        try:
            with transaction(immediate=True):
                approve(local_service, request_id, version)
            outcomes.append(f"ok-{request_id}")
        except ConflictError:
            outcomes.append(f"conflict-{request_id}")

    threads = [
        threading.Thread(target=worker, args=(first_id, first_ver)),
        threading.Thread(target=worker, args=(second_id, second_ver)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([item for item in outcomes if item.startswith("ok")]) == 1
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        view = service.repository.specimen_detail(specimen["id"])
        assert view["held_quantity"] == 4 and view["allocatable_quantity"] == 1


def test_rejection_leaves_no_occupancy(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A5", quantity=5)
        request = submit_request(service, "REL-A5", forensic_case["id"], 4)
        result = service.release.decide(request["id"], {
            "approve": False, "expected_version": request["version"], "actor": "审核员", "reason": "手续不全",
        })
        assert result["status"] == "rejected"
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 5 and view["held_quantity"] == 0
        assert result["items"][0]["allocation"] is None


def test_cancel_approved_request_releases_occupancy(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A6", quantity=5)
        request = submit_request(service, "REL-A6", forensic_case["id"], 4)
        approved = approve(service, request["id"], request["version"])
        assert service.repository.specimen_detail(specimen["id"])["allocatable_quantity"] == 1

        cancelled = service.release.cancel(approved["id"], {
            "expected_version": approved["version"], "actor": "申请人", "reason": "不再需要",
        })
        assert cancelled["status"] == "cancelled"
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 5 and view["held_quantity"] == 0
        allocation = cancelled["items"][0]["allocation"]
        assert allocation["status"] == "released" and allocation["released_at"]


def test_fulfill_turns_occupancy_into_real_consumption(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A7", quantity=5)
        request = submit_request(service, "REL-A7", forensic_case["id"], 4)
        approved = approve(service, request["id"], request["version"])

        fulfilled = service.release.fulfill(approved["id"], {"actor": "保管员"})
        assert fulfilled["status"] == "fulfilled"
        assert fulfilled["items"][0]["status"] == "fulfilled"

        view = service.repository.specimen_detail(specimen["id"])
        # 占用转为真实耗用：held=0、consumed=4、账面仍为 1
        assert view["held_quantity"] == 0
        assert view["consumed_quantity"] == 4
        assert view["available_quantity"] == 1
        allocation = fulfilled["items"][0]["allocation"]
        assert allocation["status"] == "consumed" and allocation["consumed_at"]
        movements = [m for m in view["movements"] if m["movement_type"] == "领用"]
        assert len(movements) == 1 and movements[0]["quantity"] == -4

        # 履行后流水与保管账核对一致
        reconcile = service.custody.reconcile(specimen["id"])
        assert reconcile["available_matches_ledger"] is True

        # 重复履行必须被拒绝
        try:
            service.release.fulfill(approved["id"], {"actor": "保管员"})
        except ConflictError:
            pass
        else:
            raise AssertionError("已履行申请不能再次出库")


def test_frozen_specimen_excluded_and_blocks_fulfillment(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A8", quantity=5)
        request = submit_request(service, "REL-A8", forensic_case["id"], 4)
        approved = approve(service, request["id"], request["version"])

        # 批准后冻结：不能出库履行
        hold = service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "质量", "reason": "等待复核", "actor": "审核员",
        })
        try:
            service.release.fulfill(approved["id"], {"actor": "保管员"})
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结检材不能出库")

        service.custody.release_hold(hold["id"], "审核员", "复核通过")
        fulfilled = service.release.fulfill(approved["id"], {"actor": "保管员"})
        assert fulfilled["status"] == "fulfilled"

    # 全新冻结检材：审批时直接不参与分配
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "A9", quantity=5)
        service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "保全", "reason": "争议保全", "actor": "审核员",
        })
        request = submit_request(service, "REL-A9", forensic_case["id"], 1)
        try:
            approve(service, request["id"], request["version"])
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结检材不应被分配")


def test_consumption_and_occupancy_both_reduce_allocatable(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "B1", quantity=5)
        # 实际取样耗用 2
        service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 2, "movement_type": "取样",
            "idempotency_key": "withdraw-b1", "actor": "技术员", "reason": "初检",
        })
        first = submit_request(service, "REL-B1-1", forensic_case["id"], 2)
        approve(service, first["id"], first["version"])
        view = service.repository.specimen_detail(specimen["id"])
        assert view["consumed_quantity"] == 2 and view["held_quantity"] == 2
        assert view["allocatable_quantity"] == 1

        second = submit_request(service, "REL-B1-2", forensic_case["id"], 2)
        try:
            approve(service, second["id"], second["version"])
        except ConflictError:
            pass
        else:
            raise AssertionError("实际耗用 + 占用后只剩 1 份，2 份申请必须失败")


def test_visibility_allocations_on_request_and_specimen(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "B2", quantity=5)
        request = submit_request(service, "REL-B2", forensic_case["id"], 4)
        approve(service, request["id"], request["version"])

        detail = service.repository.release_detail(request["id"])
        allocation = detail["items"][0]["allocation"]
        assert allocation is not None
        assert allocation["specimen_id"] == specimen["id"]
        assert allocation["quantity"] == 4 and allocation["status"] == "held"

        view = service.repository.specimen_detail(specimen["id"])
        assert len(view["allocations"]) == 1
        assert view["allocations"][0]["request_no"] == "REL-B2"
        assert view["allocatable_quantity"] == 1

        # 已批准未履行期间，占用尚未转流水，账实核对仍应成立
        held_reconcile = service.custody.reconcile(specimen["id"])
        assert held_reconcile["available_matches_ledger"] is True
        assert held_reconcile["no_oversubscription"] is True


def test_legacy_overapproved_data_is_detected_and_repaired(client):
    """模拟旧缺陷数据：两份申请各 4 份都已批准但无占用、可用量仍为 5。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "C1", quantity=5)
        first = submit_request(service, "REL-C1-1", forensic_case["id"], 4)
        second = submit_request(service, "REL-C1-2", forensic_case["id"], 4)
        # 直接用旧逻辑的写法伪造：标记批准、写 allocated_specimen_id，但不扣量、不建占用
        for request in (first, second):
            service.connection.execute(
                "UPDATE release_requests SET status='approved',reviewed_by='旧审核员' WHERE id=?", (request["id"],)
            )
            service.connection.execute(
                "UPDATE release_items SET allocated_specimen_id=?,status='allocated' WHERE request_id=?",
                (specimen["id"], request["id"]),
            )

        audit = service.release.audit_allocations()
        assert audit["healthy"] is False
        unbacked_ids = {
            service.repository.release_detail(rid)["items"][0]["id"] for rid in (first["id"], second["id"])
        }
        assert {item["item_id"] for item in audit["unbacked_items"]} == unbacked_ids

        report = service.release.repair_allocations(actor="repairer")
        # 确定性 FIFO 修复：先批准的一单补建占用，超量的后一单整单退回 submitted
        backfilled = {item["request_id"] for item in report["occupancies_backfilled"]}
        repaired_ids = {item["request_id"] for item in report["oversubscribed_items"]}
        assert backfilled == {first["id"]}
        assert repaired_ids == {second["id"]}
        assert service.repository.require_release(first["id"])["status"] == "approved"
        assert service.repository.require_release(second["id"])["status"] == "submitted"

        after = service.release.audit_allocations()
        assert after["healthy"] is True
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 1 and view["held_quantity"] == 4

        # 修复后只剩 1 份，退回的 4 份申请重新审批仍应失败
        try:
            approve(service, second["id"], service.repository.require_release(second["id"])["version"])
        except ConflictError:
            pass
        else:
            raise AssertionError("修复后超分申请仍不应通过")


def test_legacy_repairable_data_backfills_occupancy(client):
    """旧数据量足够时：修复补建占用并同步扣减账面。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "C2", quantity=5)
        request = submit_request(service, "REL-C2", forensic_case["id"], 4)
        service.connection.execute(
            "UPDATE release_requests SET status='approved',reviewed_by='旧审核员' WHERE id=?", (request["id"],)
        )
        service.connection.execute(
            "UPDATE release_items SET allocated_specimen_id=?,status='allocated' WHERE request_id=?",
            (specimen["id"], request["id"]),
        )

        report = service.release.repair_allocations()
        assert len(report["occupancies_backfilled"]) == 1
        assert report["audit_after"]["healthy"] is True
        view = service.repository.specimen_detail(specimen["id"])
        assert view["available_quantity"] == 1 and view["held_quantity"] == 4
        # 补建的占用可以正常履行
        service.release.fulfill(request["id"], {"actor": "保管员"})
        assert service.repository.specimen_detail(specimen["id"])["consumed_quantity"] == 4


def test_orphan_allocation_is_detected_and_removed(client):
    """失败事务遗留的无主占用要能被核对发现、被修复清除。"""
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen = prepare_lot(service, "C3", quantity=5)

    # 用关闭外键的独立连接伪造一条无主占用
    rogue = __import__("sqlite3").connect(database_path())
    rogue.execute("PRAGMA foreign_keys=OFF")
    rogue.execute(
        "INSERT INTO specimen_allocations(specimen_id,release_item_id,request_id,quantity,status,created_by,created_at) "
        "VALUES(?,?,?,4,'held','rogue','2026-09-03T00:00:00+00:00')",
        (specimen["id"], 999999, 999999),
    )
    rogue.commit()
    rogue.close()

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        audit = service.release.audit_allocations()
        assert len(audit["orphan_allocations"]) == 1
        report = service.release.repair_allocations()
        assert len(report["orphans_removed"]) == 1
        assert service.release.audit_allocations()["healthy"] is True
        # 无主占用从未真实扣量，清除后账面保持 5
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 5

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.forensics.schemas import (
    ForensicCaseCreate,
    ForensicCasePatch,
    ForensicCaseTransition,
    AlertDecision,
    ObservationCreate,
    ReleaseCreate,
    ReleaseCancel,
    ReleaseDecision,
    ReleaseFulfill,
    HoldCreate,
    HoldRelease,
    LocationCreate,
    SpecimenCreate,
    MovePlacement,
    PlacementCreate,
    PolicyCreate,
    ProtocolCreate,
    ReadingCreate,
    AgencyCreate,
    ExaminationComplete,
    ExaminationCreate,
    ExaminationInvalidate,
    ExaminationStart,
    WithdrawalCreate,
)
from app.forensics.service import ForensicService


router = APIRouter(prefix="/api/forensics", tags=["鉴定案件"])


def _service() -> ForensicService:
    return ForensicService(get_connection())


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.read")
    return _service().dashboard()


@router.post("/agencies", status_code=201)
def create_agency(data: AgencyCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).forensic_cases.create_agency(data.model_dump(mode="json"))


@router.put("/agencies/{agency_id}/restrictions")
def update_agency_rules(
    agency_id: int,
    restrictions: dict[str, Any],
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("forensic_cases.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).forensic_cases.update_agency_rules(agency_id, restrictions)


@router.post("/cases", status_code=201)
def create_forensic_case(data: ForensicCaseCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).forensic_cases.create_forensic_case(data.model_dump(mode="json"))


@router.get("/cases")
def list_forensic_cases(
    status: str | None = None,
    crop: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("forensic_cases.read")
    items, total = _service().repository.list_forensic_cases(status=status, crop=crop, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/cases/{case_id}")
def forensic_case_detail(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.read")
    return _service().repository.forensic_case_detail(case_id)


@router.patch("/cases/{case_id}")
def update_forensic_case(
    case_id: int,
    data: ForensicCasePatch,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("forensic_cases.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).forensic_cases.update_forensic_case(
            case_id, data.model_dump(mode="json", exclude_unset=True)
        )


@router.post("/cases/{case_id}/transition")
def transition_forensic_case(
    case_id: int,
    data: ForensicCaseTransition,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("forensic_cases.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).forensic_cases.transition(case_id, data.model_dump(mode="json"))


@router.get("/cases/{case_id}/restrictions")
def forensic_case_restrictions(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.read")
    return _service().forensic_cases.restrictions_for(case_id)


@router.post("/locations", status_code=201)
def create_location(data: LocationCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.create_location(data.model_dump(mode="json"))


@router.get("/locations/{location_id}")
def location_detail(location_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.read")
    return _service().repository.location_detail(location_id)


@router.post("/locations/{location_id}/status/{status}")
def change_location_status(
    location_id: int,
    status: str,
    expected_version: int,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.change_location_status(location_id, status, expected_version)


@router.post("/specimens", status_code=201)
def create_specimen(data: SpecimenCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.create_specimen(data.model_dump(mode="json"))


@router.get("/specimens/{specimen_id}")
def specimen_detail(specimen_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.read")
    return _service().repository.specimen_detail(specimen_id)


@router.get("/specimens/{specimen_id}/reconcile")
def reconcile_lot(specimen_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.read")
    return _service().custody.reconcile(specimen_id)


@router.post("/placements", status_code=201)
def place_specimen(data: PlacementCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.place_specimen(data.model_dump(mode="json"))


@router.post("/placements/{placement_id}/move")
def move_placement(
    placement_id: int,
    data: MovePlacement,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.move_placement(placement_id, data.model_dump(mode="json"))


@router.post("/withdrawals", status_code=201)
def withdraw(data: WithdrawalCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.withdraw(data.model_dump(mode="json"))


@router.post("/holds", status_code=201)
def impose_hold(data: HoldCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.impose_hold(data.model_dump(mode="json"))


@router.post("/holds/{hold_id}/release")
def release_hold(
    hold_id: int,
    data: HoldRelease,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).custody.release_hold(hold_id, data.actor, data.reason)


@router.post("/protocols", status_code=201)
def create_protocol(data: ProtocolCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.create_protocol(data.model_dump(mode="json"))


@router.post("/examinations", status_code=201)
def schedule_examination(data: ExaminationCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.schedule_examination(data.model_dump(mode="json"))


@router.get("/examinations/{examination_id}")
def examination_detail(examination_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.read")
    return _service().repository.examination_detail(examination_id)


@router.post("/examinations/{examination_id}/start")
def start_examination(examination_id: int, data: ExaminationStart, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.start_examination(examination_id, data.model_dump(mode="json"))


@router.post("/examinations/{examination_id}/observations", status_code=201)
def add_observation(examination_id: int, data: ObservationCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.add_observation(examination_id, data.model_dump(mode="json"))


@router.put("/observations/{count_id}")
def replace_observation(count_id: int, data: ObservationCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.replace_observation(count_id, data.model_dump(mode="json"))


@router.post("/examinations/{examination_id}/complete")
def complete_examination(examination_id: int, data: ExaminationComplete, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("examination.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.complete_examination(examination_id, data.model_dump(mode="json"))


@router.post("/examinations/{examination_id}/invalidate")
def invalidate_examination(examination_id: int, data: ExaminationInvalidate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.invalidate_examination(examination_id, data.model_dump(mode="json"))


@router.post("/policies", status_code=201)
def create_policy(data: PolicyCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).examinations.create_policy(data.model_dump(mode="json"))


@router.get("/review-schedules/due")
def due_schedules(
    before: date,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
) -> list[dict]:
    principal.require("examination.read")
    return _service().examinations.due_schedules(before, limit)


@router.post("/readings", status_code=201)
def add_reading(data: ReadingCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).quality.add_reading(data.model_dump(mode="python"))


@router.post("/readings/import")
def import_readings(rows: list[ReadingCreate], principal: Principal = Depends(current_principal)) -> dict:
    principal.require("custody.write")
    payload = [item.model_dump(mode="python") for item in rows]
    with transaction(immediate=True) as connection:
        return ForensicService(connection).quality.import_readings(payload)


@router.get("/locations/{location_id}/environment")
def environment_summary(
    location_id: int,
    hours: int = Query(default=24, ge=1, le=24 * 90),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("custody.read")
    return _service().quality.location_summary(location_id, hours)


@router.get("/alerts")
def open_alerts(severity: str | None = None, principal: Principal = Depends(current_principal)) -> list[dict]:
    principal.require("quality.review")
    return _service().quality.open_alerts(severity)


@router.post("/alerts/{alert_id}/decision")
def decide_alert(alert_id: int, data: AlertDecision, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("quality.review")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).quality.decide_alert(alert_id, data.model_dump(mode="json"))


@router.post("/releases", status_code=201)
def create_release(data: ReleaseCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.read")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.create_request(data.model_dump(mode="json"))


@router.post("/releases/{request_id}/submit")
def submit_release(
    request_id: int,
    expected_version: int,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("forensic_cases.read")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.submit(request_id, expected_version)


@router.post("/releases/{request_id}/decision")
def decide_release(
    request_id: int,
    data: ReleaseDecision,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("release.approve")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.decide(request_id, data.model_dump(mode="json"))


@router.post("/releases/{request_id}/cancel")
def cancel_release(
    request_id: int,
    data: ReleaseCancel,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("release.approve")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.cancel(request_id, data.model_dump(mode="json"))


@router.post("/releases/{request_id}/fulfill")
def fulfill_release(
    request_id: int,
    data: ReleaseFulfill,
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("release.approve")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.fulfill(request_id, data.model_dump(mode="json"))


@router.get("/releases/doctor/check")
def release_doctor(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("release.approve")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.doctor()


@router.post("/releases/doctor/repair")
def release_repair(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("release.approve")
    with transaction(immediate=True) as connection:
        return ForensicService(connection).release.repair(actor=principal.display_name)


@router.get("/releases/{request_id}")
def release_detail(request_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("forensic_cases.read")
    return _service().repository.release_detail(request_id)

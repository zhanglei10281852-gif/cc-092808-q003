from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "forensics.db"
_local = threading.local()

SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id,permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id,role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    reagency_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_events(resource_type,reagency_id);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope,idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id,department_id,starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS submitting_agencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agency_code TEXT NOT NULL UNIQUE,
    agency_name TEXT NOT NULL,
    jurisdiction_code TEXT NOT NULL,
    contact_address TEXT NOT NULL DEFAULT '',
    licensed_on TEXT,
    accreditation_no TEXT,
    contact_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forensic_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT NOT NULL UNIQUE,
    case_name TEXT NOT NULL,
    discipline TEXT NOT NULL,
    entrusted_matter TEXT NOT NULL DEFAULT '',
    agency_id INTEGER REFERENCES submitting_agencies(id),
    case_source TEXT NOT NULL CHECK(case_source IN ('委托','移送','委派','指定','援助')),
    accepted_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','quarantine','accepted','restricted','retired')),
    return_reason TEXT NOT NULL DEFAULT '',
    case_profile_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forensic_cases_crop ON forensic_cases(discipline,status);
CREATE INDEX IF NOT EXISTS idx_forensic_cases_source ON forensic_cases(agency_id);
CREATE TABLE IF NOT EXISTS case_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES forensic_cases(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_events ON case_events(case_id,id);

CREATE TABLE IF NOT EXISTS storage_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_code TEXT NOT NULL UNIQUE,
    facility TEXT NOT NULL,
    room TEXT NOT NULL,
    rack TEXT NOT NULL,
    shelf TEXT NOT NULL,
    capacity_units REAL NOT NULL CHECK(capacity_units > 0),
    reference_value REAL NOT NULL,
    humidity_percent REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','closed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS specimens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_no TEXT NOT NULL UNIQUE,
    case_id INTEGER NOT NULL REFERENCES forensic_cases(id) ON DELETE RESTRICT,
    parent_specimen_id INTEGER REFERENCES specimens(id),
    received_year INTEGER NOT NULL CHECK(received_year BETWEEN 1800 AND 2200),
    initial_quantity REAL NOT NULL CHECK(initial_quantity > 0),
    available_quantity REAL NOT NULL CHECK(available_quantity >= 0),
    integrity_percent REAL CHECK(integrity_percent >= 0 AND integrity_percent <= 100),
    packaging TEXT NOT NULL DEFAULT '',
    sealed_on TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','stored','held','depleted','disposed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_forensic_case ON specimens(case_id,status);
CREATE TABLE IF NOT EXISTS specimen_placements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES storage_locations(id) ON DELETE RESTRICT,
    quantity REAL NOT NULL CHECK(quantity > 0),
    container_code TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    removed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(container_code,placed_at)
);
CREATE INDEX IF NOT EXISTS idx_placements_active ON specimen_placements(location_id,removed_at);
CREATE TABLE IF NOT EXISTS custody_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    placement_id INTEGER REFERENCES specimen_placements(id),
    movement_type TEXT NOT NULL CHECK(movement_type IN ('入库','移库','取样','领用','归还','报废','盘点调整')),
    quantity REAL NOT NULL,
    from_location_id INTEGER REFERENCES storage_locations(id),
    to_location_id INTEGER REFERENCES storage_locations(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_lot ON custody_events(specimen_id,id);
CREATE TABLE IF NOT EXISTS specimen_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    hold_type TEXT NOT NULL CHECK(hold_type IN ('保全','质量','权限','争议')),
    reason TEXT NOT NULL,
    imposed_by TEXT NOT NULL,
    imposed_at TEXT NOT NULL,
    released_by TEXT,
    released_at TEXT,
    release_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_active ON specimen_holds(specimen_id,released_at);

CREATE TABLE IF NOT EXISTS examination_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    discipline TEXT NOT NULL,
    observation_target INTEGER NOT NULL CHECK(observation_target > 0),
    checkpoint_count INTEGER NOT NULL CHECK(checkpoint_count > 0),
    reference_value REAL NOT NULL,
    turnaround_days INTEGER NOT NULL CHECK(turnaround_days > 0),
    conclusion_rule TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(protocol_code,version)
);
CREATE TABLE IF NOT EXISTS examinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    examination_no TEXT NOT NULL UNIQUE,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE RESTRICT,
    protocol_id INTEGER NOT NULL REFERENCES examination_protocols(id) ON DELETE RESTRICT,
    examination_type TEXT NOT NULL CHECK(examination_type IN ('受理初检','补充检验','异常复核')),
    sample_quantity REAL NOT NULL CHECK(sample_quantity > 0),
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','running','completed','invalidated','cancelled')),
    conformity_percent REAL,
    reliability_index REAL,
    invalid_reason TEXT,
    requested_by TEXT NOT NULL,
    performed_by TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tests_due ON examinations(status,scheduled_for);
CREATE INDEX IF NOT EXISTS idx_tests_lot ON examinations(specimen_id,created_at);
CREATE TABLE IF NOT EXISTS examination_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    examination_id INTEGER NOT NULL REFERENCES examinations(id) ON DELETE CASCADE,
    checkpoint_no INTEGER NOT NULL,
    items_checked INTEGER NOT NULL CHECK(items_checked > 0),
    conforming_count INTEGER NOT NULL CHECK(conforming_count >= 0),
    exception_count INTEGER NOT NULL CHECK(exception_count >= 0),
    unusable_count INTEGER NOT NULL CHECK(unusable_count >= 0),
    pending_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_count >= 0),
    sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
    observed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(examination_id,checkpoint_no,sequence_no)
);
CREATE TABLE IF NOT EXISTS review_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discipline TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    interval_months INTEGER NOT NULL CHECK(interval_months > 0),
    warning_days INTEGER NOT NULL CHECK(warning_days >= 0),
    minimum_conformity_percent REAL NOT NULL CHECK(minimum_conformity_percent BETWEEN 0 AND 100),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(discipline,risk_level,version)
);
CREATE TABLE IF NOT EXISTS review_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    source_examination_id INTEGER REFERENCES examinations(id),
    policy_id INTEGER NOT NULL REFERENCES review_policies(id) ON DELETE RESTRICT,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','notified','scheduled','superseded','waived')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(specimen_id,due_on)
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON review_schedules(status,due_on);

CREATE TABLE IF NOT EXISTS storage_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES storage_locations(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    reference_value REAL NOT NULL,
    humidity_percent REAL NOT NULL,
    source_key TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(location_id,source_key)
);
CREATE INDEX IF NOT EXISTS idx_readings_time ON storage_readings(location_id,observed_at);
CREATE TABLE IF NOT EXISTS quality_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL UNIQUE,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    specimen_id INTEGER REFERENCES specimens(id),
    location_id INTEGER REFERENCES storage_locations(id),
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','resolved','dismissed')),
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON quality_alerts(status,severity,created_at);

CREATE TABLE IF NOT EXISTS release_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT NOT NULL UNIQUE,
    requester TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','approved','rejected','fulfilled','cancelled')),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    decision_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS release_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES release_requests(id) ON DELETE CASCADE,
    case_id INTEGER NOT NULL REFERENCES forensic_cases(id) ON DELETE RESTRICT,
    quantity REAL NOT NULL CHECK(quantity > 0),
    allocated_specimen_id INTEGER REFERENCES specimens(id),
    status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','allocated','fulfilled','unavailable')),
    UNIQUE(request_id,case_id)
);
CREATE TABLE IF NOT EXISTS specimen_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id INTEGER NOT NULL REFERENCES specimens(id) ON DELETE RESTRICT,
    release_item_id INTEGER NOT NULL REFERENCES release_items(id) ON DELETE RESTRICT,
    request_id INTEGER NOT NULL REFERENCES release_requests(id) ON DELETE RESTRICT,
    quantity REAL NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','consumed','released')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    released_at TEXT,
    release_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(release_item_id)
);
CREATE INDEX IF NOT EXISTS idx_allocations_specimen ON specimen_allocations(specimen_id,status);
CREATE INDEX IF NOT EXISTS idx_allocations_request ON specimen_allocations(request_id,status);
CREATE TABLE IF NOT EXISTS outbox_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','published','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_by TEXT,
    locked_at TEXT,
    published_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_events(status,available_at,id);
'''

PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看部门", "departments", "read"),
    ("departments.write", "维护部门", "departments", "write"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
    ("forensic_cases.read", "查看鉴定材料", "forensic_cases", "read"),
    ("forensic_cases.write", "维护鉴定材料", "forensic_cases", "write"),
    ("custody.read", "查看库存", "custody", "read"),
    ("custody.write", "维护库存", "custody", "write"),
    ("examination.read", "查看检验记录", "examination", "read"),
    ("examination.write", "执行检验任务", "examination", "write"),
    ("quality.review", "复核质量结果", "quality", "review"),
    ("release.approve", "审批鉴定领用", "release", "approve"),
]


def database_path() -> Path:
    raw = os.getenv("FORENSICS_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    if connection.in_transaction:
        marker = f"nested_{id(object())}"
        connection.execute(f"SAVEPOINT {marker}")
        try:
            yield connection
            connection.execute(f"RELEASE SAVEPOINT {marker}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {marker}")
            connection.execute(f"RELEASE SAVEPOINT {marker}")
            raise
        return
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def init_db() -> None:
    timestamp = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = [
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("registrar", "检材登记员", "登记案件检材并维护保管信息"),
            ("technician", "鉴定技术员", "执行取样与专业检验"),
            ("curator", "案件审核员", "复核鉴定质量与领用"),
            ("auditor", "审计查看员", "只读查看业务和审计记录"),
        ]
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, timestamp, timestamp),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, timestamp),
        )
        role_permissions = {
            "registrar": ["forensic_cases.read", "forensic_cases.write", "custody.read", "custody.write"],
            "technician": ["forensic_cases.read", "custody.read", "examination.read", "examination.write"],
            "curator": ["forensic_cases.read", "custody.read", "examination.read", "quality.review", "release.approve"],
            "auditor": ["forensic_cases.read", "custody.read", "examination.read", "audit.read"],
        }
        for role_code, codes in role_permissions.items():
            role_id = connection.execute("SELECT id FROM roles WHERE code=?", (role_code,)).fetchone()[0]
            for permission_code in codes:
                permission_id = connection.execute("SELECT id FROM permissions WHERE code=?", (permission_code,)).fetchone()[0]
                connection.execute(
                    "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) VALUES(?,?,?)",
                    (role_id, permission_id, timestamp),
                )


def migrate_db() -> None:
    init_db()

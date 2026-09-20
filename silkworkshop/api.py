"""HTTP 路由：把 JSON 请求映射到领域函数。"""

from __future__ import annotations

import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from . import domain
from .errors import NotFound, ValidationError


def _idem_key(headers, body):
    key = _header(headers, "Idempotency-Key")
    return key or body.get("idempotency_key")


def _header(headers, name):
    if headers is None:
        return None
    value = headers.get(name) if hasattr(headers, "get") else None
    if value:
        return value
    items = headers.items() if hasattr(headers, "items") else []
    for key, val in items:
        if key.lower() == name.lower():
            return val
    return None


def _query(req, name):
    values = req.query.get(name)
    return values[0] if values else None


# ---------------------------------------------------------------- 各端点


def create_batch(req):
    body = req.body
    data = domain.register_batch(
        name=body.get("name"),
        source_order=body.get("source_order"),
        designer_id=body.get("designer_id"),
        silk_type=body.get("silk_type"),
        total_area_cm2=body.get("total_area_cm2", 0),
        dye_notes=body.get("dye_notes", ""),
        cleaning_notes=body.get("cleaning_notes", ""),
        reuse_value_per_cm2_pence=body.get("reuse_value_per_cm2_pence", 0),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def get_batch(req):
    return 200, domain.get_batch(req.id)


def split_piece(req):
    body = req.body
    return 200, domain.split_piece(
        req.id,
        parts=body.get("parts", []),
        actor=body.get("actor", "system"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def get_piece(req):
    return 200, domain.get_piece(req.id)


def return_to_workshop(req):
    body = req.body
    return 200, domain.return_to_workshop(
        piece_ids=body.get("piece_ids", []),
        actor=body.get("actor", "system"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def convert_to_display(req):
    body = req.body
    return 200, domain.convert_to_display(
        piece_ids=body.get("piece_ids", []),
        actor=body.get("actor", "system"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def create_plan(req):
    body = req.body
    data = domain.create_plan(
        title=body.get("title"),
        craft_type=body.get("craft_type"),
        material_per_participant_cm2=body.get("material_per_participant_cm2", 0),
        briefing=body.get("briefing", ""),
        required_qualifications=body.get("required_qualifications", []),
        fee_pence=body.get("fee_pence", 0),
        instructor_share_percent=body.get("instructor_share_percent", 0),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def update_briefing(req):
    body = req.body
    return 200, domain.update_briefing(
        req.id,
        briefing=body.get("briefing", ""),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )


def register_instructor(req):
    body = req.body
    data = domain.register_instructor(
        name=body.get("name"),
        qualifications=body.get("qualifications", []),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def record_payout(req):
    body = req.body
    return 200, domain.record_payout(
        req.id,
        amount_pence=body.get("amount_pence", 0),
        actor=body.get("actor", "system"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def register_participant(req):
    body = req.body
    data = domain.register_participant(
        name=body.get("name"),
        is_minor=body.get("is_minor", False),
        guardian_name=body.get("guardian_name", ""),
        guardian_contact=body.get("guardian_contact", ""),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def schedule_session(req):
    body = req.body
    data = domain.schedule_session(
        plan_id=body.get("plan_id"),
        instructor_id=body.get("instructor_id"),
        starts_at=body.get("starts_at"),
        capacity=body.get("capacity", 0),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def get_session(req):
    return 200, domain.get_session(req.id)


def reserve_pieces(req):
    body = req.body
    return 200, domain.reserve_pieces(
        req.id,
        piece_ids=body.get("piece_ids", []),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )


def enroll(req):
    body = req.body
    return 200, domain.enroll(
        req.id,
        participant=body.get("participant", {}),
        choices=body.get("choices"),
        fee_pence=body.get("fee_pence"),
        actor=body.get("actor", "system"),
        idempotency_key=_idem_key(req.headers, body),
    )


def issue_materials(req):
    body = req.body
    return 200, domain.issue_materials(
        req.id,
        actor=body.get("actor", "system"),
        piece_ids=body.get("piece_ids"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def record_work(req):
    body = req.body
    return 200, domain.record_work(
        req.id,
        participant_id=body.get("participant_id"),
        piece_usages=body.get("piece_usages", []),
        actor=body.get("actor", "system"),
        craft_type=body.get("craft_type"),
        title=body.get("title", ""),
        offline_ref=body.get("offline_ref"),
        idempotency_key=_idem_key(req.headers, body),
    )


def reschedule_session(req):
    body = req.body
    return 200, domain.reschedule_session(
        req.id,
        new_starts_at=body.get("new_starts_at"),
        actor=body.get("actor", "system"),
        reason=body.get("reason", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def reassign_instructor(req):
    body = req.body
    return 200, domain.reassign_instructor(
        req.id,
        new_instructor_id=body.get("new_instructor_id"),
        actor=body.get("actor", "system"),
        reason=body.get("reason", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def cancel_session(req):
    body = req.body
    return 200, domain.cancel_session(
        req.id,
        actor=body.get("actor", "system"),
        takeover=body.get("takeover"),
        dispositions=body.get("dispositions"),
        reason=body.get("reason", ""),
        idempotency_key=_idem_key(req.headers, body),
    )


def complete_session(req):
    body = req.body
    return 200, domain.complete_session(
        req.id, actor=body.get("actor", "system"), idempotency_key=_idem_key(req.headers, body)
    )


def settle_session(req):
    body = req.body
    return 200, domain.settle_session(
        req.id, actor=body.get("actor", "system"), idempotency_key=_idem_key(req.headers, body)
    )


def get_settlement(req):
    return 200, domain.get_settlement(req.id)


def grant_consent(req):
    body = req.body
    data = domain.grant_consent(
        subject_type=body.get("subject_type"),
        subject_id=body.get("subject_id"),
        category=body.get("category"),
        purpose=body.get("purpose"),
        granted_by=body.get("granted_by"),
        granted_by_role=body.get("granted_by_role", "self"),
        expires_at=body.get("expires_at"),
        note=body.get("note", ""),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def get_consent(req):
    return 200, domain.get_consent(req.id)


def withdraw_consent(req):
    body = req.body
    return 200, domain.withdraw_consent(
        req.id, actor=body.get("actor", "system"), idempotency_key=_idem_key(req.headers, body)
    )


def publish(req):
    body = req.body
    data = domain.publish(
        subject_type=body.get("subject_type"),
        subject_id=body.get("subject_id"),
        category=body.get("category"),
        purpose=body.get("purpose"),
        channel=body.get("channel", "in_store"),
        ref=body.get("ref", ""),
        at=body.get("at"),
        idempotency_key=_idem_key(req.headers, body),
    )
    return 201, data


def trace_work(req):
    return 200, domain.trace_work(req.id)


def reuse_report(req):
    return 200, domain.reuse_report(designer_id=_query(req, "designer_id"))


def instructor_pay_report(req):
    instructor_id = _query(req, "instructor_id")
    if not instructor_id:
        raise ValidationError("缺少查询参数 instructor_id")
    return 200, domain.instructor_pay_report(instructor_id)


ROUTES = [
    ("POST", re.compile(r"^/batches$"), create_batch),
    ("GET", re.compile(r"^/batches/(?P<id>[^/]+)$"), get_batch),
    ("POST", re.compile(r"^/pieces/(?P<id>[^/]+)/split$"), split_piece),
    ("GET", re.compile(r"^/pieces/(?P<id>[^/]+)$"), get_piece),
    ("POST", re.compile(r"^/pieces/return-to-workshop$"), return_to_workshop),
    ("POST", re.compile(r"^/pieces/convert-to-display$"), convert_to_display),
    ("POST", re.compile(r"^/plans$"), create_plan),
    ("POST", re.compile(r"^/plans/(?P<id>[^/]+)/briefing$"), update_briefing),
    ("POST", re.compile(r"^/instructors$"), register_instructor),
    ("POST", re.compile(r"^/instructors/(?P<id>[^/]+)/payouts$"), record_payout),
    ("POST", re.compile(r"^/participants$"), register_participant),
    ("POST", re.compile(r"^/sessions$"), schedule_session),
    ("GET", re.compile(r"^/sessions/(?P<id>[^/]+)$"), get_session),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/reserve$"), reserve_pieces),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/enroll$"), enroll),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/issue$"), issue_materials),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/works$"), record_work),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/reschedule$"), reschedule_session),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/reassign-instructor$"), reassign_instructor),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/cancel$"), cancel_session),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/complete$"), complete_session),
    ("POST", re.compile(r"^/sessions/(?P<id>[^/]+)/settle$"), settle_session),
    ("GET", re.compile(r"^/sessions/(?P<id>[^/]+)/settlement$"), get_settlement),
    ("POST", re.compile(r"^/consents$"), grant_consent),
    ("GET", re.compile(r"^/consents/(?P<id>[^/]+)$"), get_consent),
    ("POST", re.compile(r"^/consents/(?P<id>[^/]+)/withdraw$"), withdraw_consent),
    ("POST", re.compile(r"^/publications$"), publish),
    ("GET", re.compile(r"^/works/(?P<id>[^/]+)/trace$"), trace_work),
    ("GET", re.compile(r"^/reports/reuse$"), reuse_report),
    ("GET", re.compile(r"^/reports/instructor-pay$"), instructor_pay_report),
]


def handle(method, raw_path, body, headers):
    """路由入口，返回 (status, payload)。"""
    parsed = urlsplit(raw_path)
    query = parse_qs(parsed.query)
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        match = pattern.match(parsed.path)
        if match:
            request = SimpleNamespace(body=body or {}, query=query, headers=headers, **match.groupdict())
            status, data = handler(request)
            return status, {"data": data}
    raise NotFound(f"未知路由 {method} {parsed.path}")

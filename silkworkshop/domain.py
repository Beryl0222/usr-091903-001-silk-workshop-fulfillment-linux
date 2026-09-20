"""真丝体验履约的领域逻辑：材料、场次、授权与费用。

设计要点：
- 裁片自面料批次拆出后形成树，所有状态流转写入追加式台账，
  拆分、退回工坊、改作展示品都不会丢失来源。
- 所有写操作支持幂等键：断网补录领料与成品时，重复提交不会重复扣减。
- 场次保留"原承诺"快照；改期只改排期，并重新核算材料是否够用。
- 授权按主体、类别、用途、期限分别取得；撤回后立即阻止新的展示。
"""

from __future__ import annotations

import functools
from datetime import datetime, timezone

from .errors import Conflict, ConsentError, NotFound, ValidationError
from .models import (
    CONSENT_CATEGORIES,
    ActivityPlan,
    Consent,
    Enrollment,
    FabricBatch,
    FeeEntry,
    Handover,
    Instructor,
    MaterialPiece,
    Movement,
    Participant,
    Publication,
    Session,
    Work,
)
from .store import STORE

EPS = 1e-6


# ---------------------------------------------------------------- 基础工具


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value, field_name):
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{field_name} 不是有效的 ISO 时间: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _require(value, message):
    if value is None or value == "":
        raise ValidationError(message)
    return value


def _fingerprint(fn_name, args, kwargs):
    parts = [repr(a) for a in args]
    parts.extend(f"{k}={kwargs[k]!r}" for k in sorted(kwargs))
    return f"{fn_name}({', '.join(parts)})"


def idempotent(fn):
    """写操作幂等包装：同一幂等键重复提交直接返回首次结果，不重复扣减。

    仅当函数成功返回时才记录结果；抛错的请求可安全重试。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = kwargs.pop("idempotency_key", None)
        with STORE.lock:
            if not key:
                return fn(*args, **kwargs)
            fingerprint = _fingerprint(fn.__name__, args, kwargs)
            record = STORE.idempotency.get(key)
            if record is not None:
                if record["fingerprint"] != fingerprint:
                    raise Conflict(f"幂等键 {key} 已绑定其他请求，不能复用")
                result, replayed = record["result"], True
            else:
                result = fn(*args, **kwargs)
                STORE.idempotency[key] = {"fingerprint": fingerprint, "result": result}
                replayed = False
            if isinstance(result, dict):
                return {**result, "replayed": bool(result.get("replayed")) or replayed}
            return result

    return wrapper


def _movement(piece, kind, from_status, to_status, actor, session_id=None, work_id=None, note="", area=None):
    entry = Movement(
        id=STORE.next_id("movement"),
        seq=len(STORE.movements) + 1,
        piece_id=piece.id,
        kind=kind,
        from_status=from_status,
        to_status=to_status,
        area_cm2=piece.area_cm2 if area is None else area,
        actor=actor,
        session_id=session_id,
        work_id=work_id,
        note=note,
        created_at=_now(),
    )
    STORE.movements.append(entry)
    return entry


def _batch(batch_id):
    batch = STORE.batches.get(batch_id)
    if batch is None:
        raise NotFound(f"面料批次不存在: {batch_id}")
    return batch


def _piece(piece_id):
    piece = STORE.pieces.get(piece_id)
    if piece is None:
        raise NotFound(f"裁片不存在: {piece_id}")
    return piece


def _plan(plan_id):
    plan = STORE.plans.get(plan_id)
    if plan is None:
        raise NotFound(f"活动方案不存在: {plan_id}")
    return plan


def _instructor(instructor_id):
    instructor = STORE.instructors.get(instructor_id)
    if instructor is None:
        raise NotFound(f"讲师不存在: {instructor_id}")
    return instructor


def _session(session_id):
    session = STORE.sessions.get(session_id)
    if session is None:
        raise NotFound(f"场次不存在: {session_id}")
    return session


def _ensure_session_open(session, allow_completed=False):
    allowed = ("scheduled", "rescheduled") + (("completed",) if allow_completed else ())
    if session.status not in allowed:
        raise Conflict(f"场次 {session.id} 当前状态 {session.status}，不能执行该操作")


def _check_qualifications(instructor, plan):
    if not instructor.active:
        raise Conflict(f"讲师 {instructor.id} 已停用")
    missing = [q for q in plan.required_qualifications if q not in instructor.qualifications]
    if missing:
        raise Conflict(f"讲师 {instructor.name} 缺少资质: {', '.join(missing)}")


def _new_piece(batch_id, parent_id, label, area_cm2, status="stock", location=None, holder=None):
    piece = MaterialPiece(
        id=STORE.next_id("piece"),
        batch_id=batch_id,
        parent_id=parent_id,
        label=label,
        area_cm2=round(float(area_cm2), 2),
        status=status,
        location=location,
        holder=holder,
        created_at=_now(),
    )
    STORE.pieces[piece.id] = piece
    return piece


def _piece_chain(piece):
    """从裁片一路向上回到批次根，形成来源链。"""
    chain = []
    current = piece
    while current is not None:
        chain.append(
            {
                "piece_id": current.id,
                "label": current.label,
                "area_cm2": current.area_cm2,
                "status": current.status,
            }
        )
        current = STORE.pieces.get(current.parent_id) if current.parent_id else None
    return chain


# ---------------------------------------------------------------- 面料批次与裁片


@idempotent
def register_batch(
    name,
    source_order,
    designer_id,
    silk_type,
    total_area_cm2,
    dye_notes="",
    cleaning_notes="",
    reuse_value_per_cm2_pence=0,
    actor="system",
):
    """登记裁衣剩余真丝批次，并生成整批根裁片。"""
    _require(name, "批次名称不能为空")
    _require(source_order, "来源裁衣单号不能为空")
    _require(designer_id, "设计师不能为空")
    area = float(total_area_cm2)
    if area <= 0:
        raise ValidationError("面料面积必须为正数")
    batch = FabricBatch(
        id=STORE.next_id("batch"),
        name=name,
        source_order=source_order,
        designer_id=designer_id,
        silk_type=silk_type,
        total_area_cm2=round(area, 2),
        dye_notes=dye_notes,
        cleaning_notes=cleaning_notes,
        reuse_value_per_cm2_pence=int(reuse_value_per_cm2_pence),
        created_at=_now(),
    )
    STORE.batches[batch.id] = batch
    root = _new_piece(batch.id, None, f"{name}-整批", area)
    _movement(root, "register", None, "stock", actor, note=f"来自裁衣单 {source_order}")
    return {"batch": batch.to_dict(), "root_piece": root.to_dict()}


@idempotent
def split_piece(piece_id, parts, actor, note=""):
    """把在库裁片拆成子片；不足原面积时自动生成余料子片，面积守恒。"""
    piece = _piece(piece_id)
    if piece.status != "stock":
        raise Conflict(f"裁片 {piece_id} 当前状态 {piece.status}，仅在库裁片可拆分")
    if not parts:
        raise ValidationError("拆分至少需要一个目标尺寸")
    cleaned = []
    for index, part in enumerate(parts, start=1):
        area = round(float(part.get("area_cm2", 0)), 2)
        if area <= 0:
            raise ValidationError(f"第 {index} 片面积必须为正数")
        cleaned.append((part.get("label") or f"{piece.label}-{index}", area))
    total = round(sum(area for _, area in cleaned), 2)
    if total > piece.area_cm2 + EPS:
        raise ValidationError(f"拆分面积 {total}cm² 超出原裁片 {piece.area_cm2}cm²")
    children = []
    for label, area in cleaned:
        children.append(_new_piece(piece.batch_id, piece.id, label, area))
    remainder = round(piece.area_cm2 - total, 2)
    if remainder > EPS:
        children.append(_new_piece(piece.batch_id, piece.id, f"{piece.label}-余料", remainder))
    piece.status = "split"
    _movement(piece, "split", "stock", "split", actor, note=note or f"拆分为 {len(children)} 片")
    for child in children:
        _movement(child, "split_out", None, "stock", actor, note=f"来自 {piece.id}")
    return {"piece": piece.to_dict(), "children": [c.to_dict() for c in children]}


@idempotent
def return_to_workshop(piece_ids, actor, note=""):
    """退回工坊：在库/已预留/已领用的裁片退回，来源链保留。"""
    returned = []
    for piece_id in piece_ids:
        piece = _piece(piece_id)
        if piece.status not in ("stock", "reserved", "issued"):
            raise Conflict(f"裁片 {piece_id} 状态 {piece.status}，不能退回工坊")
        old = piece.status
        session_id = piece.location
        piece.status = "returned"
        piece.location = None
        piece.holder = actor
        _movement(piece, "return_workshop", old, "returned", actor, session_id=session_id, note=note)
        returned.append(piece.to_dict())
    return {"returned": returned}


@idempotent
def convert_to_display(piece_ids, actor, note=""):
    """改作展示品：来源链保留，状态变为展示品。"""
    converted = []
    for piece_id in piece_ids:
        piece = _piece(piece_id)
        if piece.status not in ("stock", "reserved", "issued"):
            raise Conflict(f"裁片 {piece_id} 状态 {piece.status}，不能改作展示品")
        old = piece.status
        session_id = piece.location
        piece.status = "display"
        piece.location = None
        piece.holder = actor
        _movement(piece, "display", old, "display", actor, session_id=session_id, note=note)
        converted.append(piece.to_dict())
    return {"display": converted}


# ---------------------------------------------------------------- 方案与讲师


@idempotent
def create_plan(
    title,
    craft_type,
    material_per_participant_cm2,
    briefing,
    required_qualifications,
    fee_pence,
    instructor_share_percent,
    actor="system",
):
    _require(title, "方案名称不能为空")
    material = float(material_per_participant_cm2)
    if material <= 0:
        raise ValidationError("人均用料必须为正数")
    if int(fee_pence) < 0:
        raise ValidationError("报名费不能为负")
    if not 0 <= int(instructor_share_percent) <= 100:
        raise ValidationError("讲师分成比例须在 0-100 之间")
    plan = ActivityPlan(
        id=STORE.next_id("plan"),
        title=title,
        craft_type=craft_type,
        material_per_participant_cm2=round(material, 2),
        briefing=briefing,
        briefing_version=1,
        required_qualifications=list(required_qualifications or []),
        fee_pence=int(fee_pence),
        instructor_share_percent=int(instructor_share_percent),
        created_at=_now(),
    )
    STORE.plans[plan.id] = plan
    return {"plan": plan.to_dict()}


@idempotent
def update_briefing(plan_id, briefing, actor="system"):
    """更新工艺讲解：版本号递增，已排期场次的原承诺快照不受影响。"""
    plan = _plan(plan_id)
    plan.briefing = briefing
    plan.briefing_version += 1
    return {"plan": plan.to_dict()}


@idempotent
def register_instructor(name, qualifications, actor="system"):
    _require(name, "讲师姓名不能为空")
    instructor = Instructor(
        id=STORE.next_id("instructor"),
        name=name,
        qualifications=list(qualifications or []),
        created_at=_now(),
    )
    STORE.instructors[instructor.id] = instructor
    return {"instructor": instructor.to_dict()}


@idempotent
def register_participant(name, is_minor=False, guardian_name="", guardian_contact="", actor="system"):
    _require(name, "参与者姓名不能为空")
    if is_minor and not guardian_name:
        raise ValidationError("未成年人须登记监护人")
    participant = Participant(
        id=STORE.next_id("participant"),
        name=name,
        is_minor=bool(is_minor),
        guardian_name=guardian_name,
        guardian_contact=guardian_contact,
        created_at=_now(),
    )
    STORE.participants[participant.id] = participant
    return {"participant": participant.to_dict()}


# ---------------------------------------------------------------- 场次履约


@idempotent
def schedule_session(plan_id, instructor_id, starts_at, capacity, actor="system"):
    plan = _plan(plan_id)
    instructor = _instructor(instructor_id)
    _check_qualifications(instructor, plan)
    _parse_iso(starts_at, "starts_at")
    if int(capacity) <= 0:
        raise ValidationError("场次容量必须为正数")
    session = Session(
        id=STORE.next_id("session"),
        plan_id=plan_id,
        instructor_id=instructor_id,
        starts_at=starts_at,
        capacity=int(capacity),
        commitments={
            "plan_id": plan_id,
            "instructor_id": instructor_id,
            "starts_at": starts_at,
            "fee_pence": plan.fee_pence,
            "material_per_participant_cm2": plan.material_per_participant_cm2,
            "briefing_version": plan.briefing_version,
            "capacity": int(capacity),
            "note": "原承诺快照：排期、讲师、费用与用料标准以此为准，改期不修改本快照",
        },
        history=[{"event": "scheduled", "starts_at": starts_at, "actor": actor, "at": _now()}],
        created_at=_now(),
    )
    STORE.sessions[session.id] = session
    return {"session": _session_dict(session)}


def _reserve_piece(session, piece, actor):
    old = piece.status
    piece.status = "reserved"
    piece.location = session.id
    piece.holder = actor
    if piece.id not in session.reservations:
        session.reservations.append(piece.id)
    _movement(piece, "reserve", old, "reserved", actor, session_id=session.id)


@idempotent
def reserve_pieces(session_id, piece_ids, actor):
    """为场次预留裁片；同一片重复预留天然幂等。"""
    session = _session(session_id)
    _ensure_session_open(session)
    reserved = []
    for piece_id in piece_ids:
        piece = _piece(piece_id)
        if piece.status == "reserved" and piece.location == session_id:
            continue
        if piece.status != "stock":
            raise Conflict(f"裁片 {piece_id} 状态 {piece.status}，不可预留")
        _reserve_piece(session, piece, actor)
        reserved.append(piece.to_dict())
    return {"session_id": session_id, "reserved": reserved, "sufficiency": _sufficiency(session)}


@idempotent
def enroll(session_id, participant, choices=None, fee_pence=None, actor="system"):
    """报名：记录参与者选择；报名费默认取原承诺价格；指定裁片自动预留。"""
    session = _session(session_id)
    _ensure_session_open(session)
    active = [e for e in STORE.enrollments.values() if e.session_id == session_id and e.status == "active"]
    if len(active) >= session.capacity:
        raise Conflict("场次已满")
    participant = dict(participant or {})
    if participant.get("id"):
        participant_obj = STORE.participants.get(participant["id"])
        if participant_obj is None:
            raise NotFound(f"参与者不存在: {participant['id']}")
    else:
        participant_obj = register_participant(
            name=participant.get("name", ""),
            is_minor=participant.get("is_minor", False),
            guardian_name=participant.get("guardian_name", ""),
            guardian_contact=participant.get("guardian_contact", ""),
            actor=actor,
        )["participant"]
        participant_obj = STORE.participants[participant_obj["id"]]
    for existing in active:
        if existing.participant_id == participant_obj.id:
            raise Conflict(f"参与者 {participant_obj.id} 已报名本场次")
    choices = dict(choices or {})
    reserved_piece_ids = []
    for piece_id in choices.get("piece_ids", []):
        piece = _piece(piece_id)
        if piece.status == "stock":
            _reserve_piece(session, piece, actor)
            reserved_piece_ids.append(piece_id)
        elif piece.status == "reserved" and piece.location == session_id:
            reserved_piece_ids.append(piece_id)
        else:
            raise Conflict(f"参与者指定的裁片 {piece_id} 不可用（状态 {piece.status}）")
    fee = session.commitments.get("fee_pence", 0) if fee_pence is None else int(fee_pence)
    enrollment = Enrollment(
        id=STORE.next_id("enrollment"),
        session_id=session_id,
        participant_id=participant_obj.id,
        choices=choices,
        fee_pence=fee,
        created_at=_now(),
    )
    STORE.enrollments[enrollment.id] = enrollment
    STORE.fees.append(
        FeeEntry(
            id=STORE.next_id("fee"),
            session_id=session_id,
            kind="participant_fee",
            amount_pence=fee,
            enrollment_id=enrollment.id,
            note="报名费（按原承诺价格）",
            created_at=_now(),
        )
    )
    return {
        "enrollment": enrollment.to_dict(),
        "participant": participant_obj.to_dict(),
        "reserved_piece_ids": reserved_piece_ids,
        "sufficiency": _sufficiency(session),
    }


@idempotent
def issue_materials(session_id, actor, piece_ids=None, note=""):
    """领料：把本场预留裁片发给讲师。断网补录凭幂等键不会重复扣减。"""
    session = _session(session_id)
    _ensure_session_open(session)
    if piece_ids is None:
        ids = [
            pid
            for pid in session.reservations
            if STORE.pieces[pid].status == "reserved" and STORE.pieces[pid].location == session_id
        ]
    else:
        ids = list(piece_ids)
    if not ids:
        raise ValidationError("没有可领用的预留裁片")
    issued = []
    for piece_id in ids:
        piece = _piece(piece_id)
        if piece.status != "reserved" or piece.location != session_id:
            raise Conflict(f"裁片 {piece_id} 未预留给本场次（状态 {piece.status}），不能领用")
        piece.status = "issued"
        piece.holder = session.instructor_id
        _movement(piece, "issue", "reserved", "issued", actor, session_id=session_id, note=note)
        issued.append(piece.to_dict())
    return {"session_id": session_id, "issued": issued}


@idempotent
def record_work(session_id, participant_id, piece_usages, actor, craft_type=None, title="", offline_ref=None):
    """登记体验作品并扣减用料。

    - 幂等键或 (session_id, offline_ref) 命中时直接返回首次结果，不重复扣减；
    - 部分用料自动拆出消耗子片与余料子片，余料留在场次继续可用。
    """
    session = _session(session_id)
    _ensure_session_open(session, allow_completed=True)  # 允许完成后补录
    enrollment = next(
        (
            e
            for e in STORE.enrollments.values()
            if e.session_id == session_id and e.participant_id == participant_id and e.status == "active"
        ),
        None,
    )
    if enrollment is None:
        raise NotFound(f"参与者 {participant_id} 未报名场次 {session_id}")
    if offline_ref:
        existing = STORE.works_by_offline_ref.get((session_id, offline_ref))
        if existing is not None:
            return {"work": existing.to_dict(), "replayed": True}
    if not piece_usages:
        raise ValidationError("作品至少登记一片用料")
    plan = _plan(session.plan_id)
    staged = []
    for usage in piece_usages:
        piece = _piece(usage.get("piece_id", ""))
        if piece.status != "issued" or piece.location != session_id:
            raise Conflict(f"裁片 {piece.id} 未发放到本场次，不能登记消耗")
        area = usage.get("area_cm2")
        area = piece.area_cm2 if area is None else round(float(area), 2)
        if area <= 0 or area > piece.area_cm2 + EPS:
            raise ValidationError(f"裁片 {piece.id} 用量 {area}cm² 不合法")
        staged.append((piece, area))
    work = Work(
        id=STORE.next_id("work"),
        session_id=session_id,
        participant_id=participant_id,
        craft_type=craft_type or plan.craft_type,
        title=title,
        piece_usages=[],
        offline_ref=offline_ref,
        created_by=actor,
        created_at=_now(),
    )
    for piece, area in staged:
        target = piece
        if area < piece.area_cm2 - EPS:
            # 部分消耗：拆出消耗子片与余料子片，余料仍属本场次
            used = _new_piece(piece.batch_id, piece.id, f"{piece.label}-作品用料", area,
                              status="issued", location=session_id, holder=piece.holder)
            rest = _new_piece(piece.batch_id, piece.id, f"{piece.label}-余料",
                              round(piece.area_cm2 - area, 2),
                              status="issued", location=session_id, holder=piece.holder)
            piece.status = "split"
            _movement(piece, "split", "issued", "split", actor, session_id=session_id, note="作品部分用料拆分")
            _movement(used, "split_out", None, "issued", actor, session_id=session_id, note=f"来自 {piece.id}")
            _movement(rest, "split_out", None, "issued", actor, session_id=session_id, note=f"来自 {piece.id}")
            target = used
        target.status = "consumed"
        target.holder = actor
        target.work_id = work.id
        _movement(target, "consume", "issued", "consumed", actor, session_id=session_id,
                  work_id=work.id, note=f"作品 {work.id} 用料")
        work.piece_usages.append({"piece_id": target.id, "area_cm2": area, "batch_id": target.batch_id})
    STORE.works[work.id] = work
    if offline_ref:
        STORE.works_by_offline_ref[(session_id, offline_ref)] = work
    return {"work": work.to_dict()}


# ---------------------------------------------------------------- 改期 / 换老师 / 取消


def _sufficiency(session):
    """重新核算材料是否够用：只看当前仍归本场次的预留/领用裁片。

    已完成作品的参与者不再占用未来需求；被退回、改作展示或转走的预留
    会列入 broken_reservations，并给出在库替代建议（优先同批次）。
    """
    plan = STORE.plans[session.plan_id]
    done_participants = {w.participant_id for w in STORE.works.values() if w.session_id == session.id}
    remaining = [
        e
        for e in STORE.enrollments.values()
        if e.session_id == session.id and e.status == "active" and e.participant_id not in done_participants
    ]
    required = round(plan.material_per_participant_cm2 * len(remaining), 2)
    held = [
        p
        for p in STORE.pieces.values()
        if p.location == session.id and p.status in ("reserved", "issued")
    ]
    available = round(sum(p.area_cm2 for p in held), 2)
    broken = []
    for piece_id in session.reservations:
        piece = STORE.pieces[piece_id]
        if piece.location == session.id and piece.status in ("reserved", "issued", "consumed", "split"):
            continue
        broken.append({"piece_id": piece_id, "status": piece.status})
    shortfall = round(max(0.0, required - available), 2)
    suggestions = []
    if shortfall > EPS:
        batch_ids = {STORE.pieces[pid].batch_id for pid in session.reservations if pid in STORE.pieces}
        candidates = [p for p in STORE.pieces.values() if p.status == "stock"]
        candidates.sort(key=lambda p: (p.batch_id not in batch_ids, -p.area_cm2))
        covered = 0.0
        for candidate in candidates:
            if covered >= shortfall - EPS:
                break
            suggestions.append(candidate.to_dict())
            covered += candidate.area_cm2
    return {
        "required_cm2": required,
        "available_cm2": available,
        "remaining_participants": len(remaining),
        "sufficient": available + EPS >= required,
        "shortfall_cm2": shortfall,
        "broken_reservations": broken,
        "suggestions": suggestions,
    }


@idempotent
def reschedule_session(session_id, new_starts_at, actor, reason=""):
    """改期：保留原承诺快照，只改排期，并重新核算材料是否够用。"""
    session = _session(session_id)
    _ensure_session_open(session)
    _parse_iso(new_starts_at, "new_starts_at")
    old = session.starts_at
    session.starts_at = new_starts_at
    session.status = "rescheduled"
    session.history.append(
        {"event": "rescheduled", "from": old, "to": new_starts_at, "actor": actor, "reason": reason, "at": _now()}
    )
    sufficiency = _sufficiency(session)
    session.last_sufficiency = sufficiency
    return {"session": _session_dict(session), "sufficiency": sufficiency, "commitments": session.commitments}


def _consent_belongs_to_session(consent, session_id):
    if consent.subject_type == "participant":
        return any(
            e.session_id == session_id and e.participant_id == consent.subject_id and e.status == "active"
            for e in STORE.enrollments.values()
        )
    if consent.subject_type == "work":
        work = STORE.works.get(consent.subject_id)
        return work is not None and work.session_id == session_id
    return False


@idempotent
def reassign_instructor(session_id, new_instructor_id, actor, reason=""):
    """临时换老师：校验资质，生成交接单（材料、讲解版本、待跟进授权）。"""
    session = _session(session_id)
    _ensure_session_open(session)
    plan = _plan(session.plan_id)
    new_instructor = _instructor(new_instructor_id)
    _check_qualifications(new_instructor, plan)
    old_instructor_id = session.instructor_id
    if old_instructor_id == new_instructor_id:
        raise ValidationError("新旧讲师相同，无需更换")
    materials = [
        p.to_dict()
        for p in STORE.pieces.values()
        if p.location == session_id and p.status in ("reserved", "issued")
    ]
    consent_ids = [
        c.id for c in STORE.consents.values() if c.status == "active" and _consent_belongs_to_session(c, session_id)
    ]
    handover = Handover(
        id=STORE.next_id("handover"),
        session_id=session_id,
        from_instructor_id=old_instructor_id,
        to_instructor_id=new_instructor_id,
        materials=materials,
        briefing_version=plan.briefing_version,
        consent_ids=consent_ids,
        reason=reason,
        actor=actor,
        created_at=_now(),
    )
    STORE.handovers.append(handover)
    session.instructor_id = new_instructor_id
    for piece in STORE.pieces.values():
        if piece.location == session_id and piece.status == "issued":
            piece.holder = new_instructor_id
            _movement(piece, "handover", "issued", "issued", actor,
                      session_id=session_id, note=f"交接给 {new_instructor_id}")
    session.history.append(
        {
            "event": "instructor_reassigned",
            "from": old_instructor_id,
            "to": new_instructor_id,
            "actor": actor,
            "reason": reason,
            "at": _now(),
        }
    )
    return {"session": _session_dict(session), "handover": handover.to_dict()}


@idempotent
def cancel_session(session_id, actor, takeover, dispositions=None, reason=""):
    """取消场次：必须说明材料/讲解/授权由谁接手，裁片按处置方案流转。"""
    session = _session(session_id)
    _ensure_session_open(session)
    takeover = dict(takeover or {})
    if not takeover.get("materials_to"):
        raise ValidationError("取消场次须说明材料接手人 takeover.materials_to")
    dispositions = dict(dispositions or {})
    results = []
    holdings = [
        p for p in STORE.pieces.values() if p.location == session_id and p.status in ("reserved", "issued")
    ]
    for piece in holdings:
        action = dispositions.get(piece.id, "restock")
        old = piece.status
        if action == "restock":
            piece.status, piece.location, piece.holder = "stock", None, None
            kind = "release"
        elif action == "return_to_workshop":
            piece.status, piece.location, piece.holder = "returned", None, actor
            kind = "return_workshop"
        elif action == "display":
            piece.status, piece.location, piece.holder = "display", None, actor
            kind = "display"
        elif action.startswith("transfer:"):
            target = _session(action.split(":", 1)[1])
            _ensure_session_open(target)
            piece.status, piece.location, piece.holder = "reserved", target.id, actor
            if piece.id not in target.reservations:
                target.reservations.append(piece.id)
            kind = "reserve"
        else:
            raise ValidationError(f"未知处置方式: {action}")
        _movement(piece, kind, old, piece.status, actor, session_id=session_id, note=f"场次取消处置: {action}")
        results.append({"piece_id": piece.id, "action": action, "status": piece.status})
    session.status = "cancelled"
    session.takeover = {
        "materials_to": takeover.get("materials_to"),
        "briefing_to": takeover.get("briefing_to"),
        "consents_to": takeover.get("consents_to"),
        "reason": reason,
        "actor": actor,
        "at": _now(),
    }
    session.history.append({"event": "cancelled", "actor": actor, "reason": reason, "at": _now()})
    return {"session": _session_dict(session), "dispositions": results}


@idempotent
def complete_session(session_id, actor):
    session = _session(session_id)
    _ensure_session_open(session)
    session.status = "completed"
    session.history.append({"event": "completed", "actor": actor, "at": _now()})
    return {"session": _session_dict(session)}


# ---------------------------------------------------------------- 授权与展示


@idempotent
def grant_consent(subject_type, subject_id, category, purpose, granted_by, granted_by_role, expires_at, note=""):
    """按主体、类别、用途、期限分别取得授权。未成年人影像须监护人授权。"""
    if category not in CONSENT_CATEGORIES:
        raise ValidationError(f"未知授权类别 {category}，应为 {', '.join(CONSENT_CATEGORIES)}")
    _require(purpose, "授权用途不能为空")
    if subject_type == "participant":
        participant = STORE.participants.get(subject_id)
        if participant is None:
            raise NotFound(f"参与者不存在: {subject_id}")
        if category == "minor_imagery":
            if not participant.is_minor:
                raise ValidationError("非未成年人不适用未成年人影像授权")
            if granted_by_role != "guardian":
                raise ConsentError("未成年人影像须由监护人授权")
        if category == "work_photo":
            raise ValidationError("作品照片授权的主体应为作品")
    elif subject_type == "work":
        if subject_id not in STORE.works:
            raise NotFound(f"作品不存在: {subject_id}")
        if category != "work_photo":
            raise ValidationError("作品主体仅适用作品照片授权")
    else:
        raise ValidationError("subject_type 仅支持 participant / work")
    expires = _parse_iso(expires_at, "expires_at")
    if expires <= datetime.now(timezone.utc):
        raise ValidationError("授权期限须晚于当前时间")
    consent = Consent(
        id=STORE.next_id("consent"),
        subject_type=subject_type,
        subject_id=subject_id,
        category=category,
        purpose=purpose,
        granted_by=granted_by,
        granted_by_role=granted_by_role,
        granted_at=_now(),
        expires_at=expires_at,
        note=note,
    )
    STORE.consents[consent.id] = consent
    return {"consent": consent.to_dict()}


def _find_active_consent(subject_type, subject_id, category, purpose, at):
    matches = [
        c
        for c in STORE.consents.values()
        if c.subject_type == subject_type
        and c.subject_id == subject_id
        and c.category == category
        and c.purpose == purpose
        and c.status == "active"
        and _parse_iso(c.expires_at, "expires_at") > at
    ]
    matches.sort(key=lambda c: c.granted_at)
    return matches[-1] if matches else None


@idempotent
def publish(subject_type, subject_id, category, purpose, channel, ref="", at=None):
    """发起一次展示：必须落在有效授权（类别+用途+期限）内，否则拒绝。"""
    moment = _parse_iso(at, "at") if at else datetime.now(timezone.utc)
    consent = _find_active_consent(subject_type, subject_id, category, purpose, moment)
    if consent is None:
        raise ConsentError(f"未取得 {category}/{purpose} 的有效授权，停止新的展示")
    publication = Publication(
        id=STORE.next_id("publication"),
        consent_id=consent.id,
        subject_type=subject_type,
        subject_id=subject_id,
        category=category,
        purpose=purpose,
        channel=channel,
        ref=ref,
        created_at=_now(),
    )
    STORE.publications[publication.id] = publication
    return {"publication": publication.to_dict(), "consent_id": consent.id}


@idempotent
def withdraw_consent(consent_id, actor):
    """撤回授权：立即阻止新的展示，已有展示列入下架复核。"""
    consent = STORE.consents.get(consent_id)
    if consent is None:
        raise NotFound(f"授权不存在: {consent_id}")
    if consent.status == "withdrawn":
        return {"consent": consent.to_dict(), "affected_publications": []}
    consent.status = "withdrawn"
    consent.withdrawn_at = _now()
    affected = []
    for publication in STORE.publications.values():
        if publication.consent_id == consent_id and publication.status == "active":
            publication.status = "review_required"
            affected.append(publication.to_dict())
    return {"consent": consent.to_dict(), "affected_publications": affected}


# ---------------------------------------------------------------- 费用与结算


@idempotent
def settle_session(session_id, actor="system"):
    """场次结算：报名费 → 讲师报酬 / 材料再利用估值 / 门店留存。天然幂等。"""
    session = _session(session_id)
    if session.status != "completed":
        raise Conflict("场次完成后才能结算")
    existing = STORE.settlements.get(session_id)
    if existing is not None:
        return existing
    plan = STORE.plans[session.plan_id]
    collected = sum(f.amount_pence for f in STORE.fees if f.session_id == session_id and f.kind == "participant_fee")
    instructor_pay = round(collected * plan.instructor_share_percent / 100)
    consumed_area = 0.0
    material_value = 0
    for movement in STORE.movements:
        if movement.kind == "consume" and movement.session_id == session_id:
            batch = STORE.batches[STORE.pieces[movement.piece_id].batch_id]
            consumed_area += movement.area_cm2
            material_value += round(movement.area_cm2 * batch.reuse_value_per_cm2_pence)
    ops_remainder = collected - instructor_pay - material_value

    def fee(kind, amount, instructor_id=None, note=""):
        entry = FeeEntry(
            id=STORE.next_id("fee"),
            session_id=session_id,
            kind=kind,
            amount_pence=int(amount),
            instructor_id=instructor_id,
            note=note,
            created_at=_now(),
        )
        STORE.fees.append(entry)
        return entry.to_dict()

    entries = [
        fee("instructor_payable", instructor_pay, instructor_id=session.instructor_id,
            note=f"讲师报酬 {plan.instructor_share_percent}%"),
        fee("material_reuse", material_value, note="边角料再利用估值"),
        fee("ops_remainder", ops_remainder, note="门店运营留存"),
    ]
    settlement = {
        "session_id": session_id,
        "collected_pence": collected,
        "instructor_payable_pence": instructor_pay,
        "material_reuse_pence": material_value,
        "ops_remainder_pence": ops_remainder,
        "consumed_area_cm2": round(consumed_area, 2),
        "fee_entries": entries,
        "settled_by": actor,
        "settled_at": _now(),
    }
    STORE.settlements[session_id] = settlement
    return settlement


@idempotent
def record_payout(instructor_id, amount_pence, actor, note=""):
    """登记讲师付款；累计付款不得超过应付报酬。"""
    _instructor(instructor_id)
    amount = int(amount_pence)
    if amount <= 0:
        raise ValidationError("付款金额必须为正数")
    report = instructor_pay_report(instructor_id)
    if amount > report["outstanding_pence"]:
        raise Conflict(f"付款超出应付余额 {report['outstanding_pence']} 便士")
    entry = FeeEntry(
        id=STORE.next_id("fee"),
        session_id="",
        kind="instructor_payout",
        amount_pence=amount,
        instructor_id=instructor_id,
        note=note,
        created_at=_now(),
    )
    STORE.fees.append(entry)
    return {"payout": entry.to_dict(), "report": instructor_pay_report(instructor_id)}


def instructor_pay_report(instructor_id):
    """讲师应得报酬：各场次应付、已付、未付。"""
    _instructor(instructor_id)
    payable = [f for f in STORE.fees if f.kind == "instructor_payable" and f.instructor_id == instructor_id]
    payouts = [f for f in STORE.fees if f.kind == "instructor_payout" and f.instructor_id == instructor_id]
    total_payable = sum(f.amount_pence for f in payable)
    total_paid = sum(f.amount_pence for f in payouts)
    return {
        "instructor_id": instructor_id,
        "sessions": [
            {"session_id": f.session_id, "payable_pence": f.amount_pence, "note": f.note} for f in payable
        ],
        "payable_pence": total_payable,
        "paid_pence": total_paid,
        "outstanding_pence": total_payable - total_paid,
    }


def _batch_reuse_row(batch):
    pieces = [p for p in STORE.pieces.values() if p.batch_id == batch.id]

    def area(*statuses):
        return round(sum(p.area_cm2 for p in pieces if p.status in statuses), 2)

    reused = area("consumed")
    return {
        "batch_id": batch.id,
        "name": batch.name,
        "designer_id": batch.designer_id,
        "source_order": batch.source_order,
        "total_area_cm2": batch.total_area_cm2,
        "reused_in_works_cm2": reused,
        "returned_to_workshop_cm2": area("returned"),
        "display_cm2": area("display"),
        "in_stock_cm2": area("stock"),
        "allocated_cm2": area("reserved", "issued"),
        "reuse_rate": round(reused / batch.total_area_cm2, 4) if batch.total_area_cm2 else 0.0,
    }


def reuse_report(designer_id=None):
    """设计师核清边角料再利用量：按批次汇总去向。"""
    rows = [
        _batch_reuse_row(batch)
        for batch in STORE.batches.values()
        if designer_id is None or batch.designer_id == designer_id
    ]
    totals = {
        "total_area_cm2": round(sum(r["total_area_cm2"] for r in rows), 2),
        "reused_in_works_cm2": round(sum(r["reused_in_works_cm2"] for r in rows), 2),
        "returned_to_workshop_cm2": round(sum(r["returned_to_workshop_cm2"] for r in rows), 2),
        "display_cm2": round(sum(r["display_cm2"] for r in rows), 2),
    }
    return {"rows": rows, "totals": totals}


# ---------------------------------------------------------------- 追溯与查询


def trace_work(work_id):
    """从一件体验作品查到：所用真丝、经手人、授权状态和费用去向。"""
    work = STORE.works.get(work_id)
    if work is None:
        raise NotFound(f"作品不存在: {work_id}")
    session = STORE.sessions[work.session_id]
    plan = STORE.plans[session.plan_id]
    participant = STORE.participants.get(work.participant_id)
    materials = []
    related_piece_ids = set()
    for usage in work.piece_usages:
        piece = STORE.pieces[usage["piece_id"]]
        chain = _piece_chain(piece)
        related_piece_ids.update(link["piece_id"] for link in chain)
        batch = STORE.batches[piece.batch_id]
        materials.append(
            {
                "piece_id": piece.id,
                "area_cm2": usage["area_cm2"],
                "chain": chain,
                "batch": {
                    "id": batch.id,
                    "name": batch.name,
                    "source_order": batch.source_order,
                    "designer_id": batch.designer_id,
                    "silk_type": batch.silk_type,
                    "dye_notes": batch.dye_notes,
                    "cleaning_notes": batch.cleaning_notes,
                },
            }
        )
    handlers = [
        {"actor": m.actor, "kind": m.kind, "piece_id": m.piece_id, "at": m.created_at, "note": m.note}
        for m in STORE.movements
        if m.piece_id in related_piece_ids
    ]
    handovers = [h.to_dict() for h in STORE.handovers if h.session_id == session.id]
    consents = [
        c.to_dict()
        for c in STORE.consents.values()
        if (c.subject_type == "participant" and c.subject_id == work.participant_id)
        or (c.subject_type == "work" and c.subject_id == work.id)
    ]
    fees = [f.to_dict() for f in STORE.fees if f.session_id == session.id]
    return {
        "work": work.to_dict(),
        "session": _session_dict(session),
        "plan": plan.to_dict(),
        "participant": participant.to_dict() if participant else None,
        "materials": materials,
        "handlers": handlers,
        "handovers": handovers,
        "consents": consents,
        "fees": fees,
        "settlement": STORE.settlements.get(session.id),
    }


def _session_dict(session):
    data = session.to_dict()
    data["holdings"] = [
        p.to_dict()
        for p in STORE.pieces.values()
        if p.location == session.id and p.status in ("reserved", "issued")
    ]
    return data


def get_batch(batch_id):
    batch = _batch(batch_id)
    pieces = [p.to_dict() for p in STORE.pieces.values() if p.batch_id == batch_id]
    return {"batch": batch.to_dict(), "pieces": pieces, "reuse": _batch_reuse_row(batch)}


def get_piece(piece_id):
    piece = _piece(piece_id)
    batch = STORE.batches[piece.batch_id]
    movements = [m.to_dict() for m in STORE.movements if m.piece_id == piece_id]
    return {
        "piece": piece.to_dict(),
        "chain": _piece_chain(piece),
        "batch": batch.to_dict(),
        "movements": movements,
    }


def get_session(session_id):
    session = _session(session_id)
    enrollments = [
        e.to_dict() for e in STORE.enrollments.values() if e.session_id == session_id and e.status == "active"
    ]
    handovers = [h.to_dict() for h in STORE.handovers if h.session_id == session_id]
    return {
        "session": _session_dict(session),
        "enrollments": enrollments,
        "handovers": handovers,
        "sufficiency": _sufficiency(session),
    }


def get_consent(consent_id):
    consent = STORE.consents.get(consent_id)
    if consent is None:
        raise NotFound(f"授权不存在: {consent_id}")
    publications = [p.to_dict() for p in STORE.publications.values() if p.consent_id == consent_id]
    return {"consent": consent.to_dict(), "publications": publications}


def get_settlement(session_id):
    settlement = STORE.settlements.get(session_id)
    if settlement is None:
        raise NotFound(f"场次 {session_id} 尚未结算")
    return settlement

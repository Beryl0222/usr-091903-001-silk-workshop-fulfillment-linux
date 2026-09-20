"""领域逻辑：面料谱系、活动履约、授权与追溯。

所有公开方法都在存储锁内执行；带 client_ref 的写操作通过幂等表去重，
断网期间同一笔领料/成品补录多次只扣减一次，并回放首次结果。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from storage import utcnow_iso

CRAFT_TYPES = {"手链", "发簪", "耳饰"}


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _new_id():
    return uuid.uuid4().hex


def _parse_dt(value):
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"时间格式无法解析: {value!r}（需 ISO 8601）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _loads(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _require_fields(data, fields):
    missing = [f for f in fields if data.get(f) is None]
    if missing:
        raise DomainError(f"缺少必填字段: {', '.join(missing)}")


class Domain:
    def __init__(self, store):
        self.store = store
        self.s = store

    # ---------- 基础工具 ----------

    def _row(self, sql, params=()):
        return self.s.execute(sql, params).fetchone()

    def _require(self, sql, params=(), message="未找到记录", status=404):
        row = self._row(sql, params)
        if row is None:
            raise DomainError(message, status)
        return row

    def _idempotent(self, client_ref, op, fn):
        """client_ref 已存在则回放首次结果，否则执行并固化。"""
        if not client_ref:
            try:
                return fn()
            except Exception:
                self.s.conn.rollback()
                raise
        with self.s.lock():
            existing = self._row(
                "SELECT result_json FROM idempotent_requests WHERE client_ref = ?",
                (client_ref,),
            )
            if existing is not None:
                result = json.loads(existing["result_json"])
                result["replayed"] = True
                return result
            try:
                result = fn()
                self.s.execute(
                    "INSERT INTO idempotent_requests (client_ref, op, result_json, created_at) "
                    "VALUES (?,?,?,?)",
                    (client_ref, op, _dumps(result), utcnow_iso()),
                )
                self.s.commit()
                return result
            except Exception:
                # 失败时丢弃本次所有未落盘写入，避免半笔领料污染后续请求
                self.s.conn.rollback()
                raise

    def _movement(self, piece_id, action, from_status, to_status, event_id=None,
                  handler_id=None, handler_name=None, amount_cm2=None, note=None):
        self.s.execute(
            "INSERT INTO piece_movements (piece_id, action, from_status, to_status, "
            "event_id, handler_id, handler_name, amount_cm2, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (piece_id, action, from_status, to_status, event_id, handler_id,
             handler_name, amount_cm2, note, utcnow_iso()),
        )

    def _create_piece(self, batch_id, parent_id, length, width, status, event_id, note):
        piece_id = _new_id()
        self.s.execute(
            "INSERT INTO fabric_pieces (id, code, batch_id, parent_id, length_cm, "
            "width_cm, status, event_id, disposition_note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (piece_id, f"P-{piece_id[:8].upper()}", batch_id, parent_id,
             length, width, status, event_id, note, utcnow_iso()),
        )
        return piece_id

    def _consume_area(self, piece, area_cm2, action, event_id, handler_id,
                      handler_name, note):
        """从裁片消耗面积：整块用完转 used；用掉一部分则切出余料子裁片。

        无论哪种情况，原裁片身份与 parent 链都保留，来源不丢。
        """
        total = piece["length_cm"] * piece["width_cm"]
        if piece["status"] != "available":
            raise DomainError(f"裁片 {piece['code']} 当前状态 {piece['status']}，不可领用", 409)
        if area_cm2 <= 0:
            raise DomainError("领用面积必须大于 0")
        if area_cm2 > total + 1e-9:
            raise DomainError(
                f"裁片 {piece['code']} 可用 {total:.1f}cm²，不足以扣减 {area_cm2:.1f}cm²", 409)

        remainder = max(0.0, total - area_cm2)
        if remainder <= 1e-9:
            self.s.execute("UPDATE fabric_pieces SET status = 'used' WHERE id = ?", (piece["id"],))
            self._movement(piece["id"], action, "available", "used", event_id,
                           handler_id, handler_name, area_cm2, note)
        else:
            # 面积等比折算成同宽余料，便于继续使用
            rest_len = remainder / piece["width_cm"]
            rest_id = self._create_piece(
                piece["batch_id"], piece["id"], rest_len, piece["width_cm"],
                "available", event_id or piece["event_id"], f"{action} 后余料")
            self.s.execute("UPDATE fabric_pieces SET status = 'split' WHERE id = ?", (piece["id"],))
            self._movement(piece["id"], action, "available", "split", event_id,
                           handler_id, handler_name, area_cm2, note)
            self._movement(rest_id, "split", None, "available", event_id or piece["event_id"],
                           handler_id, handler_name, remainder, "拆分产生余料")

    # ---------- 面料批次与裁片 ----------

    def create_batch(self, data):
        _require_fields(data, ["length_cm", "width_cm"])
        with self.s.lock():
            def work():
                batch_id = data.get("id") or _new_id()
                now = utcnow_iso()
                try:
                    self.s.execute(
                        "INSERT INTO fabric_batches (id, code, source, silk_type, color, "
                        "dyeing_notes, cleaning_notes, length_cm, width_cm, handler_id, "
                        "handler_name, received_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, data.get("code") or f"B-{batch_id[:8].upper()}",
                         data.get("source"), data.get("silk_type"), data.get("color"),
                         data.get("dyeing_notes"), data.get("cleaning_notes"),
                         data["length_cm"], data["width_cm"], data.get("handler_id"),
                         data.get("handler_name"), data.get("received_at") or now,
                         "in_stock"),
                    )
                except sqlite_integrity() as exc:
                    raise DomainError(f"批次编号冲突: {data.get('code')}", 409) from exc
                # 整匹面料作为首个裁片入库
                piece_id = self._create_piece(
                    batch_id, None, data["length_cm"], data["width_cm"],
                    "available", None, "入店整匹余料")
                self._movement(piece_id, "receive", None, "available",
                               handler_id=data.get("handler_id"),
                               handler_name=data.get("handler_name"),
                               amount_cm2=data["length_cm"] * data["width_cm"],
                               note="旗袍裁衣余料入库")
                self.s.commit()
                return {"batch_id": batch_id, "root_piece_id": piece_id}

            return self._idempotent(data.get("client_ref"), "create_batch", work)

    def split_piece(self, data):
        """把一块裁片拆成多块（同一块面料分到不同场次的入口）。"""
        _require_fields(data, ["piece_id"])
        with self.s.lock():
            piece = self._require(
                "SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                (data["piece_id"], data["piece_id"]), "裁片不存在")
            cuts = data.get("cuts") or []
            if not cuts:
                raise DomainError("cuts 至少包含一块拆分尺寸 {length_cm,width_cm}")
            for c in cuts:
                _require_fields(c, ["length_cm", "width_cm"])
            total_src = piece["length_cm"] * piece["width_cm"]
            total_cut = sum(c["length_cm"] * c["width_cm"] for c in cuts)
            if piece["status"] != "available":
                raise DomainError(f"裁片 {piece['code']} 状态为 {piece['status']}，不可拆分", 409)
            if total_cut > total_src + 1e-9:
                raise DomainError(
                    f"拆分总面积 {total_cut:.1f}cm² 超过裁片面积 {total_src:.1f}cm²")

            children = []
            for c in cuts:
                cid = self._create_piece(
                    piece["batch_id"], piece["id"], c["length_cm"], c["width_cm"],
                    "available", c.get("event_id") or piece["event_id"], c.get("note"))
                children.append(cid)
                self._movement(cid, "split", None, "available", c.get("event_id"),
                               data.get("handler_id"), data.get("handler_name"),
                               c["length_cm"] * c["width_cm"], "手工拆分裁片")
            remainder = max(0.0, total_src - total_cut)
            if remainder > 1e-9:
                rest_len = remainder / piece["width_cm"]
                rid = self._create_piece(
                    piece["batch_id"], piece["id"], rest_len, piece["width_cm"],
                    "available", piece["event_id"], "拆分余料")
                children.append(rid)
                self._movement(rid, "split", None, "available", piece["event_id"],
                               data.get("handler_id"), data.get("handler_name"),
                               remainder, "拆分余料")
            self.s.execute("UPDATE fabric_pieces SET status = 'split' WHERE id = ?",
                           (piece["id"],))
            self._movement(piece["id"], "split", "available", "split",
                           handler_id=data.get("handler_id"),
                           handler_name=data.get("handler_name"),
                           amount_cm2=total_cut, note=data.get("note"))
            self.s.commit()
            return {"parent_piece_id": piece["id"], "child_piece_ids": children,
                    "remainder_cm2": round(remainder, 2)}

    def assign_piece(self, data):
        """把裁片划归某场活动（材料责任落到场次）。"""
        _require_fields(data, ["piece_id", "event_id"])
        with self.s.lock():
            piece = self._require("SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                                  (data["piece_id"], data["piece_id"]), "裁片不存在")
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            self.s.execute("UPDATE fabric_pieces SET event_id = ? WHERE id = ?",
                           (event["id"], piece["id"]))
            self._movement(piece["id"], "transfer", piece["status"], piece["status"],
                           event["id"], data.get("handler_id"), data.get("handler_name"),
                           note=f"划归场次 {event['title']}")
            self.s.commit()
            return {"piece_id": piece["id"], "event_id": event["id"]}

    def return_to_workshop(self, data):
        """未用完的裁片退回工坊；只能退仍可用的裁片，谱系保留。"""
        _require_fields(data, ["piece_id"])
        with self.s.lock():
            piece = self._require("SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                                  (data["piece_id"], data["piece_id"]), "裁片不存在")
            if piece["status"] != "available":
                raise DomainError(f"仅 available 裁片可退回，当前 {piece['status']}", 409)
            self.s.execute(
                "UPDATE fabric_pieces SET status = 'returned', disposition_note = ? WHERE id = ?",
                (data.get("note") or "退回工坊", piece["id"]))
            self._movement(piece["id"], "return", "available", "returned",
                           piece["event_id"], data.get("handler_id"),
                           data.get("handler_name"), note=data.get("note"))
            self.s.commit()
            return {"piece_id": piece["id"], "status": "returned"}

    def convert_to_display(self, data):
        """裁片/余料改作展示品。"""
        _require_fields(data, ["piece_id"])
        with self.s.lock():
            piece = self._require("SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                                  (data["piece_id"], data["piece_id"]), "裁片不存在")
            if piece["status"] not in ("available",):
                raise DomainError(f"仅 available 裁片可改作展示，当前 {piece['status']}", 409)
            self.s.execute(
                "UPDATE fabric_pieces SET status = 'display', disposition_note = ? WHERE id = ?",
                (data.get("note") or "改作展示品", piece["id"]))
            self._movement(piece["id"], "display", "available", "display",
                           piece["event_id"], data.get("handler_id"),
                           data.get("handler_name"), note=data.get("note"))
            self.s.commit()
            return {"piece_id": piece["id"], "status": "display"}

    def reuse_scrap(self, data):
        """边角料再利用登记，供设计师核量。"""
        _require_fields(data, ["piece_id", "area_cm2", "product"])
        with self.s.lock():
            def work():
                piece = self._require("SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                                      (data["piece_id"], data["piece_id"]), "裁片不存在")
                self._consume_area(piece, data["area_cm2"], "reuse", piece["event_id"],
                                   data.get("handler_id"), data.get("handler_name"),
                                   f"再利用: {data.get('product', '')}")
                rid = _new_id()
                self.s.execute(
                    "INSERT INTO reuse_records (id, piece_id, product, area_cm2, "
                    "recorded_by, note, created_at) VALUES (?,?,?,?,?,?,?)",
                    (rid, piece["id"], data["product"], data["area_cm2"],
                     data.get("recorded_by"), data.get("note"), utcnow_iso()))
                self.s.commit()
                return {"reuse_id": rid, "piece_id": piece["id"],
                        "area_cm2": data["area_cm2"]}

            return self._idempotent(data.get("client_ref"), "reuse_scrap", work)

    # ---------- 方案 / 讲师 / 场次 ----------

    def create_instructor(self, data):
        _require_fields(data, ["name"])
        with self.s.lock():
            iid = data.get("id") or _new_id()
            quals = data.get("qualifications") or []
            for q in quals:
                if q.get("expires_at") and _parse_dt(q["expires_at"]) < _parse_dt(
                        q.get("issued_at") or utcnow_iso()):
                    raise DomainError(f"资质 {q.get('title')} 到期日早于签发日")
            self.s.execute(
                "INSERT INTO instructors (id, name, qualifications, specialties, active, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (iid, data["name"], _dumps(quals),
                 _dumps(data.get("specialties") or []),
                 0 if data.get("active") is False else 1, utcnow_iso()))
            self.s.commit()
            return {"instructor_id": iid}

    def create_plan(self, data):
        _require_fields(data, ["title"])
        with self.s.lock():
            pid = data.get("id") or _new_id()
            reqs = data.get("silk_requirements") or {}
            for craft, area in reqs.items():
                if craft not in CRAFT_TYPES:
                    raise DomainError(f"未知工艺品类: {craft}（可选 {sorted(CRAFT_TYPES)}）")
                if area <= 0:
                    raise DomainError(f"{craft} 的单位用量必须为正数")
            self.s.execute(
                "INSERT INTO activity_plans (id, title, craft_types, process_script, "
                "silk_requirements, payout_rate_per_head, created_at) VALUES (?,?,?,?,?,?,?)",
                (pid, data["title"], _dumps(data.get("craft_types") or list(reqs.keys())),
                 data.get("process_script"), _dumps(reqs),
                 data.get("payout_rate_per_head", 0), utcnow_iso()))
            self.s.commit()
            return {"plan_id": pid}

    def _revision(self, event_id, kind, changes, created_by):
        self.s.execute(
            "INSERT INTO event_revisions (event_id, kind, changes, created_by, created_at) "
            "VALUES (?,?,?,?,?)",
            (event_id, kind, _dumps(changes), created_by, utcnow_iso()))

    def schedule_event(self, data):
        _require_fields(data, ["plan_id", "start_at", "end_at"])
        with self.s.lock():
            plan = self._require("SELECT * FROM activity_plans WHERE id = ?",
                                 (data["plan_id"],), "活动方案不存在")
            start = _parse_dt(data["start_at"])
            end = _parse_dt(data["end_at"])
            if end <= start:
                raise DomainError("结束时间必须晚于开始时间")
            instructor = None
            if data.get("instructor_id"):
                instructor = self._require(
                    "SELECT * FROM instructors WHERE id = ? AND active = 1",
                    (data["instructor_id"],), "讲师不存在或已停用")
            eid = data.get("id") or _new_id()
            promise = {
                "title": data.get("title") or plan["title"],
                "start_at": data["start_at"],
                "end_at": data["end_at"],
                "instructor_id": data.get("instructor_id"),
                "plan_id": plan["id"],
                "committed_at": utcnow_iso(),
            }
            self.s.execute(
                "INSERT INTO events (id, plan_id, title, start_at, end_at, instructor_id, "
                "status, promise_snapshot, payout_rate_per_head, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, plan["id"], promise["title"], data["start_at"], data["end_at"],
                 data.get("instructor_id"), "scheduled", _dumps(promise),
                 data.get("payout_rate_per_head", plan["payout_rate_per_head"]),
                 utcnow_iso()))
            self._revision(eid, "create", promise, data.get("created_by"))
            self.s.commit()
            result = {"event_id": eid, "promise": promise}
            result.update(self.material_sufficiency(eid))
            return result

    def reschedule_event(self, data):
        """改期：原承诺完整保留，仅记录修订并重算材料。"""
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            if event["status"] == "cancelled":
                raise DomainError("已取消场次不能改期，请新建场次", 409)
            changes = {}
            new_start = event["start_at"]
            new_end = event["end_at"]
            if data.get("start_at"):
                new_start = data["start_at"]
                _parse_dt(new_start)
                changes["start_at"] = {"from": event["start_at"], "to": new_start}
            if data.get("end_at"):
                new_end = data["end_at"]
                _parse_dt(new_end)
                changes["end_at"] = {"from": event["end_at"], "to": new_end}
            if _parse_dt(new_end) <= _parse_dt(new_start):
                raise DomainError("结束时间必须晚于开始时间")
            if data.get("title"):
                changes["title"] = {"from": event["title"], "to": data["title"]}
            if not changes:
                raise DomainError("未提供任何改期字段")
            self.s.execute(
                "UPDATE events SET start_at = ?, end_at = ?, title = COALESCE(?, title), "
                "status = 'rescheduled' WHERE id = ?",
                (new_start, new_end, data.get("title"), event["id"]))
            self._revision(event["id"], "reschedule", changes, data.get("created_by"))
            self.s.commit()
            result = {"event_id": event["id"],
                      "original_promise": _loads(event["promise_snapshot"], None),
                      "changes": changes}
            result.update(self.material_sufficiency(event["id"]))
            return result

    def change_instructor(self, data):
        """临时换老师：记录修订；工艺讲解责任可同时交接。"""
        _require_fields(data, ["event_id", "instructor_id"])
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            if event["status"] == "cancelled":
                raise DomainError("已取消场次不能更换讲师", 409)
            new_inst = self._require(
                "SELECT * FROM instructors WHERE id = ? AND active = 1",
                (data["instructor_id"],), "讲师不存在或已停用")
            changes = {"instructor_id": {"from": event["instructor_id"],
                                         "to": new_inst["id"]}}
            self.s.execute("UPDATE events SET instructor_id = ? WHERE id = ?",
                          (new_inst["id"], event["id"]))
            self._revision(event["id"], "instructor_change", changes, data.get("created_by"))
            if data.get("transfer_process"):
                self.add_handoff({
                    "event_id": event["id"], "responsibility": "process",
                    "from_party": event["instructor_id"], "to_party": new_inst["id"],
                    "reason": data.get("reason") or "临时换老师",
                    "created_by": data.get("created_by")})
            self.s.commit()
            return {"event_id": event["id"], "changes": changes,
                    "instructor_name": new_inst["name"]}

    def cancel_event(self, data):
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            if event["status"] == "cancelled":
                raise DomainError("场次已处于取消状态", 409)
            self.s.execute("UPDATE events SET status = 'cancelled' WHERE id = ?", (event["id"],))
            self._revision(event["id"], "cancel", {"reason": data.get("reason")},
                           data.get("created_by"))
            self.s.commit()
            return {"event_id": event["id"], "status": "cancelled",
                    "handoffs": self.list_handoffs(event["id"])}

    def complete_event(self, data):
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            if event["status"] not in ("scheduled", "rescheduled"):
                raise DomainError(f"场次状态 {event['status']}，不能结项", 409)
            self.s.execute("UPDATE events SET status = 'completed' WHERE id = ?", (event["id"],))
            self._revision(event["id"], "complete", {}, data.get("created_by"))
            self.s.commit()
            return {"event_id": event["id"], "status": "completed"}

    def add_handoff(self, data):
        """登记材料 / 工艺讲解 / 照片授权三类责任的接手人。"""
        _require_fields(data, ["event_id", "responsibility", "to_party"])
        with self.s.lock():
            responsibility = data["responsibility"]
            if responsibility not in ("material", "process", "photo"):
                raise DomainError("responsibility 必须是 material/process/photo 之一")
            self._require("SELECT id FROM events WHERE id = ?", (data["event_id"],),
                          "场次不存在")
            hid = _new_id()
            self.s.execute(
                "INSERT INTO handoffs (id, event_id, responsibility, from_party, to_party, "
                "reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (hid, data["event_id"], responsibility, data.get("from_party"),
                 data["to_party"], data.get("reason"), data.get("created_by"), utcnow_iso()))
            self.s.commit()
            return {"handoff_id": hid, "event_id": data["event_id"],
                    "responsibility": responsibility, "to_party": data["to_party"]}

    def list_handoffs(self, event_id):
        rows = self.s.execute(
            "SELECT * FROM handoffs WHERE event_id = ? ORDER BY created_at", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 参与者 ----------

    def register_participant(self, data):
        _require_fields(data, ["event_id", "name"])
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
            if event["status"] == "cancelled":
                raise DomainError("场次已取消，不能报名", 409)
            pid = _new_id()
            is_minor = 1 if data.get("is_minor") else 0
            if is_minor and not data.get("guardian_name"):
                raise DomainError("未成年人必须登记监护人姓名（影像授权须监护人同意）")
            choices = data.get("choices") or []
            bad = [c for c in choices if c not in CRAFT_TYPES]
            if bad:
                raise DomainError(f"未知工艺选择: {bad}")
            self.s.execute(
                "INSERT INTO participants (id, event_id, name, is_minor, guardian_name, "
                "guardian_contact, contact, choices, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, event["id"], data["name"], is_minor, data.get("guardian_name"),
                 data.get("guardian_contact"), data.get("contact"), _dumps(choices),
                 "registered", utcnow_iso()))
            self.s.commit()
            result = {"participant_id": pid}
            result.update(self.material_sufficiency(event["id"]))
            return result

    def set_choices(self, data):
        _require_fields(data, ["participant_id"])
        with self.s.lock():
            p = self._require("SELECT * FROM participants WHERE id = ?",
                              (data["participant_id"],), "参与者不存在")
            choices = data.get("choices") or []
            bad = [c for c in choices if c not in CRAFT_TYPES]
            if bad:
                raise DomainError(f"未知工艺选择: {bad}")
            self.s.execute("UPDATE participants SET choices = ? WHERE id = ?",
                           (_dumps(choices), p["id"]))
            self.s.commit()
            result = {"participant_id": p["id"], "choices": choices}
            result.update(self.material_sufficiency(p["event_id"]))
            return result

    # ---------- 领料与成品（支持断网补录，幂等） ----------

    def allocate_material(self, data):
        _require_fields(data, ["event_id", "piece_id", "area_cm2"])
        with self.s.lock():
            def work():
                event = self._require("SELECT * FROM events WHERE id = ?",
                                      (data["event_id"],), "场次不存在")
                if event["status"] == "cancelled":
                    raise DomainError("场次已取消，不能领料", 409)
                piece = self._require("SELECT * FROM fabric_pieces WHERE id = ? OR code = ?",
                                      (data["piece_id"], data["piece_id"]), "裁片不存在")
                participant_id = data.get("participant_id")
                if participant_id:
                    self._require("SELECT id FROM participants WHERE id = ? AND event_id = ?",
                                  (participant_id, event["id"]), "参与者不在该场次")
                aid = _new_id()
                self._consume_area(
                    piece, data["area_cm2"], "allocate", event["id"],
                    data.get("handler_id"), data.get("handler_name"),
                    data.get("purpose") or "体验领料")
                self.s.execute(
                    "INSERT INTO allocations (id, client_ref, event_id, participant_id, "
                    "piece_id, area_cm2, purpose, handler_id, handler_name, recorded_at, "
                    "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, data.get("client_ref"), event["id"], participant_id, piece["id"],
                     data["area_cm2"], data.get("purpose"), data.get("handler_id"),
                     data.get("handler_name"), data.get("recorded_at") or utcnow_iso(),
                     "active", utcnow_iso()))
                self.s.commit()
                result = {"allocation_id": aid, "piece_id": piece["id"],
                          "area_cm2": data["area_cm2"]}
                result.update(self.material_sufficiency(event["id"]))
                return result

            return self._idempotent(data.get("client_ref"), "allocate_material", work)

    def reverse_allocation(self, data):
        """冲销一笔领料（如活动取消），按原面积补回可用余料子裁片。"""
        if not data.get("allocation_id") and not data.get("client_ref"):
            raise DomainError("需要 allocation_id 或原领料 client_ref")
        with self.s.lock():
            alloc = self._require("SELECT * FROM allocations WHERE id = ? OR client_ref = ?",
                                  (data["allocation_id"], data.get("client_ref", "")),
                                  "领料记录不存在")
            if alloc["status"] == "reversed":
                raise DomainError("该领料已冲销", 409)
            used = self._row(
                "SELECT 1 FROM artwork_materials WHERE allocation_id = ?", (alloc["id"],))
            if used:
                raise DomainError("领料已用于成品，不能冲销", 409)
            piece = self._require("SELECT * FROM fabric_pieces WHERE id = ?",
                                  (alloc["piece_id"],), "原裁片缺失")
            rest_len = alloc["area_cm2"] / piece["width_cm"]
            rid = self._create_piece(
                piece["batch_id"], piece["id"], rest_len, piece["width_cm"],
                "available", alloc["event_id"], "领料冲销退回")
            self.s.execute("UPDATE allocations SET status = 'reversed' WHERE id = ?",
                           (alloc["id"],))
            self._movement(rid, "transfer", None, "available", alloc["event_id"],
                           data.get("handler_id"), data.get("handler_name"),
                           alloc["area_cm2"], "领料冲销，面积退回")
            self.s.commit()
            return {"allocation_id": alloc["id"], "restored_piece_id": rid,
                    "restored_cm2": alloc["area_cm2"]}

    def record_artwork(self, data):
        _require_fields(data, ["event_id", "title", "craft_type"])
        with self.s.lock():
            def work():
                event = self._require("SELECT * FROM events WHERE id = ?",
                                      (data["event_id"],), "场次不存在")
                if data.get("craft_type") not in CRAFT_TYPES:
                    raise DomainError(f"craft_type 必须是 {sorted(CRAFT_TYPES)} 之一")
                participant_id = data.get("participant_id")
                if participant_id:
                    p = self._require(
                        "SELECT * FROM participants WHERE id = ? AND event_id = ?",
                        (participant_id, event["id"]), "参与者不在该场次")
                    if data["craft_type"] not in _loads(p["choices"], []):
                        raise DomainError(
                            f"成品品类 {data['craft_type']} 不在参与者当场选择内", 409)
                aid = _new_id()
                self.s.execute(
                    "INSERT INTO artworks (id, client_ref, event_id, participant_id, title, "
                    "craft_type, created_by, note, recorded_at, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (aid, data.get("client_ref"), event["id"], participant_id,
                     data["title"], data["craft_type"], data.get("created_by"),
                     data.get("note"), data.get("recorded_at") or utcnow_iso(),
                     utcnow_iso()))
                links = data.get("materials") or []
                if not links:
                    raise DomainError("成品必须登记至少一条用料 (allocation_id/area_cm2)")
                total = 0.0
                for link in links:
                    alloc = self._require(
                        "SELECT * FROM allocations WHERE id = ? AND status = 'active'",
                        (link["allocation_id"],), "领料不存在或已冲销")
                    if alloc["event_id"] != event["id"]:
                        raise DomainError("领料与成品不属于同一场次", 409)
                    area = link["area_cm2"]
                    if area <= 0:
                        raise DomainError("用料面积必须为正")
                    already = self.s.execute(
                        "SELECT COALESCE(SUM(area_cm2),0) AS t FROM artwork_materials "
                        "WHERE allocation_id = ?", (alloc["id"],)).fetchone()["t"]
                    if area + already > alloc["area_cm2"] + 1e-9:
                        raise DomainError(
                            f"领料 {alloc['id']} 已用于其他作品 {already:.1f}cm²，"
                            f"本次 {area:.1f}cm² 超出剩余 {alloc['area_cm2'] - already:.1f}cm²")
                    self.s.execute(
                        "INSERT INTO artwork_materials (artwork_id, piece_id, allocation_id, "
                        "area_cm2) VALUES (?,?,?,?)",
                        (aid, alloc["piece_id"], alloc["id"], area))
                    total += area
                self.s.commit()
                return {"artwork_id": aid, "event_id": event["id"],
                        "total_silk_cm2": round(total, 2)}

            return self._idempotent(data.get("client_ref"), "record_artwork", work)

    # ---------- 授权与展示 ----------

    _SUBJECT_LABELS = {
        "minor_image": "未成年人影像",
        "customer_story": "顾客故事",
        "artwork_photo": "作品照片",
    }

    def grant_consent(self, data):
        """按主体、用途、渠道、期限分别取得授权。"""
        _require_fields(data, ["subject_type", "subject_id", "purpose",
                               "granted_by", "granted_by_role", "valid_until"])
        with self.s.lock():
            subject_type = data["subject_type"]
            if subject_type not in self._SUBJECT_LABELS:
                raise DomainError(
                    f"subject_type 必须是 {sorted(self._SUBJECT_LABELS)} 之一")
            subject_id = data["subject_id"]
            if subject_type == "minor_image":
                p = self._require("SELECT * FROM participants WHERE id = ?", (subject_id,),
                                  "参与者不存在")
                if not p["is_minor"]:
                    raise DomainError("该参与者不是未成年人；作品/故事请用对应主体授权")
                if data.get("granted_by_role") != "guardian":
                    raise DomainError("未成年人影像必须由监护人授权", 403)
            elif subject_type == "customer_story":
                self._require("SELECT id FROM participants WHERE id = ?", (subject_id,),
                              "参与者不存在")
                if data.get("granted_by_role") not in ("self", "guardian"):
                    raise DomainError("顾客故事须由本人或监护人授权", 403)
            else:
                self._require("SELECT id FROM artworks WHERE id = ?", (subject_id,),
                              "作品不存在")
                if data.get("granted_by_role") not in ("self", "guardian", "staff"):
                    raise DomainError("作品照片须由作者本人/监护人授权或店员代记")

            valid_from = _parse_dt(data.get("valid_from") or utcnow_iso())
            valid_until = _parse_dt(data["valid_until"])
            if valid_until <= valid_from:
                raise DomainError("授权截止时间必须晚于起始时间")
            purpose = data["purpose"]
            channel = data.get("channel", "any")
            cid = _new_id()
            self.s.execute(
                "INSERT INTO consent_grants (id, subject_type, subject_id, purpose, channel, "
                "granted_by, granted_by_role, valid_from, valid_until, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, subject_type, subject_id, purpose, channel,
                 data["granted_by"], data["granted_by_role"],
                 data.get("valid_from") or utcnow_iso(), data["valid_until"],
                 "granted", utcnow_iso()))
            self.s.commit()
            return {"consent_id": cid, "subject_type": subject_type,
                    "purpose": purpose, "valid_until": data["valid_until"]}

    def withdraw_consent(self, data):
        """撤回授权：立即生效，此后拒绝一切新展示（历史已发生展示不追溯）。"""
        _require_fields(data, ["consent_id"])
        with self.s.lock():
            grant = self._require(
                "SELECT * FROM consent_grants WHERE id = ?", (data["consent_id"],),
                "授权记录不存在")
            if grant["status"] == "withdrawn":
                return {"consent_id": grant["id"], "status": "withdrawn", "already": True}
            self.s.execute(
                "UPDATE consent_grants SET status = 'withdrawn', withdrawn_at = ?, "
                "withdraw_reason = ? WHERE id = ?",
                (utcnow_iso(), data.get("reason"), grant["id"]))
            self.s.commit()
            return {"consent_id": grant["id"], "status": "withdrawn"}

    def _active_grant(self, subject_type, subject_id, purpose, channel, at):
        rows = self.s.execute(
            "SELECT * FROM consent_grants WHERE subject_type = ? AND subject_id = ? "
            "AND purpose = ? AND status = 'granted'",
            (subject_type, subject_id, purpose)).fetchall()
        for g in rows:
            if not (_parse_dt(g["valid_from"]) <= at <= _parse_dt(g["valid_until"])):
                continue
            if g["channel"] == "any" or g["channel"] == channel:
                return g
        return None

    def check_display(self, data):
        """展示前校验：用途、渠道、期限、撤回状态全部满足才放行，并留痕。"""
        _require_fields(data, ["subject_type", "subject_id", "purpose"])
        with self.s.lock():
            subject_type = data["subject_type"]
            subject_id = data["subject_id"]
            purpose = data["purpose"]
            channel = data.get("channel", "any")
            at = _parse_dt(data.get("at") or utcnow_iso())

            # 作品照片展示时，若画面含未成年参与者，其影像授权也必须有效
            related_participants = []
            if subject_type == "artwork_photo":
                aw = self._require("SELECT * FROM artworks WHERE id = ?", (subject_id,),
                                   "作品不存在")
                if aw["participant_id"]:
                    related_participants.append(aw["participant_id"])

            allowed, reason = True, "ok"
            grant = self._active_grant(subject_type, subject_id, purpose, channel, at)
            if grant is None:
                allowed, reason = False, "无有效授权（缺失/已撤回/已过期/用途或渠道不符）"
            else:
                for pid in related_participants:
                    p = self._row("SELECT * FROM participants WHERE id = ?", (pid,))
                    if p and p["is_minor"]:
                        if self._active_grant("minor_image", pid, purpose, channel, at) is None:
                            allowed, reason = False, "作品含未成年人影像，缺少有效监护人授权"
                            break
            self.s.execute(
                "INSERT INTO display_attempts (subject_type, subject_id, purpose, channel, "
                "allowed, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                (subject_type, subject_id, purpose, channel,
                 1 if allowed else 0, reason, utcnow_iso()))
            self.s.commit()
            return {"allowed": allowed, "reason": reason,
                    "subject_type": subject_type, "subject_id": subject_id,
                    "purpose": purpose, "channel": channel,
                    "consent_id": grant["id"] if allowed and grant else None}

    def list_consents(self, subject_type, subject_id):
        with self.s.lock():
            now = _parse_dt(utcnow_iso())
            rows = self.s.execute(
                "SELECT * FROM consent_grants WHERE subject_type = ? AND subject_id = ? "
                "ORDER BY created_at", (subject_type, subject_id)).fetchall()
            result = []
            for g in rows:
                item = dict(g)
                item["currently_active"] = (
                    g["status"] == "granted"
                    and _parse_dt(g["valid_from"]) <= now <= _parse_dt(g["valid_until"]))
                result.append(item)
            return result

    # ---------- 费用 ----------

    def record_fee(self, data):
        _require_fields(data, ["category", "direction", "amount"])
        with self.s.lock():
            def work():
                if data["category"] not in ("participant_fee", "material_cost",
                                            "workshop_refund", "instructor_payout", "other"):
                    raise DomainError("未知费用类别")
                if data["direction"] not in ("in", "out"):
                    raise DomainError("direction 必须是 in/out")
                if data.get("event_id"):
                    self._require("SELECT id FROM events WHERE id = ?", (data["event_id"],),
                                  "场次不存在")
                if data.get("artwork_id"):
                    self._require("SELECT id FROM artworks WHERE id = ?",
                                  (data["artwork_id"],), "作品不存在")
                fid = _new_id()
                self.s.execute(
                    "INSERT INTO fee_records (id, event_id, artwork_id, category, direction, "
                    "amount, note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (fid, data.get("event_id"), data.get("artwork_id"), data["category"],
                     data["direction"], data["amount"], data.get("note"),
                     data.get("created_by"), utcnow_iso()))
                self.s.commit()
                return {"fee_id": fid, "category": data["category"],
                        "direction": data["direction"], "amount": data["amount"]}

            return self._idempotent(data.get("client_ref"), "record_fee", work)

    # ---------- 批次查询 ----------

    def list_batches(self, _payload=None):
        rows = self.s.execute(
            "SELECT id, code, source, silk_type, color, length_cm, width_cm, "
            "status, received_at FROM fabric_batches ORDER BY received_at").fetchall()
        return {"batches": [dict(r) for r in rows]}

    def get_batch(self, payload):
        row = self._require("SELECT * FROM fabric_batches WHERE id = ? OR code = ?",
                            (payload["id"], payload["id"]), "批次不存在")
        result = dict(row)
        pieces = self.s.execute(
            "SELECT * FROM fabric_pieces WHERE batch_id = ? ORDER BY created_at",
            (row["id"],)).fetchall()
        result["pieces"] = [dict(p) for p in pieces]
        return result

    def reuse_report_http(self, payload):
        return self.reuse_report((payload or {}).get("batch_id") or None)

    def consents_http(self, payload):
        return {"grants": self.list_consents(
            payload["subject_type"], payload["subject_id"])}

    # ---------- 核算与追溯 ----------

    def material_sufficiency(self, event_id):
        """按活动方案的单位用量 × 参与者选择重算材料是否够用。"""
        event = self._require("SELECT * FROM events WHERE id = ?", (event_id,),
                              "场次不存在")
        plan = self._require("SELECT * FROM activity_plans WHERE id = ?",
                             (event["plan_id"],), "方案不存在")
        reqs = _loads(plan["silk_requirements"], {})
        participants = self.s.execute(
            "SELECT choices FROM participants WHERE event_id = ? AND status = 'registered'",
            (event_id,)).fetchall()
        need_by_type = {}
        for p in participants:
            for craft in _loads(p["choices"], []):
                need_by_type[craft] = need_by_type.get(craft, 0) + reqs.get(craft, 0)
        required = sum(need_by_type.values())
        available = self.s.execute(
            "SELECT COALESCE(SUM(length_cm*width_cm),0) AS a FROM fabric_pieces "
            "WHERE event_id = ? AND status = 'available'", (event_id,)).fetchone()["a"]
        allocated = self.s.execute(
            "SELECT COALESCE(SUM(area_cm2),0) AS a FROM allocations "
            "WHERE event_id = ? AND status = 'active'", (event_id,)).fetchone()["a"]
        covered = available + allocated
        return {
            "headcount": len(participants),
            "required_cm2": round(required, 2),
            "available_cm2": round(available, 2),
            "allocated_cm2": round(allocated, 2),
            "shortfall_cm2": round(max(0.0, required - covered), 2),
            "sufficient": covered + 1e-9 >= required,
            "required_by_type": {k: round(v, 2) for k, v in need_by_type.items()},
        }

    def sufficiency_http(self, payload):
        return self.material_sufficiency(payload["event_id"])

    def payout_http(self, payload):
        return self.payout_report(payload["event_id"])

    def payout_report(self, event_id):
        """讲师应得报酬：按结项时在册人头 × 单人课酬。"""
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (event_id,),
                                  "场次不存在")
            headcount = self.s.execute(
                "SELECT COUNT(*) AS c FROM participants WHERE event_id = ? AND status = 'registered'",
                (event_id,)).fetchone()["c"]
            rate = event["payout_rate_per_head"] or 0
            paid_rows = self.s.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM fee_records "
                "WHERE event_id = ? AND category = 'instructor_payout' AND direction = 'out'",
                (event_id,)).fetchone()
            instructor = None
            if event["instructor_id"]:
                instructor = self._row(
                    "SELECT id, name, qualifications, specialties FROM instructors WHERE id = ?",
                    (event["instructor_id"],))
                instructor = dict(instructor) if instructor else None
            return {"event_id": event_id, "status": event["status"],
                    "instructor": instructor, "headcount": headcount,
                    "rate_per_head": rate,
                    "payable": round(rate * headcount, 2),
                    "already_paid": paid_rows["t"],
                    "outstanding": round(max(0.0, rate * headcount - paid_rows["t"]), 2)}

    def reuse_report(self, batch_id=None):
        """设计师视角：每批真丝的再利用量与现存可用边角料。"""
        with self.s.lock():
            batches = self.s.execute(
                "SELECT * FROM fabric_batches WHERE ? IS NULL OR id = ? ORDER BY received_at",
                (batch_id, batch_id)).fetchall()
            report = []
            for b in batches:
                reused = self.s.execute(
                    "SELECT COALESCE(SUM(r.area_cm2),0) AS t, COUNT(*) AS n FROM reuse_records r "
                    "JOIN fabric_pieces p ON p.id = r.piece_id WHERE p.batch_id = ?",
                    (b["id"],)).fetchone()
                allocated = self.s.execute(
                    "SELECT COALESCE(SUM(a.area_cm2),0) AS t FROM allocations a "
                    "JOIN fabric_pieces p ON p.id = a.piece_id "
                    "WHERE p.batch_id = ? AND a.status = 'active'", (b["id"],)).fetchone()
                scraps = self.s.execute(
                    "SELECT COALESCE(SUM(length_cm*width_cm),0) AS t FROM fabric_pieces "
                    "WHERE batch_id = ? AND status = 'available'", (b["id"],)).fetchone()
                records = [dict(r) for r in self.s.execute(
                    "SELECT r.* FROM reuse_records r JOIN fabric_pieces p ON p.id = r.piece_id "
                    "WHERE p.batch_id = ? ORDER BY r.created_at", (b["id"],)).fetchall()]
                report.append({
                    "batch_id": b["id"], "code": b["code"], "silk_type": b["silk_type"],
                    "color": b["color"],
                    "reused_cm2": round(reused["t"], 2), "reuse_count": reused["n"],
                    "allocated_to_works_cm2": round(allocated["t"], 2),
                    "remaining_scrap_cm2": round(scraps["t"], 2),
                    "reuse_records": records,
                })
            return {"batches": report}

    def trace_artwork_http(self, payload):
        return self.trace_artwork(payload["artwork_id"])

    def trace_artwork(self, artwork_id):
        """从一件作品查到：所用真丝批次与谱系、经手人、授权状态、费用去向。"""
        with self.s.lock():
            aw = self._require("SELECT * FROM artworks WHERE id = ?", (artwork_id,),
                               "作品不存在")
            event = self._require("SELECT * FROM events WHERE id = ?", (aw["event_id"],),
                                  "场次不存在")
            participant = None
            if aw["participant_id"]:
                participant = self._row("SELECT * FROM participants WHERE id = ?",
                                        (aw["participant_id"],))
                participant = dict(participant) if participant else None

            materials = []
            for link in self.s.execute(
                    "SELECT * FROM artwork_materials WHERE artwork_id = ?",
                    (artwork_id,)).fetchall():
                chain = []
                pid = link["piece_id"]
                while pid:
                    node = self._row("SELECT * FROM fabric_pieces WHERE id = ?", (pid,))
                    if node is None:
                        break
                    chain.append({"piece_id": node["id"], "code": node["code"],
                                  "parent_id": node["parent_id"],
                                  "length_cm": node["length_cm"],
                                  "width_cm": node["width_cm"],
                                  "status": node["status"],
                                  "event_id": node["event_id"]})
                    pid = node["parent_id"]
                batch = self._require(
                    "SELECT b.* FROM fabric_batches b "
                    "JOIN fabric_pieces p ON p.batch_id = b.id WHERE p.id = ?",
                    (link["piece_id"],), "批次缺失")
                handlers = self.s.execute(
                    "SELECT DISTINCT handler_id, handler_name, action, created_at "
                    "FROM piece_movements WHERE piece_id = ? AND handler_id IS NOT NULL "
                    "ORDER BY created_at", (link["piece_id"],)).fetchall()
                alloc = self._row("SELECT * FROM allocations WHERE id = ?",
                                  (link["allocation_id"],))
                materials.append({
                    "area_cm2": link["area_cm2"],
                    "allocation": dict(alloc) if alloc else None,
                    "piece_chain": chain,
                    "handlers": [dict(h) for h in handlers],
                    "batch": {
                        "batch_id": batch["id"], "code": batch["code"],
                        "source": batch["source"], "silk_type": batch["silk_type"],
                        "color": batch["color"], "dyeing_notes": batch["dyeing_notes"],
                        "cleaning_notes": batch["cleaning_notes"],
                    },
                })

            consents = {"artwork_photo": self.list_consents("artwork_photo", artwork_id)}
            if participant:
                consents["customer_story"] = self.list_consents(
                    "customer_story", participant["id"])
                if participant["is_minor"]:
                    consents["minor_image"] = self.list_consents(
                        "minor_image", participant["id"])

            fees = [dict(r) for r in self.s.execute(
                "SELECT * FROM fee_records WHERE artwork_id = ? OR event_id = ? ORDER BY created_at",
                (artwork_id, event["id"])).fetchall()]
            fee_in = sum(f["amount"] for f in fees if f["direction"] == "in")
            fee_out = sum(f["amount"] for f in fees if f["direction"] == "out")

            return {
                "artwork": dict(aw),
                "event": {"event_id": event["id"], "title": event["title"],
                          "start_at": event["start_at"], "status": event["status"],
                          "original_promise": _loads(event["promise_snapshot"], None),
                          "instructor_id": event["instructor_id"],
                          "handoffs": self.list_handoffs(event["id"])},
                "participant": participant,
                "materials": materials,
                "consents": consents,
                "fees": {"records": fees, "total_in": round(fee_in, 2),
                         "total_out": round(fee_out, 2),
                         "net": round(fee_in - fee_out, 2)},
            }

    def event_detail_http(self, payload):
        return self.get_event_detail(payload["event_id"])

    def get_event_detail(self, event_id):
        with self.s.lock():
            event = self._require("SELECT * FROM events WHERE id = ?", (event_id,),
                                  "场次不存在")
            revisions = [dict(r) for r in self.s.execute(
                "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY id",
                (event_id,)).fetchall()]
            participants = [dict(p) for p in self.s.execute(
                "SELECT * FROM participants WHERE event_id = ? ORDER BY created_at",
                (event_id,)).fetchall()]
            for p in participants:
                p["choices"] = _loads(p["choices"], [])
            pieces = [dict(p) for p in self.s.execute(
                "SELECT * FROM fabric_pieces WHERE event_id = ? ORDER BY created_at",
                (event_id,)).fetchall()]
            detail = dict(event)
            detail["promise_snapshot"] = _loads(event["promise_snapshot"], None)
            detail.update({"revisions": revisions, "participants": participants,
                           "pieces": pieces, "handoffs": self.list_handoffs(event_id)})
            detail.update(self.material_sufficiency(event_id))
            return detail

    # ---------- 断网补录批量通道 ----------

    def sync(self, operations):
        """顺序执行离线队列；每条凭 client_ref 幂等，单条失败不影响其他条。"""
        ops = {
            "create_batch": self.create_batch,
            "allocate_material": self.allocate_material,
            "record_artwork": self.record_artwork,
            "reuse_scrap": self.reuse_scrap,
            "record_fee": self.record_fee,
        }
        results = []
        with self.s.lock():
            for index, entry in enumerate(operations):
                op = entry.get("op")
                payload = entry.get("payload") or {}
                ref = entry.get("client_ref") or payload.get("client_ref")
                if ref:
                    payload = {**payload, "client_ref": ref}
                try:
                    if op not in ops:
                        raise DomainError(f"不支持的离线操作: {op}")
                    output = ops[op](payload)
                    results.append({"index": index, "op": op, "client_ref": ref,
                                    "ok": True, "result": output})
                except DomainError as exc:
                    results.append({"index": index, "op": op, "client_ref": ref,
                                    "ok": False, "error": str(exc), "status": exc.status})
        return {"received": len(operations), "results": results}


def sqlite_integrity():
    return sqlite3.IntegrityError

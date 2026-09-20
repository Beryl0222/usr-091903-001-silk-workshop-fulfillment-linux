"""领域规则测试：谱系、幂等补录、改期重算、授权与追溯、报表。"""

import unittest

from domain import Domain, DomainError
from storage import Store

FUTURE = "2027-01-01T00:00:00+00:00"
T1 = "2026-10-01T10:00:00+00:00"
T2 = "2026-10-01T12:00:00+00:00"


class Case(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.d = Domain(self.s)
        self.batch = self.d.create_batch({
            "code": "B-001", "source": "门店旗袍余料", "silk_type": "素绉缎",
            "color": "黛蓝", "dyeing_notes": "不可氯漂", "cleaning_notes": "仅干洗",
            "length_cm": 100, "width_cm": 40, "handler_id": "h1", "handler_name": "王裁"})
        self.inst = self.d.create_instructor({
            "name": "李老师", "qualifications": [{"title": "苏绣指导", "issued_by": "工坊"}],
            "specialties": ["手链", "发簪"]})
        self.plan = self.d.create_plan({
            "title": "真丝手作课", "silk_requirements": {"手链": 60, "发簪": 80, "耳饰": 30},
            "process_script": "先讲缫丝再上手", "payout_rate_per_head": 50})
        self.event = self.d.schedule_event({
            "plan_id": self.plan["plan_id"], "start_at": T1, "end_at": T2,
            "instructor_id": self.inst["instructor_id"]})["event_id"]
        cut = self.d.split_piece({"piece_id": self.batch["root_piece_id"], "cuts": [
            {"length_cm": 50, "width_cm": 40, "event_id": self.event}]})
        self.piece, self.rest = cut["child_piece_ids"]


class FabricLineageTest(Case):
    def test_split_keeps_parent_chain_and_provenance(self):
        chain = []
        pid = self.piece
        while pid:
            row = self.s.execute(
                "SELECT id, parent_id, batch_id FROM fabric_pieces WHERE id = ?",
                (pid,)).fetchone()
            chain.append(dict(row))
            pid = row["parent_id"]
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[-1]["batch_id"], self.batch["batch_id"])
        self.assertIsNone(chain[-1]["parent_id"])

    def test_split_over_area_rejected(self):
        with self.assertRaises(DomainError):
            self.d.split_piece({"piece_id": self.piece, "cuts": [
                {"length_cm": 999, "width_cm": 999}]})

    def test_returned_and_display_retain_source(self):
        self.d.return_to_workshop({"piece_id": self.rest, "note": "退回工坊"})
        row = self.s.execute("SELECT status, parent_id, batch_id FROM fabric_pieces WHERE id = ?",
                             (self.rest,)).fetchone()
        self.assertEqual(row["status"], "returned")
        self.assertIsNotNone(row["parent_id"])
        self.assertEqual(row["batch_id"], self.batch["batch_id"])

    def test_returned_piece_cannot_be_allocated(self):
        self.d.return_to_workshop({"piece_id": self.rest})
        with self.assertRaises(DomainError) as ctx:
            self.d.allocate_material({"event_id": self.event, "piece_id": self.rest,
                                      "area_cm2": 10})
        self.assertEqual(ctx.exception.status, 409)

    def test_convert_to_display(self):
        self.d.convert_to_display({"piece_id": self.rest, "note": "橱窗展示"})
        row = self.s.execute("SELECT status FROM fabric_pieces WHERE id = ?",
                             (self.rest,)).fetchone()
        self.assertEqual(row["status"], "display")

    def test_partial_consume_creates_remainder_child(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        self.d.allocate_material({"event_id": self.event, "participant_id": p,
                                  "piece_id": self.piece, "area_cm2": 60})
        parent = self.s.execute("SELECT status FROM fabric_pieces WHERE id = ?",
                                (self.piece,)).fetchone()
        self.assertEqual(parent["status"], "split")
        child = self.s.execute(
            "SELECT length_cm*width_cm AS area FROM fabric_pieces WHERE parent_id = ? "
            "AND status = 'available'", (self.piece,)).fetchone()
        self.assertAlmostEqual(child["area"], 2000 - 60, places=2)


class IdempotentOfflineTest(Case):
    def test_duplicate_allocation_deducts_once(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        payload = {"client_ref": "tab-7", "event_id": self.event, "participant_id": p,
                   "piece_id": self.piece, "area_cm2": 60, "handler_name": "王裁"}
        first = self.d.allocate_material(payload)
        second = self.d.allocate_material(dict(payload))
        self.assertEqual(first["allocation_id"], second["allocation_id"])
        self.assertTrue(second["replayed"])
        total = self.s.execute(
            "SELECT SUM(area_cm2) AS t FROM allocations WHERE status='active'").fetchone()["t"]
        self.assertEqual(total, 60)
        # 已消耗面积也只算一份
        used = self.s.execute(
            "SELECT COALESCE(SUM(amount_cm2),0) AS t FROM piece_movements "
            "WHERE action='allocate'").fetchone()["t"]
        self.assertEqual(used, 60)

    def test_failed_idempotent_request_not_recorded(self):
        with self.assertRaises(DomainError):
            self.d.allocate_material({"client_ref": "bad-1", "event_id": self.event,
                                      "piece_id": self.piece, "area_cm2": 999999})
        # 同 ref 用合法参数重试应当生效（失败未固化）
        ok = self.d.allocate_material({"client_ref": "bad-1", "event_id": self.event,
                                       "piece_id": self.piece, "area_cm2": 60})
        self.assertNotIn("replayed", ok)

    def test_artwork_client_ref_replays_same_record(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 60})
        payload = {"client_ref": "w1", "event_id": self.event, "participant_id": p,
                   "title": "手链", "craft_type": "手链",
                   "materials": [{"allocation_id": a["allocation_id"], "area_cm2": 60}]}
        first = self.d.record_artwork(payload)
        second = self.d.record_artwork(dict(payload))
        self.assertEqual(first["artwork_id"], second["artwork_id"])
        count = self.s.execute("SELECT COUNT(*) AS c FROM artworks").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_sync_isolates_failures_and_dedups(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        result = self.d.sync([
            {"op": "allocate_material", "client_ref": "s1",
             "payload": {"event_id": self.event, "participant_id": p,
                         "piece_id": self.piece, "area_cm2": 60}},
            {"op": "allocate_material", "client_ref": "s1",
             "payload": {"event_id": self.event, "participant_id": p,
                         "piece_id": self.piece, "area_cm2": 60}},
            {"op": "allocate_material", "client_ref": "s2",
             "payload": {"event_id": self.event, "piece_id": "missing", "area_cm2": 1}},
        ])
        self.assertTrue(result["results"][0]["ok"])
        self.assertTrue(result["results"][1]["result"].get("replayed"))
        self.assertFalse(result["results"][2]["ok"])
        self.assertEqual(result["results"][2]["status"], 404)


class EventRevisionTest(Case):
    def test_reschedule_keeps_original_promise_and_recalculates(self):
        # 另开一场把已划归的裁片转走，制造缺料
        other = self.d.schedule_event({"plan_id": self.plan["plan_id"],
                                       "start_at": "2026-11-01T10:00:00+00:00",
                                       "end_at": "2026-11-01T12:00:00+00:00"})["event_id"]
        self.d.assign_piece({"piece_id": self.piece, "event_id": other})
        self.d.register_participant({"event_id": self.event, "name": "A",
                                     "choices": ["手链"]})
        self.assertEqual(self.d.material_sufficiency(self.event)["shortfall_cm2"], 60.0)
        self.d.assign_piece({"piece_id": self.rest, "event_id": self.event})
        out = self.d.reschedule_event({"event_id": self.event,
                                       "start_at": "2026-10-08T10:00:00+00:00",
                                       "end_at": "2026-10-08T12:00:00+00:00"})
        self.assertEqual(out["original_promise"]["start_at"], T1)
        self.assertTrue(out["sufficient"])
        detail = self.d.get_event_detail(self.event)
        self.assertEqual(detail["status"], "rescheduled")
        kinds = [r["kind"] for r in detail["revisions"]]
        self.assertIn("reschedule", kinds)

    def test_instructor_change_with_handoff(self):
        new = self.d.create_instructor({"name": "王老师"})
        out = self.d.change_instructor({"event_id": self.event,
                                        "instructor_id": new["instructor_id"],
                                        "transfer_process": True, "reason": "原讲师请假"})
        self.assertEqual(out["changes"]["instructor_id"]["from"], self.inst["instructor_id"])
        handoffs = self.d.list_handoffs(self.event)
        self.assertEqual([h["responsibility"] for h in handoffs], ["process"])
        self.assertEqual(handoffs[0]["to_party"], new["instructor_id"])

    def test_cancel_lists_handoffs_and_blocks_new_registration(self):
        self.d.add_handoff({"event_id": self.event, "responsibility": "photo",
                            "from_party": "staff-1", "to_party": "staff-2",
                            "reason": "取消活动，影像责任移交"})
        self.d.cancel_event({"event_id": self.event, "reason": "暴雨"})
        with self.assertRaises(DomainError):
            self.d.register_participant({"event_id": self.event, "name": "X"})

    def test_material_handoff_is_explicitly_recorded(self):
        self.d.add_handoff({"event_id": self.event, "responsibility": "material",
                            "from_party": "李老师", "to_party": "周助教"})
        responsibilities = {h["responsibility"] for h in self.d.list_handoffs(self.event)}
        self.assertEqual(responsibilities, {"material"})


class ConsentTest(Case):
    def _artwork(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 60})
        aw = self.d.record_artwork({"client_ref": "w1", "event_id": self.event,
                                    "participant_id": p, "title": "手链", "craft_type": "手链",
                                    "materials": [{"allocation_id": a["allocation_id"],
                                                   "area_cm2": 60}]})
        return p, aw["artwork_id"]

    def test_minor_requires_guardian(self):
        p = self.d.register_participant({"event_id": self.event, "name": "童",
                                         "is_minor": True, "guardian_name": "家长",
                                         "choices": ["手链"]})["participant_id"]
        with self.assertRaises(DomainError) as ctx:
            self.d.grant_consent({"subject_type": "minor_image", "subject_id": p,
                                  "purpose": "display", "granted_by": "童",
                                  "granted_by_role": "self", "valid_until": FUTURE})
        self.assertEqual(ctx.exception.status, 403)
        grant = self.d.grant_consent({"subject_type": "minor_image", "subject_id": p,
                                      "purpose": "display", "granted_by": "家长",
                                      "granted_by_role": "guardian", "valid_until": FUTURE})
        self.assertTrue(self.d.check_display(
            {"subject_type": "minor_image", "subject_id": p, "purpose": "display"})["allowed"])

    def test_minor_registration_requires_guardian_name(self):
        with self.assertRaises(DomainError):
            self.d.register_participant({"event_id": self.event, "name": "童",
                                         "is_minor": True})

    def test_separate_purpose_and_period(self):
        _p, aw = self._artwork()
        self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                              "purpose": "display", "granted_by": "A",
                              "granted_by_role": "self", "valid_until": FUTURE})
        self.assertFalse(self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw,
             "purpose": "promotion"})["allowed"])
        # 渠道也需匹配
        self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                              "purpose": "promotion", "channel": "store_screen",
                              "granted_by": "A", "granted_by_role": "self",
                              "valid_until": FUTURE})
        self.assertTrue(self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw, "purpose": "promotion",
             "channel": "store_screen"})["allowed"])
        self.assertFalse(self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw, "purpose": "promotion",
             "channel": "web"})["allowed"])

    def test_expired_grant_blocks_new_display(self):
        _p, aw = self._artwork()
        self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                              "purpose": "display", "granted_by": "A",
                              "granted_by_role": "self",
                              "valid_from": "2026-01-01T00:00:00+00:00",
                              "valid_until": "2026-06-01T00:00:00+00:00"})
        decision = self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw, "purpose": "display"})
        self.assertFalse(decision["allowed"])

    def test_withdrawal_stops_new_display_immediately(self):
        _p, aw = self._artwork()
        g = self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                                  "purpose": "display", "granted_by": "A",
                                  "granted_by_role": "self", "valid_until": FUTURE})
        self.assertTrue(self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw,
             "purpose": "display"})["allowed"])
        self.d.withdraw_consent({"consent_id": g["consent_id"], "reason": "顾客反悔"})
        again = self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw, "purpose": "display"})
        self.assertFalse(again["allowed"])
        attempts = self.s.execute(
            "SELECT allowed FROM display_attempts WHERE subject_id = ? ORDER BY id",
            (aw,)).fetchall()
        self.assertEqual([r["allowed"] for r in attempts], [1, 0])

    def test_minor_artwork_needs_both_grants(self):
        p = self.d.register_participant({"event_id": self.event, "name": "童",
                                         "is_minor": True, "guardian_name": "家长",
                                         "choices": ["手链"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 60})
        aw = self.d.record_artwork({"client_ref": "w1", "event_id": self.event,
                                    "participant_id": p, "title": "童作", "craft_type": "手链",
                                    "materials": [{"allocation_id": a["allocation_id"],
                                                   "area_cm2": 60}]})["artwork_id"]
        self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                              "purpose": "display", "granted_by": "家长",
                              "granted_by_role": "guardian", "valid_until": FUTURE})
        decision = self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw, "purpose": "display"})
        self.assertFalse(decision["allowed"])
        self.assertIn("监护人", decision["reason"])
        self.d.grant_consent({"subject_type": "minor_image", "subject_id": p,
                              "purpose": "display", "granted_by": "家长",
                              "granted_by_role": "guardian", "valid_until": FUTURE})
        self.assertTrue(self.d.check_display(
            {"subject_type": "artwork_photo", "subject_id": aw,
             "purpose": "display"})["allowed"])


class TraceAndReportTest(Case):
    def _full_event(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 60, "handler_id": "h1",
                                      "handler_name": "王裁"})
        aw = self.d.record_artwork({"client_ref": "w1", "event_id": self.event,
                                    "participant_id": p, "title": "A 的手链",
                                    "craft_type": "手链",
                                    "materials": [{"allocation_id": a["allocation_id"],
                                                   "area_cm2": 60}]})["artwork_id"]
        self.d.grant_consent({"subject_type": "artwork_photo", "subject_id": aw,
                              "purpose": "display", "granted_by": "A",
                              "granted_by_role": "self", "valid_until": FUTURE})
        self.d.record_fee({"event_id": self.event, "artwork_id": aw,
                           "category": "participant_fee", "direction": "in", "amount": 120})
        self.d.record_fee({"event_id": self.event, "category": "material_cost",
                           "direction": "out", "amount": 20})
        return p, aw

    def test_trace_shows_silk_handlers_consent_and_fees(self):
        _p, aw = self._full_event()
        trace = self.d.trace_artwork(aw)
        batch = trace["materials"][0]["batch"]
        self.assertEqual(batch["code"], "B-001")
        self.assertEqual(batch["dyeing_notes"], "不可氯漂")
        self.assertEqual(batch["cleaning_notes"], "仅干洗")
        self.assertGreaterEqual(len(trace["materials"][0]["piece_chain"]), 2)
        self.assertEqual(trace["fees"]["net"], 100)
        self.assertEqual(trace["consents"]["artwork_photo"][0]["status"], "granted")
        self.assertEqual(trace["event"]["original_promise"]["start_at"], T1)
        handlers = {h["handler_id"] for h in trace["materials"][0]["handlers"]}
        self.assertIn("h1", handlers)

    def test_payout_report(self):
        self.d.register_participant({"event_id": self.event, "name": "A",
                                     "choices": ["手链"]})
        self.d.register_participant({"event_id": self.event, "name": "B",
                                     "choices": ["发簪"]})
        self.d.record_fee({"event_id": self.event, "category": "instructor_payout",
                           "direction": "out", "amount": 50})
        report = self.d.payout_report(self.event)
        self.assertEqual(report["headcount"], 2)
        self.assertEqual(report["payable"], 100)
        self.assertEqual(report["outstanding"], 50)

    def test_reuse_report_quantifies_scrap_reuse(self):
        self.d.reuse_scrap({"client_ref": "r1", "piece_id": self.rest,
                            "area_cm2": 100, "product": "耳饰样品",
                            "recorded_by": "设计师陈"})
        leftover = self.s.execute(
            "SELECT id FROM fabric_pieces WHERE parent_id = ? AND status = 'available'",
            (self.rest,)).fetchone()["id"]
        self.d.reuse_scrap({"client_ref": "r2", "piece_id": leftover,
                            "area_cm2": 50, "product": "拼布小样"})
        report = self.d.reuse_report(self.batch["batch_id"])["batches"][0]
        self.assertEqual(report["reused_cm2"], 150)
        self.assertEqual(report["reuse_count"], 2)
        self.assertGreater(report["remaining_scrap_cm2"], 0)

    def test_artwork_choice_must_match_participant_selection(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["耳饰"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 30})
        with self.assertRaises(DomainError):
            self.d.record_artwork({"client_ref": "w1", "event_id": self.event,
                                   "participant_id": p, "title": "手链", "craft_type": "手链",
                                   "materials": [{"allocation_id": a["allocation_id"],
                                                  "area_cm2": 30}]})

    def test_allocation_cannot_exceed_piece_area(self):
        with self.assertRaises(DomainError):
            self.d.allocate_material({"event_id": self.event, "piece_id": self.piece,
                                      "area_cm2": 10000})

    def test_reverse_used_allocation_blocked_but_fresh_one_restores(self):
        p = self.d.register_participant({"event_id": self.event, "name": "A",
                                         "choices": ["手链"]})["participant_id"]
        a = self.d.allocate_material({"client_ref": "a1", "event_id": self.event,
                                      "participant_id": p, "piece_id": self.piece,
                                      "area_cm2": 60})
        self.d.record_artwork({"client_ref": "w1", "event_id": self.event,
                               "participant_id": p, "title": "x", "craft_type": "手链",
                               "materials": [{"allocation_id": a["allocation_id"],
                                              "area_cm2": 60}]})
        with self.assertRaises(DomainError):
            self.d.reverse_allocation({"allocation_id": a["allocation_id"]})

        # 未用于作品的领料可冲销，并把面积作为新余料子裁片补回
        fresh = self.d.allocate_material({"client_ref": "a2", "event_id": self.event,
                                          "participant_id": p, "piece_id": self.rest,
                                          "area_cm2": 30})
        out = self.d.reverse_allocation({"allocation_id": fresh["allocation_id"]})
        self.assertEqual(out["restored_cm2"], 30)
        restored = self.s.execute("SELECT status, parent_id FROM fabric_pieces WHERE id = ?",
                                  (out["restored_piece_id"],)).fetchone()
        self.assertEqual(restored["status"], "available")
        self.assertEqual(restored["parent_id"], self.rest)


if __name__ == "__main__":
    unittest.main()

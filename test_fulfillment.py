"""体验履约的领域流程与 HTTP 接口测试。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from silkworkshop import domain
from silkworkshop.errors import Conflict, ConsentError, ValidationError
from silkworkshop.store import STORE

FUTURE = "2099-01-01T00:00:00+00:00"
FAR_FUTURE = "2099-06-01T00:00:00+00:00"
T1 = "2026-10-01T14:00:00+00:00"
T2 = "2026-10-08T14:00:00+00:00"


def make_batch(total=1200, designer="designer-mei"):
    return domain.register_batch(
        name="云锦真丝",
        source_order="QIPAO-1024",
        designer_id=designer,
        silk_type="素绉缎",
        total_area_cm2=total,
        dye_notes="低温固色，避免暴晒",
        cleaning_notes="不可机洗，冷水手洗",
        reuse_value_per_cm2_pence=3,
        actor="clerk-1",
    )


def make_plan(**overrides):
    params = dict(
        title="真丝手链体验",
        craft_type="手链",
        material_per_participant_cm2=100,
        briefing="盘扣与滚边基础讲解",
        required_qualifications=["silk-craft", "teaching"],
        fee_pence=4500,
        instructor_share_percent=40,
        actor="ops",
    )
    params.update(overrides)
    return domain.create_plan(**params)


def make_instructor(name="林老师", quals=("silk-craft", "teaching")):
    return domain.register_instructor(name=name, qualifications=list(quals), actor="ops")


class DomainTest(unittest.TestCase):
    def setUp(self):
        STORE.reset()

    def _session_with_material(self, area=400, participants=1):
        """批次→拆分→方案→讲师→场次→预留→报名→领料。"""
        batch = make_batch()
        root = batch["root_piece"]["id"]
        split = domain.split_piece(root, [{"label": "手链长条", "area_cm2": area}], actor="clerk-1")
        piece = split["children"][0]["id"]
        remainder = split["children"][1]["id"]  # 拆分自动生成的余料子片
        plan = make_plan()
        instructor = make_instructor()
        session = domain.schedule_session(
            plan_id=plan["plan"]["id"], instructor_id=instructor["instructor"]["id"],
            starts_at=T1, capacity=8, actor="ops",
        )
        session_id = session["session"]["id"]
        domain.reserve_pieces(session_id, [piece], actor="clerk-1")
        enrolled = []
        for index in range(participants):
            result = domain.enroll(session_id, participant={"name": f"顾客{index + 1}"}, actor="ops")
            enrolled.append(result["participant"]["id"])
        domain.issue_materials(session_id, actor="clerk-2")
        return {
            "batch": batch, "piece": piece, "remainder": remainder, "plan": plan,
            "instructor": instructor, "session_id": session_id, "participants": enrolled,
        }

    # ---------------------------------------------------------- 材料与来源

    def test_split_conserves_area_and_provenance(self):
        batch = make_batch()
        root = batch["root_piece"]["id"]
        result = domain.split_piece(
            root,
            [{"label": "手链长条", "area_cm2": 400}, {"label": "发簪片", "area_cm2": 300}],
            actor="clerk-1",
        )
        children = result["children"]
        # 400 + 300 + 自动余料 500 = 1200，面积守恒
        self.assertEqual(sorted(c["area_cm2"] for c in children), [300, 400, 500])
        for child in children:
            self.assertEqual(child["batch_id"], batch["batch"]["id"])
            self.assertEqual(child["parent_id"], root)
        self.assertEqual(STORE.pieces[root].status, "split")
        detail = domain.get_piece(children[0]["id"])
        self.assertEqual(detail["chain"][-1]["piece_id"], root)
        self.assertEqual(detail["batch"]["source_order"], "QIPAO-1024")

    def test_split_rejects_oversize_and_non_stock(self):
        batch = make_batch()
        root = batch["root_piece"]["id"]
        with self.assertRaises(ValidationError):
            domain.split_piece(root, [{"label": "超大", "area_cm2": 9999}], actor="clerk-1")
        domain.split_piece(root, [{"label": "A", "area_cm2": 100}], actor="clerk-1")
        with self.assertRaises(Conflict):
            domain.split_piece(root, [{"label": "B", "area_cm2": 100}], actor="clerk-1")

    def test_return_and_display_keep_provenance(self):
        ctx = self._session_with_material()
        piece = ctx["piece"]
        domain.return_to_workshop([piece], actor="clerk-3", note="场次余料退回")
        self.assertEqual(STORE.pieces[piece].status, "returned")
        detail = domain.get_piece(piece)
        kinds = [m["kind"] for m in detail["movements"]]
        self.assertIn("return_workshop", kinds)
        self.assertEqual(detail["batch"]["id"], ctx["batch"]["batch"]["id"])
        # 另一片改作展示品（从余料子片再拆）
        split = domain.split_piece(ctx["remainder"], [{"label": "展示样", "area_cm2": 50}], actor="c")
        sample = split["children"][0]["id"]
        domain.convert_to_display([sample], actor="clerk-3")
        self.assertEqual(STORE.pieces[sample].status, "display")
        report = domain.reuse_report(designer_id="designer-mei")
        row = report["rows"][0]
        self.assertEqual(row["returned_to_workshop_cm2"], 400)
        self.assertEqual(row["display_cm2"], 50)

    # ---------------------------------------------------------- 幂等与补录

    def test_issue_replay_same_key_no_double_deduct(self):
        ctx = self._session_with_material()
        session_id = ctx["session_id"]
        # 场次里已领过一次；再补一块料验证幂等
        split = domain.split_piece(ctx["remainder"], [{"label": "补料", "area_cm2": 100}], actor="c")
        extra = split["children"][0]["id"]
        domain.reserve_pieces(session_id, [extra], actor="clerk-1")
        first = domain.issue_materials(session_id, actor="clerk-2", idempotency_key="offline-issue-1")
        issues_before = [m for m in STORE.movements if m.kind == "issue"]
        second = domain.issue_materials(session_id, actor="clerk-2", idempotency_key="offline-issue-1")
        issues_after = [m for m in STORE.movements if m.kind == "issue"]
        self.assertTrue(second["replayed"])
        self.assertEqual(len(issues_before), len(issues_after))
        self.assertEqual(STORE.pieces[extra].status, "issued")
        self.assertEqual(first["issued"], second["issued"])

    def test_record_work_replay_and_offline_ref_no_double_consume(self):
        ctx = self._session_with_material()
        session_id, participant = ctx["session_id"], ctx["participants"][0]
        piece = ctx["piece"]
        first = domain.record_work(
            session_id, participant, [{"piece_id": piece}], actor="clerk-2",
            title="云锦手链", offline_ref="pad-offline-7", idempotency_key="work-key-1",
        )
        consumes_before = [m for m in STORE.movements if m.kind == "consume"]
        # 断网重试：同一幂等键
        second = domain.record_work(
            session_id, participant, [{"piece_id": piece}], actor="clerk-2",
            title="云锦手链", offline_ref="pad-offline-7", idempotency_key="work-key-1",
        )
        # 换了幂等键但同一补录凭据
        third = domain.record_work(
            session_id, participant, [{"piece_id": piece}], actor="clerk-2",
            title="云锦手链", offline_ref="pad-offline-7", idempotency_key="work-key-2",
        )
        consumes_after = [m for m in STORE.movements if m.kind == "consume"]
        self.assertEqual(len(consumes_before), len(consumes_after))
        self.assertEqual(first["work"]["id"], second["work"]["id"])
        self.assertEqual(first["work"]["id"], third["work"]["id"])
        self.assertTrue(second["replayed"])
        self.assertTrue(third["replayed"])
        self.assertEqual(len(STORE.works), 1)

    def test_partial_consume_creates_remainder_with_provenance(self):
        ctx = self._session_with_material()
        piece = ctx["piece"]
        result = domain.record_work(
            ctx["session_id"], ctx["participants"][0],
            [{"piece_id": piece, "area_cm2": 150}], actor="clerk-2", title="手链",
        )
        usage = result["work"]["piece_usages"][0]
        used = STORE.pieces[usage["piece_id"]]
        self.assertEqual(used.status, "consumed")
        self.assertEqual(used.parent_id, piece)
        self.assertEqual(STORE.pieces[piece].status, "split")
        remainder = [
            p for p in STORE.pieces.values()
            if p.parent_id == piece and p.status == "issued"
        ]
        self.assertEqual(len(remainder), 1)
        self.assertEqual(remainder[0].area_cm2, 250)
        self.assertEqual(remainder[0].location, ctx["session_id"])

    # ---------------------------------------------------------- 换老师 / 取消 / 改期

    def test_reassign_requires_qualification_and_records_handover(self):
        ctx = self._session_with_material()
        session_id = ctx["session_id"]
        unqualified = make_instructor(name="实习生", quals=("teaching",))
        with self.assertRaises(Conflict):
            domain.reassign_instructor(session_id, unqualified["instructor"]["id"], actor="ops")
        # 给参与者一份授权，交接单应列出待跟进授权
        participant = ctx["participants"][0]
        domain.grant_consent(
            subject_type="participant", subject_id=participant, category="customer_story",
            purpose="in_store_display", granted_by="顾客1", granted_by_role="self", expires_at=FUTURE,
        )
        qualified = make_instructor(name="周老师")
        result = domain.reassign_instructor(
            session_id, qualified["instructor"]["id"], actor="ops", reason="林老师临时请假",
        )
        handover = result["handover"]
        self.assertEqual(handover["from_instructor_id"], ctx["instructor"]["instructor"]["id"])
        self.assertEqual(handover["briefing_version"], 1)
        self.assertEqual(len(handover["materials"]), 1)
        self.assertEqual(len(handover["consent_ids"]), 1)
        self.assertEqual(STORE.sessions[session_id].instructor_id, qualified["instructor"]["id"])
        self.assertEqual(STORE.pieces[ctx["piece"]].holder, qualified["instructor"]["id"])

    def test_cancel_records_takeover_and_dispositions(self):
        ctx = self._session_with_material()
        session_id = ctx["session_id"]
        with self.assertRaises(ValidationError):
            domain.cancel_session(session_id, actor="ops", takeover={})
        other = domain.schedule_session(
            plan_id=ctx["plan"]["plan"]["id"], instructor_id=ctx["instructor"]["instructor"]["id"],
            starts_at=T2, capacity=4, actor="ops",
        )["session"]["id"]
        split = domain.split_piece(ctx["remainder"], [{"label": "耳饰片", "area_cm2": 80}], actor="c")
        extra = split["children"][0]["id"]
        domain.reserve_pieces(session_id, [extra], actor="clerk-1")
        result = domain.cancel_session(
            session_id, actor="ops",
            takeover={"materials_to": "clerk-9", "briefing_to": "周老师", "consents_to": "门店经理"},
            dispositions={ctx["piece"]: "return_to_workshop", extra: f"transfer:{other}"},
            reason="讲师临时缺席",
        )
        session = result["session"]
        self.assertEqual(session["status"], "cancelled")
        self.assertEqual(session["takeover"]["materials_to"], "clerk-9")
        self.assertEqual(STORE.pieces[ctx["piece"]].status, "returned")
        moved = STORE.pieces[extra]
        self.assertEqual((moved.status, moved.location), ("reserved", other))

    def test_reschedule_preserves_commitments_and_flags_shortfall(self):
        ctx = self._session_with_material(participants=2)
        session_id = ctx["session_id"]
        # 两人需 200cm²，再退回 300cm² 到工坊，只剩 100cm² 可用
        split = domain.split_piece(ctx["remainder"], [{"label": "备料", "area_cm2": 300}], actor="c")
        backup = split["children"][0]["id"]
        domain.reserve_pieces(session_id, [backup], actor="clerk-1")
        domain.return_to_workshop([backup], actor="clerk-3")
        result = domain.reschedule_session(session_id, T2, actor="ops", reason="商场活动冲突")
        # 原承诺保留
        self.assertEqual(result["commitments"]["starts_at"], T1)
        self.assertEqual(result["commitments"]["fee_pence"], 4500)
        self.assertEqual(result["session"]["starts_at"], T2)
        self.assertEqual(result["session"]["status"], "rescheduled")
        # 重新核算：400 领用仍在，需求 200，足够；退回的 300 列入失效预留
        sufficiency = result["sufficiency"]
        self.assertEqual(sufficiency["required_cm2"], 200)
        self.assertTrue(sufficiency["sufficient"])
        self.assertEqual(sufficiency["broken_reservations"][0]["piece_id"], backup)
        # 将领用料也退回，制造缺口并给出替代建议
        domain.return_to_workshop([ctx["piece"]], actor="clerk-3")
        again = domain.reschedule_session(session_id, "2026-10-15T14:00:00+00:00", actor="ops")
        self.assertFalse(again["sufficiency"]["sufficient"])
        self.assertEqual(again["sufficiency"]["shortfall_cm2"], 200)
        self.assertTrue(again["sufficiency"]["suggestions"])

    # ---------------------------------------------------------- 授权

    def test_consent_guardian_expiry_purpose_and_withdrawal(self):
        minor = domain.register_participant(
            name="小琪", is_minor=True, guardian_name="琪妈妈", actor="ops",
        )["participant"]
        with self.assertRaises(ConsentError):
            domain.grant_consent(
                subject_type="participant", subject_id=minor["id"], category="minor_imagery",
                purpose="social_media", granted_by="店员", granted_by_role="staff", expires_at=FUTURE,
            )
        consent = domain.grant_consent(
            subject_type="participant", subject_id=minor["id"], category="minor_imagery",
            purpose="social_media", granted_by="琪妈妈", granted_by_role="guardian", expires_at=FUTURE,
        )["consent"]
        # 用途不符：店内展示不在已取得的用途中
        with self.assertRaises(ConsentError):
            domain.publish(
                subject_type="participant", subject_id=minor["id"], category="minor_imagery",
                purpose="in_store_display", channel="store",
            )
        # 期限之外
        with self.assertRaises(ConsentError):
            domain.publish(
                subject_type="participant", subject_id=minor["id"], category="minor_imagery",
                purpose="social_media", channel="weibo", at=FAR_FUTURE,
            )
        publication = domain.publish(
            subject_type="participant", subject_id=minor["id"], category="minor_imagery",
            purpose="social_media", channel="weibo",
        )["publication"]
        withdrawn = domain.withdraw_consent(consent["id"], actor="ops")
        self.assertEqual(withdrawn["consent"]["status"], "withdrawn")
        self.assertEqual(withdrawn["affected_publications"][0]["id"], publication["id"])
        # 撤回后停止新的展示
        with self.assertRaises(ConsentError):
            domain.publish(
                subject_type="participant", subject_id=minor["id"], category="minor_imagery",
                purpose="social_media", channel="weibo",
            )

    def test_consent_rejects_past_expiry(self):
        participant = domain.register_participant(name="老顾客", actor="ops")["participant"]
        with self.assertRaises(ValidationError):
            domain.grant_consent(
                subject_type="participant", subject_id=participant["id"], category="customer_story",
                purpose="in_store_display", granted_by="老顾客", granted_by_role="self",
                expires_at="2020-01-01T00:00:00+00:00",
            )

    # ---------------------------------------------------------- 追溯与费用

    def test_trace_work_shows_silk_handlers_consents_and_fees(self):
        ctx = self._session_with_material()
        session_id, participant = ctx["session_id"], ctx["participants"][0]
        domain.grant_consent(
            subject_type="participant", subject_id=participant, category="customer_story",
            purpose="in_store_display", granted_by="顾客1", granted_by_role="self", expires_at=FUTURE,
        )
        work = domain.record_work(
            session_id, participant, [{"piece_id": ctx["piece"], "area_cm2": 100}],
            actor="clerk-2", title="云锦手链",
        )["work"]
        domain.complete_session(session_id, actor="ops")
        domain.settle_session(session_id, actor="ops")
        trace = domain.trace_work(work["id"])
        # 所用真丝：批次与注意事项
        material = trace["materials"][0]
        self.assertEqual(material["batch"]["id"], ctx["batch"]["batch"]["id"])
        self.assertEqual(material["batch"]["dye_notes"], "低温固色，避免暴晒")
        self.assertEqual(material["batch"]["source_order"], "QIPAO-1024")
        self.assertEqual(material["chain"][-1]["piece_id"], ctx["batch"]["root_piece"]["id"])
        # 经手人：登记、拆分、预留、领料、消耗
        kinds = {h["kind"] for h in trace["handlers"]}
        self.assertTrue({"register", "split", "reserve", "issue", "consume"} <= kinds)
        # 授权状态
        self.assertEqual(trace["consents"][0]["status"], "active")
        # 费用去向：报名费 + 讲师报酬 + 材料再利用 + 门店留存
        fee_kinds = {f["kind"] for f in trace["fees"]}
        self.assertTrue({"participant_fee", "instructor_payable", "material_reuse", "ops_remainder"} <= fee_kinds)
        settlement = trace["settlement"]
        self.assertEqual(settlement["collected_pence"], 4500)
        self.assertEqual(settlement["instructor_payable_pence"], 1800)
        self.assertEqual(settlement["material_reuse_pence"], 300)  # 100cm² × 3 便士
        self.assertEqual(settlement["ops_remainder_pence"], 2400)

    def test_settlement_is_idempotent_and_pay_report(self):
        ctx = self._session_with_material()
        session_id = ctx["session_id"]
        domain.record_work(session_id, ctx["participants"][0], [{"piece_id": ctx["piece"]}], actor="c")
        domain.complete_session(session_id, actor="ops")
        first = domain.settle_session(session_id, actor="ops")
        fees_before = len(STORE.fees)
        second = domain.settle_session(session_id, actor="ops")
        self.assertEqual(len(STORE.fees), fees_before)
        self.assertEqual(first["instructor_payable_pence"], second["instructor_payable_pence"])
        instructor_id = ctx["instructor"]["instructor"]["id"]
        report = domain.instructor_pay_report(instructor_id)
        self.assertEqual(report["payable_pence"], 1800)
        self.assertEqual(report["outstanding_pence"], 1800)
        domain.record_payout(instructor_id, 1000, actor="finance")
        report = domain.instructor_pay_report(instructor_id)
        self.assertEqual(report["paid_pence"], 1000)
        self.assertEqual(report["outstanding_pence"], 800)
        with self.assertRaises(Conflict):
            domain.record_payout(instructor_id, 900, actor="finance")

    def test_cross_session_split_keeps_shared_source(self):
        """同一块面料拆到不同场次，两边作品都能追溯回同一批次。"""
        batch = make_batch()
        root = batch["root_piece"]["id"]
        split = domain.split_piece(
            root, [{"label": "甲场料", "area_cm2": 200}, {"label": "乙场料", "area_cm2": 200}], actor="c",
        )
        piece_a, piece_b = split["children"][0]["id"], split["children"][1]["id"]
        plan = make_plan()
        instructor = make_instructor()
        works = []
        for piece, starts in ((piece_a, T1), (piece_b, T2)):
            session = domain.schedule_session(
                plan_id=plan["plan"]["id"], instructor_id=instructor["instructor"]["id"],
                starts_at=starts, capacity=4, actor="ops",
            )["session"]
            domain.reserve_pieces(session["id"], [piece], actor="c")
            enrolled = domain.enroll(session["id"], participant={"name": "顾客"}, actor="ops")
            domain.issue_materials(session["id"], actor="c")
            work = domain.record_work(
                session["id"], enrolled["participant"]["id"], [{"piece_id": piece}], actor="c",
            )["work"]
            works.append(work)
        for work in works:
            trace = domain.trace_work(work["id"])
            self.assertEqual(trace["materials"][0]["batch"]["id"], batch["batch"]["id"])
        report = domain.reuse_report(designer_id="designer-mei")
        self.assertEqual(report["rows"][0]["reused_in_works_cm2"], 400)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        from service import Handler

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        STORE.reset()

    def _post(self, path, payload, headers=None):
        request = UrlRequest(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_unknown_route_404(self):
        status, payload = self._get("/nothing-here")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_validation_error_maps_to_422(self):
        status, payload = self._post("/batches", {
            "name": "坏批次", "source_order": "X", "designer_id": "d",
            "silk_type": "缎", "total_area_cm2": -5,
        })
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_full_flow_over_http_with_idempotency_header(self):
        status, batch = self._post("/batches", {
            "name": "织锦缎", "source_order": "QIPAO-2048", "designer_id": "designer-li",
            "silk_type": "织锦缎", "total_area_cm2": 600,
            "dye_notes": "固色低温", "cleaning_notes": "干洗优先",
            "reuse_value_per_cm2_pence": 2, "actor": "clerk-1",
        })
        self.assertEqual(status, 201)
        root = batch["data"]["root_piece"]["id"]
        status, split = self._post(f"/pieces/{root}/split", {
            "parts": [{"label": "发簪片", "area_cm2": 200}], "actor": "clerk-1",
        })
        self.assertEqual(status, 200)
        piece = split["data"]["children"][0]["id"]
        _, plan = self._post("/plans", {
            "title": "真丝发簪体验", "craft_type": "发簪", "material_per_participant_cm2": 100,
            "briefing": "发簪工艺", "required_qualifications": ["silk-craft"],
            "fee_pence": 6000, "instructor_share_percent": 50,
        })
        _, instructor = self._post("/instructors", {"name": "沈老师", "qualifications": ["silk-craft"]})
        status, session = self._post("/sessions", {
            "plan_id": plan["data"]["plan"]["id"], "instructor_id": instructor["data"]["instructor"]["id"],
            "starts_at": T1, "capacity": 6,
        })
        self.assertEqual(status, 201)
        session_id = session["data"]["session"]["id"]
        self._post(f"/sessions/{session_id}/reserve", {"piece_ids": [piece], "actor": "clerk-1"})
        _, enrolled = self._post(f"/sessions/{session_id}/enroll", {"participant": {"name": "顾客甲"}})
        participant = enrolled["data"]["participant"]["id"]
        # 断网补录领料：同一幂等键重放不重复扣减
        headers = {"Idempotency-Key": "pad-issue-42"}
        _, first = self._post(f"/sessions/{session_id}/issue", {"actor": "clerk-2"}, headers)
        _, second = self._post(f"/sessions/{session_id}/issue", {"actor": "clerk-2"}, headers)
        self.assertFalse(first["data"]["replayed"])
        self.assertTrue(second["data"]["replayed"])
        _, work = self._post(f"/sessions/{session_id}/works", {
            "participant_id": participant, "piece_usages": [{"piece_id": piece, "area_cm2": 120}],
            "actor": "clerk-2", "title": "织锦发簪", "offline_ref": "pad-1",
        })
        work_id = work["data"]["work"]["id"]
        status, trace = self._get(f"/works/{work_id}/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["data"]["materials"][0]["batch"]["name"], "织锦缎")
        self.assertEqual(trace["data"]["materials"][0]["area_cm2"], 120)

    def test_consent_denied_over_http(self):
        _, participant = self._post("/participants", {"name": "游客"})
        participant_id = participant["data"]["participant"]["id"]
        status, payload = self._post("/publications", {
            "subject_type": "participant", "subject_id": participant_id,
            "category": "customer_story", "purpose": "social_media", "channel": "weibo",
        })
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "consent_denied")


if __name__ == "__main__":
    unittest.main()

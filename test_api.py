"""HTTP 合约测试：健康检查保持原合约，领域接口走 JSON，错误带状态码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload, make_app


class ServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.store, cls.domain, handler_cls = make_app()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.store.close()

    def call(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            body = json.loads(error.read().decode("utf-8"))
            return error.code, body


class HealthContractTest(ServerMixin, unittest.TestCase):
    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, health_payload())

    def test_unknown_route_is_not_exposed(self):
        status, body = self.call("GET", "/unknown")
        self.assertEqual(status, 404)


class ApiFlowTest(ServerMixin, unittest.TestCase):
    def test_full_workflow_over_http(self):
        _, batch = self.call("POST", "/batches", {
            "code": "B-HTTP-1", "source": "旗袍余料", "silk_type": "素绉缎",
            "length_cm": 100, "width_cm": 40, "dyeing_notes": "不可氯漂",
            "cleaning_notes": "干洗", "handler_name": "王裁"})
        _, inst = self.call("POST", "/instructors", {"name": "李老师"})
        _, plan = self.call("POST", "/plans", {
            "title": "手链课", "silk_requirements": {"手链": 60},
            "payout_rate_per_head": 40})
        _, event = self.call("POST", "/events", {
            "plan_id": plan["plan_id"],
            "start_at": "2026-10-01T10:00:00+00:00",
            "end_at": "2026-10-01T12:00:00+00:00",
            "instructor_id": inst["instructor_id"]})
        eid = event["event_id"]

        _, split = self.call("POST", "/pieces/split", {
            "piece_id": batch["root_piece_id"],
            "cuts": [{"length_cm": 50, "width_cm": 40, "event_id": eid}]})
        _, listed = self.call("GET", "/batches")
        self.assertIn("B-HTTP-1", [b["code"] for b in listed["batches"]])

        _, person = self.call("POST", "/participants", {
            "event_id": eid, "name": "小明", "is_minor": True,
            "guardian_name": "大明", "choices": ["手链"]})
        status, suff = self.call("GET", f"/events/{eid}/sufficiency")
        self.assertEqual(status, 200)
        self.assertTrue(suff["sufficient"])

        _, alloc = self.call("POST", "/allocations", {
            "client_ref": "tab-100", "event_id": eid,
            "participant_id": person["participant_id"],
            "piece_id": split["child_piece_ids"][0], "area_cm2": 60,
            "handler_name": "王裁"})
        # 断网重放：同一 client_ref
        _, replay = self.call("POST", "/allocations", {
            "client_ref": "tab-100", "event_id": eid,
            "participant_id": person["participant_id"],
            "piece_id": split["child_piece_ids"][0], "area_cm2": 60})
        self.assertEqual(alloc["allocation_id"], replay["allocation_id"])
        self.assertTrue(replay["replayed"])

        _, artwork = self.call("POST", "/artworks", {
            "client_ref": "art-1", "event_id": eid,
            "participant_id": person["participant_id"], "title": "小明手链",
            "craft_type": "手链",
            "materials": [{"allocation_id": alloc["allocation_id"], "area_cm2": 60}]})

        # 授权：先拒（作品照 + 未成年人均未授权）
        _, decision0 = self.call("POST", "/displays/check", {
            "subject_type": "artwork_photo", "subject_id": artwork["artwork_id"],
            "purpose": "display"})
        self.assertFalse(decision0["allowed"])

        _, c_art = self.call("POST", "/consents", {
            "subject_type": "artwork_photo", "subject_id": artwork["artwork_id"],
            "purpose": "display", "granted_by": "大明", "granted_by_role": "guardian",
            "valid_until": "2027-01-01T00:00:00+00:00"})
        _, decision1 = self.call("POST", "/displays/check", {
            "subject_type": "artwork_photo", "subject_id": artwork["artwork_id"],
            "purpose": "display"})
        self.assertFalse(decision1["allowed"], "仅作品照授权时仍应拦截（缺未成年人影像）")

        _, c_minor = self.call("POST", "/consents", {
            "subject_type": "minor_image", "subject_id": person["participant_id"],
            "purpose": "display", "granted_by": "大明", "granted_by_role": "guardian",
            "valid_until": "2027-01-01T00:00:00+00:00"})
        _, decision2 = self.call("POST", "/displays/check", {
            "subject_type": "artwork_photo", "subject_id": artwork["artwork_id"],
            "purpose": "display"})
        self.assertTrue(decision2["allowed"])

        self.call("POST", "/consents/withdraw", {"consent_id": c_minor["consent_id"]})
        _, decision3 = self.call("POST", "/displays/check", {
            "subject_type": "artwork_photo", "subject_id": artwork["artwork_id"],
            "purpose": "display"})
        self.assertFalse(decision3["allowed"], "撤回后必须停止新展示")

        _, fee = self.call("POST", "/fees", {
            "event_id": eid, "artwork_id": artwork["artwork_id"],
            "category": "participant_fee", "direction": "in", "amount": 120})
        self.assertEqual(fee["amount"], 120)

        _, trace = self.call("GET", f"/artworks/{artwork['artwork_id']}/trace")
        self.assertEqual(trace["materials"][0]["batch"]["code"], "B-HTTP-1")
        self.assertEqual(trace["fees"]["total_in"], 120)
        self.assertIn("minor_image", trace["consents"])

        _, payout = self.call("GET", f"/events/{eid}/payout")
        self.assertEqual(payout["payable"], 40)

    def test_sync_batch_endpoint(self):
        _, batch = self.call("POST", "/batches", {"code": "B-SYNC", "length_cm": 10,
                                                   "width_cm": 10})
        _, plan = self.call("POST", "/plans", {"title": "x",
                                               "silk_requirements": {"手链": 10}})
        _, event = self.call("POST", "/events", {
            "plan_id": plan["plan_id"], "start_at": "2026-10-01T10:00:00+00:00",
            "end_at": "2026-10-01T12:00:00+00:00"})
        _, synced = self.call("POST", "/sync", {"operations": [
            {"op": "record_fee", "client_ref": "f1",
             "payload": {"event_id": event["event_id"], "category": "material_cost",
                         "direction": "out", "amount": 5}},
            {"op": "record_fee", "client_ref": "f1",
             "payload": {"event_id": event["event_id"], "category": "material_cost",
                         "direction": "out", "amount": 5}},
            {"op": "allocate_material", "client_ref": "f2",
             "payload": {"event_id": event["event_id"],
                         "piece_id": batch["root_piece_id"], "area_cm2": 1000}},
        ]})
        self.assertEqual(synced["received"], 3)
        self.assertTrue(synced["results"][0]["ok"])
        self.assertTrue(synced["results"][1]["result"]["replayed"])
        self.assertFalse(synced["results"][2]["ok"])

    def test_reschedule_reports_shortfall_over_http(self):
        _, batch = self.call("POST", "/batches", {"code": "B-RS", "length_cm": 10,
                                                   "width_cm": 10})
        _, plan = self.call("POST", "/plans", {"title": "x",
                                               "silk_requirements": {"手链": 200}})
        _, event = self.call("POST", "/events", {
            "plan_id": plan["plan_id"], "start_at": "2026-10-01T10:00:00+00:00",
            "end_at": "2026-10-01T12:00:00+00:00"})
        self.call("POST", f"/pieces/assign",
                  {"piece_id": batch["root_piece_id"], "event_id": event["event_id"]})
        self.call("POST", "/participants", {"event_id": event["event_id"],
                                            "name": "A", "choices": ["手链"]})
        _, out = self.call("POST", f"/events/{event['event_id']}/reschedule",
                           {"start_at": "2026-10-09T10:00:00+00:00",
                            "end_at": "2026-10-09T12:00:00+00:00"})
        self.assertEqual(out["original_promise"]["start_at"],
                         "2026-10-01T10:00:00+00:00")
        self.assertFalse(out["sufficient"])
        self.assertEqual(out["shortfall_cm2"], 100.0)

    def test_validation_errors_have_status_codes(self):
        status, body = self.call("POST", "/batches", {"length_cm": 10})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, _ = self.call("POST", "/allocations", {"event_id": "x",
                                                        "piece_id": "y", "area_cm2": 1})
        self.assertEqual(status, 404)

    def test_bad_json_returns_400(self):
        req = Request(f"{self.base_url}/batches", data=b"{not json", method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=3)
            self.fail("应当返回 400")
        except HTTPError as error:
            self.assertEqual(error.code, 400)


if __name__ == "__main__":
    unittest.main()

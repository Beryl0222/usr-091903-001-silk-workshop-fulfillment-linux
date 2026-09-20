"""真丝体验材料履约：HTTP 入口。

在原有健康检查合约之上挂载领域接口。仅用标准库；
默认内存库便于测试，--db 指定文件即可现场离线持久化。
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import Domain, DomainError
from storage import Store

SERVICE_ID = "silk-workshop-fulfillment"
SERVICE_NAME = "真丝体验材料履约"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 每条路由：方法、路径正则、领域方法名；捕获组写入 payload
ROUTES = [
    ("POST", r"^/batches$", "create_batch"),
    ("GET", r"^/batches$", "list_batches"),
    ("GET", r"^/batches/(?P<id>[A-Za-z0-9_-]+)$", "get_batch"),
    ("POST", r"^/pieces/split$", "split_piece"),
    ("POST", r"^/pieces/assign$", "assign_piece"),
    ("POST", r"^/pieces/return$", "return_to_workshop"),
    ("POST", r"^/pieces/display$", "convert_to_display"),
    ("POST", r"^/reuses$", "reuse_scrap"),
    ("GET", r"^/reports/reuse$", "reuse_report_http"),

    ("POST", r"^/instructors$", "create_instructor"),
    ("POST", r"^/plans$", "create_plan"),
    ("POST", r"^/events$", "schedule_event"),
    ("GET", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)$", "event_detail_http"),
    ("POST", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/reschedule$", "reschedule_event"),
    ("POST", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/instructor$", "change_instructor"),
    ("POST", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/cancel$", "cancel_event"),
    ("POST", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/complete$", "complete_event"),
    ("GET", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/sufficiency$", "sufficiency_http"),
    ("GET", r"^/events/(?P<event_id>[A-Za-z0-9_-]+)/payout$", "payout_http"),
    ("POST", r"^/handoffs$", "add_handoff"),

    ("POST", r"^/participants$", "register_participant"),
    ("POST", r"^/participants/choices$", "set_choices"),

    ("POST", r"^/allocations$", "allocate_material"),
    ("POST", r"^/allocations/reverse$", "reverse_allocation"),
    ("POST", r"^/artworks$", "record_artwork"),
    ("GET", r"^/artworks/(?P<artwork_id>[A-Za-z0-9_-]+)/trace$", "trace_artwork_http"),

    ("POST", r"^/consents$", "grant_consent"),
    ("POST", r"^/consents/withdraw$", "withdraw_consent"),
    ("GET", r"^/consents$", "consents_http"),
    ("POST", r"^/displays/check$", "check_display"),

    ("POST", r"^/fees$", "record_fee"),
    ("POST", r"^/sync$", "_sync"),
]


def build_handler(domain: Domain):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, health_payload())
                return
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            query = self._query()
            for route_method, pattern, action in ROUTES:
                if route_method != method:
                    continue
                match = re.match(pattern, path)
                if not match:
                    continue
                payload = {}
                if method == "POST":
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        payload = json.loads(raw.decode("utf-8") or "{}")
                        if not isinstance(payload, dict):
                            raise ValueError("请求体必须是 JSON 对象")
                    except (ValueError, UnicodeDecodeError) as exc:
                        self._send(400, {"error": f"请求体解析失败: {exc}"})
                        return
                payload.update(match.groupdict())
                if method == "GET":
                    payload.update(query)
                try:
                    if action == "_sync":
                        result = domain.sync(payload.get("operations") or [])
                    else:
                        result = getattr(domain, action)(payload)
                    self._send(200, result)
                except DomainError as exc:
                    self._send(exc.status, {"error": str(exc)})
                except (KeyError, TypeError, ValueError) as exc:
                    self._send(400, {"error": f"请求参数有误: {exc}"})
                except Exception:
                    import traceback
                    traceback.print_exc()
                    self._send(500, {"error": "服务内部错误"})
                return
            self._send(404, {"error": f"接口不存在: {method} {path}"})

        def _query(self):
            if "?" not in self.path:
                return {}
            out = {}
            for pair in self.path.split("?", 1)[1].split("&"):
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                from urllib.parse import unquote
                out[unquote(key)] = unquote(value)
            return out

        def log_message(self, *_args):
            return

    return Handler


def make_app(db_path=":memory:"):
    store = Store(db_path)
    domain = Domain(store)
    return store, domain, build_handler(domain)


# 默认实例，保持 `from service import Handler` 的既有合约
store, domain, Handler = make_app()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=":memory:", help="SQLite 文件路径，默认内存库")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        check_store, _check_domain, _check_handler = make_app(":memory:")
        check_store.close()
        print("基础检查通过")
        return
    if args.db != ":memory:":
        run_store, _run_domain, handler_cls = make_app(args.db)
    else:
        handler_cls = Handler
    ThreadingHTTPServer(("0.0.0.0", args.port), handler_cls).serve_forever()


if __name__ == "__main__":
    main()

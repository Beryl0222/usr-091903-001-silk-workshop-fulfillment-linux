"""真丝体验材料履约的服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from silkworkshop import api
from silkworkshop.errors import DomainError

SERVICE_ID = "silk-workshop-fulfillment"
SERVICE_NAME = "真丝体验材料履约"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 履约领域接口。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        if method == "GET" and self.path == "/health":
            self._send(200, health_payload())
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": {"code": "bad_json", "message": "请求体不是有效 JSON"}})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": {"code": "bad_json", "message": "请求体必须是 JSON 对象"}})
                return
        try:
            status, payload = api.handle(method, self.path, body, self.headers)
        except DomainError as error:
            self._send(error.status, {"error": {"code": error.code, "message": str(error)}})
            return
        except Exception as error:  # pragma: no cover - 防御性兜底
            self._send(500, {"error": {"code": "internal", "message": str(error)}})
            return
        self._send(status, payload)

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

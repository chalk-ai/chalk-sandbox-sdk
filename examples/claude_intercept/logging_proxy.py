#!/usr/bin/env python3
"""Small reverse proxy that records forwarded Anthropic requests and responses."""

import base64
import datetime
import http.client
import http.server
import json
import os
import ssl
import threading
import traceback
import urllib.parse


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
SECRET_HEADERS = {
    "anthropic-api-key",
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-claude-intercept-token",
}
INTERNAL_REQUEST_HEADERS = {
    "chalk-forwarded-host",
    "chalk-forwarded-scheme",
    "chalk-forwarded-port",
    "chalk-forwarded-path",
    "x-envoy-original-host",
    "x-envoy-original-path",
    "x-claude-intercept-token",
}
LOG_PATH = "/tmp/claude-traffic.jsonl"
LOG_LOCK = threading.Lock()
TOKEN = os.environ["CLAUDE_INTERCEPT_TOKEN"]


def body_for_log(body):
    try:
        return {"text": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(body).decode("ascii")}


def safe_headers(headers):
    return {
        key: "[redacted]" if key.lower() in SECRET_HEADERS else value
        for key, value in headers.items()
    }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "claude-intercept/0.1"

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        if self.path == "/logs":
            if self.headers.get("x-claude-intercept-token") != TOKEN:
                self.send_error(403)
                return
            try:
                with open(LOG_PATH, "rb") as log_file:
                    body = log_file.read()
            except FileNotFoundError:
                body = b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.proxy()

    def do_POST(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()

    def do_PATCH(self):
        self.proxy()

    def do_DELETE(self):
        self.proxy()

    def do_HEAD(self):
        self.proxy()

    def do_OPTIONS(self):
        self.proxy()

    def proxy(self):
        if self.headers.get("x-claude-intercept-token") != TOKEN:
            self.send_error(403)
            return

        length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(length) if length else b""
        raw_path = self.headers.get("chalk-forwarded-path", self.path)
        parsed_path = urllib.parse.urlsplit(raw_path)
        request_path = parsed_path.path or "/"
        if parsed_path.query:
            request_path += "?" + parsed_path.query

        raw_host = self.headers.get("chalk-forwarded-host", "api.anthropic.com")
        target_host = raw_host.lower().rstrip(".").split(":", 1)[0]
        if target_host != "api.anthropic.com" and not target_host.endswith(
            ".anthropic.com"
        ):
            self.send_error(403, "forwarded host is not an Anthropic host")
            return

        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "request": {
                "method": self.command,
                "url": f"https://{target_host}{request_path}",
                "headers": safe_headers(self.headers),
                "body": body_for_log(request_body),
            },
        }

        try:
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in HOP_BY_HOP_HEADERS
                | INTERNAL_REQUEST_HEADERS
                | {"host", "content-length"}
            }
            headers["Host"] = target_host
            if request_body:
                headers["Content-Length"] = str(len(request_body))

            connection = http.client.HTTPSConnection(
                target_host,
                443,
                timeout=300,
                context=ssl.create_default_context(),
            )
            connection.request(
                self.command,
                request_path,
                body=request_body or None,
                headers=headers,
            )
            response = connection.getresponse()
            response_headers = response.getheaders()

            self.send_response(response.status, response.reason)
            for key, value in response_headers:
                if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}:
                    self.send_header(key, value)
            self.end_headers()

            response_body = bytearray()
            content_type = next(
                (
                    value.lower()
                    for key, value in response_headers
                    if key.lower() == "content-type"
                ),
                "",
            )
            read = (
                response.readline
                if "text/event-stream" in content_type
                else response.read
            )
            while chunk := read(65536):
                response_body.extend(chunk)
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                    self.wfile.flush()
            connection.close()

            entry["response"] = {
                "status": response.status,
                "headers": safe_headers(dict(response_headers)),
                "body": body_for_log(bytes(response_body)),
            }
        except Exception as error:
            entry["error"] = {
                "message": str(error),
                "traceback": traceback.format_exc(),
            }
            self.send_error(502, str(error))
        finally:
            with LOG_LOCK:
                with open(LOG_PATH, "a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def log_message(self, format, *args):
        print(format % args, flush=True)


http.server.ThreadingHTTPServer(("0.0.0.0", 18081), Handler).serve_forever()

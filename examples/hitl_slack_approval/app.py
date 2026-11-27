"""In-container human-in-the-loop (HITL) approval service.

Runs inside a Chalk ``Container`` workload. On startup it posts a message to a
Slack channel with **Approve** / **Reject** buttons. The buttons are Slack
*URL buttons* that point back at this container's own public URL — Chalk injects
that URL into the pod as ``CHALK_CONTAINER_WEB_URL``. Clicking a button opens
the browser at ``<public-url>/decision?...`` which records the decision in
in-memory state, updates the original Slack message to show the outcome, and
renders a small confirmation page.

Why URL buttons (and not Slack interactive ``block_actions``)? Interactive
buttons require the Slack app's *Interactivity Request URL* to be pre-configured
to a stable endpoint and the payloads to be signature-verified. A Container's
public URL is allocated fresh on each deploy, so a URL button that simply links
back to ``CHALK_CONTAINER_WEB_URL`` is the self-contained way to get a real
"call back into my service" approval flow with no external configuration.

State is intentionally in-memory: a dict keyed by request id. Restarting the
container clears it.

Standard library only — no pip dependencies needed in the image.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("hitl")

PORT: int = int(os.environ.get("PORT", "8080"))
SLACK_TOKEN: str = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL: str = os.environ.get("SLACK_CHANNEL", "#chalkbot-dev")
# Chalk injects the container's externally-reachable URL here for Container
# workloads that publish a port.
PUBLIC_URL: str = (os.environ.get("CHALK_CONTAINER_WEB_URL") or "").rstrip("/")

# --------------------------------------------------------------------------
# In-memory state
# --------------------------------------------------------------------------
_LOCK = threading.Lock()
# request id -> record
REQUESTS: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Slack helpers
# --------------------------------------------------------------------------
def slack_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON payload to the Slack Web API and return the parsed response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body: dict[str, Any] = json.load(resp)
    except urllib.error.URLError as exc:  # network / DNS / TLS failure
        log.error("Slack %s transport error: %s", method, exc)
        return {"ok": False, "error": f"transport_error: {exc}"}
    if not body.get("ok"):
        log.error("Slack %s API error: %s", method, body.get("error"))
    return body


def _build_blocks(req_id: str, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the Block Kit blocks for an approval request in its current state."""
    status = rec["status"]
    token = rec["token"]
    detail = rec["detail"]
    title = rec["title"]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🔐 Approval requested", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}*\n{detail}\n\n*Request ID:* `{req_id}`",
            },
        },
    ]

    if status == "pending":
        approve_url = f"{PUBLIC_URL}/decision?id={req_id}&action=approve&token={token}"
        reject_url = f"{PUBLIC_URL}/decision?id={req_id}&action=reject&token={token}"
        blocks.append(
            {
                "type": "actions",
                "block_id": "decision",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action_id": "approve",
                        "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                        "url": approve_url,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "action_id": "reject",
                        "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                        "url": reject_url,
                    },
                ],
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Status: *PENDING* · requested {rec['created_at']}"}
                ],
            }
        )
    else:
        emoji = "✅" if status == "approved" else "❌"
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{emoji} *{status.upper()}* at {rec['decided_at']}",
                    }
                ],
            }
        )
    return blocks


def post_request(title: str, detail: str) -> str:
    """Create an approval request, post it to Slack, and return its id."""
    req_id = secrets.token_hex(4)
    rec: dict[str, Any] = {
        "title": title,
        "detail": detail,
        "status": "pending",
        "created_at": _now_iso(),
        "decided_at": None,
        "token": secrets.token_urlsafe(12),
        "channel": None,
        "ts": None,
    }
    with _LOCK:
        REQUESTS[req_id] = rec

    resp = slack_call(
        "chat.postMessage",
        {
            "channel": SLACK_CHANNEL,
            "text": f"Approval requested: {title}",
            "blocks": _build_blocks(req_id, rec),
        },
    )
    if resp.get("ok"):
        with _LOCK:
            rec["channel"] = resp.get("channel")
            rec["ts"] = resp.get("ts")
        log.info("Posted approval request %s to %s (ts=%s)", req_id, SLACK_CHANNEL, resp.get("ts"))
    else:
        log.error("Could not post approval request %s: %s", req_id, resp.get("error"))
    return req_id


def _update_message(req_id: str) -> None:
    """Edit the original Slack message to reflect the recorded decision."""
    with _LOCK:
        rec = REQUESTS.get(req_id)
        if not rec or not rec.get("ts"):
            return
        payload = {
            "channel": rec["channel"],
            "ts": rec["ts"],
            "text": f"Approval {rec['status']}: {rec['title']}",
            "blocks": _build_blocks(req_id, rec),
        }
    slack_call("chat.update", payload)


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background:#0f1117; color:#e6e8ee; display:flex; min-height:100vh; margin:0;
         align-items:center; justify-content:center; }}
  .card {{ background:#171a23; border:1px solid #262b38; border-radius:14px; padding:36px 40px;
          max-width:460px; box-shadow:0 12px 40px rgba(0,0,0,.4); }}
  .badge {{ font-size:54px; line-height:1; }}
  h1 {{ font-size:22px; margin:14px 0 6px; }}
  p {{ color:#9aa3b2; line-height:1.5; margin:6px 0; }}
  code {{ background:#0f1117; padding:2px 6px; border-radius:6px; color:#cdd3df; }}
  .ok {{ color:#3fb950; }} .no {{ color:#f85149; }} .muted {{ color:#6e7892; font-size:13px; }}
</style></head>
<body><div class="card">
  <div class="badge">{badge}</div>
  <h1 class="{cls}">{heading}</h1>
  {body}
  <p class="muted">{footer}</p>
</div></body></html>"""


def _render(title: str, badge: str, heading: str, cls: str, body: str, footer: str) -> bytes:
    return _PAGE.format(
        title=html.escape(title),
        badge=badge,
        heading=html.escape(heading),
        cls=cls,
        body=body,
        footer=html.escape(footer),
    ).encode("utf-8")


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "hitl-approval/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access log
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/healthz", "/health"):
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/":
            self._index()
            return
        if path == "/status":
            qs = urllib.parse.parse_qs(parsed.query)
            req_id = (qs.get("id") or [""])[0]
            with _LOCK:
                rec = REQUESTS.get(req_id)
                snapshot = dict(rec) if rec else None
            if snapshot is None:
                self._json(404, {"error": "not_found", "id": req_id})
            else:
                snapshot.pop("token", None)
                self._json(200, {"id": req_id, **snapshot})
            return
        if path == "/decision":
            self._decision(urllib.parse.parse_qs(parsed.query))
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/requests":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid_json"})
                return
            title = str(payload.get("title", "Untitled approval"))
            detail = str(payload.get("detail", ""))
            req_id = post_request(title, detail)
            self._json(201, {"id": req_id, "status": "pending"})
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    # -- handlers ---------------------------------------------------------
    def _decision(self, qs: dict[str, list[str]]) -> None:
        req_id = (qs.get("id") or [""])[0]
        action = (qs.get("action") or [""])[0]
        token = (qs.get("token") or [""])[0]

        with _LOCK:
            rec = REQUESTS.get(req_id)
            if rec is None:
                body = self._render_unknown(req_id)
                self._send(404, body)
                return
            if not secrets.compare_digest(token, rec["token"]):
                self._send(
                    403,
                    _render(
                        "Forbidden", "🚫", "Invalid or missing token", "no",
                        "<p>This approval link is not valid.</p>",
                        f"Request {req_id}",
                    ),
                )
                return
            if action not in ("approve", "reject"):
                self._send(400, _render("Bad request", "⚠️", "Unknown action", "no", "", req_id))
                return

            if rec["status"] == "pending":
                rec["status"] = "approved" if action == "approve" else "rejected"
                rec["decided_at"] = _now_iso()
                already = False
            else:
                already = True
            status = rec["status"]
            title = rec["title"]
            decided_at = rec["decided_at"]

        if not already:
            _update_message(req_id)

        approved = status == "approved"
        badge = "✅" if approved else "❌"
        cls = "ok" if approved else "no"
        heading = f"Request {status}"
        note = "It was already decided — your click did not change it." if already else "Recorded. The Slack message has been updated."
        body = (
            f"<p><strong>{html.escape(title)}</strong></p>"
            f"<p>Decision: <span class='{cls}'>{status.upper()}</span></p>"
            f"<p class='muted'>Decided at {html.escape(str(decided_at))} · request <code>{html.escape(req_id)}</code></p>"
            f"<p>{html.escape(note)}</p>"
        )
        self._send(200, _render(f"{status.title()}", badge, heading, cls, body, "You can close this tab."))

    def _render_unknown(self, req_id: str) -> bytes:
        return _render(
            "Unknown request", "❓", "Unknown request", "no",
            "<p>No approval request matches this link. It may have been lost on a restart "
            "(state is in-memory).</p>",
            f"Request {req_id}",
        )

    def _index(self) -> None:
        with _LOCK:
            rows = []
            for rid, rec in sorted(REQUESTS.items(), key=lambda kv: kv[1]["created_at"], reverse=True):
                cls = {"pending": "muted", "approved": "ok", "rejected": "no"}.get(rec["status"], "muted")
                rows.append(
                    f"<p><code>{html.escape(rid)}</code> — {html.escape(rec['title'])} "
                    f"· <span class='{cls}'>{rec['status'].upper()}</span></p>"
                )
        body = "".join(rows) or "<p class='muted'>No approval requests yet.</p>"
        footer = f"Public URL: {PUBLIC_URL or '(not set)'} · channel {SLACK_CHANNEL}"
        self._send(200, _render("HITL approvals", "🔐", "Approval service", "", body, footer))


def _post_startup_request() -> None:
    """Post one demo approval request so the workflow is visible immediately."""
    if not SLACK_TOKEN:
        log.error("No SLACK_BOT_TOKEN in environment; not posting startup request.")
        return
    post_request(
        "Deploy build #4242 to production",
        "Container HITL demo — approve to promote, reject to hold. "
        "Clicking a button calls back into this container via its public URL.",
    )


def main() -> None:
    log.info("HITL approval service starting on :%d", PORT)
    log.info("Public URL (CHALK_CONTAINER_WEB_URL): %s", PUBLIC_URL or "(not injected)")
    log.info("Slack channel: %s · token present: %s", SLACK_CHANNEL, bool(SLACK_TOKEN))

    if not PUBLIC_URL:
        log.warning("CHALK_CONTAINER_WEB_URL is not set — Slack buttons will have no callback URL.")

    # Bind/listen first so the published port answers health checks immediately,
    # then post the Slack request off the main thread (more can be created at
    # runtime via POST /requests).
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=_post_startup_request, name="startup-post", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

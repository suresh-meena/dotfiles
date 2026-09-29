"""Human browser session and confirmation pages for job actions.

The browser surface is deliberately separate from bearer-token API mutation:
users log in with a human token, then receive a short-lived opaque cookie.
No browser mutation accepts Authorization credentials.
"""

from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import ClientDisconnect

from .. import auth, budget
from ..engine import admission
from ..engine import state
from ..errors import FqError
from ..util import parse_utc, utcnow
from ..views import job_document

COOKIE = "fq_ui_session"
SESSION_TTL_S = 8 * 60 * 60
MAX_SESSIONS = 1024
MAX_FORM_BYTES = 16 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; style-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class Session:
    token_id: str
    csrf: str
    expires_at: float


def _configured_origin(value: str | None, *, dev_mode: bool = False) -> tuple[str, str] | None:
    """Accept HTTPS in production, plus loopback HTTP in explicit dev mode."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    dev_loopback = dev_mode and parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1")
    if (not (parts.scheme == "https" or dev_loopback) or not host or parts.username or parts.password
            or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        return None
    normalized_host = host.lower()
    try:
        normalized_host.encode("ascii")
    except UnicodeEncodeError:
        return None
    netloc = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    if port is not None:
        default_port = 443 if parts.scheme == "https" else 80
        if port != default_port:
            netloc += f":{port}"
    origin = f"{parts.scheme}://{netloc}"
    return origin, netloc


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" "
        "content=\"width=device-width,initial-scale=1\"><title>" + _e(title) + " · fleetq</title>"
        "<body><header><a href=\"/ui/\">fleetq</a></header><main><h1>" + _e(title) + "</h1>" + body
        + "</main></body></html>", status_code=status,
    )


async def _form(request: Request) -> dict[str, str]:
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        raise FqError("invalid_argument", "expected application/x-www-form-urlencoded")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        raise FqError("invalid_argument", "form is too large")
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_FORM_BYTES:
                raise FqError("invalid_argument", "form is too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise FqError("invalid_argument", "form upload was interrupted") from exc
    raw = b"".join(chunks)
    if re.search(rb"%(?![0-9A-Fa-f]{2})", raw):
        raise FqError("invalid_argument", "malformed URL encoding")
    values = parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=32, encoding="utf-8", errors="strict")
    if any(len(items) != 1 for items in values.values()):
        raise FqError("invalid_argument", "duplicate form fields are not allowed")
    return {key: items[0] for key, items in values.items()}


def install_ui(app: FastAPI, rt: Any) -> None:
    dev_mode = bool(getattr(rt, "ui_dev_mode", False))
    configured = _configured_origin(getattr(rt, "ui_origin", None), dev_mode=dev_mode)
    if configured is None:
        return
    origin, expected_host = configured
    secure_cookie = origin.startswith("https://")
    sessions: dict[str, Session] = {}

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            if request.headers.get("host", "") != expected_host:
                response = Response("Invalid Host", status_code=421)
            elif request.method == "POST" and request.headers.get("origin", "") != origin:
                response = Response("Invalid Origin", status_code=403)
            else:
                response = await call_next(request)
            for key, value in SECURITY_HEADERS.items():
                response.headers[key] = value
            return response
        return await call_next(request)

    def clean_sessions() -> None:
        now = time.monotonic()
        for sid, session in list(sessions.items()):
            if session.expires_at <= now:
                sessions.pop(sid, None)
        if len(sessions) >= MAX_SESSIONS:
            oldest = sorted(sessions, key=lambda sid: sessions[sid].expires_at)
            for sid in oldest[:len(sessions) - MAX_SESSIONS + 1]:
                sessions.pop(sid, None)

    async def current_session(request: Request) -> tuple[auth.Principal, Session, str]:
        sid = request.cookies.get(COOKIE, "")
        session = sessions.get(sid)
        if not sid or session is None or session.expires_at <= time.monotonic():
            sessions.pop(sid, None)
            raise FqError("unauthorized", "browser session expired; sign in again")

        def lookup(conn):
            row = conn.execute("SELECT * FROM tokens WHERE id = ?", (session.token_id,)).fetchone()
            if row is None or row["revoked_at"] or row["kind"] != "human":
                raise FqError("unauthorized", "human login token is no longer active")
            if row["expires_at"] and not rt.clock_ok():
                raise FqError("unauthorized", "clock health is uncertain; token expiry cannot be checked")
            if row["expires_at"] and parse_utc(row["expires_at"]) <= parse_utc(utcnow()):
                raise FqError("unauthorized", "human login token has expired")
            return auth.Principal(token_id=row["id"], owner=row["owner"], kind=row["kind"], label=row["label"],
                                  scopes=frozenset(row["scopes"].split()), allow_clusters=bool(row["allow_clusters"]))
        principal = await rt.store.run(lookup)
        if not principal.has("read"):
            raise FqError("forbidden", "browser login requires the read scope")
        return principal, session, sid

    def error(exc: FqError) -> HTMLResponse:
        statuses = {"unauthorized": 401, "forbidden": 403, "not_found": 404,
                    "conflict": 409, "not_modifiable": 409, "invalid_argument": 400}
        return _page("Request refused", "<p>" + _e(exc.message) + "</p><p><a href=\"/ui/\">Continue</a></p>",
                     status=exc.http_status if exc.http_status else statuses.get(exc.code, 400))

    def login_form(message: str = "") -> HTMLResponse:
        body = ("<p>Sign in with your personal fleetq human token. Service and agent tokens cannot log in.</p>"
                + ("<p role=\"alert\">" + _e(message) + "</p>" if message else "")
                + "<form method=\"post\" action=\"/ui/login\"><label>Human token "
                  "<input type=\"password\" name=\"token\" autocomplete=\"current-password\" required></label> "
                  "<button type=\"submit\">Sign in</button></form>")
        return _page("Sign in", body)

    def cookie(response: Response, sid: str, *, max_age: int = SESSION_TTL_S) -> None:
        response.set_cookie(COOKIE, sid, max_age=max_age, path="/ui", secure=secure_cookie, httponly=True,
                            samesite="strict")

    @app.get("/ui", include_in_schema=False)
    async def ui_root():
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/ui/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_get(request: Request):
        try:
            await current_session(request)
            return RedirectResponse("/ui/", status_code=303)
        except FqError:
            return login_form()

    @app.post("/ui/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_post(request: Request):
        try:
            form = await _form(request)
            token = form.get("token", "")
            if not token or len(token) > 256:
                raise FqError("unauthorized", "invalid human token")
            principal = await rt.store.run(lambda c: auth.verify(c, token, clock_ok=rt.clock_ok()))
            if principal.kind != "human":
                raise FqError("forbidden", "only human tokens may create browser sessions")
            principal.require("read")
            clean_sessions()
            sid = secrets.token_urlsafe(32)
            sessions[sid] = Session(principal.token_id, secrets.token_urlsafe(32), time.monotonic() + SESSION_TTL_S)
            response = RedirectResponse("/ui/", status_code=303)
            cookie(response, sid)
            return response
        except (FqError, UnicodeDecodeError, ValueError) as exc:
            if isinstance(exc, FqError):
                message = exc.message
            else:
                message = "invalid form encoding"
            return login_form(message)

    @app.post("/ui/logout", include_in_schema=False)
    async def logout(request: Request):
        try:
            _principal, _session, sid = await current_session(request)
            form = await _form(request)
            if not hmac.compare_digest(form.get("csrf", ""), _session.csrf):
                raise FqError("forbidden", "CSRF token mismatch")
            sessions.pop(sid, None)
            response = RedirectResponse("/ui/login", status_code=303)
            response.delete_cookie(COOKIE, path="/ui", secure=secure_cookie, httponly=True, samesite="strict")
            return response
        except FqError as exc:
            return error(exc)
        except (UnicodeDecodeError, ValueError):
            return _page("Request refused", "<p>Invalid form encoding.</p>", status=400)

    @app.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request):
        try:
            principal, session, _sid = await current_session(request)
            def query(conn):
                if principal.has("read_all") or principal.has("manage_all"):
                    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 200").fetchall()
                else:
                    rows = conn.execute("SELECT * FROM jobs WHERE owner = ? ORDER BY id DESC LIMIT 200",
                                        (principal.owner,)).fetchall()
                return [job_document(conn, row) for row in rows]
            jobs = await rt.store.run(query)
            body = "<p>Signed in as " + _e(principal.owner) + ".</p>"
            body += "<p><a href=\"/ui/status\">Quota and token status</a></p>"
            body += "<form method=\"post\" action=\"/ui/logout\"><input type=\"hidden\" name=\"csrf\" value=\"" + _e(session.csrf) + "\"><button>Sign out</button></form>"
            if not jobs:
                body += "<p>No jobs.</p>"
            else:
                body += "<table><thead><tr><th>ID</th><th>Name</th><th>State</th><th>Owner</th></tr></thead><tbody>"
                for job in jobs:
                    body += ("<tr><td><a href=\"/ui/jobs/" + str(int(job["id"])) + "\">" + str(int(job["id"]))
                             + "</a></td><td>" + _e(job["name"]) + "</td><td>" + _e(job["phase"])
                             + "</td><td>" + _e(job["owner"]) + "</td></tr>")
                body += "</tbody></table>"
            return _page("Jobs", body)
        except FqError as exc:
            if exc.code == "unauthorized":
                return login_form()
            return error(exc)

    @app.get("/ui/status", response_class=HTMLResponse, include_in_schema=False)
    async def status_page(request: Request):
        try:
            principal, session, _sid = await current_session(request)
            def query(conn):
                quotas = admission.effective_quota(principal)
                active = admission.active_job_count(conn, token_id=principal.token_id)
                rows = conn.execute(
                    "SELECT id, owner, kind, label, scopes, quota_json, allow_clusters, created_at, expires_at, revoked_at "
                    "FROM tokens " + ("ORDER BY created_at DESC LIMIT 200" if principal.has("admin") else "WHERE owner = ? ORDER BY created_at DESC LIMIT 200"),
                    () if principal.has("admin") else (principal.owner,)).fetchall()
                since = utcnow()[:10] + "T00:00:00.000000Z"
                ledger = budget.remote_cost_ledger(conn, since) if principal.has("admin") else []
                return quotas, active, [dict(row) for row in rows], ledger
            quotas, active, tokens, ledger = await rt.store.run(query)
            body = ("<p>Quota status for this login token (limits are token-specific).</p><ul>"
                    + "".join("<li>" + _e(key) + ": " + _e(value) + (" (" + str(active) + " active)" if key == "active_jobs" else "") + "</li>" for key, value in quotas.items())
                    + "</ul><h2>Tokens for " + _e(principal.owner) + "</h2>")
            if tokens:
                body += "<table><thead><tr><th>ID</th><th>Owner</th><th>Kind</th><th>Label</th><th>Scopes</th><th>Status</th></tr></thead><tbody>"
                for token in tokens:
                    body += ("<tr><td>" + _e(token["id"]) + "</td><td>" + _e(token["owner"]) + "</td><td>" + _e(token["kind"]) + "</td><td>" + _e(token["label"])
                             + "</td><td>" + _e(token["scopes"]) + "</td><td>" + ("revoked" if token["revoked_at"] else "active") + "</td></tr>")
                body += "</tbody></table>"
            else:
                body += "<p>No tokens.</p>"
            if principal.has("admin"):
                body += "<h2>Managed site query budgets</h2>"
                if ledger:
                    body += ("<table><thead><tr><th>Site</th><th>Class</th><th>RPC/min</th><th>Available RPC</th>"
                             "<th>RPC today</th><th>Denied today</th><th>Sessions available</th>"
                             "<th>Sessions today</th><th>Bytes available</th><th>Bytes today</th></tr></thead><tbody>")
                    for site in ledger:
                        for entry in site["classes"]:
                            body += ("<tr><td>" + _e(site["site_id"]) + "</td><td>" + _e(entry["op_class"])
                                     + "</td><td>" + _e(entry["per_minute"]) + "</td><td>" + _e(entry["available"])
                                     + "</td><td>" + _e(entry["rpc_today"]) + "</td><td>" + _e(entry["denied_today"])
                                     + "</td><td>" + _e(site["sessions"]["available"]) + "</td><td>"
                                     + _e(site["sessions"]["used_today"]) + "</td><td>"
                                     + _e(site["bytes"]["available"]) + "</td><td>"
                                     + _e(site["bytes"]["used_today"]) + "</td></tr>")
                    body += "</tbody></table>"
                else:
                    body += "<p>No enabled managed sites.</p>"
                body += ("<h2>Create token</h2><p>The bearer value is displayed once after creation. Store it securely.</p>"
                         "<form method=\"post\" action=\"/ui/tokens/create\"><input type=\"hidden\" name=\"csrf\" value=\"" + _e(session.csrf) + "\">"
                         "<label>Owner <input name=\"owner\" required maxlength=\"128\"></label> "
                         "<label>Kind <select name=\"kind\"><option>human</option><option>agent</option><option>service</option></select></label> "
                         "<label>Label <input name=\"label\" required maxlength=\"128\"></label> "
                         "<label>Scopes (space separated) <input name=\"scopes\" value=\"read logs submit manage_own\"></label> "
                         "<label>Quota JSON <input name=\"quota\" value=\"{}\"></label> "
                         "<label><input type=\"checkbox\" name=\"clusters\" value=\"yes\"> Allow clusters</label> <button>Create</button></form>")
                body += "<h2>Revoke token</h2>"
                for token in tokens:
                    if not token["revoked_at"] and token["id"] != principal.token_id:
                        body += ("<form method=\"post\" action=\"/ui/tokens/revoke\"><input type=\"hidden\" name=\"csrf\" value=\"" + _e(session.csrf)
                                 + "\"><input type=\"hidden\" name=\"token_id\" value=\"" + _e(token["id"]) + "\"><button>Revoke " + _e(token["label"]) + " (" + _e(token["id"]) + ")</button></form>")
            body += "<p><a href=\"/ui/\">Back to jobs</a></p>"
            return _page("Quota and token status", body)
        except FqError as exc:
            if exc.code == "unauthorized":
                return login_form()
            return error(exc)

    @app.post("/ui/tokens/create", include_in_schema=False)
    async def token_create(request: Request):
        try:
            principal, session, _sid = await current_session(request)
            principal.require("admin")
            form = await _form(request)
            if not hmac.compare_digest(form.get("csrf", ""), session.csrf):
                raise FqError("forbidden", "CSRF token mismatch")
            owner, kind, label = form.get("owner", "").strip(), form.get("kind", ""), form.get("label", "").strip()
            scopes = tuple(form.get("scopes", "").split())
            if not owner or not label or len(owner) > 128 or len(label) > 128:
                raise FqError("invalid_argument", "owner and label are required and limited to 128 characters")
            # UI-issued tokens cannot grant platform-wide administration or permits.
            if set(scopes) & {"admin", "manage_all", "permits"}:
                raise FqError("forbidden", "use the local CLI to issue privileged tokens")
            try:
                quota = json.loads(form.get("quota", "{}"))
            except json.JSONDecodeError as exc:
                raise FqError("invalid_argument", "quota must be a JSON object") from exc
            if not isinstance(quota, dict) or set(quota) - set(admission.DEFAULT_QUOTAS.get(kind, {})) or any(type(v) is not int or v < 0 for v in quota.values()):
                raise FqError("invalid_argument", "quota must contain non-negative integer limits supported for the selected token kind")
            token_id, token = await rt.store.run(lambda c: auth.create_token(
                c, owner=owner, kind=kind, label=label, scopes=scopes,
                allow_clusters=form.get("clusters") == "yes", quota=quota))
            return _page("Token created", "<p>Token ID: " + _e(token_id) + "</p><p>Copy this value now; it will not be shown again.</p><pre>" + _e(token) + "</pre><p><a href=\"/ui/status\">Return to status</a></p>")
        except FqError as exc:
            return error(exc)
        except (UnicodeDecodeError, ValueError):
            return _page("Request refused", "<p>Invalid form encoding.</p>", status=400)

    @app.post("/ui/tokens/revoke", include_in_schema=False)
    async def token_revoke(request: Request):
        try:
            principal, session, _sid = await current_session(request)
            principal.require("admin")
            form = await _form(request)
            if not hmac.compare_digest(form.get("csrf", ""), session.csrf):
                raise FqError("forbidden", "CSRF token mismatch")
            token_id = form.get("token_id", "")
            def revoke(conn):
                row = conn.execute("SELECT owner FROM tokens WHERE id = ?", (token_id,)).fetchone()
                if row is None or (row["owner"] != principal.owner and not principal.has("admin")):
                    raise FqError("not_found", "token does not exist")
                auth.revoke_token(conn, token_id)
            await rt.store.run(revoke)
            return RedirectResponse("/ui/status", status_code=303)
        except FqError as exc:
            return error(exc)
        except (UnicodeDecodeError, ValueError):
            return _page("Request refused", "<p>Invalid form encoding.</p>", status=400)

    @app.get("/ui/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
    async def job_page(job_id: int, request: Request):
        try:
            principal, session, _sid = await current_session(request)
            def query(conn):
                job = state.get_job(conn, job_id)
                if not auth.can_read(principal, job):
                    raise FqError("not_found", f"job {job_id} does not exist")
                return job_document(conn, job)
            job = await rt.store.run(query)
            body = ("<p>Owner: " + _e(job["owner"]) + "</p><p>State: " + _e(job["phase"])
                    + "</p><p>Desired state: " + _e(job["desired_state"]) + "</p><p>Reason: "
                    + _e(job["reason"] or "—") + "</p><p>Execution outcome: "
                    + _e(job["execution"]["outcome"] or "—") + "</p><p>Remote may be live: "
                    + ("yes" if job["remote_may_be_live"] else "no") + "</p>")
            if auth.can_manage(principal, {"owner": job["owner"], "token_id": job["submitter"]["token"] if job["submitter"] else ""}):
                actions = []
                if job["phase"] != "TERMINAL":
                    actions.append(("cancel", "Cancel"))
                if job["phase"] in ("PENDING", "HELD", "BLOCKED"):
                    actions.append(("hold", "Hold"))
                if job["phase"] == "HELD":
                    actions.append(("release", "Release held job"))
                for action, label in actions:
                    body += ("<form method=\"get\" action=\"/ui/jobs/" + str(job_id) + "/confirm/" + action
                             + "\"><button>" + _e(label) + "…</button></form>")
            body += "<p><a href=\"/ui/\">Back to jobs</a></p>"
            return _page("Job " + str(job_id) + ": " + str(job["name"]), body)
        except FqError as exc:
            if exc.code == "unauthorized":
                return login_form()
            return error(exc)

    @app.get("/ui/jobs/{job_id}/confirm/{action}", response_class=HTMLResponse, include_in_schema=False)
    async def confirm(job_id: int, action: str, request: Request):
        try:
            principal, session, _sid = await current_session(request)
            if action not in ("cancel", "hold", "release"):
                raise FqError("not_found", "unknown action")
            job = await rt.store.run(lambda c: state.get_job(c, job_id))
            if not auth.can_manage(principal, job):
                raise FqError("not_found", f"job {job_id} does not exist")
            if action == "cancel" and job["phase"] == "TERMINAL":
                message = "This job is already terminal."
                warning = True
            elif action == "hold" and job["phase"] not in ("PENDING", "HELD", "BLOCKED"):
                raise FqError("not_modifiable", "only unlaunched jobs can be held")
            elif action == "release" and job["phase"] != "HELD":
                raise FqError("not_modifiable", "job is not held")
            else:
                message = {"cancel": "Cancellation is a request; active remote work may take time to stop.",
                           "hold": "This prevents an unlaunched job from dispatching.",
                           "release": "This returns the held job to the pending queue."}[action]
                warning = False
            if warning:
                return _page("Confirm " + action, "<p>" + _e(message) + "</p><p><a href=\"/ui/jobs/" + str(job_id) + "\">Back</a></p>")
            label = {"cancel": "Confirm cancellation request", "hold": "Confirm hold", "release": "Confirm release"}[action]
            body = ("<p>" + _e(message) + "</p><p>Job: " + _e(job["name"]) + " (" + str(job_id) + ")</p>"
                    + "<form method=\"post\" action=\"/ui/jobs/" + str(job_id) + "/actions/" + action + "\">"
                    + "<input type=\"hidden\" name=\"csrf\" value=\"" + _e(session.csrf) + "\">"
                    + "<input type=\"hidden\" name=\"version\" value=\"" + str(job["version"]) + "\">"
                    + "<label><input type=\"checkbox\" name=\"confirm\" value=\"yes\" required> I confirm this action</label> "
                    + "<button>" + _e(label) + "</button></form><p><a href=\"/ui/jobs/" + str(job_id) + "\">Cancel</a></p>")
            return _page("Confirm " + action, body)
        except FqError as exc:
            if exc.code == "unauthorized":
                return login_form()
            return error(exc)

    @app.post("/ui/jobs/{job_id}/actions/{action}", include_in_schema=False)
    async def act(job_id: int, action: str, request: Request):
        try:
            principal, session, _sid = await current_session(request)
            form = await _form(request)
            if not hmac.compare_digest(form.get("csrf", ""), session.csrf):
                raise FqError("forbidden", "CSRF token mismatch")
            if form.get("confirm") != "yes":
                raise FqError("invalid_argument", "explicit confirmation is required")
            if action not in ("cancel", "hold", "release"):
                raise FqError("not_found", "unknown action")
            version_raw = form.get("version", "")
            if not version_raw.isdigit():
                raise FqError("invalid_argument", "invalid job version")
            expect = int(version_raw)
            def mutate(conn):
                job = state.get_job(conn, job_id)
                if not auth.can_manage(principal, job):
                    raise FqError("not_found", f"job {job_id} does not exist")
                if action == "cancel":
                    if job["phase"] != "TERMINAL":
                        state.update_job(conn, job_id, event="cancel_requested", actor=principal.label,
                                         expect_version=expect, desired_state="CANCEL", cancel_requested=1)
                elif action == "hold":
                    if job["phase"] not in ("PENDING", "HELD", "BLOCKED"):
                        raise FqError("not_modifiable", "only unlaunched jobs can be held")
                    state.update_job(conn, job_id, event="held", actor=principal.label, expect_version=expect,
                                     desired_state="HOLD", phase="HELD")
                else:
                    if job["phase"] != "HELD":
                        raise FqError("not_modifiable", "job is not held")
                    state.update_job(conn, job_id, event="released", actor=principal.label, expect_version=expect,
                                     desired_state="RUN", phase="PENDING", reason=None)
            await rt.store.run(mutate)
            if rt.controller is not None:
                rt.controller.wake()
                rt.controller.hub.publish()
            return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)
        except FqError as exc:
            return error(exc)
        except (UnicodeDecodeError, ValueError):
            return _page("Request refused", "<p>Invalid form encoding.</p>", status=400)

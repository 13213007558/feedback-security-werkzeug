"""Divergence audit: outer gateway raw-prefix policy vs Werkzeug route result.

Two WSGI drivers are used for the *same* logical request:

1. ``server_environ`` builds the environ the way a real PEP 3333 server does,
   starting from the verbatim HTTP request target: parse the request line, then
   percent-decode exactly once into ``PATH_INFO`` (mirrors
   ``werkzeug.serving.WSGIRequestHandler.make_environ``).  No dot or slash
   normalization happens at this layer.  This is the ground truth for "what the
   server actually received".

2. ``client_environ`` uses Werkzeug's test ``EnvironBuilder``.  It ALSO decodes
   once (``unquote`` in ``get_environ``), but it runs ``urlsplit`` on the input
   first, which rewrites leading ``//`` and treats the input string as a
   convenience argument rather than a wire byte sequence.  The two are printed
   side by side so the test client's reprocessing is never mistaken for raw
   server bytes.

Run:  .venv/bin/python security-audit/audit.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from werkzeug.test import EnvironBuilder  # noqa: E402
from werkzeug.wrappers import Request  # noqa: E402

from app import SecureApp  # noqa: E402
from app import URL_MAP  # noqa: E402
from app import naive_gateway_allows  # noqa: E402

ADMIN_IDENTITIES = (None, "admin")


@dataclass(frozen=True)
class Case:
    label: str
    raw_target: str
    script_name: str = ""
    note: str = ""


def server_environ(raw_target: str, script_name: str, identity: str | None):
    """Mirror WSGIRequestHandler.make_environ from the HTTP request target."""
    request_url = urlsplit(raw_target)
    # This netloc repair is exactly what serving.py does for origin-form
    # request targets that begin with "//".
    if not request_url.scheme and request_url.netloc:
        path_info = f"/{request_url.netloc}{request_url.path}"
    else:
        path_info = request_url.path
    path_info = unquote(path_info)  # servers decode PATH_INFO exactly once

    if script_name:
        prefix = unquote(script_name)
        if path_info.startswith(prefix):
            path_info = path_info[len(prefix) :] or "/"
        script = prefix
    else:
        script = ""

    environ = EnvironBuilder(
        path="/",
        environ_overrides={
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
            "REQUEST_METHOD": "GET",
            "wsgi.url_scheme": "http",
        },
    ).get_environ()
    environ["SCRIPT_NAME"] = script
    environ["PATH_INFO"] = path_info
    environ["REQUEST_URI"] = raw_target
    environ["RAW_URI"] = raw_target
    environ["QUERY_STRING"] = request_url.query
    if identity is not None:
        environ["web.identity"] = identity
    return environ


def client_environ(raw_target: str, base_url: str | None, identity: str | None):
    overrides = {"REQUEST_URI": raw_target, "RAW_URI": raw_target}
    if identity is not None:
        overrides["web.identity"] = identity
    builder = EnvironBuilder(path=raw_target, base_url=base_url)
    environ = builder.get_environ()
    environ.update(overrides)
    return environ


def route_only(environ):
    """What Werkzeug routes, independent of the WSGI response cycle."""
    request = Request(environ)
    adapter = URL_MAP.bind_to_request(request)
    try:
        endpoint, args = adapter.match()
        return f"{endpoint} {args}".rstrip()
    except Exception as exc:  # noqa: BLE001 - want the literal routing outcome
        new_url = getattr(exc, "new_url", "")
        code = getattr(exc, "code", type(exc).__name__)
        return f"{code} -> {new_url}" if new_url else f"{code}"


def call_app(environ):
    captured: dict = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = SecureApp()(environ, start_response)
    raw = b"".join(chunks).decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        # Redirects carry an empty or HTML body rather than JSON.
        body = {}
    if "redirect_location" not in body:
        body["redirect_location"] = captured["headers"].get("Location", "")
    return captured["status"].split()[0], body


CASES = [
    Case("baseline public", "/public/hello", note="ordinary route, allowed"),
    Case("baseline admin", "/admin", note="ordinary protected route, denied anon"),
    Case("admin sub path", "/admin/users/42", note="protected prefix, path rule"),
    Case("encoded first letter", "/%61dmin/users", note="%61 == 'a' after one decode"),
    Case("encoded slash inside", "/admin%2fusers", note="%2f -> '/' inside PATH_INFO"),
    Case("double encoded slash", "/admin%252fusers", note="%25 -> %, slash stays literal"),
    Case("double encoded letter", "/%2561dmin", note="gateway AND router see %61dmin"),
    Case("leading double slash", "//admin/users", note="urlsplit netloc repair differs"),
    Case("inner double slash", "/admin//users", note="merge_slashes redirect"),
    Case("dot segment traversal", "/public/../admin/users", note="no dot normalization"),
    Case("encoded dot traversal", "/public/%2e%2e/admin/users", note="%2e -> '.' after decode"),
    Case("encoded dot single", "/admin/%2e/users", note="%2e -> '.' literal segment"),
    Case("path converter catch", "/files/etc/passwd", note="path rule absorbs slashes"),
    Case("encoded slash to path rule", "/files/a%2fb", note="one decode splits the segment"),
    Case("mounted app admin", "/app/admin/users", script_name="/app",
         note="SCRIPT_NAME strips the mount prefix"),
    Case("mounted encoded admin", "/app/%61dmin/users", script_name="/app",
         note="decode happens before prefix strip"),
    Case("encoded mount prefix", "/%61pp/admin", script_name="/app",
         note="SCRIPT_NAME itself percent-encoded on the wire"),
    Case("query confusion", "/public/hello?next=/admin", note="query must not be routed"),
    Case("trailing slash redirect", "/admin/", note="strict_slashes on /admin"),
]


def main() -> None:
    rows = []
    for case in CASES:
        base_url = f"http://localhost{case.script_name}" if case.script_name else None
        s_env = server_environ(case.raw_target, case.script_name, None)
        c_env = client_environ(case.raw_target, base_url, None)

        status_anon, body_anon = call_app(server_environ(case.raw_target, case.script_name, None))
        status_admin, body_admin = call_app(server_environ(case.raw_target, case.script_name, "admin"))

        row = {
            "label": case.label,
            "raw_request_target": case.raw_target,
            "script_name_wire": case.script_name or "-",
            "note": case.note,
            "gateway_raw_prefix_allows": naive_gateway_allows(case.raw_target),
            "server": {
                "REQUEST_URI": s_env["REQUEST_URI"],
                "SCRIPT_NAME": s_env["SCRIPT_NAME"],
                "PATH_INFO": s_env["PATH_INFO"],
                "routed": route_only(s_env),
                "anon_http": status_anon,
                "anon_endpoint": body_anon.get("endpoint"),
                "admin_http": status_admin,
                "admin_endpoint": body_admin.get("endpoint"),
            },
            "test_client": {
                "SCRIPT_NAME": c_env["SCRIPT_NAME"],
                "PATH_INFO": c_env["PATH_INFO"],
                "routed": route_only(c_env),
            },
            "environ_diverges": (
                s_env["PATH_INFO"] != c_env["PATH_INFO"]
                or s_env["SCRIPT_NAME"] != c_env["SCRIPT_NAME"]
            ),
        }
        rows.append(row)

    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

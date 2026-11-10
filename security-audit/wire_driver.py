"""Byte-level WSGI driver that never goes through the test client.

The audit's server-side ground truth is produced here from the *raw request
target bytes* exactly as a PEP 3333 server would:

1. decode the ASCII/latin-1 bytes to the WSGI native ``str`` (no percent
   decoding yet) for ``REQUEST_URI`` / ``RAW_URI``;
2. percent-decode the path **once** to build ``PATH_INFO``;
3. split an optional mount prefix into ``SCRIPT_NAME``.

Neither dot segments nor repeated slashes are normalized here, matching
``wsgiref`` and ``werkzeug.serving.make_environ``.  Use this rather than the
test client when you need to reason about the bytes the server actually
received.

Run:  .venv/bin/python security-audit/wire_driver.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import SecureApp  # noqa: E402
from app import URL_MAP  # noqa: E402
from app import naive_gateway_allows  # noqa: E402
from werkzeug.wrappers import Request  # noqa: E402

# (label, raw request-target bytes, wire SCRIPT_NAME before decoding)
WIRE_CASES = [
    ("public baseline", b"/public/hello", b""),
    ("admin baseline", b"/admin", b""),
    ("encoded letter %61", b"/%61dmin/users", b""),
    ("encoded slash %2f", b"/admin%2fusers", b""),
    ("double-encoded slash", b"/admin%252fusers", b""),
    ("leading double slash", b"//admin/users", b""),
    ("dot traversal", b"/public/../admin/users", b""),
    ("encoded dot traversal", b"/public/%2e%2e/admin/users", b""),
    ("path converter encoded slash", b"/files/a%2fb", b""),
    ("mounted encoded letter", b"/app/%61dmin/users", b"/app"),
]


def wire_to_environ(raw_target: bytes, raw_script: bytes, identity: str | None):
    target = raw_target.decode("latin1")
    script = unquote(raw_script.decode("latin1"))
    path = unquote(target.split("?", 1)[0])
    if script and path.startswith(script):
        path = path[len(script) :] or "/"

    environ = {
        "REQUEST_METHOD": "GET",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "SCRIPT_NAME": script,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "REQUEST_URI": target,
        "RAW_URI": target,
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": sys.stderr,
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if identity is not None:
        environ["web.identity"] = identity
    return environ


def route(environ):
    adapter = URL_MAP.bind_to_request(Request(environ))
    try:
        endpoint, args = adapter.match()
        return f"{endpoint} {args}".rstrip()
    except Exception as exc:  # noqa: BLE001
        new_url = getattr(exc, "new_url", "")
        code = getattr(exc, "code", type(exc).__name__)
        return f"{code} -> {new_url}" if new_url else f"{code}"


def call(environ):
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    raw = b"".join(SecureApp()(environ, start_response)).decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    body.setdefault("redirect_location", captured["headers"].get("Location", ""))
    return captured["status"].split()[0], body


def main() -> None:
    rows = []
    for label, target, script in WIRE_CASES:
        anon_env = wire_to_environ(target, script, None)
        admin_env = wire_to_environ(target, script, "admin")
        anon_status, anon_body = call(anon_env)
        admin_status, _ = call(admin_env)
        rows.append(
            {
                "label": label,
                "raw_bytes": target.decode("latin1"),
                "gateway_raw_prefix_allows": naive_gateway_allows(
                    target.decode("latin1")
                ),
                "SCRIPT_NAME": anon_env["SCRIPT_NAME"],
                "PATH_INFO": anon_env["PATH_INFO"],
                "routed": route(anon_env),
                "anon_http": anon_status,
                "admin_http": admin_status,
                "anon_endpoint": anon_body.get("endpoint"),
            }
        )
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

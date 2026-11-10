"""Executable assertions for the gateway/routing divergence findings.

Runnable either as plain Python or under pytest:

    .venv/bin/python security-audit/test_authorization_placement.py
    .venv/bin/pytest security-audit/test_authorization_placement.py

The central invariant: any request that *routes* to a protected endpoint must
be denied for an anonymous caller, regardless of how the raw request target was
encoded or normalized.  The outer gateway's raw-prefix decision is allowed to
disagree, but it must never be the authority that protects the endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import PROTECTED_ENDPOINTS  # noqa: E402
from audit import call_app  # noqa: E402
from audit import client_environ  # noqa: E402
from audit import route_only  # noqa: E402
from audit import server_environ  # noqa: E402
from app import naive_gateway_allows  # noqa: E404


def _routed_endpoint(environ):
    routed = route_only(environ).split()[0]
    return routed if routed in PROTECTED_ENDPOINTS or routed.startswith(
        ("admin_",)
    ) else None


def test_encoded_letter_bypasses_gateway_but_app_still_denies():
    # Gateway sees /%61dmin/... (no literal /admin prefix) and allows it;
    # the WSGI server decodes %61 -> 'a', Werkzeug routes to admin_sub.
    raw = "/%61dmin/users"
    assert naive_gateway_allows(raw) is True
    status, body = call_app(server_environ(raw, "", None))
    assert status == "403"
    assert body["endpoint"] == "admin_sub"


def test_encoded_slash_still_reaches_protected_endpoint():
    raw = "/admin%2fusers"
    status, body = call_app(server_environ(raw, "", None))
    assert status == "403"
    assert body["endpoint"] == "admin_sub"
    # An admin identity is admitted, proving the route itself matches.
    status_admin, body_admin = call_app(server_environ(raw, "", "admin"))
    assert status_admin == "200"
    assert body_admin["endpoint"] == "admin_sub"


def test_double_encoding_does_not_smuggle_a_slash():
    # %252f -> %2f after the single WSGI decode; the slash stays literal,
    # so no admin rule matches. This is the safe control case.
    status, body = call_app(server_environ("/admin%252fusers", "", None))
    assert status == "404"
    assert body.get("endpoint") is None


def test_leading_double_slash_routes_to_admin_regardless_of_gateway():
    raw = "//admin/users"
    assert naive_gateway_allows(raw) is True
    status, body = call_app(server_environ(raw, "", None))
    assert status == "403"
    assert body["endpoint"] == "admin_sub"


def test_dot_segments_are_not_normalized_by_wsgi_or_routing():
    # Werkzeug does not collapse '..', so the literal path fails to match.
    # The protection is not bypassed; it simply 404s.
    for raw in ("/public/../admin/users", "/public/%2e%2e/admin/users"):
        status, body = call_app(server_environ(raw, "", None))
        assert status == "404"
        assert body.get("endpoint") is None


def test_mounted_app_decodes_before_stripping_script_name():
    raw, script = "/app/%61dmin/users", "/app"
    status, body = call_app(server_environ(raw, script, None))
    assert status == "403"
    assert body["endpoint"] == "admin_sub"


def test_legitimate_encoded_path_keeps_working():
    # Control: encoding a slash inside a public path-converter route is a
    # legitimate use and must remain accessible.
    status, body = call_app(server_environ("/files/a%2fb", "", None))
    assert status == "200"
    assert body["endpoint"] == "files"
    assert body["view_args"] == {"filename": "a/b"}


def test_test_client_double_slash_is_not_server_truth():
    # The EnvironBuilder reprocesses its string input via urlsplit and turns
    # '//admin/users' into PATH_INFO '/users'; a real server keeps the bytes.
    server_env = server_environ("//admin/users", "", None)
    client_env = client_environ("//admin/users", None, None)
    assert server_env["PATH_INFO"] in ("//admin/users", "/admin/users")
    assert client_env["PATH_INFO"] == "/users"
    assert route_only(server_env).startswith("admin_sub")
    assert route_only(client_env) == "404"


def _run_all():
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print(f"\n{len(checks)} checks passed")


if __name__ == "__main__":
    _run_all()

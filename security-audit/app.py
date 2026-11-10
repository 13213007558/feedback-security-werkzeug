"""Minimal Werkzeug application used by the gateway/routing divergence audit.

The app deliberately keeps three kinds of rules only:

* ordinary single-segment routes (``/public/...``)
* a ``path`` converter route that itself matches slashes (``/files/...``)
* a protected prefix (``/admin``) whose rules are marked ``requires_admin``

Authorization is decided from the *routed endpoint*, never from a raw prefix
comparison.  :func:`naive_gateway_allows` models the outer gateway that only
looks at the undecoded request target, so the audit can show where the two
layers disagree.
"""

from __future__ import annotations

import json
import typing as t

from werkzeug.exceptions import HTTPException
from werkzeug.routing import Map
from werkzeug.routing import Rule
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

# Endpoint -> whether it needs an authenticated administrator.  This is the
# single source of truth for authorization and is keyed off the *route result*,
# not off any string in the URL.
PROTECTED_ENDPOINTS: t.Final[frozenset[str]] = frozenset(
    {"admin_home", "admin_sub"}
)

URL_MAP = Map(
    [
        Rule("/", endpoint="index"),
        Rule("/public/hello", endpoint="public_hello"),
        Rule("/public/items/<name>", endpoint="public_item"),
        Rule("/files/<path:filename>", endpoint="files"),
        Rule("/admin", endpoint="admin_home"),
        Rule("/admin/<path:sub>", endpoint="admin_sub"),
    ]
)


def naive_gateway_allows(raw_request_target: str) -> bool:
    """Model of the outer gateway: compare the *raw* target's prefix.

    A real gateway sees the undecoded request line.  The deliberately naive
    policy here is "anything whose path does not literally start with
    ``/admin`` is public".  It performs no percent-decoding and no path
    normalization, which is exactly the class of bug under investigation.
    """
    path = raw_request_target.split("?", 1)[0]
    return not path.startswith("/admin")


def _identity(request: Request) -> str:
    # The harness injects the authenticated identity here; default is anonymous.
    return request.environ.get("web.identity", "anon")


class SecureApp:
    """Callable WSGI app that authorizes from the matched endpoint."""

    def __init__(self, url_map: Map = URL_MAP) -> None:
        self.url_map = url_map

    def __call__(
        self, environ: t.Mapping[str, t.Any], start_response: t.Callable
    ) -> list[bytes]:
        request = Request(environ)
        response = self.dispatch(request)
        return response(environ, start_response)

    def dispatch(self, request: Request) -> Response:
        adapter = self.url_map.bind_to_request(request)
        try:
            endpoint, view_args = adapter.match()
        except HTTPException as exc:
            # RequestRedirect (308 merge/strict-slash), NotFound (404),
            # MethodNotAllowed (405) all surface here *before* any endpoint
            # is selected, so there is nothing application-level to protect.
            return self._error(exc)

        # Authorization happens here, on the route result.  The raw URL is
        # never inspected for the allow/deny decision.
        if endpoint in PROTECTED_ENDPOINTS and _identity(request) != "admin":
            return Response(
                self._body(
                    endpoint,
                    view_args,
                    request,
                    status="403 forbidden (endpoint requires admin)",
                ),
                status=403,
                mimetype="application/json",
            )

        return Response(
            self._body(endpoint, view_args, request, status="200 ok"),
            status=200,
            mimetype="application/json",
        )

    def _error(self, exc: HTTPException) -> Response:
        new_url = getattr(exc, "new_url", "")
        if new_url:
            return exc.get_response()
        return Response(
            json.dumps(
                {
                    "endpoint": None,
                    "view_args": {},
                    "status": f"{exc.code} {exc.name.lower()}",
                    "redirect_location": "",
                }
            ),
            status=exc.code,
            mimetype="application/json",
        )

    def _body(self, endpoint, view_args, request: Request, status: str) -> str:
        return json.dumps(
            {
                "endpoint": endpoint,
                "view_args": view_args,
                "identity": _identity(request),
                "status": status,
                "request_path": request.path,
                "root_path": request.root_path,
            }
        )


application = SecureApp()

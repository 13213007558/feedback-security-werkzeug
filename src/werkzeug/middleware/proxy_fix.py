"""
X-Forwarded-For Proxy Fix
=========================

This module provides a middleware that adjusts the WSGI environ based on
``X-Forwarded-`` headers that proxies in front of an application may
set.

It is recommended that the WSGI application and server sit behind a real HTTP
server for efficiency and security, and there may be other proxies in front of
that depending on the services you use to host the server. In this case, WSGI
may see the request as coming from that server rather than the real client.
Proxies set various headers to track where the request actually came from.

This middleware should only be used if the application is actually
behind such a proxy, and should be configured with the number of proxies
that are chained in front of it. Not all proxies set all the headers.
Since incoming headers can be faked, you must set how many proxies are
setting each header so the middleware knows what to trust.

If a header has invalid syntax, it is ignored. That is, it will be as if
``ProxyFix`` wasn't set for that header, so you'll see the original incorrect
but trusted value from the closest proxy.

When the same application is reachable both through a reverse proxy and
through a direct internal entry point, forwarded headers on direct
connections can be faked by the client. Passing ``trusted_proxies`` limits
which connecting peers are allowed to provide ``X-Forwarded-`` headers. The
check is based solely on the unmodified ``REMOTE_ADDR`` of the direct
connection, never on ``X-Forwarded-For``, so a client cannot vouch for
itself.

.. autoclass:: ProxyFix

:copyright: 2007 Pallets
:license: BSD-3-Clause
"""

from __future__ import annotations

import ipaddress
import typing as t
import wsgiref.types as wt

from ..http import parse_list_header


class ProxyFix:
    """Adjust the WSGI environ based on ``X-Forwarded-`` headers that proxies in
    front of the application may set.

    -   ``X-Forwarded-For`` sets ``REMOTE_ADDR``.
    -   ``X-Forwarded-Proto`` sets ``wsgi.url_scheme``.
    -   ``X-Forwarded-Host`` sets ``HTTP_HOST``, ``SERVER_NAME``, and
        ``SERVER_PORT``.
    -   ``X-Forwarded-Port`` sets ``HTTP_HOST`` and ``SERVER_PORT``.
    -   ``X-Forwarded-Prefix`` sets ``SCRIPT_NAME``.

    You must tell the middleware how many proxies set each header so it
    knows what values to trust. It is a security issue to trust values
    that came from the client rather than a proxy.

    The original values of the headers are stored in the WSGI
    environ as ``werkzeug.proxy_fix.orig``, a dict.

    :param app: The WSGI application to wrap.
    :param x_for: Number of values to trust for ``X-Forwarded-For``.
    :param x_proto: Number of values to trust for ``X-Forwarded-Proto``.
    :param x_host: Number of values to trust for ``X-Forwarded-Host``.
    :param x_port: Number of values to trust for ``X-Forwarded-Port``.
    :param x_prefix: Number of values to trust for
        ``X-Forwarded-Prefix``.
    :param trusted_proxies: Configure which direct peers are trusted to
        set ``X-Forwarded-`` headers. Each item is an IPv4 or IPv6
        address (``"10.0.1.1"``) or network (``"10.0.0.0/8"``,
        ``"fd00::/8"``). ``ipaddress`` objects may be passed as well.
        When given, forwarded headers are applied only if the unmodified
        ``REMOTE_ADDR`` matches one of these entries; otherwise all
        forwarded headers are ignored for that request. When not given,
        every connection is treated as a proxy as in previous versions.
        An invalid entry raises ``ValueError`` while constructing the
        middleware, before any request is handled.

    .. code-block:: python

        from werkzeug.middleware.proxy_fix import ProxyFix
        # App is behind one proxy that sets the -For and -Host headers.
        app = ProxyFix(app, x_for=1, x_host=1)

    .. versionadded:: 3.2
        The ``trusted_proxies`` parameter restricts which direct peers may
        provide ``X-Forwarded-`` headers based on their ``REMOTE_ADDR``.

    .. versionchanged:: 3.2
        ``SERVER_NAME`` and ``SERVER_PORT`` are not modified.

    .. versionchanged:: 1.0
        The ``num_proxies`` argument and attribute; the ``get_remote_addr`` method; and
        the environ keys ``orig_remote_addr``, ``orig_wsgi_url_scheme``, and
        ``orig_http_host`` were removed.

    .. versionchanged:: 0.15
        All headers support multiple values. Each header is configured with a separate
        number of trusted proxies.

        Original WSGI environ values are stored in the ``werkzeug.proxy_fix.orig`` dict.

        Support ``X-Forwarded-Port`` and ``X-Forwarded-Prefix``.

        ``X-Forwarded-Host`` and ``X-Forwarded-Port`` modify
        ``SERVER_NAME`` and ``SERVER_PORT``.
    """

    def __init__(
        self,
        app: wt.WSGIApplication,
        x_for: int = 1,
        x_proto: int = 1,
        x_host: int = 0,
        x_port: int = 0,
        x_prefix: int = 0,
        trusted_proxies: t.Iterable[str | ipaddress.IPv4Network | ipaddress.IPv6Network]
        | None = None,
    ) -> None:
        self.app = app
        self.x_for = x_for
        self.x_proto = x_proto
        self.x_host = x_host
        self.x_port = x_port
        self.x_prefix = x_prefix

        # Parse once at construction so a bad configuration fails before the
        # application starts serving requests instead of on the first request.
        self.trusted_proxies: tuple[
            ipaddress.IPv4Network | ipaddress.IPv6Network, ...
        ] = (
            tuple(ipaddress.ip_network(item) for item in trusted_proxies)
            if trusted_proxies is not None
            else ()
        )

    def _is_trusted(self, environ: wt.WSGIEnvironment) -> bool:
        """Whether the direct peer of this request may provide forwarded
        headers. Only the original ``REMOTE_ADDR`` is used; the
        ``X-Forwarded-For`` header is never considered.

        A missing, malformed, or IPv4-mapped address is never trusted, and a
        network is only matched against an address of the same IP version.
        """
        if not self.trusted_proxies:
            return True

        remote_addr = environ.get("REMOTE_ADDR")

        if not remote_addr:
            return False

        try:
            address = ipaddress.ip_address(remote_addr)
        except ValueError:
            return False

        # Never treat an IPv4 address embedded in IPv6 form as its IPv4
        # counterpart; such ambiguity must be resolved explicitly.
        if getattr(address, "ipv4_mapped", None) is not None:
            return False

        return any(
            address.version == network.version and address in network
            for network in self.trusted_proxies
        )

    def _get_real_value(self, trusted: int, value: str | None) -> str | None:
        """Get the real value from a list header based on the configured
        number of trusted proxies.

        :param trusted: Number of values to trust in the header.
        :param value: Comma separated list header value to parse.
        :return: The real value, or ``None`` if there are fewer values
            than the number of trusted proxies.

        .. versionchanged:: 1.0
            Renamed from ``_get_trusted_comma``.

        .. versionadded:: 0.15
        """
        if not (trusted and value):
            return None
        values = parse_list_header(value)
        if len(values) >= trusted:
            return values[-trusted]
        return None

    def __call__(
        self, environ: wt.WSGIEnvironment, start_response: wt.StartResponse
    ) -> t.Iterable[bytes]:
        """Modify the WSGI environ based on the various ``Forwarded``
        headers before calling the wrapped application. Store the
        original environ values in ``werkzeug.proxy_fix.orig_{key}``.
        """
        environ_get = environ.get
        environ.update(
            {
                "werkzeug.proxy_fix.orig": {
                    "REMOTE_ADDR": environ_get("REMOTE_ADDR"),
                    "wsgi.url_scheme": environ_get("wsgi.url_scheme"),
                    "HTTP_HOST": environ_get("HTTP_HOST"),
                    "SCRIPT_NAME": environ_get("SCRIPT_NAME"),
                }
            }
        )

        # A direct, untrusted peer may have sent forged forwarded headers;
        # ignore the whole set for this request. Each header still uses its
        # own configured hop count below.
        if not self._is_trusted(environ):
            return self.app(environ, start_response)

        if x_for := self._get_real_value(
            self.x_for, environ_get("HTTP_X_FORWARDED_FOR")
        ):
            environ["REMOTE_ADDR"] = x_for

        if x_proto := self._get_real_value(
            self.x_proto, environ_get("HTTP_X_FORWARDED_PROTO")
        ):
            environ["wsgi.url_scheme"] = x_proto

        if x_host := self._get_real_value(
            self.x_host, environ_get("HTTP_X_FORWARDED_HOST")
        ):
            environ["HTTP_HOST"] = x_host

        if x_port := self._get_real_value(
            self.x_port, environ_get("HTTP_X_FORWARDED_PORT")
        ):
            if host := environ_get("HTTP_HOST"):
                # "]" to check for IPv6 address without port
                if ":" in host and not host.endswith("]"):
                    host = host.rpartition(":")[0]

                environ["HTTP_HOST"] = f"{host}:{x_port}"

        if x_prefix := self._get_real_value(
            self.x_prefix, environ_get("HTTP_X_FORWARDED_PREFIX")
        ):
            environ["SCRIPT_NAME"] = x_prefix

        return self.app(environ, start_response)

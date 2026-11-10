Tell Werkzeug it is Behind a Proxy
==================================

When using a reverse proxy, or many Python hosting platforms, the proxy
will intercept and forward all external requests to the local WSGI
server.

From the WSGI server and application's perspectives, requests are now
coming from the HTTP server to the local address, rather than from
the remote address to the external server address.

HTTP servers should set ``X-Forwarded-`` headers to pass on the real
values to the application. The application can then be told to trust and
use those values by wrapping it with the
:doc:`../middleware/proxy_fix` middleware provided by Werkzeug.

This middleware should only be used if the application is actually
behind a proxy, and should be configured with the number of proxies that
are chained in front of it. Not all proxies set all the headers. Since
incoming headers can be faked, you must set how many proxies are setting
each header so the middleware knows what to trust.

.. code-block:: python

    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

Remember, only apply this middleware if you are behind a proxy, and set
the correct number of proxies that set each header. It can be a security
issue if you get this configuration wrong.

Trusting Specific Proxies
-------------------------

The same application may be reachable both through the reverse proxy and
through a direct internal entry point. A direct client can send forged
``X-Forwarded-`` headers, so trusting the headers based only on the hop
counts is not enough in that setup. Pass ``trusted_proxies`` to state which
direct peers are allowed to provide forwarded headers. Each entry is an
explicit IPv4 or IPv6 address or a network (CIDR) written as a string, or an
``ipaddress`` network object:

.. code-block:: python

    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_prefix=1,
        trusted_proxies=[
            # Local reverse proxy on the loopback interface.
            "127.0.0.1",
            "::1",
            # An internal IPv4 proxy network.
            "10.0.0.0/8",
            # A remote IPv6 proxy (exact address and routed subnet).
            "2001:db8:1::42",
            "2001:db8:2::/48",
        ],
    )

For each request, the middleware checks the unmodified
``REMOTE_ADDR`` against this list before doing anything. The
``X-Forwarded-For`` header is never used to make this decision, so a client
cannot vouch for itself.

- If the direct peer matches a trusted entry, the ``X-Forwarded-`` headers
  are applied using each header's own configured hop count. The ``x_for``
  count does not apply to ``X-Forwarded-Host``, ``X-Forwarded-Port``, or
  ``X-Forwarded-Prefix``.
- If the direct peer is not trusted, or its address is missing, malformed,
  or an IPv4-mapped IPv6 address, all ``X-Forwarded-`` headers are ignored
  for that request and the application sees the actual connection values.
  A direct request with forged forwarded headers therefore still shows its
  real source address.
- If ``trusted_proxies`` is not given, every connection is treated as a
  proxy, preserving the previous behavior.

Invalid addresses or networks raise ``ValueError`` when the middleware is
constructed, so a configuration error is reported before any request is
served. For example, a network with host bits set such as
``"10.0.0.1/24"`` is rejected; use the network address ``"10.0.0.0/24"``
instead.

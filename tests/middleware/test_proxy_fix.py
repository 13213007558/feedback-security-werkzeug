import pytest

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import Map
from werkzeug.routing import Rule
from werkzeug.test import Client
from werkzeug.test import create_environ
from werkzeug.utils import redirect
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response


@pytest.mark.parametrize(
    ("kwargs", "base", "url_root"),
    (
        pytest.param(
            {},
            {
                "REMOTE_ADDR": "192.168.0.2",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.1",
                "HTTP_X_FORWARDED_PROTO": "https",
            },
            "https://spam/",
            id="for",
        ),
        pytest.param(
            {"x_proto": 1},
            {"HTTP_HOST": "spam", "HTTP_X_FORWARDED_PROTO": "https"},
            "https://spam/",
            id="proto",
        ),
        pytest.param(
            {"x_host": 1},
            {"HTTP_HOST": "spam", "HTTP_X_FORWARDED_HOST": "eggs"},
            "http://eggs/",
            id="host",
        ),
        pytest.param(
            {"x_port": 1},
            {"HTTP_HOST": "spam", "HTTP_X_FORWARDED_PORT": "8080"},
            "http://spam:8080/",
            id="port, host without port",
        ),
        pytest.param(
            {"x_port": 1},
            {"HTTP_HOST": "spam:9000", "HTTP_X_FORWARDED_PORT": "8080"},
            "http://spam:8080/",
            id="port, host with port",
        ),
        pytest.param(
            {"x_port": 1},
            {
                "SERVER_NAME": "spam",
                "SERVER_PORT": "9000",
                "HTTP_X_FORWARDED_PORT": "8080",
            },
            "http://spam:9000/",
            id="port, no host",
        ),
        pytest.param(
            {"x_prefix": 1},
            {"HTTP_HOST": "spam", "HTTP_X_FORWARDED_PREFIX": "/eggs"},
            "http://spam/eggs/",
            id="prefix",
        ),
        pytest.param(
            {"x_for": 1, "x_proto": 1, "x_host": 1, "x_port": 1, "x_prefix": 1},
            {
                "REMOTE_ADDR": "192.168.0.2",
                "HTTP_HOST": "spam:9000",
                "HTTP_X_FORWARDED_FOR": "192.168.0.1",
                "HTTP_X_FORWARDED_PROTO": "https",
                "HTTP_X_FORWARDED_HOST": "eggs",
                "HTTP_X_FORWARDED_PORT": "443",
                "HTTP_X_FORWARDED_PREFIX": "/ham",
            },
            "https://eggs/ham/",
            id="all",
        ),
        pytest.param(
            {"x_for": 2},
            {
                "REMOTE_ADDR": "192.168.0.3",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.1, 192.168.0.2",
            },
            "http://spam/",
            id="multiple for",
        ),
        pytest.param(
            {"x_for": 0},
            {
                "REMOTE_ADDR": "192.168.0.1",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.2",
            },
            "http://spam/",
            id="ignore 0",
        ),
        pytest.param(
            {"x_for": 3},
            {
                "REMOTE_ADDR": "192.168.0.1",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.3, 192.168.0.2",
            },
            "http://spam/",
            id="ignore len < trusted",
        ),
        pytest.param(
            {},
            {
                "REMOTE_ADDR": "192.168.0.2",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.3, 192.168.0.1",
            },
            "http://spam/",
            id="ignore untrusted",
        ),
        pytest.param(
            {"x_for": 2},
            {
                "REMOTE_ADDR": "192.168.0.1",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": ", 192.168.0.3",
            },
            "http://spam/",
            id="ignore empty",
        ),
        pytest.param(
            {"x_for": 2, "x_prefix": 1},
            {
                "REMOTE_ADDR": "192.168.0.2",
                "HTTP_HOST": "spam",
                "HTTP_X_FORWARDED_FOR": "192.168.0.1, 192.168.0.3",
                "HTTP_X_FORWARDED_PREFIX": "/ham, /eggs",
            },
            "http://spam/eggs/",
            id="prefix < for",
        ),
        pytest.param(
            {"x_host": 1},
            {"HTTP_HOST": "spam", "HTTP_X_FORWARDED_HOST": "[2001:db8::a]"},
            "http://[2001:db8::a]/",
            id="ipv6 host",
        ),
        pytest.param(
            {"x_port": 1},
            {"HTTP_HOST": "[2001:db8::a]", "HTTP_X_FORWARDED_PORT": "8080"},
            "http://[2001:db8::a]:8080/",
            id="ipv6 port, host without port",
        ),
        pytest.param(
            {"x_port": 1},
            {"HTTP_HOST": "[2001:db8::a]:9000", "HTTP_X_FORWARDED_PORT": "8080"},
            "http://[2001:db8::a]:8080/",
            id="ipv6 - port, host with port",
        ),
    ),
)
def test_proxy_fix(monkeypatch, kwargs, base, url_root):
    monkeypatch.setattr(Response, "autocorrect_location_header", True)

    @Request.application
    def app(request):
        # for header
        assert request.remote_addr == "192.168.0.1"
        # proto, host, port, prefix headers
        assert request.url_root == url_root

        urls = url_map.bind_to_request(request)
        parrot_url = urls.build("parrot")
        # build includes prefix
        assert urls.build("parrot") == "/".join((request.script_root, "parrot"))
        # match doesn't include prefix
        assert urls.match("/parrot")[0] == "parrot"

        # With autocorrect_location_header enabled, location header will
        # start with url_root
        return redirect(parrot_url)

    url_map = Map([Rule("/parrot", endpoint="parrot")])
    app = ProxyFix(app, **kwargs)

    base.setdefault("REMOTE_ADDR", "192.168.0.1")
    environ = create_environ(environ_overrides=base)

    # host is always added, remove it if the test doesn't set it
    if "HTTP_HOST" not in base:
        del environ["HTTP_HOST"]

    with pytest.deprecated_call():
        response = Client(app).open(Request(environ))

    assert response.location == f"{url_root}parrot"


def test_trusted_proxies_interleaved() -> None:
    """Direct and proxied requests alternate through one client. Only
    requests whose direct peer is trusted use the forwarded headers; the
    original environ is always stored before any rewriting."""
    seen: list[dict[str, object]] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(
            {
                "remote_addr": request.remote_addr,
                "url": request.url,
                "script_root": request.script_root,
                "orig": request.environ["werkzeug.proxy_fix.orig"],
            }
        )
        return Response()

    trusted = ["127.0.0.1", "10.0.0.0/8", "2001:db8::/32"]
    wrapped = ProxyFix(
        app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=1,
        trusted_proxies=trusted,
    )
    client = Client(wrapped)

    forwarded_headers = {
        "X-Forwarded-For": "198.51.100.20",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "example.com",
        "X-Forwarded-Port": "443",
        "X-Forwarded-Prefix": "/app",
    }

    # Direct request with forged forwarded headers from an untrusted peer.
    client.open(
        path="/path",
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
        headers=forwarded_headers,
    )
    # Trusted reverse proxy carrying the real client information.
    client.open(
        path="/path",
        environ_overrides={"REMOTE_ADDR": "10.0.0.5"},
        headers=forwarded_headers,
    )
    # Another direct request proves the decision is per-connection.
    client.open(
        path="/path",
        environ_overrides={"REMOTE_ADDR": "192.0.2.9"},
        headers=forwarded_headers,
    )

    direct, proxied, direct_again = seen

    assert direct["remote_addr"] == "203.0.113.7"
    assert direct["url"] == "http://localhost/path"
    assert direct["script_root"] == ""
    assert direct["orig"]["REMOTE_ADDR"] == "203.0.113.7"
    assert direct["orig"]["HTTP_HOST"] == "localhost"
    assert direct["orig"]["SCRIPT_NAME"] == ""

    assert proxied["remote_addr"] == "198.51.100.20"
    assert proxied["url"] == "https://example.com/app/path"
    assert proxied["script_root"] == "/app"
    assert proxied["orig"]["REMOTE_ADDR"] == "10.0.0.5"
    assert proxied["orig"]["HTTP_HOST"] == "localhost"
    assert proxied["orig"]["SCRIPT_NAME"] == ""

    assert direct_again["remote_addr"] == "192.0.2.9"
    assert direct_again["url"] == "http://localhost/path"
    assert direct_again["script_root"] == ""


def test_trusted_proxies_independent_state() -> None:
    """Two middleware instances wrapping the same app keep separate trusted
    proxy configuration and never share mutable per-request state."""
    seen: list[dict[str, object]] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append({"remote_addr": request.remote_addr, "url": request.url})
        return Response()

    strict = ProxyFix(app, x_for=1, trusted_proxies=["10.0.0.0/8"])
    permissive = ProxyFix(app, x_for=1, trusted_proxies=["0.0.0.0/0"])

    headers = {"X-Forwarded-For": "198.51.100.20"}
    Client(strict).open(
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"}, headers=headers
    )
    Client(permissive).open(
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"}, headers=headers
    )
    Client(strict).open(
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"}, headers=headers
    )

    assert [item["remote_addr"] for item in seen] == [
        "203.0.113.7",
        "198.51.100.20",
        "203.0.113.7",
    ]
    assert strict.trusted_proxies != permissive.trusted_proxies


@pytest.mark.parametrize(
    "trusted_proxies",
    [
        ["not-an-ip"],
        ["10.0.0.0/8/4"],
        ["10.0.0.1/24"],
        ["gggg::1"],
        ["10.0.0.0/33"],
    ],
)
def test_trusted_proxies_invalid_config(trusted_proxies: list[str]) -> None:
    """Bad configuration is rejected while constructing, before serving."""
    with pytest.raises(ValueError):
        ProxyFix(Response(), trusted_proxies=trusted_proxies)


def test_trusted_proxies_untrusted_remote_addr_cases() -> None:
    seen: list[str | None] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(request.remote_addr)
        return Response()

    wrapped = ProxyFix(app, x_for=1, trusted_proxies=["10.0.0.0/8", "2001:db8::/32"])
    client = Client(wrapped)
    headers = {"X-Forwarded-For": "198.51.100.20"}

    # Malformed direct address.
    client.open(environ_overrides={"REMOTE_ADDR": "not-an-ip"}, headers=headers)
    # IPv4-mapped IPv6 address is not matched against the IPv4 network.
    client.open(
        environ_overrides={"REMOTE_ADDR": "::ffff:10.0.0.5"},
        headers=headers,
    )
    # IPv6 direct address outside the trusted network.
    client.open(environ_overrides={"REMOTE_ADDR": "2001:dead::1"}, headers=headers)

    assert seen == ["not-an-ip", "::ffff:10.0.0.5", "2001:dead::1"]


def test_trusted_proxies_missing_remote_addr() -> None:
    seen: list[str | None] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(request.remote_addr)
        return Response()

    wrapped = ProxyFix(app, x_for=1, trusted_proxies=["127.0.0.1"])

    environ = create_environ()
    assert "REMOTE_ADDR" not in environ
    environ["HTTP_X_FORWARDED_FOR"] = "198.51.100.20"

    Client(wrapped).open(Request(environ))
    assert seen == [None]


def test_trusted_proxies_ipv6_peer() -> None:
    seen: list[dict[str, object]] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(
            {
                "remote_addr": request.remote_addr,
                "url": request.url,
                "script_root": request.script_root,
            }
        )
        return Response()

    wrapped = ProxyFix(
        app,
        x_for=1,
        x_proto=1,
        x_prefix=1,
        trusted_proxies=["127.0.0.1", "2001:db8::/32"],
    )
    client = Client(wrapped)
    headers = {
        "X-Forwarded-For": "198.51.100.20",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Prefix": "/ipv6",
    }

    client.open(
        path="/path",
        environ_overrides={"REMOTE_ADDR": "2001:db8::beef"},
        headers=headers,
    )

    assert seen == [
        {
            "remote_addr": "198.51.100.20",
            "url": "https://localhost/ipv6/path",
            "script_root": "/ipv6",
        }
    ]


def test_trusted_proxies_separate_hop_counts() -> None:
    """The x_for hop count is never applied to the other headers."""
    seen: list[dict[str, object]] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(
            {
                "remote_addr": request.remote_addr,
                "url": request.url,
                "script_root": request.script_root,
            }
        )
        return Response()

    wrapped = ProxyFix(
        app,
        x_for=2,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=1,
        trusted_proxies=["10.0.0.0/8"],
    )
    client = Client(wrapped)
    headers = {
        "X-Forwarded-For": "198.51.100.20, 198.51.100.21",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "example.com",
        "X-Forwarded-Port": "443",
        "X-Forwarded-Prefix": "/app",
    }

    client.open(
        path="/path",
        environ_overrides={"REMOTE_ADDR": "10.0.0.5"},
        headers=headers,
    )

    assert seen == [
        {
            "remote_addr": "198.51.100.20",
            "url": "https://example.com/app/path",
            "script_root": "/app",
        }
    ]


def test_trusted_proxies_cannot_self_vouch() -> None:
    """The X-Forwarded-For header never establishes trust; only the direct
    REMOTE_ADDR matters."""
    seen: list[str | None] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(request.remote_addr)
        return Response()

    wrapped = ProxyFix(app, x_for=1, trusted_proxies=["10.0.0.0/8"])

    client = Client(wrapped)
    client.open(
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )

    assert seen == ["203.0.113.7"]


def test_trusted_proxies_none_keeps_legacy_behavior() -> None:
    """Without trusted_proxies, every connection is still rewritten."""
    seen: list[str | None] = []

    @Request.application
    def app(request: Request) -> Response:
        seen.append(request.remote_addr)
        return Response()

    wrapped = ProxyFix(app, x_for=1, trusted_proxies=None)
    assert wrapped.trusted_proxies == ()

    Client(wrapped).open(
        environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
        headers={"X-Forwarded-For": "198.51.100.20"},
    )

    assert seen == ["198.51.100.20"]

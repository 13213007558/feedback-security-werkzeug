import io

import pytest
from conftest import mark_dev_server

from werkzeug.middleware.http_proxy import ProxyMiddleware
from werkzeug.test import Client
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body=b"proxied"):
        super().__init__(body)

    def getheaders(self):
        return [("Content-Type", "text/plain")]


class FakeConnection:
    """Records the HTTP messages sent by the proxy without any network."""

    instances = []
    send_fail_at = None

    def __init__(self, host, port, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.calls = []
        self.closed = False
        self.got_response = False
        self._send_count = 0
        FakeConnection.instances.append(self)

    def connect(self):
        self.calls.append(("connect",))

    def putrequest(self, method, url, **kwargs):
        self.calls.append(("putrequest", method, url))

    def putheader(self, key, value):
        self.calls.append(("putheader", key, value))

    def endheaders(self, **kwargs):
        self.calls.append(("endheaders",))

    def send(self, data):
        self._send_count += 1

        if FakeConnection.send_fail_at == self._send_count:
            raise OSError("send failed")

        self.calls.append(("send", bytes(data)))

    def getresponse(self):
        self.got_response = True
        return FakeResponse()

    def close(self):
        self.closed = True

    def sent_headers(self):
        return {
            call[1].lower(): call[2] for call in self.calls if call[0] == "putheader"
        }

    def sent_body(self):
        return b"".join(
            data for call in self.calls if call[0] == "send" for data in call[1:]
        )


def decode_chunked(wire):
    """Parse a chunked-coded message body strictly, returning the decoded
    bytes. Fails if any frame is malformed or bytes remain after the
    terminating chunk."""
    out = bytearray()
    pos = 0

    while True:
        end = wire.index(b"\r\n", pos)
        size = int(wire[pos:end], 16)
        pos = end + 2

        if size == 0:
            assert wire[pos : pos + 2] == b"\r\n"
            pos += 2
            assert pos == len(wire), "bytes after terminating chunk"
            return bytes(out)

        out += wire[pos : pos + size]
        pos += size
        assert wire[pos : pos + 2] == b"\r\n"
        pos += 2


@pytest.fixture
def fake_connection(monkeypatch):
    FakeConnection.instances = []
    FakeConnection.send_fail_at = None
    monkeypatch.setattr(
        "werkzeug.middleware.http_proxy.client.HTTPConnection", FakeConnection
    )
    return FakeConnection


def run_proxy(environ):
    app = ProxyMiddleware(Response("ROOT"), {"/foo": {"target": "http://example.com/"}})
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status

    body = b"".join(app(environ, start_response))
    return captured, body


def terminated_environ(body, content_length=None, terminated=True):
    builder = EnvironBuilder(
        path="/foo/x",
        method="POST",
        input_stream=io.BytesIO(body),
        content_length=content_length,
    )
    environ = builder.get_environ()

    if content_length is None:
        environ.pop("CONTENT_LENGTH", None)

    if terminated:
        environ["wsgi.input_terminated"] = True

    return environ


def test_proxy_chunked_body_framing(fake_connection):
    # The WSGI server de-chunked the request and promises to terminate the
    # stream. The body contains newlines and text that looks like an HTTP
    # request, which must stay inside this message.
    body = (
        b"first line\r\n"
        b"GET /smuggled HTTP/1.1\r\n"
        b"Host: evil.example\r\n"
        b"\r\n"
        b"0\r\n\r\n"
        b"tail"
    )
    environ = terminated_environ(body)
    _, response_body = run_proxy(environ)

    assert response_body == b"proxied"
    con = fake_connection.instances[0]
    headers = con.sent_headers()
    assert "content-length" not in headers
    assert headers["transfer-encoding"] == "chunked"
    # The send record decodes as exactly one terminated chunked message
    # whose payload is the original body.
    assert decode_chunked(con.sent_body()) == body


def test_proxy_known_content_length(fake_connection):
    body = b"known length body\r\nwith newline"
    environ = terminated_environ(body, content_length=len(body), terminated=False)
    run_proxy(environ)

    con = fake_connection.instances[0]
    headers = con.sent_headers()
    assert headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in headers
    assert con.sent_body() == body


def test_proxy_empty_terminated_body(fake_connection):
    environ = terminated_environ(b"")
    run_proxy(environ)

    con = fake_connection.instances[0]
    headers = con.sent_headers()
    assert headers["transfer-encoding"] == "chunked"
    assert con.sent_body() == b"0\r\n\r\n"


def test_proxy_unterminated_empty_body(fake_connection):
    # The server did not declare the input terminated, so there is no body
    # to read and no framing may be invented.
    environ = terminated_environ(b"", terminated=False)
    run_proxy(environ)

    con = fake_connection.instances[0]
    headers = con.sent_headers()
    assert "content-length" not in headers
    assert "transfer-encoding" not in headers
    assert con.sent_body() == b""


def test_proxy_read_error_closes_connection(fake_connection):
    class FailingStream:
        def __init__(self):
            self.reads = 0

        def read(self, size=-1):
            self.reads += 1

            if self.reads == 1:
                return b"partial"

            raise RuntimeError("client disconnected")

    environ = terminated_environ(b"")
    environ["wsgi.input"] = FailingStream()

    with pytest.raises(RuntimeError, match="client disconnected"):
        run_proxy(environ)

    con = fake_connection.instances[0]
    assert con.closed
    assert not con.got_response


def test_proxy_send_error_closes_connection(fake_connection):
    fake_connection.send_fail_at = 1
    environ = terminated_environ(b"x" * 100)
    captured, _ = run_proxy(environ)

    assert captured["status"].startswith("502")
    con = fake_connection.instances[0]
    assert con.closed
    assert not con.got_response


@mark_dev_server
def test_http_proxy(standard_app):
    app = ProxyMiddleware(
        Response("ROOT"),
        {
            "/foo": {
                "target": standard_app.url,
                "host": "faked.invalid",
                "headers": {"X-Special": "foo"},
            },
            "/bar": {
                "target": standard_app.url,
                "host": None,
                "remove_prefix": True,
                "headers": {"X-Special": "bar"},
            },
            "/autohost": {"target": standard_app.url},
        },
    )

    client = Client(app)

    r = client.get("/")
    assert r.data == b"ROOT"

    r = client.get("/foo/bar")
    assert r.json["HTTP_X_SPECIAL"] == "foo"
    assert r.json["HTTP_HOST"] == "faked.invalid"
    assert r.json["PATH_INFO"] == "/foo/bar"

    r = client.get("/bar/baz?a=a&b=b")
    assert r.json["HTTP_X_SPECIAL"] == "bar"
    assert r.json["HTTP_HOST"] == "localhost"
    assert r.json["PATH_INFO"] == "/baz"
    assert r.json["QUERY_STRING"] == "a=a&b=b"

    r = client.get("/autohost/aha")
    assert "HTTP_X_SPECIAL" not in r.json
    assert r.json["HTTP_HOST"] == "127.0.0.1"
    assert r.json["PATH_INFO"] == "/autohost/aha"

    # test if characters allowed in URL are not encoded by proxy
    r = client.get("/autohost/$")
    assert r.json["REQUEST_URI"] == "/autohost/$"

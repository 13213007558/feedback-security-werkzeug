import datetime as dt
import json
from io import BytesIO

import pytest

from werkzeug import wrappers
from werkzeug.datastructures import Accept
from werkzeug.datastructures import ETag
from werkzeug.datastructures import Headers
from werkzeug.datastructures import LanguageAccept
from werkzeug.datastructures import MIMEAccept
from werkzeug.datastructures import MultiDict
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import BadRequest
from werkzeug.exceptions import ParameterSourceConflict
from werkzeug.exceptions import SecurityError
from werkzeug.exceptions import UnsupportedMediaType
from werkzeug.http import COEP
from werkzeug.http import COOP
from werkzeug.test import Client
from werkzeug.test import create_environ
from werkzeug.test import run_wsgi_app
from werkzeug.wsgi import _LimitedStream


def assert_environ(environ, method):
    assert environ["REQUEST_METHOD"] == method
    assert environ["PATH_INFO"] == "/"
    assert environ["SCRIPT_NAME"] == ""
    assert environ["SERVER_NAME"] == "localhost"
    assert environ["wsgi.version"] == (1, 0)
    assert environ["wsgi.url_scheme"] == "http"


def test_base_request():
    @wrappers.Request.application
    def app(request: wrappers.Request) -> wrappers.Response:
        return wrappers.Response()

    client = Client(app)

    # get requests
    response = client.get("/?foo=bar&foo=hehe")
    request = response.request
    assert request.args == MultiDict([("foo", "bar"), ("foo", "hehe")])
    assert request.form == MultiDict()
    assert request.data == b""
    assert_environ(request.environ, "GET")

    # post requests with form data
    response = client.post(
        "/?blub=blah",
        data="foo=blub+hehe&blah=42",
        content_type="application/x-www-form-urlencoded",
    )
    request = response.request
    assert request.args == MultiDict([("blub", "blah")])
    assert request.form == MultiDict([("foo", "blub hehe"), ("blah", "42")])
    assert request.data == b""
    # currently we do not guarantee that the values are ordered correctly
    # for post data.
    # assert response['form_as_list'] == [('foo', ['blub hehe']), ('blah', ['42'])]
    assert_environ(request.environ, "POST")

    # patch requests with form data
    response = client.patch(
        "/?blub=blah",
        data="foo=blub+hehe&blah=42",
        content_type="application/x-www-form-urlencoded",
    )
    request = response.request
    assert request.args == MultiDict([("blub", "blah")])
    assert request.form == MultiDict([("foo", "blub hehe"), ("blah", "42")])
    assert request.data == b""
    assert_environ(request.environ, "PATCH")

    # post requests with json data
    json = b'{"foo": "bar", "blub": "blah"}'
    response = client.post("/?a=b", data=json, content_type="application/json")
    request = response.request
    assert request.data == json
    assert request.args == MultiDict([("a", "b")])
    assert request.form == MultiDict()


def test_query_string_is_bytes():
    req = wrappers.Request.from_values("/?foo=%2f")
    assert req.query_string == b"foo=%2f"


def test_request_repr():
    req = wrappers.Request.from_values("/foobar")
    assert "<Request 'http://localhost/foobar' [GET]>" == repr(req)
    req = wrappers.Request.from_values("/привет")
    assert "<Request 'http://localhost/привет' [GET]>" == repr(req)


def test_access_route():
    req = wrappers.Request.from_values(
        headers={"X-Forwarded-For": "192.168.1.2, 192.168.1.1"},
        environ_base={"REMOTE_ADDR": "192.168.1.3"},
    )
    assert req.access_route == ("192.168.1.2", "192.168.1.1")
    assert req.remote_addr == "192.168.1.3"

    req = wrappers.Request.from_values(environ_base={"REMOTE_ADDR": "192.168.1.3"})
    assert list(req.access_route) == ["192.168.1.3"]


def test_url_request_descriptors():
    req = wrappers.Request.from_values("/bar?foo=baz", "http://example.com/test")
    assert req.path == "/bar"
    assert req.full_path == "/bar?foo=baz"
    assert req.script_root == "/test"
    assert req.url == "http://example.com/test/bar?foo=baz"
    assert req.base_url == "http://example.com/test/bar"
    assert req.url_root == "http://example.com/test/"
    assert req.host_url == "http://example.com/"
    assert req.host == "example.com"
    assert req.scheme == "http"

    req = wrappers.Request.from_values("/bar?foo=baz", "https://example.com/test")
    assert req.scheme == "https"


def test_url_request_descriptors_query_quoting():
    quoted = "http%3A%2F%2Fwww.example.com%2F%3Fnext%3D%2Fbaz%23my%3Dhash"
    unquoted = "http://www.example.com/?next%3D/baz%23my%3Dhash"
    req = wrappers.Request.from_values(f"/bar?next={quoted}", "http://example.com/")
    assert req.path == "/bar"
    assert req.full_path == f"/bar?next={quoted}"
    assert req.url == f"http://example.com/bar?next={unquoted}"


def test_url_request_descriptors_hosts():
    req = wrappers.Request.from_values("/bar?foo=baz", "http://example.com/test")
    req.trusted_hosts = ["example.com"]
    assert req.path == "/bar"
    assert req.full_path == "/bar?foo=baz"
    assert req.script_root == "/test"
    assert req.url == "http://example.com/test/bar?foo=baz"
    assert req.base_url == "http://example.com/test/bar"
    assert req.url_root == "http://example.com/test/"
    assert req.host_url == "http://example.com/"
    assert req.host == "example.com"
    assert req.scheme == "http"

    req = wrappers.Request.from_values("/bar?foo=baz", "https://example.com/test")
    assert req.scheme == "https"

    req = wrappers.Request.from_values("/bar?foo=baz", "http://example.com/test")
    req.trusted_hosts = ["example.org"]
    pytest.raises(SecurityError, lambda: req.url)
    pytest.raises(SecurityError, lambda: req.base_url)
    pytest.raises(SecurityError, lambda: req.url_root)
    pytest.raises(SecurityError, lambda: req.host_url)
    pytest.raises(SecurityError, lambda: req.host)


def test_authorization():
    request = wrappers.Request.from_values(
        headers={"Authorization": "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="}
    )
    a = request.authorization
    assert a.type == "basic"
    assert a.username == "Aladdin"
    assert a.password == "open sesame"


def test_authorization_with_unicode():
    request = wrappers.Request.from_values(
        headers={"Authorization": "Basic 0YDRg9GB0YHQutC40IE60JHRg9C60LLRiw=="}
    )
    a = request.authorization
    assert a.type == "basic"
    assert a.username == "русскиЁ"
    assert a.password == "Буквы"


def test_request_application():
    @wrappers.Request.application
    def application(request):
        return wrappers.Response("Hello World!")

    @wrappers.Request.application
    def failing_application(request):
        raise BadRequest()

    resp = wrappers.Response.from_app(application, create_environ())
    assert resp.data == b"Hello World!"
    assert resp.status_code == 200

    resp = wrappers.Response.from_app(failing_application, create_environ())
    assert b"Bad Request" in resp.data
    assert resp.status_code == 400


def test_request_access_control():
    request = wrappers.Request.from_values(
        headers={
            "Origin": "https://palletsprojects.com",
            "Access-Control-Request-Headers": "X-A, X-B",
            "Access-Control-Request-Method": "PUT",
        }
    )
    assert request.origin == "https://palletsprojects.com"
    assert request.access_control_request_headers == {"X-A", "X-B"}
    assert request.access_control_request_method == "PUT"


def test_response_access_control():
    response = wrappers.Response("Hello World")
    assert response.access_control_allow_credentials is False
    response.access_control_allow_credentials = True
    response.access_control_allow_headers = ["X-A", "X-B"]
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert set(response.headers["Access-Control-Allow-Headers"].split(", ")) == {
        "X-A",
        "X-B",
    }


def test_base_response():
    response = wrappers.Response("öäü")
    assert response.text == "öäü"

    # set cookie
    response = wrappers.Response()
    response.set_cookie(
        "foo",
        value="bar",
        max_age=60,
        expires=0,
        path="/blub",
        domain="example.org",
        samesite="Strict",
    )
    assert response.headers.to_wsgi_list() == [
        ("Content-Type", "text/plain; charset=utf-8"),
        (
            "Set-Cookie",
            "foo=bar; Domain=example.org;"
            " Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60;"
            " Path=/blub; SameSite=Strict",
        ),
    ]

    # delete cookie
    response = wrappers.Response()
    response.delete_cookie("foo")
    assert response.headers.to_wsgi_list() == [
        ("Content-Type", "text/plain; charset=utf-8"),
        (
            "Set-Cookie",
            "foo=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/",
        ),
    ]

    # close call forwarding
    closed = []

    class Iterable:
        def __next__(self):
            raise StopIteration()

        def __iter__(self):
            return self

        def close(self):
            closed.append(True)

    response = wrappers.Response(Iterable())
    response.call_on_close(lambda: closed.append(True))
    app_iter, status, headers = run_wsgi_app(response, create_environ(), buffered=True)
    assert status == "200 OK"
    assert "".join(app_iter) == ""
    assert len(closed) == 2

    # with statement
    del closed[:]
    response = wrappers.Response(Iterable())
    with response:
        pass
    assert len(closed) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [
        (200, "200 OK"),
        (404, "404 Not Found"),
        (588, "588 Unknown"),
        (999, "999 Unknown"),
    ],
)
def test_response_set_status_code(status_code, expected_status):
    response = wrappers.Response()
    response.status_code = status_code
    assert response.status_code == status_code
    assert response.status == expected_status


@pytest.mark.parametrize(
    ("status", "expected_status_code", "expected_status"),
    [
        ("404", 404, "404 Not Found"),
        ("588", 588, "588 Unknown"),
        ("999", 999, "999 Unknown"),
        ("200 OK", 200, "200 OK"),
        ("999 WTF", 999, "999 WTF"),
        ("wtf", 0, "0 wtf"),
        ("200 TEA POT", 200, "200 TEA POT"),
        (200, 200, "200 OK"),
        (400, 400, "400 Bad Request"),
    ],
)
def test_response_set_status(status, expected_status_code, expected_status):
    response = wrappers.Response()
    response.status = status
    assert response.status_code == expected_status_code
    assert response.status == expected_status

    response = wrappers.Response(status=status)
    assert response.status_code == expected_status_code
    assert response.status == expected_status


def test_response_init_status_empty_string():
    # invalid status codes
    with pytest.raises(ValueError) as info:
        wrappers.Response(None, "")

    assert "Empty status argument" in str(info.value)


def test_type_forcing():
    def wsgi_application(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/html")])
        return ["Hello World!"]

    base_response = wrappers.Response("Hello World!", content_type="text/html")

    class SpecialResponse(wrappers.Response):
        def foo(self):
            return 42

    # good enough for this simple application, but don't ever use that in
    # real world examples!
    fake_env = {}

    for orig_resp in wsgi_application, base_response:
        response = SpecialResponse.force_type(orig_resp, fake_env)
        assert response.__class__ is SpecialResponse
        assert response.foo() == 42
        assert response.text == "Hello World!"
        assert response.content_type == "text/html"

    # without env, no arbitrary conversion
    pytest.raises(TypeError, SpecialResponse.force_type, wsgi_application)


def test_accept():
    request = wrappers.Request(
        {
            "HTTP_ACCEPT": "text/xml,application/xml,application/xhtml+xml,"
            "text/html;q=0.9,text/plain;q=0.8,image/png,*/*;q=0.5",
            "HTTP_ACCEPT_ENCODING": "gzip,deflate",
            "HTTP_ACCEPT_LANGUAGE": "en-us,en;q=0.5",
            "SERVER_NAME": "eggs",
            "SERVER_PORT": "80",
        }
    )
    assert request.accept_mimetypes == MIMEAccept(
        [
            ("text/xml", 1),
            ("application/xml", 1),
            ("application/xhtml+xml", 1),
            ("image/png", 1),
            ("text/html", 0.9),
            ("text/plain", 0.8),
            ("*/*", 0.5),
        ]
    )
    assert request.accept_encodings == Accept([("gzip", 1), ("deflate", 1)])
    assert request.accept_languages == LanguageAccept([("en-us", 1), ("en", 0.5)])

    request = wrappers.Request(
        {"HTTP_ACCEPT": "", "SERVER_NAME": "example.org", "SERVER_PORT": "80"}
    )
    assert request.accept_mimetypes == MIMEAccept()


def test_etag_request():
    request = wrappers.Request(
        {
            "HTTP_CACHE_CONTROL": "no-store, no-cache",
            "HTTP_IF_MATCH": 'W/"foo", bar, "baz"',
            "HTTP_IF_NONE_MATCH": 'W/"foo", bar, "baz"',
            "HTTP_IF_MODIFIED_SINCE": "Tue, 22 Jan 2008 11:18:44 GMT",
            "HTTP_IF_UNMODIFIED_SINCE": "Tue, 22 Jan 2008 11:18:44 GMT",
            "SERVER_NAME": "eggs",
            "SERVER_PORT": "80",
        }
    )
    assert request.cache_control.no_store
    assert request.cache_control.no_cache

    for etags in request.if_match, request.if_none_match:
        assert etags.contains_strong("baz")
        assert etags.contains_weak(ETag.from_header('W/"foo"'))
        assert etags.contains_weak("foo")
        assert not etags.contains_strong("foo")

    value = dt.datetime(2008, 1, 22, 11, 18, 44, tzinfo=dt.UTC)
    assert request.if_modified_since == value
    assert request.if_unmodified_since == value


def test_user_agent():
    user_agent = "Mozilla/5.0 (X11; Linux x86_64; rv:94.0) Gecko/20100101 Firefox/94.0"
    request = wrappers.Request({"HTTP_USER_AGENT": user_agent})
    assert request.user_agent == user_agent
    assert str(request.user_agent) == user_agent
    assert request.user_agent.lower() == user_agent.lower()

    with pytest.deprecated_call():
        assert request.user_agent.to_header() == user_agent

    with pytest.deprecated_call():
        assert request.user_agent.string == user_agent

    with pytest.deprecated_call():
        assert request.user_agent.browser is None


def test_stream_wrapping():
    class LowercasingStream:
        def __init__(self, stream):
            self._stream = stream

        def read(self, size=-1):
            return self._stream.read(size).lower()

        def readline(self, size=-1):
            return self._stream.readline(size).lower()

    data = b"foo=Hello+World"
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )
    req.stream = LowercasingStream(req.stream)
    assert req.form["foo"] == "hello world"


def test_data_descriptor_triggers_parsing():
    data = b"foo=Hello+World"
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )

    assert req.data == b""
    assert req.form["foo"] == "Hello World"


def test_get_data_method_parsing_caching_behavior():
    data = b"foo=Hello+World"
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )

    # get_data() caches, so form stays available
    assert req.get_data() == data
    assert req.form["foo"] == "Hello World"
    assert req.get_data() == data

    # here we access the form data first, caching is bypassed
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )
    assert req.form["foo"] == "Hello World"
    assert req.get_data() == b""

    # Another case is uncached get data which trashes everything
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )
    assert req.get_data(cache=False) == data
    assert req.get_data(cache=False) == b""
    assert req.form == {}

    # Or we can implicitly start the form parser which is similar to
    # the old .data behavior
    req = wrappers.Request.from_values(
        "/", method="POST", data=data, content_type="application/x-www-form-urlencoded"
    )
    assert req.get_data(parse_form_data=True) == b""
    assert req.form["foo"] == "Hello World"


class SourceContractRequest(wrappers.Request):
    parameter_source_contract = {
        "approve": "form",
        "next": "query",
        "token": "unique",
    }


def _contract_request(path="/", **kwargs):
    kwargs.setdefault("method", "POST")
    if kwargs["method"] != "GET":
        kwargs.setdefault("content_type", "application/x-www-form-urlencoded")
    return SourceContractRequest.from_values(path, **kwargs)


def test_parameter_source_contract_form_only_in_query():
    req = _contract_request("/?approve=secret-value", data="other=1")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.args

    e = exc_info.value
    assert isinstance(e, BadRequest)
    assert e.parameter == "approve"
    assert e.rule == "form"
    assert e.sources == ("query string",)
    # the description identifies the field and sources, never the value
    assert "'approve'" in e.description
    assert "query string" in e.description
    assert "form body" in e.description
    assert "secret-value" not in e.description


def test_parameter_source_contract_query_only_in_form():
    req = _contract_request(data="next=/other")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.form

    e = exc_info.value
    assert e.parameter == "next"
    assert e.rule == "query"
    assert e.sources == ("form body",)
    assert "/other" not in e.description


def test_parameter_source_contract_same_value_still_conflicts():
    # identical values in both channels are still a conflict
    req = _contract_request("/?approve=1", data="approve=1")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.values

    assert exc_info.value.sources == ("query string", "form body")


def test_parameter_source_contract_decoded_names():
    # percent-encoding differences don't hide a parameter, the decoded
    # name is compared
    req = _contract_request("/?%61pprove=1", data="")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.args

    assert exc_info.value.parameter == "approve"

    req = _contract_request(data="%6eext=1")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.form

    assert exc_info.value.parameter == "next"


def test_parameter_source_contract_unique():
    # sent twice in the query string
    req = _contract_request("/?token=a&token=b", method="GET")

    with pytest.raises(ParameterSourceConflict) as exc_info:
        _ = req.args

    assert exc_info.value.parameter == "token"
    assert exc_info.value.rule == "unique"

    # sent once in each channel
    req = _contract_request("/?token=a", data="token=b")

    with pytest.raises(ParameterSourceConflict):
        _ = req.values

    # sent exactly once is allowed, absent is allowed
    req = _contract_request("/?token=a&next=/home", method="GET")
    assert req.args["token"] == "a"
    req = _contract_request("/", method="GET")
    assert "token" not in req.args


def test_parameter_source_contract_multivalue_allowed():
    # unlisted multi-value parameters are unaffected
    req = _contract_request("/?tags=a&tags=b", data="tags=c&other=1")
    assert req.values.getlist("tags") == ["a", "b", "c"]
    assert req.values["other"] == "1"


def test_parameter_source_contract_unknown_fields_unaffected():
    # the contract does not expand to unlisted fields
    req = _contract_request("/?other=a", data="other=b")
    assert req.values.getlist("other") == ["a", "b"]


def test_parameter_source_contract_consistent_access_order():
    # args, form, values, and get_data all reach the same conclusion,
    # regardless of the order they are accessed in
    req = _contract_request("/?approve=1", data="other=1")

    accesses = (lambda: req.args, lambda: req.form, lambda: req.values, req.get_data)

    for access in accesses:
        with pytest.raises(ParameterSourceConflict):
            access()

    # a clean request allows every access order too
    for first in ("args", "form", "values", "get_data"):
        req = _contract_request("/?next=/home", data="approve=1")
        getattr(req, first) if first != "get_data" else req.get_data()
        assert req.args["next"] == "/home"
        assert req.form["approve"] == "1"


def test_parameter_source_contract_get_request():
    # a form-only parameter in the query string conflicts even though
    # GET values don't include the form
    req = _contract_request("/?approve=1", method="GET")

    with pytest.raises(ParameterSourceConflict):
        _ = req.values


def test_parameter_source_contract_multipart_closes_files():
    captured = {}

    class CaptureRequest(SourceContractRequest):
        def _parse_form_data(self, stream):
            result = super()._parse_form_data(stream)

            if result is not None:
                captured["files"] = result[1]

            return result

    req = CaptureRequest.from_values(
        "/?approve=1",
        method="POST",
        data={"upload": (BytesIO(b"file-content"), "test.txt"), "other": "x"},
        content_type="multipart/form-data",
    )

    with pytest.raises(ParameterSourceConflict):
        _ = req.form

    # upload streams created while parsing are closed on rejection
    assert captured["files"]["upload"].stream.closed

    # the rejection is cached, further access raises again instead of
    # returning a value cached while checking
    for access in (lambda: req.args, lambda: req.form, lambda: req.values):
        with pytest.raises(ParameterSourceConflict):
            access()


def test_parameter_source_contract_multipart_allowed():
    req = SourceContractRequest.from_values(
        "/?next=/home",
        method="POST",
        data={"approve": "1", "tags": ["a", "b"]},
        content_type="multipart/form-data",
    )
    assert req.form["approve"] == "1"
    assert req.form.getlist("tags") == ["a", "b"]


def test_parameter_source_contract_file_not_text_value():
    # a file upload is never merged with a same-named text field
    body = (
        b'--foo\r\nContent-Disposition: form-data; name="approve"\r\n\r\nyes\r\n'
        b'--foo\r\nContent-Disposition: form-data; name="approve";'
        b' filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nfile-content\r\n'
        b"--foo--\r\n"
    )

    with SourceContractRequest.from_values(
        method="POST", data=body, content_type="multipart/form-data; boundary=foo"
    ) as req:
        assert req.form["approve"] == "yes"
        assert req.files["approve"].filename == "a.txt"

    # a file alone does not satisfy or violate a form rule
    body = (
        b'--foo\r\nContent-Disposition: form-data; name="approve";'
        b' filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nfile-content\r\n'
        b"--foo--\r\n"
    )

    with SourceContractRequest.from_values(
        method="POST", data=body, content_type="multipart/form-data; boundary=foo"
    ) as req:
        assert "approve" not in req.form
        assert req.files["approve"].filename == "a.txt"


def test_parameter_source_contract_malformed_body():
    # a multipart content type without a boundary parses to an empty
    # form, the contract still applies to the query string
    req = SourceContractRequest.from_values(
        "/?approve=1",
        method="POST",
        data=(
            b"--foo\r\nContent-Disposition: form-data; name=approve\r\n\r\n1\r\n"
            b"--foo--"
        ),
        content_type="multipart/form-data",
    )

    with pytest.raises(ParameterSourceConflict):
        _ = req.values

    # a truncated multipart body parses to an empty form without
    # crashing or masking the contract
    req = SourceContractRequest.from_values(
        method="POST",
        data=b"--foo\r\nContent-Disposition: form-data; name=approve\r\n\r\n1",
        content_type="multipart/form-data; boundary=foo",
    )
    assert req.form == {}
    assert not req.files


def test_parameter_source_contract_invalid_rule():
    class BadContractRequest(wrappers.Request):
        parameter_source_contract = {"approve": "header"}

    req = BadContractRequest.from_values("/?approve=1")

    with pytest.raises(ValueError, match="Unknown parameter source rule"):
        _ = req.args


def test_authenticate():
    resp = wrappers.Response()
    resp.www_authenticate.type = "basic"
    resp.www_authenticate.parameters["realm"] = "Testing"
    assert resp.headers["WWW-Authenticate"] == "Basic realm=Testing"
    del resp.www_authenticate
    assert "WWW-Authenticate" not in resp.headers


def test_authenticate_quoted_qop():
    """The qop parameter is always quoted for digest auth, even if it doesn't
    need to be.
    """
    resp = wrappers.Response()
    resp.www_authenticate = WWWAuthenticate(
        "digest", {"realm": "REALM", "nonce": "NONCE", "qop": "auth, auth-int"}
    )

    actual = resp.headers["WWW-Authenticate"]
    expected = 'Digest realm="REALM", nonce="NONCE", qop="auth, auth-int"'
    assert actual == expected

    resp.www_authenticate.parameters["qop"] = "auth"
    actual = resp.headers["WWW-Authenticate"]
    expected = 'Digest realm="REALM", nonce="NONCE", qop="auth"'
    assert actual == expected


def test_response_stream():
    response = wrappers.Response()

    with pytest.deprecated_call():
        response.stream.write(b"Hello ")
        response.stream.write(b"World!")

    assert response.response == [b"Hello ", b"World!"]
    assert response.data == b"Hello World!"


def test_common_response_descriptors():
    response = wrappers.Response()
    response.mimetype = "text/html"
    assert response.mimetype == "text/html"
    assert response.content_type == "text/html; charset=utf-8"
    assert response.mimetype_params == {"charset": "utf-8"}
    response.mimetype_params["x-foo"] = "yep"
    del response.mimetype_params["charset"]
    assert response.content_type == "text/html; x-foo=yep"

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)

    assert response.content_length is None
    response.content_length = "42"
    assert response.content_length == 42

    for attr in "date", "expires":
        assert getattr(response, attr) is None
        setattr(response, attr, now)
        assert getattr(response, attr) == now

    assert response.age is None
    age_td = dt.timedelta(days=1, minutes=3, seconds=5)
    response.age = age_td
    assert response.age == age_td
    response.age = 42
    assert response.age == dt.timedelta(seconds=42)

    assert response.retry_after is None
    response.retry_after = now
    assert response.retry_after == now

    assert not response.vary
    response.vary.add("Cookie")
    response.vary.add("Content-Language")
    assert "cookie" in response.vary
    assert response.vary.to_header() == "Cookie, Content-Language"
    response.headers["Vary"] = "Content-Encoding"
    assert response.vary.as_set() == {"content-encoding"}

    response.allow.update(["GET", "POST"])
    assert response.headers["Allow"] == "GET, POST"

    response.content_language.add("en-US")
    response.content_language.add("fr")
    assert response.headers["Content-Language"] == "en-US, fr"


def test_common_request_descriptors():
    request = wrappers.Request.from_values(
        content_type="text/html; charset=utf-8",
        content_length="23",
        headers={
            "Referer": "http://www.example.com/",
            "Date": "Sat, 28 Feb 2009 19:04:35 GMT",
            "Max-Forwards": "10",
            "Content-Encoding": "gzip",
        },
    )

    assert request.content_type == "text/html; charset=utf-8"
    assert request.mimetype == "text/html"
    assert request.mimetype_params == {"charset": "utf-8"}
    assert request.content_length == 23
    assert request.referrer == "http://www.example.com/"
    assert request.date == dt.datetime(2009, 2, 28, 19, 4, 35, tzinfo=dt.UTC)
    assert request.max_forwards == 10
    assert request.content_encoding == "gzip"


def test_request_mimetype_always_lowercase():
    request = wrappers.Request.from_values(content_type="APPLICATION/JSON")
    assert request.mimetype == "application/json"


def test_shallow_mode():
    request = wrappers.Request(
        {"QUERY_STRING": "foo=bar", "SERVER_NAME": "eggs", "SERVER_PORT": "80"},
        shallow=True,
    )
    assert request.args["foo"] == "bar"
    pytest.raises(RuntimeError, lambda: request.stream)
    pytest.raises(RuntimeError, lambda: request.data)
    pytest.raises(RuntimeError, lambda: request.form)


def test_form_parsing_failed():
    with wrappers.Request.from_values(
        data=b"--blah\r\n",
        content_type="multipart/form-data; boundary=foo",
        method="POST",
    ) as request:
        assert not request.files
        assert not request.form

    # Bad Content-Type
    request = wrappers.Request.from_values(
        data=b"test",
        content_type=", ",
        method="POST",
    )
    assert not request.form


def test_file_closing():
    data = (
        b"--foo\r\n"
        b'Content-Disposition: form-data; name="foo"; filename="foo.txt"\r\n'
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"file contents, just the contents\r\n"
        b"--foo--"
    )
    with wrappers.Request.from_values(
        data=data,
        content_type="multipart/form-data; boundary=foo",
        method="POST",
    ) as req:
        foo = req.files["foo"]
        assert foo.mimetype == "text/plain"
        assert foo.filename == "foo.txt"
        assert foo.closed is False

    assert foo.closed is True


def test_file_closing_with():
    data = (
        b"--foo\r\n"
        b'Content-Disposition: form-data; name="foo"; filename="foo.txt"\r\n'
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"file contents, just the contents\r\n"
        b"--foo--"
    )
    with wrappers.Request.from_values(
        data=data,
        content_type="multipart/form-data; boundary=foo",
        method="POST",
    ) as req:
        foo = req.files["foo"]
        assert foo.mimetype == "text/plain"
        assert foo.filename == "foo.txt"

    assert foo.closed is True


def test_response_streamed():
    r = wrappers.Response()
    assert not r.is_streamed
    r = wrappers.Response("Hello World")
    assert not r.is_streamed
    r = wrappers.Response(["foo", "bar"])
    assert not r.is_streamed

    def gen():
        if 0:
            yield None

    r = wrappers.Response(gen())
    assert r.is_streamed


def test_response_iter_wrapping():
    def uppercasing(iterator):
        for item in iterator:
            yield item.upper()

    def generator():
        yield "foo"
        yield "bar"

    req = wrappers.Request.from_values()
    resp = wrappers.Response(generator())
    del resp.headers["Content-Length"]
    resp.response = uppercasing(resp.iter_encoded())
    actual_resp = wrappers.Response.from_app(resp, req.environ, buffered=True)
    assert actual_resp.data == b"FOOBAR"


def test_response_freeze():
    response = wrappers.Response(x for x in ("foo", "bar"))

    with pytest.deprecated_call():
        response.freeze()

    assert response.response == [b"foobar"]
    assert response.content_length == 6
    assert response.etag


def test_response_content_length_uses_encode():
    r = wrappers.Response("你好")
    assert r.calculate_content_length() == 6


def test_other_method_payload():
    data = b"Hello World"
    req = wrappers.Request.from_values(
        data=data,
        content_type="text/plain",
        method="MADE_UP",
    )
    assert req.data == data
    assert isinstance(req.stream, _LimitedStream)


def test_urlfication():
    resp = wrappers.Response()
    resp.location = "http://üser:pässword@☃.net/påth"
    resp.content_location = "http://☃.net/"
    assert (
        resp.headers["Location"]
        == "http://%C3%BCser:p%C3%A4ssword@xn--n3h.net/p%C3%A5th"
    )
    assert resp.headers["Content-Location"] == "http://xn--n3h.net/"


def test_response_iterator():
    resp = wrappers.Response("Hello Wörld!")
    assert resp.response == ["Hello Wörld!".encode()]
    assert resp.text == "Hello Wörld!"
    assert resp.content_length == 13
    assert not resp.is_streamed
    assert resp.is_sequence

    # try the same for manual assignment
    resp.set_data("Wörd")
    assert resp.response == ["Wörd".encode()]
    assert resp.text == "Wörd"
    assert resp.content_length == 5
    assert not resp.is_streamed
    assert resp.is_sequence

    # automatic generator sequence conversion
    resp.response = iter(("Hello ", "Wörld!"))
    assert resp.is_streamed
    assert not resp.is_sequence
    assert resp.text == "Hello Wörld!"
    assert resp.response == ["Hello Wörld!".encode()]
    assert not resp.is_streamed
    assert resp.is_sequence

    # automatic generator sequence conversion
    resp.response = iter(("Hello ", "Wörld!"))
    resp.implicit_sequence_conversion = False
    assert resp.is_streamed
    assert not resp.is_sequence

    with pytest.raises(RuntimeError):
        resp.get_data()

    resp.make_sequence()
    assert resp.text == "Hello Wörld!"
    assert resp.response == ["Hello Wörld!".encode()]
    assert not resp.is_streamed
    assert resp.is_sequence


def test_values():
    r = wrappers.Request.from_values(
        method="POST", query_string={"a": "1"}, data={"a": "2", "b": "2"}
    )
    assert r.values["a"] == "1"
    assert r.values["b"] == "2"

    # form should not be combined for GET method
    r = wrappers.Request.from_values(
        method="GET", query_string={"a": "1"}, data={"a": "2", "b": "2"}
    )
    assert r.values["a"] == "1"
    assert "b" not in r.values


def test_response_headers_passthrough():
    headers = Headers()
    resp = wrappers.Response(headers=headers)
    assert resp.headers is headers


def test_response_304_no_content_length():
    resp = wrappers.Response("Test", status=304)
    resp.apply_status()
    assert "Content-Length" not in resp.headers


def test_ranges():
    # basic range stuff
    req = wrappers.Request.from_values()
    assert req.range is None
    req = wrappers.Request.from_values(headers={"Range": "bytes=0-499"})
    assert req.range.ranges == [(0, 500)]

    resp = wrappers.Response()
    resp.content_range = req.range.make_content_range(1000)
    assert resp.content_range.units == "bytes"
    assert resp.content_range.start == 0
    assert resp.content_range.stop == 500
    assert resp.content_range.length == 1000
    assert resp.headers["Content-Range"] == "bytes 0-499/1000"

    resp.content_range = None
    assert "Content-Range" not in resp.headers

    resp.headers["Content-Range"] = "bytes 0-499/1000"
    assert resp.content_range.units == "bytes"
    assert resp.content_range.start == 0
    assert resp.content_range.stop == 500
    assert resp.content_range.length == 1000


def test_csp():
    resp = wrappers.Response()
    resp.content_security_policy.default_src = "'self'"
    assert resp.headers["Content-Security-Policy"] == "default-src 'self'"
    resp.content_security_policy.script_src = "'self' palletsprojects.com"
    assert (
        resp.headers["Content-Security-Policy"]
        == "default-src 'self'; script-src 'self' palletsprojects.com"
    )

    resp.content_security_policy = None
    assert "Content-Security-Policy" not in resp.headers


def test_auto_content_length():
    resp = wrappers.Response("Hello World!")
    assert resp.content_length == 12

    resp = wrappers.Response(["Hello World!"])
    assert resp.content_length is None
    resp.apply_status()
    assert resp.content_length == 12


def test_stream_content_length():
    resp = wrappers.Response()

    with pytest.deprecated_call():
        resp.stream.writelines([b"foo", b"bar", b"baz"])

    resp.apply_status()
    assert resp.content_length == 9

    resp = wrappers.Response("foo")

    with pytest.deprecated_call():
        resp.stream.writelines([b"bar", b"baz"])

    resp.apply_status()
    assert resp.content_length == 9


def test_disabled_auto_content_length():
    class MyResponse(wrappers.Response):
        automatically_set_content_length = False

    with pytest.deprecated_call():
        resp = MyResponse("Hello World!")

    assert resp.content_length is None

    resp = MyResponse(["Hello World!"])
    assert resp.content_length is None

    with pytest.deprecated_call():
        assert "Content-Length" not in resp.get_wsgi_headers({})


@pytest.mark.parametrize(
    ("auto", "location", "expect"),
    (
        (False, "/test", "/test"),
        (False, "/\\\\test.example?q", "/%5C%5Ctest.example?q"),
        (True, "/test", "http://localhost/test"),
        (True, "test", "http://localhost/a/b/test"),
        (True, "./test", "http://localhost/a/b/test"),
        (True, "../test", "http://localhost/a/test"),
    ),
)
def test_location_header_autocorrect(auto, location, expect):
    env = create_environ("/a/b/c")
    resp = wrappers.Response("Hello World!")
    resp.autocorrect_location_header = auto
    resp.location = location

    with pytest.deprecated_call():
        assert resp.get_wsgi_headers(env)["Location"] == expect


def test_204_and_1XX_response_has_no_content_length():
    response = wrappers.Response(status=204)
    response.content_length = 123
    response.apply_status()
    assert response.content_length is None

    response = wrappers.Response(status=100)
    response.content_length = 123
    response.apply_status()
    assert response.content_length is None


def test_malformed_204_response_has_no_content_length():
    # flask-restful can generate a malformed response when doing `return '', 204`
    response = wrappers.Response(status=204)
    response.set_data(b"test")
    assert response.content_length == 4

    env = create_environ()
    app_iter, status, headers = response.get_wsgi_response(env)
    assert status == "204 No Content"
    assert "Content-Length" not in headers
    assert b"".join(app_iter) == b""  # ensure data will not be sent


def test_request_method_case_sensitivity():
    req = wrappers.Request(
        {"REQUEST_METHOD": "get", "SERVER_NAME": "eggs", "SERVER_PORT": "80"}
    )
    assert req.method == "GET"


def test_write_length():
    response = wrappers.Response()

    with pytest.deprecated_call():
        length = response.stream.write(b"bar")

    assert length == 3


def test_stream_zip():
    import zipfile

    response = wrappers.Response()

    with pytest.deprecated_call():
        with zipfile.ZipFile(response.stream, mode="w") as z:
            z.writestr("foo", b"bar")

    with zipfile.ZipFile(BytesIO(response.data), mode="r") as z:
        assert z.namelist() == ["foo"]
        assert z.read("foo") == b"bar"


class TestSetCookie:
    def test_secure(self):
        response = wrappers.Response()
        response.set_cookie(
            "foo",
            value="bar",
            max_age=60,
            expires=0,
            path="/blub",
            domain="example.org",
            secure=True,
            samesite=None,
        )
        assert response.headers.to_wsgi_list() == [
            ("Content-Type", "text/plain; charset=utf-8"),
            (
                "Set-Cookie",
                "foo=bar; Domain=example.org;"
                " Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60;"
                " Secure; Path=/blub",
            ),
        ]

    def test_httponly(self):
        response = wrappers.Response()
        response.set_cookie(
            "foo",
            value="bar",
            max_age=60,
            expires=0,
            path="/blub",
            domain="example.org",
            secure=False,
            httponly=True,
            samesite=None,
        )
        assert response.headers.to_wsgi_list() == [
            ("Content-Type", "text/plain; charset=utf-8"),
            (
                "Set-Cookie",
                "foo=bar; Domain=example.org;"
                " Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60;"
                " HttpOnly; Path=/blub",
            ),
        ]

    def test_secure_and_httponly(self):
        response = wrappers.Response()
        response.set_cookie(
            "foo",
            value="bar",
            max_age=60,
            expires=0,
            path="/blub",
            domain="example.org",
            secure=True,
            httponly=True,
            samesite=None,
        )
        assert response.headers.to_wsgi_list() == [
            ("Content-Type", "text/plain; charset=utf-8"),
            (
                "Set-Cookie",
                "foo=bar; Domain=example.org;"
                " Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60;"
                " Secure; HttpOnly; Path=/blub",
            ),
        ]

    def test_samesite(self):
        response = wrappers.Response()
        response.set_cookie(
            "foo",
            value="bar",
            max_age=60,
            expires=0,
            path="/blub",
            domain="example.org",
            secure=False,
            samesite="strict",
        )
        assert response.headers.to_wsgi_list() == [
            ("Content-Type", "text/plain; charset=utf-8"),
            (
                "Set-Cookie",
                "foo=bar; Domain=example.org;"
                " Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=60;"
                " Path=/blub; SameSite=Strict",
            ),
        ]


class TestJSON:
    def test_request(self):
        value = {"ä": "b"}
        request = wrappers.Request.from_values(json=value)
        assert request.json == value
        assert request.data

    def test_response(self):
        value = {"ä": "b"}
        response = wrappers.Response(
            response=json.dumps(value), content_type="application/json"
        )
        assert response.json == value

    def test_bad_content_type(self):
        value = [1, 2, 3]
        request = wrappers.Request.from_values(json=value, content_type="text/plain")

        with pytest.raises(UnsupportedMediaType):
            request.get_json()

        assert request.get_json(silent=True) is None
        assert request.get_json(force=True) == value

    def test_bad_data(self):
        request = wrappers.Request.from_values(
            data=b'{"a":}', content_type="application/json"
        )
        assert request.get_json(silent=True) is None

        with pytest.raises(BadRequest):
            request.get_json()

    def test_cache_disabled(self):
        value = [1, 2, 3]
        request = wrappers.Request.from_values(json=value)
        assert request.get_json(cache=False) == [1, 2, 3]
        assert not request.data

        with pytest.raises(BadRequest):
            request.get_json()


def test_response_coop():
    response = wrappers.Response("Hello World")
    assert response.cross_origin_opener_policy is COOP.UNSAFE_NONE
    response.cross_origin_opener_policy = COOP.SAME_ORIGIN
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_response_coep():
    response = wrappers.Response("Hello World")
    assert response.cross_origin_embedder_policy is COEP.UNSAFE_NONE
    response.cross_origin_embedder_policy = COEP.REQUIRE_CORP
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"

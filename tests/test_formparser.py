import io
from os.path import dirname
from os.path import join

import pytest

from werkzeug import formparser
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.sansio.multipart import PartHeaderError
from werkzeug.test import Client
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response


@Request.application
def form_data_consumer(request):
    result_object = request.args["object"]
    if result_object == "text":
        return Response(repr(request.form["text"]))
    f = request.files[result_object]
    return Response(
        b"\n".join(
            (
                repr(f.filename).encode("ascii"),
                repr(f.name).encode("ascii"),
                repr(f.content_type).encode("ascii"),
                f.stream.read(),
            )
        )
    )


def get_contents(filename):
    with open(filename, "rb") as f:
        return f.read()


class TestFormParser:
    def test_limiting(self):
        data = b"foo=Hello+World&bar=baz"
        req = Request.from_values(
            data=data,
            content_type="application/x-www-form-urlencoded",
            method="POST",
        )
        req.max_content_length = 400
        assert req.form["foo"] == "Hello World"

        input_stream = io.BytesIO(b"foo=123456")
        req = Request.from_values(
            input_stream=input_stream,
            content_type="application/x-www-form-urlencoded",
            method="POST",
        )
        req.max_content_length = 4
        pytest.raises(RequestEntityTooLarge, lambda: req.form["foo"])
        # content-length was set, so request could exit early without reading anything
        assert input_stream.read() == b"foo=123456"

        data = (
            b"--foo\r\nContent-Disposition: form-field; name=foo\r\n\r\n"
            b"Hello World\r\n"
            b"--foo\r\nContent-Disposition: form-field; name=bar\r\n\r\n"
            b"bar=baz\r\n--foo--"
        )

        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            req.max_content_length = 400
            assert req.form["foo"] == "Hello World"

        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            req.max_form_memory_size = 7
            pytest.raises(RequestEntityTooLarge, lambda: req.form["foo"])

        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            req.max_form_memory_size = 400
            assert req.form["foo"] == "Hello World"

        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            req.max_form_parts = 1
            pytest.raises(RequestEntityTooLarge, lambda: req.form["foo"])

    def test_urlencoded_no_max(self) -> None:
        r = Request.from_values(method="POST", data={"a": 1, "b": 2})
        r.max_form_parts = 1

        assert r.form["a"] == "1"
        assert r.form["b"] == "2"

    def test_urlencoded_silent_decode(self) -> None:
        r = Request.from_values(
            data=b"\x80",
            content_type="application/x-www-form-urlencoded",
            method="POST",
        )
        assert not r.form

    def test_missing_multipart_boundary(self):
        data = (
            b"--foo\r\nContent-Disposition: form-field; name=foo\r\n\r\n"
            b"Hello World\r\n"
            b"--foo\r\nContent-Disposition: form-field; name=bar\r\n\r\n"
            b"bar=baz\r\n--foo--"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data",
            method="POST",
        ) as req:
            assert req.form == {}

    def test_chunk_split_on_line_break_before_epilogue(self):
        data = b"".join(
            (
                # exactly 64 bytes of header
                b"--thirteenbytes\r\n",
                b"Content-Disposition: form-data; name=tx3065\r\n\r\n",
                # payload that fills 65535 bytes together with the header
                b"\n".join([b"\r" * 31] * 2045 + [b"y" * 31]),
                # This newline is split by the first chunk
                b"\r\n",
                # extra payload that also has the final newline split exactly
                # at the chunk size.
                b"\n".join([b"\r" * 31] * 2047 + [b"x" * 30]),
                b"\r\n--thirteenbytes--",
            )
        )

        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=thirteenbytes",
            method="POST",
        ) as req:
            assert len(req.form["tx3065"]) == (131072 - 64 - 1)
            assert req.form["tx3065"][-1] == "x"
            assert req.form["tx3065"][65470:65473] == "y\r\n"

    def test_parse_form_data_put_without_content(self) -> None:
        """PUT without content parses to empty form data."""
        with Request.from_values(method="PUT") as request:
            assert not request.form
            assert not request.files
            assert not request.data

    def test_parse_form_data_get_without_content(self) -> None:
        """GET parses to empty form data."""
        with Request.from_values() as request:
            assert not request.form
            assert not request.files
            assert not request.data

    def test_parse_form_post_data_trailing_CR(self):
        for k in [1, 2]:
            sample = b"\0" * 65535 + b"\x0d" * k
            with Request.from_values(
                data={"foo": (io.BytesIO(sample), "test.txt")}, method="POST"
            ) as req:
                assert req.files["foo"].read() == sample

    def test_parse_bad_content_type(self) -> None:
        with Request.from_values(content_type="bad-mime-type") as request:
            assert not request.form
            assert not request.files
            assert not request.data


class TestMultiPart:
    def test_basic(self):
        resources = join(dirname(__file__), "multipart")
        client = Client(form_data_consumer)

        repository = [
            (
                "firefox3-2png1txt",
                "---------------------------186454651713519341951581030105",
                [
                    ("anchor.png", "file1", "image/png", "file1.png"),
                    ("application_edit.png", "file2", "image/png", "file2.png"),
                ],
                "example text",
            ),
            (
                "firefox3-2pnglongtext",
                "---------------------------14904044739787191031754711748",
                [
                    ("accept.png", "file1", "image/png", "file1.png"),
                    ("add.png", "file2", "image/png", "file2.png"),
                ],
                "--long text\r\n--with boundary\r\n--lookalikes--",
            ),
            (
                "opera8-2png1txt",
                "----------zEO9jQKmLc2Cq88c23Dx19",
                [
                    ("arrow_branch.png", "file1", "image/png", "file1.png"),
                    ("award_star_bronze_1.png", "file2", "image/png", "file2.png"),
                ],
                "blafasel öäü",
            ),
            (
                "webkit3-2png1txt",
                "----WebKitFormBoundaryjdSFhcARk8fyGNy6",
                [
                    ("gtk-apply.png", "file1", "image/png", "file1.png"),
                    ("gtk-no.png", "file2", "image/png", "file2.png"),
                ],
                "this is another text with ümläüts",
            ),
            (
                "ie6-2png1txt",
                "---------------------------7d91b03a20128",
                [
                    ("file1.png", "file1", "image/x-png", "file1.png"),
                    ("file2.png", "file2", "image/x-png", "file2.png"),
                ],
                "ie6 sucks :-/",
            ),
        ]

        for name, boundary, files, text in repository:
            folder = join(resources, name)
            data = get_contents(join(folder, "request.http"))
            for filename, field, content_type, fsname in files:
                with client.post(
                    f"/?object={field}",
                    data=data,
                    content_type=f'multipart/form-data; boundary="{boundary}"',
                    content_length=len(data),
                ) as response:
                    lines = response.data.split(b"\n", 3)
                    assert lines[0] == repr(filename).encode("ascii")
                    assert lines[1] == repr(field).encode("ascii")
                    assert lines[2] == repr(content_type).encode("ascii")
                    assert lines[3] == get_contents(join(folder, fsname))

            with client.post(
                "/?object=text",
                data=data,
                content_type=f'multipart/form-data; boundary="{boundary}"',
                content_length=len(data),
            ) as response:
                assert response.text == repr(text)

    def test_ie7_unc_path(self):
        client = Client(form_data_consumer)
        data_file = join(dirname(__file__), "multipart", "ie7_full_path_request.http")
        data = get_contents(data_file)
        boundary = "---------------------------7da36d1b4a0164"
        with client.post(
            "/?object=cb_file_upload_multiple",
            data=data,
            content_type=f'multipart/form-data; boundary="{boundary}"',
            content_length=len(data),
        ) as response:
            lines = response.data.split(b"\n", 3)
            assert lines[0] == b"'Sellersburg Town Council Meeting 02-22-2010doc.doc'"

    def test_premature_end_of_file(self):
        data = (
            b"--foo\r\n"
            b'Content-Disposition: form-data; name="test"; filename="test.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
            b"file contents and no end"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            assert not req.files
            assert not req.form

    def test_file_no_content_type(self):
        data = (
            b"--foo\r\n"
            b'Content-Disposition: form-data; name="test"; filename="test.txt"\r\n\r\n'
            b"file contents\r\n--foo--"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as data:
            assert data.files["test"].filename == "test.txt"
            assert data.files["test"].read() == b"file contents"

    def test_extra_newline(self):
        # this test looks innocent but it was actually timing out in
        # the Werkzeug 0.5 release version (#394)
        data = (
            b"\r\n\r\n--foo\r\n"
            b'Content-Disposition: form-data; name="foo"\r\n\r\n'
            b"a string\r\n"
            b"--foo--"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            assert not req.files
            assert req.form["foo"] == "a string"

    def test_headers(self):
        data = (
            b"--foo\r\n"
            b'Content-Disposition: form-data; name="foo"; filename="foo.txt"\r\n'
            b"X-Custom-Header: blah\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"file contents, just the contents\r\n"
            b"--foo--"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            foo = req.files["foo"]
            assert foo.mimetype == "text/plain"
            assert foo.mimetype_params == {"charset": "utf-8"}
            assert foo.headers["Content-Type"] == foo.content_type
            assert foo.content_type == "text/plain; charset=utf-8"
            assert foo.headers["X-Custom-Header"] == "blah"

    @pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
    def test_nonstandard_line_endings(self, ending: bytes):
        data = ending.join(
            (
                b"--foo",
                b"Content-Disposition: form-data; name=foo",
                b"",
                b"this is just bar",
                b"--foo",
                b"Content-Disposition: form-data; name=bar",
                b"",
                b"blafasel",
                b"--foo--",
            )
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as req:
            assert req.form["foo"] == "this is just bar"
            assert req.form["bar"] == "blafasel"

    def test_failures(self):
        def parse_multipart(stream, boundary, content_length):
            parser = formparser.MultiPartParser(content_length)
            return parser.parse(stream, boundary, content_length)

        data = b"--foo\r\n\r\nHello World\r\n--foo--"
        pytest.raises(ValueError, parse_multipart, io.BytesIO(data), b"foo", len(data))

        data = (
            b"--foo\r\nContent-Disposition: form-field; name=foo\r\n\r\nHello World\r\n"
        )
        pytest.raises(ValueError, parse_multipart, io.BytesIO(data), b"foo", len(data))

    def test_empty_multipart(self) -> None:
        with Request.from_values(
            method="POST",
            data=b"--boundary--",
            content_type="multipart/form-data; boundary=boundary",
        ) as request:
            assert not request.form
            assert not request.files
            assert not request.data


class TestMultiPartParser:
    def test_file_rfc2231_filename_continuations(self):
        data = (
            b"--foo\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Disposition: form-data; name=rfc2231;\r\n"
            b"	filename*0*=ascii''a%20b%20;\r\n"
            b"	filename*1*=c%20d%20;\r\n"
            b'	filename*2="e f.txt"\r\n\r\n'
            b"file contents\r\n--foo--"
        )
        with Request.from_values(
            data=data,
            content_type="multipart/form-data; boundary=foo",
            method="POST",
        ) as request:
            assert request.files["rfc2231"].filename == "a b c d e f.txt"
            assert request.files["rfc2231"].read() == b"file contents"


def test_multipart_max_form_memory_size() -> None:
    """max_form_memory_size is tracked across multiple data events."""
    data = b"--bound\r\nContent-Disposition: form-field; name=a\r\n\r\n"
    data += b"a" * 15 + b"\r\n--bound--"
    # The buffer size is less than the max size, so multiple data events will be
    # returned. The field size is greater than the max.
    parser = formparser.MultiPartParser(max_form_memory_size=10, buffer_size=5)

    with pytest.raises(RequestEntityTooLarge):
        parser.parse(io.BytesIO(data), b"bound", None)


class TestStrictPartHeaders:
    def _body(self, parts: list[bytes]) -> bytes:
        return b"--b\r\n" + b"\r\n--b\r\n".join(parts) + b"\r\n--b--"

    def _counting_factory(self, calls: list[str | None]):
        def factory(total_content_length, content_type, filename, content_length=None):
            calls.append(filename)
            return io.BytesIO()

        return factory

    def test_rejected_part_never_creates_file(self) -> None:
        calls: list[str | None] = []
        good = (
            b'Content-Disposition: form-data; name="ok"; filename="ok.txt"\r\n\r\n'
            b"good"
        )
        bad = (
            b'Content-Disposition: form-data; name="bad";'
            b' filename="a"; filename="b"\r\n\r\nevil'
        )
        data = self._body([good, bad])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        with pytest.raises(PartHeaderError) as exc_info:
            parser.parse(io.BytesIO(data), b"b", None)

        message = str(exc_info.value)
        assert "Part 2" in message
        assert "filename" in message
        # Only the valid first part reached the stream factory.
        assert calls == ["ok.txt"]

    def test_compat_mode_still_parses_both_parts(self) -> None:
        calls: list[str | None] = []
        good = (
            b'Content-Disposition: form-data; name="ok"; filename="ok.txt"\r\n\r\n'
            b"good"
        )
        ambiguous = (
            b'Content-Disposition: form-data; name="dup";'
            b' filename="a"; filename="b"\r\n\r\nevil'
        )
        data = self._body([good, ambiguous])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
        )

        _, files = parser.parse(io.BytesIO(data), b"b", None)

        assert calls == ["ok.txt", "b"]
        assert files["dup"].filename == "b"

    def test_duplicate_disposition_header_rejected_before_file(self) -> None:
        calls: list[str | None] = []
        bad = (
            b'Content-Disposition: form-data; name="f"; filename="a"\r\n'
            b'Content-Disposition: form-data; name="f"; filename="b"\r\n\r\nx'
        )
        data = self._body([bad])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        with pytest.raises(PartHeaderError) as exc_info:
            parser.parse(io.BytesIO(data), b"b", None)

        assert "Part 1" in str(exc_info.value)
        assert "content-disposition" in str(exc_info.value)
        assert calls == []

    def test_repeated_field_names_still_form_multivalue(self) -> None:
        calls: list[str | None] = []
        first = b'Content-Disposition: form-data; name="n"\r\n\r\nv1'
        second = b'Content-Disposition: form-data; name="n"\r\n\r\nv2'
        data = self._body([first, second])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        form, files = parser.parse(io.BytesIO(data), b"b", None)

        assert form.getlist("n") == ["v1", "v2"]
        assert files.getlist("n") == []
        assert calls == []

    def test_repeated_file_names_still_allowed(self) -> None:
        calls: list[str | None] = []
        first = (
            b'Content-Disposition: form-data; name="up"; filename="a.txt"\r\n\r\n'
            b"one"
        )
        second = (
            b'Content-Disposition: form-data; name="up"; filename="a.txt"\r\n\r\n'
            b"two"
        )
        data = self._body([first, second])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        _, files = parser.parse(io.BytesIO(data), b"b", None)

        assert len(files.getlist("up")) == 2
        assert calls == ["a.txt", "a.txt"]

    def test_valid_canonical_filename_star_accepted(self) -> None:
        calls: list[str | None] = []
        part = (
            b'Content-Disposition: form-data; name="up"; filename="ascii.txt";'
            b" filename*=utf-8''%E2%9C%93.txt\r\n\r\nx"
        )
        data = self._body([part])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        _, files = parser.parse(io.BytesIO(data), b"b", None)

        assert files["up"].filename == "✓.txt"
        assert calls == ["✓.txt"]

    def test_invalid_extended_encoding_rejected_before_file(self) -> None:
        calls: list[str | None] = []
        part = (
            b"Content-Disposition: form-data; name=up;"
            b" filename*=bogus''x.txt\r\n\r\nx"
        )
        data = self._body([part])
        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
        )

        with pytest.raises(PartHeaderError):
            parser.parse(io.BytesIO(data), b"b", None)

        assert calls == []

    def test_headers_split_across_chunks_still_rejected(self) -> None:
        calls: list[str | None] = []
        part = (
            b'Content-Disposition: form-data; name="bad";'
            b' filename="a"; filename="b"\r\n\r\nx'
        )
        data = self._body([part])

        class OneByteStream:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self._pos = 0

            def read(self, size: int = -1) -> bytes:
                if self._pos >= len(self._payload):
                    return b""

                chunk = self._payload[self._pos : self._pos + 1]
                self._pos += 1
                return chunk

        parser = formparser.MultiPartParser(
            stream_factory=self._counting_factory(calls),
            strict_part_headers=True,
            buffer_size=1,
        )

        with pytest.raises(PartHeaderError) as exc_info:
            parser.parse(OneByteStream(data), b"b", None)

        assert "Part 1" in str(exc_info.value)
        assert calls == []


class StrictRequest(Request):
    strict_part_headers = True


def test_request_strict_part_headers_attribute_opt_in() -> None:
    ambiguous = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="f";'
        b' filename="a"; filename="b"\r\n\r\nx\r\n--b--'
    )

    # Default Request keeps compatibility behavior.
    with Request.from_values(
        data=ambiguous,
        content_type="multipart/form-data; boundary=b",
        method="POST",
    ) as default_request:
        assert default_request.files["f"].filename == "b"

    # Strict Request silently returns empty form data by default.
    with StrictRequest.from_values(
        data=ambiguous,
        content_type="multipart/form-data; boundary=b",
        method="POST",
    ) as strict_request:
        assert not strict_request.form
        assert not strict_request.files


def test_request_strict_mode_accepts_valid_upload() -> None:
    body = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="up"; filename="ok.txt"\r\n\r\n'
        b"contents\r\n--b--"
    )
    with StrictRequest.from_values(
        data=body,
        content_type="multipart/form-data; boundary=b",
        method="POST",
    ) as request:
        assert request.files["up"].filename == "ok.txt"
        assert request.files["up"].read() == b"contents"

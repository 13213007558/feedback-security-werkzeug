import pytest

from werkzeug.datastructures import Headers
from werkzeug.sansio.multipart import Data
from werkzeug.sansio.multipart import Epilogue
from werkzeug.sansio.multipart import Field
from werkzeug.sansio.multipart import File
from werkzeug.sansio.multipart import MultipartDecoder
from werkzeug.sansio.multipart import MultipartEncoder
from werkzeug.sansio.multipart import NeedData
from werkzeug.sansio.multipart import PartHeaderError
from werkzeug.sansio.multipart import Preamble


def test_decoder_simple() -> None:
    boundary = b"---------------------------9704338192090380615194531385$"
    decoder = MultipartDecoder(boundary)
    data = """
-----------------------------9704338192090380615194531385$
Content-Disposition: form-data; name="fname"

ß∑œß∂ƒå∂
-----------------------------9704338192090380615194531385$
Content-Disposition: form-data; name="lname"; filename="bob"

asdasd
-----------------------------9704338192090380615194531385$--
    """.replace("\n", "\r\n").encode()
    decoder.receive_data(data)
    decoder.receive_data(None)
    events = [decoder.next_event()]
    while not isinstance(events[-1], Epilogue):
        events.append(decoder.next_event())
    assert events == [
        Preamble(data=b""),
        Field(
            name="fname",
            headers=Headers([("Content-Disposition", 'form-data; name="fname"')]),
        ),
        Data(data="ß∑œß∂ƒå∂".encode(), more_data=False),
        File(
            name="lname",
            filename="bob",
            headers=Headers(
                [("Content-Disposition", 'form-data; name="lname"; filename="bob"')]
            ),
        ),
        Data(data=b"asdasd", more_data=False),
        Epilogue(data=b"    "),
    ]
    encoder = MultipartEncoder(boundary)
    result = b""
    for event in events:
        result += encoder.send_event(event)
    assert data == result


@pytest.mark.parametrize(
    "data_start",
    [
        b"A",
        b"\n",
        b"\r",
        b"\r\n",
        b"\n\r",
        b"A\n",
        b"A\r",
        b"A\r\n",
        b"A\n\r",
    ],
)
@pytest.mark.parametrize("data_end", [b"", b"\r\n--foo"])
def test_decoder_data_start_with_different_newline_positions(
    data_start: bytes, data_end: bytes
) -> None:
    boundary = b"foo"
    data = (
        b"\r\n--foo\r\n"
        b'Content-Disposition: form-data; name="test"; filename="testfile"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"" + data_start + b"\r\nBCDE" + data_end
    )
    decoder = MultipartDecoder(boundary)
    decoder.receive_data(data)
    events = [decoder.next_event()]
    # We want to check up to data start event
    while not isinstance(events[-1], Data):
        events.append(decoder.next_event())

    expected = data_start if data_end == b"" else data_start + b"\r\nBCDE"

    assert events == [
        Preamble(data=b""),
        File(
            name="test",
            filename="testfile",
            headers=Headers(
                [
                    (
                        "Content-Disposition",
                        'form-data; name="test"; filename="testfile"',
                    ),
                    ("Content-Type", "application/octet-stream"),
                ]
            ),
        ),
        Data(data=expected, more_data=True),
    ]


def test_chunked_boundaries() -> None:
    boundary = b"--boundary"
    decoder = MultipartDecoder(boundary)
    decoder.receive_data(b"--")
    assert isinstance(decoder.next_event(), NeedData)
    decoder.receive_data(b"--boundary\r\n")
    assert isinstance(decoder.next_event(), Preamble)
    decoder.receive_data(b"Content-Disposition: form-data;")
    assert isinstance(decoder.next_event(), NeedData)
    decoder.receive_data(b'name="fname"\r\n\r\n')
    assert isinstance(decoder.next_event(), Field)
    decoder.receive_data(b"longer than the boundary")
    assert isinstance(decoder.next_event(), Data)
    decoder.receive_data(b"also longer, but includes a linebreak\r\n--")
    assert isinstance(decoder.next_event(), Data)
    assert isinstance(decoder.next_event(), NeedData)
    decoder.receive_data(b"--boundary--\r\n")
    event = decoder.next_event()
    assert isinstance(event, Data)
    assert not event.more_data
    decoder.receive_data(None)
    assert isinstance(decoder.next_event(), Epilogue)


def test_empty_field() -> None:
    boundary = b"foo"
    decoder = MultipartDecoder(boundary)
    data = """
--foo
Content-Disposition: form-data; name="text"
Content-Type: text/plain; charset="UTF-8"

Some Text
--foo
Content-Disposition: form-data; name="empty"
Content-Type: text/plain; charset="UTF-8"

--foo--
    """.replace("\n", "\r\n").encode()
    decoder.receive_data(data)
    decoder.receive_data(None)
    events = [decoder.next_event()]
    while not isinstance(events[-1], Epilogue):
        events.append(decoder.next_event())
    assert events == [
        Preamble(data=b""),
        Field(
            name="text",
            headers=Headers(
                [
                    ("Content-Disposition", 'form-data; name="text"'),
                    ("Content-Type", 'text/plain; charset="UTF-8"'),
                ]
            ),
        ),
        Data(data=b"Some Text", more_data=False),
        Field(
            name="empty",
            headers=Headers(
                [
                    ("Content-Disposition", 'form-data; name="empty"'),
                    ("Content-Type", 'text/plain; charset="UTF-8"'),
                ]
            ),
        ),
        Data(data=b"", more_data=False),
        Epilogue(data=b"    "),
    ]
    encoder = MultipartEncoder(boundary)
    result = b""
    for event in events:
        result += encoder.send_event(event)
    assert data == result


def _feed_decoder(data: bytes, *, strict: bool, chunk_size: int) -> list:
    """Feed ``data`` in fixed-size chunks so headers span multiple
    ``receive_data`` calls, then collect the part events."""
    decoder = MultipartDecoder(b"b", strict_part_headers=strict)

    for index in range(0, len(data), chunk_size):
        decoder.receive_data(data[index : index + chunk_size])

    decoder.receive_data(None)
    events = []

    while True:
        event = decoder.next_event()

        if isinstance(event, Epilogue):
            return events

        if isinstance(event, (Field, File)):
            events.append(event)


def _part(headers: bytes, body: bytes = b"x") -> bytes:
    return b"--b\r\n" + headers + b"\r\n\r\n" + body + b"\r\n--b--"


@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_strict_rejects_duplicate_filename_parameter(chunk_size: int) -> None:
    data = _part(
        b'Content-Disposition: form-data; name="f";'
        b' filename="a.txt"; filename="b.txt"'
    )

    # Compatibility mode silently keeps the last value, regardless of chunking.
    compat = _feed_decoder(data, strict=False, chunk_size=chunk_size)
    assert isinstance(compat[0], File)
    assert compat[0].filename == "b.txt"

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=chunk_size)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "filename" in message


@pytest.mark.parametrize("chunk_size", [1, 5, 64])
def test_strict_rejects_duplicate_disposition_header(chunk_size: int) -> None:
    data = _part(
        b'Content-Disposition: form-data; name="f"; filename="a.txt"\r\n'
        b'Content-Disposition: form-data; name="f"; filename="b.txt"'
    )

    # Compatibility only inspects the first header of a repeated name.
    compat = _feed_decoder(data, strict=False, chunk_size=chunk_size)
    assert compat[0].filename == "a.txt"

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=chunk_size)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "content-disposition" in message


def test_strict_rejects_duplicate_name_parameter() -> None:
    data = _part(b'Content-Disposition: form-data; name="a"; name="b"')

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=1)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "name" in message


def test_strict_rejects_invalid_extended_encoding() -> None:
    data = _part(b"Content-Disposition: form-data; name=f; filename*=bogus''x.txt")

    # Compatibility leaves the unsupported value quoted rather than failing.
    compat = _feed_decoder(data, strict=False, chunk_size=1)
    assert compat[0].filename == "x.txt"

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=1)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "filename" in message


def test_strict_rejects_malformed_extended_value() -> None:
    data = _part(b"Content-Disposition: form-data; name=f; filename*=not-encoded")

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=2)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "filename" in message


def test_strict_accepts_canonical_filename_and_extended() -> None:
    data = _part(
        b'Content-Disposition: form-data; name="f"; filename="ascii.txt";'
        b" filename*=utf-8''%E2%9C%93.txt"
    )
    events = _feed_decoder(data, strict=True, chunk_size=1)
    assert len(events) == 1
    assert isinstance(events[0], File)
    assert events[0].name == "f"
    assert events[0].filename == "✓.txt"


def test_strict_extended_wins_regardless_of_order() -> None:
    data = _part(
        b"Content-Disposition: form-data; name=f;"
        b" filename*=utf-8''%E2%9C%93.txt; filename=\"ascii.txt\""
    )
    events = _feed_decoder(data, strict=True, chunk_size=4)
    assert events[0].filename == "✓.txt"


def test_strict_accepts_unicode_extended_filename() -> None:
    data = _part(
        b"Content-Disposition: form-data; name=f;"
        b" filename*=utf-8''%E6%96%87%E4%BB%B6.txt"
    )
    events = _feed_decoder(data, strict=True, chunk_size=1)
    assert events[0].filename == "文件.txt"


def test_strict_accepts_empty_filename() -> None:
    data = _part(b'Content-Disposition: form-data; name="f"; filename=""')
    events = _feed_decoder(data, strict=True, chunk_size=1)
    assert isinstance(events[0], File)
    assert events[0].filename == ""


def test_strict_accepts_semicolon_inside_quoted_value() -> None:
    data = _part(
        b'Content-Disposition: form-data; name="f";'
        b' filename="report; final.txt"'
    )
    events = _feed_decoder(data, strict=True, chunk_size=2)
    assert events[0].filename == "report; final.txt"


def test_strict_accepts_escaped_quote_in_filename() -> None:
    data = _part(
        b'Content-Disposition: form-data; name="f";'
        b' filename="a\\"b.txt"'
    )
    events = _feed_decoder(data, strict=True, chunk_size=1)
    assert events[0].filename == 'a"b.txt'


def test_strict_allows_repeated_field_names_across_parts() -> None:
    data = (
        b'--b\r\nContent-Disposition: form-data; name="n"\r\n\r\nv1\r\n'
        b'--b\r\nContent-Disposition: form-data; name="n"\r\n\r\nv2\r\n--b--'
    )
    events = _feed_decoder(data, strict=True, chunk_size=3)
    assert [event.name for event in events] == ["n", "n"]


def test_strict_reports_second_part_index() -> None:
    data = (
        b'--b\r\nContent-Disposition: form-data; name="ok"\r\n\r\nv1\r\n'
        b"--b\r\n"
        b'Content-Disposition: form-data; name="bad";'
        b' filename="a"; filename="b"\r\n\r\nv2\r\n--b--'
    )

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=1)

    message = str(exc_info.value)
    assert "Part 2" in message
    assert "filename" in message


def test_strict_accepts_rfc2231_continuation() -> None:
    data = _part(
        b"Content-Disposition: form-data; name=rfc2231;\r\n"
        b"\tfilename*0*=ascii''a%20b%20;\r\n"
        b"\tfilename*1*=c%20d%20;\r\n"
        b'\tfilename*2="e f.txt"'
    )
    events = _feed_decoder(data, strict=True, chunk_size=2)
    assert events[0].filename == "a b c d e f.txt"


def test_strict_rejects_duplicate_continuation_segment() -> None:
    data = _part(
        b"Content-Disposition: form-data; name=n;\r\n"
        b"\tfilename*0*=ascii''a;\r\n"
        b"\tfilename*0*=ascii''b"
    )

    with pytest.raises(PartHeaderError) as exc_info:
        _feed_decoder(data, strict=True, chunk_size=1)

    message = str(exc_info.value)
    assert "Part 1" in message
    assert "filename" in message

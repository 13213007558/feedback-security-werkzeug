from __future__ import annotations

import re
import typing as t
from dataclasses import dataclass
from enum import auto
from enum import Enum
from urllib.parse import unquote

from ..datastructures.headers import Headers
from ..exceptions import RequestEntityTooLarge
from ..http import _charset_value_re
from ..http import _parameter_key_re
from ..http import _parameter_mark_re
from ..http import _parameter_token_value_re
from ..http import parse_options_header
from ..http import unquote_header_value

# Charsets accepted for RFC 2231/5987 ``key*=charset''value`` parameters. This
# mirrors the safe list used by :func:`http.parse_options_header`.
_EXTENDED_CHARSETS = frozenset({"ascii", "us-ascii", "utf-8", "iso-8859-1"})


class Event:
    pass


@dataclass(frozen=True)
class Preamble(Event):
    data: bytes


@dataclass(frozen=True)
class Field(Event):
    name: str
    headers: Headers


@dataclass(frozen=True)
class File(Event):
    name: str
    filename: str
    headers: Headers


@dataclass(frozen=True)
class Data(Event):
    data: bytes
    more_data: bool


@dataclass(frozen=True)
class Epilogue(Event):
    data: bytes


class NeedData(Event):
    pass


NEED_DATA = NeedData()


class PartHeaderError(ValueError):
    """Raised in strict mode when a part's ``Content-Disposition`` header is
    ambiguous or invalid.

    The message identifies the 1-based position of the offending part and the
    ambiguous header or parameter name. Because the error is raised while the
    part headers are decoded, the ``stream_factory`` has not created a file for
    the rejected part yet.

    .. versionadded:: 3.2
    """


@dataclass(frozen=True)
class _Disposition:
    """Validated result of parsing a ``Content-Disposition`` header value."""

    disposition: str
    name: str | None
    filename: str | None


# Tracks one raw parameter occurrence while scanning a header value. ``kind``
# is "plain", "extended" (``key*``), or "continuation" (``key*n*``).
@dataclass
class _RawParameter:
    key: str
    value: str
    kind: str
    number: int | None = None


def _strict_part_header_error(part_index: int, detail: str) -> PartHeaderError:
    return PartHeaderError(
        f"Part {part_index} has an ambiguous Content-Disposition header: {detail}"
    )


def _tokenize_disposition(
    value: str, part_index: int
) -> tuple[str, list[_RawParameter]]:
    """Split a ``Content-Disposition`` value into its disposition token and raw
    parameters, enforcing the RFC 9110 grammar instead of silently skipping
    malformed pieces.
    """
    head, has_semicolon, rest = value.partition(";")
    disposition = head.strip(" \t")

    if not disposition:
        raise _strict_part_header_error(part_index, "missing disposition")

    if not has_semicolon:
        return disposition, []

    parameters: list[_RawParameter] = []
    rest = rest.lstrip(" \t")
    scan_pos = 0
    length = len(rest)

    while scan_pos < length:
        key_match = _parameter_key_re.match(rest, scan_pos)

        if key_match is None:
            raise _strict_part_header_error(part_index, "malformed parameter")

        raw_key = key_match.group(1)
        scan_pos = key_match.end()
        mark_match = _parameter_mark_re.search(raw_key)
        assert mark_match is not None
        continuation_number = mark_match.group(1)
        has_continuation = continuation_number is not None
        has_charset = mark_match.group(2) is not None
        base_key = raw_key[: mark_match.start()].lower()

        if has_continuation:
            # key*n* or key*n: an RFC 2231 continuation segment. Only the
            # first segment is expected to carry a charset marker.
            kind = "continuation"
        elif has_charset:
            kind = "extended"
        else:
            kind = "plain"

        value_match = _parameter_token_value_re.match(rest, scan_pos)

        if value_match is not None:
            raw_value = value_match.group()
            scan_pos = value_match.end()
        elif rest.startswith('"', scan_pos):
            quote_pos = scan_pos + 1

            while quote_pos < length:
                if rest.startswith(("\\\\", '\\"'), quote_pos):
                    quote_pos += 2
                elif rest.startswith('"', quote_pos):
                    quote_pos += 1
                    break
                else:
                    quote_pos += 1
            else:
                raise _strict_part_header_error(
                    part_index, f"unclosed quoted value for parameter {base_key!r}"
                )

            raw_value = rest[scan_pos:quote_pos]
            scan_pos = quote_pos
        else:
            raise _strict_part_header_error(
                part_index, f"parameter {base_key!r} has no value"
            )

        parameters.append(
            _RawParameter(
                key=base_key,
                value=raw_value,
                kind=kind,
                number=int(continuation_number)
                if continuation_number is not None
                else None,
            )
        )

        if scan_pos >= length:
            break

        if rest[scan_pos] != ";":
            raise _strict_part_header_error(
                part_index, f"expected ';' after parameter {base_key!r}"
            )

        scan_pos += 1

        while scan_pos < length and rest[scan_pos] in " \t":
            scan_pos += 1

    return disposition, parameters


def _decode_extended_value(raw: str, part_index: int, key: str) -> str:
    """Decode an RFC 2231/5987 ``charset''percent-encoded`` value.

    Only the charsets accepted in compatibility mode are valid; anything else
    is rejected rather than left quoted.
    """
    charset_match = _charset_value_re.match(raw)

    if charset_match is None:
        raise _strict_part_header_error(
            part_index, f"invalid extended encoding for parameter {key!r}"
        )

    charset, encoded = charset_match.groups()
    charset = charset.lower()

    if charset not in _EXTENDED_CHARSETS:
        raise _strict_part_header_error(
            part_index, f"unsupported charset {charset!r} for parameter {key!r}"
        )

    return unquote(encoded, encoding=charset, errors="replace")


def _decode_continuation_value(
    raw: str,
    prior_charset: str | None,
    part_index: int,
    key: str,
) -> tuple[str, str]:
    """Decode one RFC 2231 continuation segment (``key*n*`` or ``key*n=``).

    The first segment carries the charset marker; later segments reuse it. A
    later segment without a marker is decoded using the prior charset. Returns
    the decoded value and the charset to pass to the next segment.
    """
    charset_match = _charset_value_re.match(raw)

    if charset_match is not None:
        charset, encoded = charset_match.groups()
        charset = charset.lower()
    elif prior_charset is not None:
        charset = prior_charset
        encoded = unquote_header_value(raw)
    else:
        raise _strict_part_header_error(
            part_index,
            f"continuation segment for parameter {key!r} is missing a charset",
        )

    if charset not in _EXTENDED_CHARSETS:
        raise _strict_part_header_error(
            part_index, f"unsupported charset {charset!r} for parameter {key!r}"
        )

    # Every RFC 2231 segment is percent encoded, including later segments that
    # omit the charset marker and may be quoted.
    return unquote(encoded, encoding=charset, errors="replace"), charset


def _decode_plain_value(raw: str) -> str:
    return unquote_header_value(raw).replace("%22", '"')


def _parse_strict_disposition(value: str, part_index: int) -> _Disposition:
    """Parse and validate a single ``Content-Disposition`` header value.

    Duplicate parameters are rejected, while the canonical ``filename`` plus
    valid ``filename*`` pairing is accepted with the extended value taking
    precedence, matching the compatibility parser's precedence rules.
    """
    disposition, raw_parameters = _tokenize_disposition(value, part_index)

    seen_kinds: dict[str, set[str]] = {}
    seen_numbers: dict[str, set[int]] = {}

    for parameter in raw_parameters:
        kinds = seen_kinds.setdefault(parameter.key, set())

        if parameter.kind == "continuation":
            # RFC 2231 splits one value into ordered key*n* segments, so the
            # key repeats legitimately; the same segment number may not.
            assert parameter.number is not None
            numbers = seen_numbers.setdefault(parameter.key, set())

            if parameter.number in numbers:
                raise _strict_part_header_error(
                    part_index,
                    f"duplicate parameter {parameter.key!r}",
                )

            numbers.add(parameter.number)
        elif parameter.kind in kinds:
            raise _strict_part_header_error(
                part_index,
                f"duplicate parameter {parameter.key!r}",
            )

        kinds.add(parameter.kind)

    plain: dict[str, str] = {}
    extended: dict[str, str] = {}
    continuations: dict[str, list[str]] = {}
    continuation_charsets: dict[str, str] = {}

    for parameter in raw_parameters:
        if parameter.kind == "extended":
            extended[parameter.key] = _decode_extended_value(
                parameter.value, part_index, parameter.key
            )
        elif parameter.kind == "continuation":
            decoded_value, charset = _decode_continuation_value(
                parameter.value,
                continuation_charsets.get(parameter.key),
                part_index,
                parameter.key,
            )
            continuation_charsets[parameter.key] = charset
            continuations.setdefault(parameter.key, []).append(decoded_value)
        else:
            plain[parameter.key] = _decode_plain_value(parameter.value)

    for key in continuations:
        # A continuation series is its own complete definition and cannot be
        # combined with a plain or single extended value.
        if key in plain or key in extended:
            raise _strict_part_header_error(
                part_index,
                f"conflicting parameter {key!r}",
            )

    # A plain value plus a valid extended value is the canonical combination;
    # the extended value always wins, regardless of ordering.
    plain.update(extended)
    plain.update({key: "".join(pieces) for key, pieces in continuations.items()})
    decoded = plain

    return _Disposition(
        disposition=disposition.lower(),
        name=decoded.get("name"),
        filename=decoded.get("filename"),
    )


def _strict_part_headers(headers: Headers, part_index: int) -> _Disposition:
    """Validate part headers in strict mode and return the parsed disposition.

    Rejects repeated header lines (most importantly repeated
    ``Content-Disposition``) before any file is created for the part.
    """
    seen: set[str] = set()

    for header_name, _ in headers:
        normalized = header_name.lower()

        if normalized in seen:
            raise _strict_part_header_error(
                part_index, f"duplicate header {normalized!r}"
            )

        seen.add(normalized)

    disposition_value = headers.get("Content-Disposition")

    if disposition_value is None:
        raise _strict_part_header_error(part_index, "missing Content-Disposition")

    return _parse_strict_disposition(disposition_value, part_index)


class State(Enum):
    PREAMBLE = auto()
    PART = auto()
    DATA = auto()
    DATA_START = auto()
    EPILOGUE = auto()
    COMPLETE = auto()


# Multipart line breaks MUST be CRLF (\r\n) by RFC-7578, except that
# many implementations break this and either use CR or LF alone.
LINE_BREAK = b"(?:\r\n|\n|\r)"
BLANK_LINE_RE = re.compile(b"(?:\r\n\r\n|\r\r|\n\n)", re.MULTILINE)
LINE_BREAK_RE = re.compile(LINE_BREAK, re.MULTILINE)
# Header values can be continued via a space or tab after the linebreak, as
# per RFC2231
HEADER_CONTINUATION_RE = re.compile(b"%s[ \t]" % LINE_BREAK, re.MULTILINE)
# This must be long enough to contain any line breaks plus any
# additional boundary markers (--) such that they will be found in a
# subsequent search
SEARCH_EXTRA_LENGTH = 8


class MultipartDecoder:
    """Decodes a multipart message as bytes into Python events.

    The part data is returned as available to allow the caller to save
    the data from memory to disk, if desired.

    .. versionchanged:: 3.1.4
        Handle chunks that split a``\r\n`` sequence.
    """

    def __init__(
        self,
        boundary: bytes,
        max_form_memory_size: int | None = None,
        *,
        max_parts: int | None = None,
        strict_part_headers: bool = False,
    ) -> None:
        self.buffer = bytearray()
        self.complete = False
        self.max_form_memory_size = max_form_memory_size
        self.max_parts = max_parts
        self.strict_part_headers = strict_part_headers
        self.state = State.PREAMBLE
        self.boundary = boundary

        # Note in the below \h i.e. horizontal whitespace is used
        # as [^\S\n\r] as \h isn't supported in python.

        # The preamble must end with a boundary where the boundary is
        # prefixed by a line break, RFC2046. Except that many
        # implementations including Werkzeug's tests omit the line
        # break prefix. In addition the first boundary could be the
        # epilogue boundary (for empty form-data) hence the matching
        # group to understand if it is an epilogue boundary.
        self.preamble_re = re.compile(
            rb"%s?--%s(--[^\S\n\r]*%s?|[^\S\n\r]*%s)"
            % (LINE_BREAK, re.escape(boundary), LINE_BREAK, LINE_BREAK),
            re.MULTILINE,
        )
        # A boundary must include a line break prefix and suffix, and
        # may include trailing whitespace. In addition the boundary
        # could be the epilogue boundary hence the matching group to
        # understand if it is an epilogue boundary.
        self.boundary_re = re.compile(
            rb"%s--%s(--[^\S\n\r]*%s?|[^\S\n\r]*%s)"
            % (LINE_BREAK, re.escape(boundary), LINE_BREAK, LINE_BREAK),
            re.MULTILINE,
        )
        self._search_position = 0
        self._parts_decoded = 0

    def receive_data(self, data: bytes | None) -> None:
        if data is None:
            self.complete = True
        elif (
            self.max_form_memory_size is not None
            and len(self.buffer) + len(data) > self.max_form_memory_size
        ):
            # Ensure that data within single event does not exceed limit.
            # Also checked across accumulated events in MultiPartParser.
            raise RequestEntityTooLarge()
        else:
            self.buffer.extend(data)

    def next_event(self) -> Event:
        event: Event = NEED_DATA
        if self.state == State.PREAMBLE:
            match = self.preamble_re.search(self.buffer, self._search_position)
            if match is not None:
                if match.group(1).startswith(b"--"):
                    self.state = State.EPILOGUE
                else:
                    self.state = State.PART
                data = bytes(self.buffer[: match.start()])
                del self.buffer[: match.end()]
                event = Preamble(data=data)
                self._search_position = 0
            else:
                # Update the search start position to be equal to the
                # current buffer length (already searched) minus a
                # safe buffer for part of the search target.
                self._search_position = max(
                    0, len(self.buffer) - len(self.boundary) - SEARCH_EXTRA_LENGTH
                )

        elif self.state == State.PART:
            match = BLANK_LINE_RE.search(self.buffer, self._search_position)
            if match is not None:
                headers = self._parse_headers(self.buffer[: match.start()])
                # The final header ends with a single CRLF, however a
                # blank line indicates the start of the
                # body. Therefore the end is after the first CRLF.
                headers_end = (match.start() + match.end()) // 2

                # Validate before consuming the header block or creating a
                # streaming event so that a rejected part never reaches the
                # caller's stream factory.
                part_index = self._parts_decoded + 1

                if self.strict_part_headers:
                    disposition = _strict_part_headers(headers, part_index)
                    name = disposition.name
                    filename = disposition.filename
                else:
                    if "Content-Disposition" not in headers:
                        raise ValueError("Missing Content-Disposition header")

                    disposition, extra = parse_options_header(
                        headers["Content-Disposition"]
                    )
                    name = t.cast(str, extra.get("name"))
                    filename = extra.get("filename")

                del self.buffer[:headers_end]

                if filename is not None:
                    event = File(
                        filename=filename,
                        headers=headers,
                        name=name,
                    )
                else:
                    event = Field(
                        headers=headers,
                        name=name,
                    )
                self.state = State.DATA_START
                self._search_position = 0
                self._parts_decoded += 1

                if self.max_parts is not None and self._parts_decoded > self.max_parts:
                    raise RequestEntityTooLarge()
            else:
                # Update the search start position to be equal to the
                # current buffer length (already searched) minus a
                # safe buffer for part of the search target.
                self._search_position = max(0, len(self.buffer) - SEARCH_EXTRA_LENGTH)

        elif self.state == State.DATA_START:
            data, del_index, more_data = self._parse_data(self.buffer, start=True)
            del self.buffer[:del_index]
            event = Data(data=data, more_data=more_data)
            if more_data:
                self.state = State.DATA

        elif self.state == State.DATA:
            data, del_index, more_data = self._parse_data(self.buffer, start=False)
            del self.buffer[:del_index]
            if data or not more_data:
                event = Data(data=data, more_data=more_data)

        elif self.state == State.EPILOGUE and self.complete:
            event = Epilogue(data=bytes(self.buffer))
            del self.buffer[:]
            self.state = State.COMPLETE

        if self.complete and isinstance(event, NeedData):
            raise ValueError(f"Invalid form-data cannot parse beyond {self.state}")

        return event

    def _parse_headers(self, data: bytes | bytearray) -> Headers:
        headers: list[tuple[str, str]] = []
        # Merge the continued headers into one line
        data = HEADER_CONTINUATION_RE.sub(b" ", data)
        # Now there is one header per line
        for line in data.splitlines():
            line = line.strip(b" \t")

            if line != b"":
                name, _, value = line.decode().partition(":")
                headers.append((name.strip(" \t"), value.strip(" \t")))
        return Headers(headers)

    def _parse_data(
        self, data: bytes | bytearray, *, start: bool
    ) -> tuple[bytes, int, bool]:
        # Body parts must start with CRLF (or CR or LF)
        if start:
            match = LINE_BREAK_RE.match(data)
            data_start = t.cast(t.Match[bytes], match).end()
        else:
            data_start = 0

        if self.buffer.find(b"--" + self.boundary) == -1:
            # No complete boundary in the buffer, but there may be
            # a partial boundary at the end.
            data_end = del_index = (
                self._last_partial_boundary_index(data[data_start:]) + data_start
            )
            more_data = True
        else:
            match = self.boundary_re.search(data)
            if match is not None:
                if match.group(1).startswith(b"--"):
                    self.state = State.EPILOGUE
                else:
                    self.state = State.PART
                data_end = match.start()
                del_index = match.end()
            else:
                data_end = del_index = (
                    self._last_partial_boundary_index(data[data_start:]) + data_start
                )
            more_data = match is None
        return bytes(data[data_start:data_end]), del_index, more_data

    def _last_partial_boundary_index(self, data: bytes | bytearray) -> int:
        # Find the last index following which a partial boundary
        # could be present in the data. This will be the earliest
        # position of a LF or a CR, unless that position is more
        # than a complete boundary from the end in which case there
        # is no partial boundary.
        complete_boundary_index = len(data) - len(b"\r\n--" + self.boundary)
        try:
            last_nl = data.rindex(b"\n")
        except ValueError:
            last_nl = len(data)
        else:
            if last_nl < complete_boundary_index:
                last_nl = len(data)
        try:
            last_cr = data.rindex(b"\r")
        except ValueError:
            last_cr = len(data)
        else:
            if last_cr < complete_boundary_index:
                last_cr = len(data)
        return min(last_nl, last_cr)


class MultipartEncoder:
    """Used by the test client to create a multipart body. This is not secure
    and must only be used during testing, never in applications.

    :meta private:
    """

    def __init__(self, boundary: bytes) -> None:
        self.boundary = boundary
        self.state = State.PREAMBLE

    def send_event(self, event: Event) -> bytes:
        if isinstance(event, Preamble) and self.state == State.PREAMBLE:
            self.state = State.PART
            return event.data
        elif isinstance(event, (Field, File)) and self.state in {
            State.PREAMBLE,
            State.PART,
            State.DATA,
        }:
            data = b"\r\n--" + self.boundary + b"\r\n"
            data += b'Content-Disposition: form-data; name="%s"' % event.name.encode()
            if isinstance(event, File):
                data += b'; filename="%s"' % event.filename.encode()
            data += b"\r\n"
            for name, value in t.cast(Field, event).headers:
                if name.lower() != "content-disposition":
                    data += f"{name}: {value}\r\n".encode()
            self.state = State.DATA_START
            return data
        elif isinstance(event, Data) and self.state == State.DATA_START:
            self.state = State.DATA
            if len(event.data) > 0:
                return b"\r\n" + event.data
            else:
                return event.data
        elif isinstance(event, Data) and self.state == State.DATA:
            return event.data
        elif isinstance(event, Epilogue):
            self.state = State.COMPLETE
            return b"\r\n--" + self.boundary + b"--\r\n" + event.data
        else:
            raise ValueError(f"Cannot generate {event} in state: {self.state}")

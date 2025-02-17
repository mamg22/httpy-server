import asyncio
from collections.abc import MutableMapping, Iterator
from dataclasses import dataclass
from datetime import datetime
import html
from http import HTTPStatus
import sys
import pathlib
import re
import socket
from typing import Protocol, Self, runtime_checkable
from urllib import parse as uparse


@runtime_checkable
class SupportsLower(Protocol):
    def lower(self) -> Self: ...


class CaseInsensitiveDict[K: SupportsLower, V](MutableMapping):
    data: dict[K, tuple[K, V]]

    def __init__(self) -> None:
        self.data = {}

    def __getitem__(self, key: K) -> V:
        return self.data[key.lower()][1]

    def __setitem__(self, key: K, item: V) -> None:
        self.data[key.lower()] = (key, item)

    def __delitem__(self, key: K) -> None:
        del self.data[key.lower()]

    def __iter__(self) -> Iterator[K]:
        return (key for key, _ in self.data.values())

    def __len__(self) -> int:
        return len(self.data)

    def __contains__(self, key: object, /) -> bool:
        if isinstance(key, SupportsLower):
            return key.lower() in self.data
        else:
            raise KeyError(
                f"Object of type {type(key)} doesn't support .lower(), cannot be used as index"
            )

    def __repr__(self) -> str:
        name = type(self).__name__
        contents = repr({key: val for key, val in self.data.values()})
        return f"{name}({contents})"


@dataclass
class HTTPRequest:
    method: bytes
    target: uparse.SplitResultBytes
    version: tuple[int, int]
    headers: CaseInsensitiveDict[bytes, bytes]
    body: bytes


def make_response(
    code: int,
    body: bytes | None = None,
    headers: dict[bytes, bytes] | None = None,
) -> bytes:
    if headers is None:
        headers = {}

    phrase = HTTPStatus(code).phrase
    status_line = f"HTTP/1.0 {code} {phrase}\r\n".encode()

    headers[b"Server"] = b"httpy/0.1"
    if body is not None:
        headers[b"Content-Length"] = str(len(body)).encode()

    header_lines = (b"%b: %b" % (field, value) for field, value in headers.items())
    header_section = b"\r\n".join(header_lines)

    response = status_line + header_section + b"\r\n\r\n" + (body or b"")

    return response


LOG_FORMAT = '{ip} - - [{datetime}] "{line}" {code} {size}'


def log_response(
    ip: str, receive_time: datetime, request_line: bytes, code: int, size: int
) -> None:
    dt = receive_time.strftime("%d/%b/%Y:%H:%M:%S %z")
    line = request_line.decode()
    msg = LOG_FORMAT.format(ip=ip, datetime=dt, line=line, code=code, size=size)
    print(msg, file=sys.stderr)


class HTTPError(Exception): ...


class RequestParseError(HTTPError):
    message: str
    code: int

    def __init__(self, message: str, code: int = 400) -> None:
        self.message = message
        self.code = code

    def as_response(self) -> bytes:
        body = self.message.encode()

        return make_response(self.code, body)


VALID_METHODS = b"GET HEAD POST".split()
HTTP_VERSION_MATCHER = re.compile(rb"HTTP\/(\d)+\.(\d)+")


def parse_request_line(
    request_line: bytes,
) -> tuple[bytes, uparse.SplitResultBytes, tuple[int, int]]:
    try:
        method, target, version = map(bytes.strip, request_line.split(maxsplit=2))
    except ValueError:
        raise RequestParseError("Invalid request line format")

    if method not in VALID_METHODS:
        raise RequestParseError(f"Unknown request method '{method}'")

    parsed_target = uparse.urlsplit(target)

    if parsed_target.scheme and parsed_target.netloc:
        raise RequestParseError("Absolute URIs not supported", 501)

    if not parsed_target.path.strip() or not parsed_target.path.startswith(b"/"):
        raise RequestParseError("Invalid request URI")

    if matches := HTTP_VERSION_MATCHER.fullmatch(version):
        major, minor = map(int, matches.groups())
    else:
        raise RequestParseError(
            f"Could not parse HTTP version {repr(version.decode())}"
        )

    return method, parsed_target, (major, minor)


async def parse_request(reader: asyncio.StreamReader):
    request_line = (await reader.readline()).rstrip(b"\r\n")

    method, target, version = parse_request_line(request_line)

    headers = CaseInsensitiveDict[bytes, bytes]()
    while line := (await reader.readline()).rstrip():
        if not line:
            break

        field, value = map(bytes.strip, line.split(b":", 1))
        headers[field] = value

    reader.feed_eof()

    try:
        length = int(headers[b"Content-Length"])
        if length < 0:
            raise RequestParseError("Content-Length cannot be negative")
        elif length == 0:
            body = b""
        else:
            body = await reader.read(length)
    except ValueError as e:
        raise RequestParseError("Invalid Content-Length value") from e
    except KeyError:
        if method == b"POST":
            raise RequestParseError("Content-Length required in POST requests")
        body = b""
        while True:
            chunk = await reader.read(1024)
            body += chunk

            if len(chunk) < 1024:
                break

    return HTTPRequest(method, target, version, headers, body)


DIR_TEMPLATE = """\
<!DOCTYPE html>
<html>
    <meta charset="utf-8">
    <title>Directory listing for {path}</title>
<body>
    Listing contents of path {path}
    <ul>
        {listing}
    </ul>
</body>
</html>
"""


def handle_request(request: HTTPRequest, writer: asyncio.StreamWriter) -> None:
    received = datetime.now()

    if request.method in [b"GET", b"HEAD"]:
        target_path = request.target.path
        path = pathlib.Path(target_path.removeprefix(b"/").decode()).resolve()

        try:
            if path.is_relative_to(pathlib.Path.cwd()):
                if path.is_file():
                    content = path.read_bytes()
                else:
                    listing = """<li><a href="..">..</a></li>"""
                    for file in path.iterdir():
                        listing += """<li><a href="{href}">{file}</a></li>""".format(
                            href=uparse.quote(
                                str(file.relative_to(pathlib.Path.cwd()))
                            ),
                            file=html.escape(file.name),
                        )

                    content = DIR_TEMPLATE.format(
                        path=request.target.path.decode(), listing=listing
                    ).encode()

                if request.method == b"GET":
                    response = make_response(200, content)
                else:
                    response = make_response(
                        200, None, {b"Content-Length": str(len(content)).encode()}
                    )
            else:
                response = make_response(403)
        except FileNotFoundError:
            response = make_response(404)
    else:
        response = make_response(501)

    line = b"%b %b HTTP/%i.%i" % (
        request.method,
        request.target.path + request.target.query + request.target.fragment,
        *request.version,
    )
    log_response(
        "{}:{}".format(*writer.get_extra_info("peername")),
        received,
        line,
        int(response.splitlines()[0].split()[1]),
        len(response),
    )

    writer.write(response)


async def connection_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        request = await parse_request(reader)
    except RequestParseError as err:
        writer.write(err.as_response())
    else:
        handle_request(request, writer)

    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main():
    server = await asyncio.start_server(
        connection_handler, host="0", port=8000, family=socket.AF_INET
    )

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

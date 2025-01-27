import asyncio
from collections.abc import MutableMapping, Iterator
from dataclasses import dataclass
import pathlib
import re
import socket
from typing import Protocol, Self
from urllib import parse as uparse


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


def fix_bare_cr[Str: (str, bytes)](text: Str) -> Str:
    bare_finder = r"\r[^\n]"
    match text:
        case str():
            return re.sub(bare_finder, " ", text)
        case bytes():
            return re.sub(bare_finder.encode("ASCII"), b" ", text)


VALID_METHODS = b"GET HEAD OPTIONS TRACE PUT DELETE POST PATCH CONNECT".split()
HTTP_VERSION_MATCHER = re.compile(rb"HTTP\/(\d)\.(\d)")


def parse_request_line(
    request_line: bytes,
) -> tuple[bytes, uparse.SplitResultBytes, tuple[int, int]]:
    method, target, version = map(bytes.strip, request_line.split(b" ", 2))

    if method not in VALID_METHODS:
        raise ValueError(f"Unknown request method '{method}'")

    parsed_target = uparse.urlsplit(target)

    if matches := HTTP_VERSION_MATCHER.fullmatch(version):
        major, minor = map(int, matches.groups())
    else:
        raise ValueError(f"Could not parse HTTP version '{version}'")

    return method, parsed_target, (major, minor)


async def parse_request(reader: asyncio.StreamReader):
    request_line = fix_bare_cr(await reader.readline()).rstrip(b"\r\n")

    method, target, version = parse_request_line(request_line)

    headers = CaseInsensitiveDict()
    while line := fix_bare_cr(await reader.readline()):
        if line.isspace():
            break

        field, value = line.split(b":", 1)
        if field.rstrip() != field:
            raise ValueError(f"Field name {repr(field)} contains trailing whitespace")

        headers[field.lstrip()] = value.strip()

    reader.feed_eof()

    try:
        length = headers["Content-Length"]
        body = await reader.read(int(length))
    except KeyError:
        body = b""
        while True:
            chunk = await reader.read(1024)
            body += chunk

            if len(chunk) < 1024:
                break

    return HTTPRequest(method, target, version, headers, body)


async def connection_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    request = await parse_request(reader)

    if request.method in [b"GET", b"HEAD"]:
        target_path = request.target.path
        path = pathlib.Path(target_path.removeprefix(b"/").decode()).resolve()

        try:
            if path.is_relative_to(pathlib.Path.cwd()):
                if path.is_file():
                    file = open(path)
                    content = file.read()
                else:
                    dir_info = (
                        f"Listing contents of path {request.target.path.decode()}"
                    )
                    files = (file.name for file in path.iterdir())

                    content = f"{dir_info}\n\n{"\n".join(files)}"

                length = len(content)

                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n".encode()
                )

                if request.method == b"GET":
                    writer.write(content.encode())
            else:
                writer.write(b"HTTP/1.1 403 Forbidden\r\n")
        except FileNotFoundError:
            writer.write(b"HTTP/1.1 404 Not Found\r\n")
    else:
        writer.write(b"HTTP/1.1 501 Not Implemented\r\n")

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

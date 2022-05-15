from __future__ import annotations

import logging
import typing as t
from http import HTTPStatus

_logger = logging.getLogger(__name__)

DEFAULT_HTTP_VERSION: str = "HTTP/1.1"


class HTTPMessage:
    def __init__(self, start_line: str, headers: t.Dict):
        self.start_line = start_line
        self.headers = headers

    @classmethod
    def from_bytes(cls, data: bytes) -> HTTPMessage:
        header = {}
        try:
            message: str = data.decode(encoding="utf-8", errors="surrogateescape")
            fields = message.split("\r\n")
            start_line = fields[0]
            # parsing headers
            for line in fields[1:]:
                if len(line) > 2:
                    k: str
                    k, v = line.split(": ", maxsplit=1)
                    header[k] = v
        except (ValueError, TypeError, IndexError):
            _logger.exception("Can't parse request:\n'%s'\n", data)
            raise
        else:
            return cls(start_line, header)

    def as_bytes(self, encoding="utf-8") -> bytes:
        data: bytes = b""
        data_lst = [self.start_line] + [f"{k}: {v}" for k, v in self.headers.items()]
        data += bytes("\r\n".join(data_lst), encoding=encoding, errors="surrogateescape")
        return data


class Request(HTTPMessage):
    """ Represents HTTP request. Provides sufficient functionality for the server. """

    method: str
    request_target: str
    http_version: str

    def __init__(self, start_line: str, headers: t.Dict):
        super(Request, self).__init__(start_line, headers)

        # start line of the request: section 3.1.1 of [RFC7230]
        self.method, self.request_target, self.http_version = start_line.split(maxsplit=2)


class Response(HTTPMessage):
    """ Represents HTTP response """
    status: HTTPStatus
    http_version: str
    content: t.Optional[bytes] = None

    def __init__(self):
        super(Response, self).__init__(start_line="", headers=dict())
        self.status = HTTPStatus.OK
        self.http_version = DEFAULT_HTTP_VERSION

        # Section 6.1 of [RFC7230]:
        # "A server that does not support persistent connections MUST send the
        # "close" connection option in every response message that does not
        # have a 1xx (Informational) status code."
        self.headers["Connection"] = "close"

    def set_content(self, data: bytes, content_type: str, enc: t.Optional[str] = None,
                    head_request: bool = False):
        if head_request:
            self.content = b""
        else:
            self.content = data
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(data))
        if enc is not None:
            self.headers["Content-Encoding"] = enc

    def as_bytes(self, encoding="utf-8") -> bytes:
        # update start line
        self.start_line = " ".join([self.http_version, str(self.status.value), self.status.name])
        data = super(Response, self).as_bytes(encoding)

        # append binary content
        if self.content is not None:
            data += b"\r\n\r\n" + self.content
        return data

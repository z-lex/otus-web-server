from __future__ import annotations

import logging
import mimetypes
import socket
import typing as t
import urllib.parse
from email.utils import formatdate
from http import HTTPStatus
from multiprocessing import Process
from pathlib import Path

from asyncserver import OtusAsyncServer, ReaderStream, WriterStream
from httpclasses import Request, Response

_logger = logging.getLogger(__name__)


class WorkerProcess(Process):
    """ Represents HTTP server worker process. All methods of :class:`multiprocessing.Process` are
    available.

    :param accepting_socket: socket for accepting new TCP connections.
    :param document_root: directory with static content
    """

    #: Maximum request total size in bytes
    MAX_REQUEST_SIZE: t.Final[int] = 1000

    #: Protocol versioning: section 2.6 of [RFC7230]
    SUPPORTED_HTTP_VERSION: t.Final[str] = "HTTP/1.1"

    SUPPORTED_HTTP_METHODS: t.Final[t.Tuple[str, ...]] = ("GET", "HEAD",)

    SERVER_NAME: t.Final[str] = "OTUServer"

    def __init__(self, accepting_socket: socket.socket, document_root: Path):
        self._document_root: Path = Path(document_root).expanduser().resolve()
        self._server = OtusAsyncServer(accepting_socket, self._client_connected_cb)
        super(WorkerProcess, self).__init__(target=self._server.serve_forever)

    def _get_content_info_for_target(self, request_target: str) -> t.Tuple[str, str, Path]:
        parsed = urllib.parse.urlparse(request_target)

        unquoted = urllib.parse.unquote(parsed.path, encoding="utf-8")

        request_path = Path(unquoted)
        if unquoted.endswith("/"):
            request_path = request_path.joinpath("index.html")

        content_path = self._document_root.joinpath(request_path.relative_to("/")).resolve()

        # the file must be in the root directory or below
        if not str(self._document_root) in str(content_path):
            raise PermissionError
        elif not content_path.is_file():
            raise FileNotFoundError

        mtype, enc = mimetypes.guess_type(content_path)
        if not mtype:
            # section 4.5.1 of [RFC2046]:
            # "The "octet-stream" subtype is used to indicate that a body contains arbitrary
            # binary data."
            mtype = "application/octet-stream"

        return mtype, enc, content_path

    @staticmethod
    def _read_content_data(content_path: Path, chunk_size: int = 128) -> t.Generator[bytes]:
        with open(content_path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if data == b"":
                    break
                yield data

    async def _client_connected_cb(self, reader: ReaderStream, writer: WriterStream):

        try:
            data: bytes = await reader.readuntil(max_buf_size=self.MAX_REQUEST_SIZE,
                                                 chunk_size=32, sep=b"\r\n\r\n")
        except Exception as e:
            if isinstance(e, OverflowError):
                _logger.error("cant receive all data, request is too big")
            elif isinstance(e, ConnectionAbortedError):
                _logger.error("request is incomplete")
            else:
                _logger.exception("an unknown error occurred while receiving data")
            return

        _logger.debug("received data len: '%i'", len(data))
        if not len(data):
            return

        status = HTTPStatus.OK
        request: t.Optional[Request] = None

        try:
            request = Request.from_bytes(data)
        except Exception:
            status = HTTPStatus.BAD_REQUEST
        else:
            if request.method not in self.SUPPORTED_HTTP_METHODS:
                status = HTTPStatus.METHOD_NOT_ALLOWED

        response = Response()
        response.status = status

        content_gen: t.Optional[t.Generator[bytes]] = None

        if request and response.status == HTTPStatus.OK:
            try:
                content_type, enc, content_path = \
                    self._get_content_info_for_target(request.request_target)
                content_length = content_path.stat().st_size
            except FileNotFoundError:
                response.status = HTTPStatus.NOT_FOUND
            except PermissionError:
                response.status = HTTPStatus.FORBIDDEN
            except ValueError:
                response.status = HTTPStatus.BAD_REQUEST
            else:
                response.set_content_info(content_length, content_type, enc)
                if request.method == "GET":
                    content_gen = self._read_content_data(content_path)

        # sending response
        response.headers["Date"] = formatdate(usegmt=True)
        response.headers["Server"] = self.SERVER_NAME
        response_data: bytes = response.as_bytes()
        try:
            await writer.writeall(response_data)

            # sending binary content by chunks
            if content_gen:
                for chunk in content_gen:
                    await writer.writeall(chunk)

        except ConnectionAbortedError:
            _logger.error("can't write header data, connection was interrupted")
        except Exception:
            _logger.exception("an unknown error occurred during sending data")
        else:
            _logger.debug("response sent: %s", response_data)

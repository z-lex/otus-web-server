from __future__ import annotations

import logging
import mimetypes
import socket
import typing as t
from email.utils import formatdate
from http import HTTPStatus
from multiprocessing import Process
from pathlib import Path

from asyncserver import OtusAsyncServer, ReaderStream, WriterStream
from httpclasses import Request, Response
from tools import resolve_percents_in_http_target, split_query_from_http_target

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

    def _get_content_for_target(self, request_target: str) -> t.Tuple[str, str, bytes]:
        request_target, query = split_query_from_http_target(request_target)

        request_target = resolve_percents_in_http_target(request_target)

        request_path = Path(request_target)
        if request_target.endswith("/"):
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
        with open(content_path, "rb") as f:
            data = f.read()

        return mtype, enc, data

    async def _client_connected_cb(self, reader: ReaderStream, writer: WriterStream):

        data: bytes = await reader.read(self.MAX_REQUEST_SIZE)
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

        if request and response.status == HTTPStatus.OK:
            try:
                content_type, enc, content = self._get_content_for_target(request.request_target)
            except FileNotFoundError:
                response.status = HTTPStatus.NOT_FOUND
            except PermissionError:
                response.status = HTTPStatus.FORBIDDEN
            except ValueError:
                response.status = HTTPStatus.BAD_REQUEST
            else:
                response.set_content(content, content_type, enc, request.method == "HEAD")

        # sending response
        response.headers["Date"] = formatdate(usegmt=True)
        response.headers["Server"] = self.SERVER_NAME
        response_data: bytes = response.as_bytes()
        await writer.write(response_data)
        _logger.debug("response sent: %s", response_data)

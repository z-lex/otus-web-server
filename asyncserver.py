from __future__ import annotations

import heapq
import logging
import select
import socket
import time
import typing as t
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from errno import EAGAIN, EBADF, ECONNABORTED, ECONNRESET, EINTR, ENOTCONN, EPIPE, ESHUTDOWN, \
    EWOULDBLOCK

_DISCONNECTED = frozenset({ECONNRESET, ENOTCONN, ESHUTDOWN, ECONNABORTED, EPIPE, EBADF})

_logger = logging.getLogger(__name__)

EXCEPTIONS_SELECT_FLAGS = select.POLLERR | select.POLLHUP | select.POLLNVAL
READING_SELECT_FLAGS = select.POLLIN | select.POLLPRI | EXCEPTIONS_SELECT_FLAGS
WRITING_SELECT_FLAGS = select.POLLOUT | EXCEPTIONS_SELECT_FLAGS


@dataclass
class SockInfo:
    """ :class:`socket.socket` wrapper class that stores additional information about the socket
    properties and state required by server loop
    """
    sock: socket.socket

    # Only check for exceptions if object was either readable or writable.
    select_flags: int = EXCEPTIONS_SELECT_FLAGS

    #: initial socket descriptor
    fd: int = field(init=False)

    accepting: bool = False
    readable_now: bool = False
    writable_now: bool = False

    def __post_init__(self):
        self.fd = self.sock.fileno()

    @property
    def closed(self) -> bool:
        return self.sock.fileno() < 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError as e:
            if e.args[0] not in (ENOTCONN, EBADF):
                raise

    def clear_ready_flags(self):
        self.readable_now = False
        self.writable_now = False


class AsyncStream(ABC):
    """ Abstract class of the asynchronous data stream """

    def __init__(self, sock_info: SockInfo):
        self.sock_info = sock_info

    @abstractmethod
    def __await__(self):
        pass

    def __repr__(self):
        return f"<{self.__class__.__name__}: fd={self.fd}>"

    @property
    def fd(self):
        return self.sock_info.fd

    @property
    def readable_now(self):
        return self.sock_info.readable_now

    @property
    def writable_now(self):
        return self.sock_info.writable_now

    @property
    def closed(self) -> bool:
        return self.sock_info.closed

    def close(self):
        self.sock_info.close()


class ReaderStream(AsyncStream):
    """ Asynchronous socket reader """

    def __await__(self):
        if self.readable_now:
            # release the coroutine immediately if the state is already set
            n = yield
        else:
            n = yield "read"
        return n

    async def read(self, n: int = 65536) -> bytes:
        await self
        data = b""
        try:
            data = self.sock_info.sock.recv(n)
            if not data:
                self.close()
        except OSError as e:
            if e.args[0] in _DISCONNECTED:
                self.close()
            else:
                raise
        finally:
            self.sock_info.readable_now = False
        return data

    async def readuntil(self, max_buf_size: int = 65536, chunk_size: int = 65536,
                        sep: bytes = b"\r\n\r\n") -> bytes:
        """ Read data asynchronously from the stream until separator is received.

        :param max_buf_size:
        :param chunk_size:
        :param sep: separator byte string
        :return: resulting data

        :raises OverflowError: if the size of the received data exceeds ``max_buf_size``
        :raises ConnectionAbortedError: if the connection closed before ``sep`` is received
        :raises OSError: if an unhandled socket error occurred
        """
        if chunk_size > max_buf_size:
            chunk_size = max_buf_size

        data: bytes = b""
        bytes_read: int = 0
        while not self.closed and not data.endswith(sep):
            chunk = await self.read(chunk_size)
            _logger.debug("read request chunk: %s", chunk)
            bytes_read += len(chunk)
            if bytes_read > max_buf_size:
                raise OverflowError
            data += chunk

        if not data.endswith(sep):
            raise ConnectionAbortedError

        return data.rstrip(sep)


class WriterStream(AsyncStream):
    """ Asynchronous socket writer """

    def __await__(self):
        if self.writable_now:
            # release the coroutine immediately if the state is already set
            n = yield
        else:
            n = yield "write"
        return n

    async def write(self, data: bytes) -> int:
        await self
        try:
            bytes_sent = self.sock_info.sock.send(data)
        except OSError as e:
            if e.args[0] == EWOULDBLOCK:
                bytes_sent = 0
            elif e.args[0] in _DISCONNECTED:
                self.close()
                bytes_sent = 0
            else:
                raise
        finally:
            if self.closed:
                self.sock_info.writable_now = False

        return bytes_sent

    async def writeall(self, data: bytes) -> None:
        """ Like :meth:`write`, but continues to write data to the stream until either entire
        ``data`` buffer has been sent or an error occurs.

        :raises ConnectionAbortedError: if the connection was closed before all data is sent
        :raises OSError: if an unhandled socket error occurred
        """
        bytes_sent = 0
        bytes_to_send = len(data)

        while bytes_sent < bytes_to_send and not self.closed:
            bytes_sent += await self.write(data[bytes_sent:])

        if bytes_sent < bytes_to_send:
            raise ConnectionAbortedError


@dataclass
class ConnCallbackCoroutine:
    """ Dataclass for storing information about the coroutine created from the
    ``on_connected`` callback argument passed to :class:`OtusAsyncServer`
    """
    coro: t.Coroutine
    reader: ReaderStream
    writer: WriterStream
    waiting_for_reader: bool = False
    waiting_for_writer: bool = False

    def close_streams(self):
        self.reader.close()
        self.writer.close()


class OtusAsyncServer:
    """ An example implementation of an asynchronous server. Made for educational purposes.

    Code used to work with epoll:
    https://github.com/m13253/python-asyncore-epoll

    Information used to work with Python's built-in coroutines:
    https://peps.python.org/pep-0492/
    https://docs.python.org/3/reference/datamodel.html#coroutines
    https://mleue.com/posts/yield-to-async-await/

    The client connection callback and asynchronous read/write streams are made by an example of
    the ``asyncio`` module.

    :param accepting_socket: socket used for accept new client connected
    :param on_connected: callback function called when a new client connects
    """

    def __init__(self, accepting_socket: socket.socket,
                 on_connected: t.Callable[[ReaderStream, WriterStream], t.Coroutine]):
        self._socket = accepting_socket
        self._socket_map: t.Dict[int, SockInfo] = {
            # first socket in socket map for accepting new connections
            self._socket.fileno(): SockInfo(
                sock=self._socket,
                select_flags=READING_SELECT_FLAGS,
                accepting=True
            )
        }

        self._callback = on_connected
        self._callback_coros: t.List[t.Tuple[float, ConnCallbackCoroutine]] = []

    def _accept(self, info: SockInfo) -> None:
        # add new sockets to socket map
        _logger.debug("accept on sock fd = %i called", info.fd)
        try:
            new_sock, addr = info.sock.accept()
        except TypeError:
            _logger.debug("accept non-error exception: TypeError", exc_info=True)
            return
        except OSError as e:
            if e.args[0] in (EWOULDBLOCK, ECONNABORTED, EAGAIN):
                _logger.debug("accept non-error exception: OSError: %s", e)
                return
            else:
                raise
        else:
            new_sock.setblocking(False)

            # add reader socket
            reader_sock = new_sock
            self._socket_map[reader_sock.fileno()] = SockInfo(reader_sock, READING_SELECT_FLAGS)
            # add writer socket
            writer_sock = new_sock.dup()
            self._socket_map[writer_sock.fileno()] = SockInfo(writer_sock, WRITING_SELECT_FLAGS)

            # add coroutine
            reader = ReaderStream(self._socket_map[reader_sock.fileno()])
            writer = WriterStream(self._socket_map[writer_sock.fileno()])
            coro: ConnCallbackCoroutine = ConnCallbackCoroutine(
                coro=self._callback(reader, writer), reader=reader, writer=writer)
            heapq.heappush(self._callback_coros, (time.time(), coro))
            _logger.debug("coro %s created", coro)

    def _epoll_poller(self, timeout=0.0) -> None:
        """A poller which uses epoll(), supported on Linux 2.5.44 and newer."""
        pollster = select.epoll()

        fd: int
        info: SockInfo
        for fd, info in self._socket_map.items():
            pollster.register(fd, info.select_flags)
        try:
            r = pollster.poll(timeout)
        except select.error as err:
            if err.args[0] != EINTR:
                raise
            r = []

        for fd, flags in r:
            info = self._socket_map.get(fd)
            if info is None:
                continue

            # update socket state
            if flags & select.POLLIN:
                if info.accepting:
                    self._accept(info)
                    continue
                info.readable_now = True
            if flags & select.POLLOUT:
                info.writable_now = True

    def serve_forever(self, timeout: float = 300.0):
        """ Server main loop """
        iter = 0
        while self._socket_map:
            iter += 1
            _logger.debug("serve_forever iteration %i, socket fds: %s",
                          iter, list(self._socket_map.keys()))

            self._epoll_poller(timeout)

            if len(self._callback_coros):
                cinfo: ConnCallbackCoroutine
                # get the longest awaited coroutine (with minimum timestamp)
                _, cinfo = heapq.heappop(self._callback_coros)
                try:
                    # don't unblock if the coro waits for socket
                    if not cinfo.waiting_for_reader and not cinfo.waiting_for_writer:
                        result = cinfo.coro.send(None)

                        # interrupt manually if reader or writer closed
                        if cinfo.reader.closed or cinfo.writer.closed:
                            _logger.debug("socket closed. interrupting coroutine")
                            raise StopIteration

                        if result == "read":
                            cinfo.waiting_for_reader = True
                            _logger.debug("coro suspended on read from %s: %s",
                                          cinfo.reader.fd, cinfo)
                        elif result == "write":
                            cinfo.waiting_for_writer = True
                            _logger.debug("coro suspended on write from %s: %s",
                                          cinfo.writer.fd, cinfo)
                    else:
                        if cinfo.waiting_for_reader and cinfo.reader.readable_now or \
                                cinfo.waiting_for_writer and cinfo.writer.writable_now:
                            cinfo.waiting_for_reader = False
                            cinfo.waiting_for_writer = False
                            _logger.debug(
                                "iter %i on sock fd = %i: readable: %i, writable: %i", iter,
                                cinfo.reader.fd if cinfo.reader.readable_now else cinfo.writer.fd,
                                cinfo.reader.readable_now, cinfo.writer.writable_now
                            )
                            cinfo.coro.send(None)

                    # push coroutine with the new timestamp
                    heapq.heappush(self._callback_coros, (time.time(), cinfo))
                except StopIteration:
                    _logger.debug("coro %s has finished, closing sockets", cinfo.coro)
                    self._socket_map.pop(cinfo.reader.fd, None)
                    self._socket_map.pop(cinfo.writer.fd, None)
                    cinfo.close_streams()

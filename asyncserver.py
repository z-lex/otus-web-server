from __future__ import annotations

import contextlib
import heapq
import logging
import select
import socket
import time
import typing as t
from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from errno import EAGAIN, EBADF, ECONNABORTED, ECONNRESET, EINTR, ENOTCONN, EPIPE, ESHUTDOWN, \
    EWOULDBLOCK

_DISCONNECTED = frozenset({ECONNRESET, ENOTCONN, ESHUTDOWN, ECONNABORTED, EPIPE, EBADF})

_logger = logging.getLogger(__name__)


@dataclass
class ConnCallbackCoroutine:
    """ Dataclass for storing information about the coroutine created from the
    ``on_connected`` callback argument passed to :class:`OtusAsyncServer`
    """
    coro: t.Coroutine
    reader: ReaderStream
    writer: WriterStream
    waiting_for_sock: bool = False

    def close_streams(self):
        self.reader.close()
        self.writer.close()


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
    waiters: t.List[t.Tuple[float, ConnCallbackCoroutine]] = field(default_factory=list)

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


class CmdName(Enum):
    READ = "read"
    WRITE = "write"


@dataclass
class Command:
    """ Represents action to be performed by the server loop when ``await`` is called on some
    awaitable object.
    """
    cmd_name: CmdName
    sock_info: SockInfo


class AsyncSockWaiter(ABC):
    """ Abstract class of the awaitable that sends a specific command to the server loop and
    suspends the current coroutine until the socket changes its state to the desired state
    """

    #: command to be sent to the server loop
    cmd: CmdName = None

    #: desired socket state
    sock_info_ready_attr: str = None

    def __init__(self, sock_info: SockInfo):
        self.sock_info = sock_info

    def __await__(self):
        if getattr(self.sock_info, self.sock_info_ready_attr, None) is True:
            # release the coroutine immediately if the state is already set
            n = yield
        else:
            n = yield Command(cmd_name=self.cmd, sock_info=self.sock_info)
        return n


class AsyncReadSockWaiter(AsyncSockWaiter):
    """ Suspends the current coroutine until its :attr:`sock_info` becomes readable """
    cmd = CmdName.READ
    sock_info_ready_attr = "readable_now"


class AsyncWriteSockWaiter(AsyncSockWaiter):
    """ Suspends the current coroutine until its :attr:`sock_info` becomes writable """
    cmd = CmdName.WRITE
    sock_info_ready_attr = "writable_now"


class AsyncStream(ABC):
    """ Abstract class of the asynchronous data stream """
    waiter_cls = AsyncSockWaiter

    def __init__(self, sock_info: SockInfo):
        self.sock_info = sock_info
        self.waiter = self.waiter_cls(self.sock_info)

    def __repr__(self):
        return f"<{self.__class__.__name__}: fd={self.fd}>"

    @property
    def fd(self):
        return self.sock_info.fd

    @property
    def closed(self) -> bool:
        return self.sock_info.closed

    def close(self):
        self.sock_info.close()


class ReaderStream(AsyncStream):
    """ Asynchronous socket reader """
    waiter_cls = AsyncReadSockWaiter

    async def read(self, n: int = -1) -> bytes:
        await self.waiter
        try:
            data = self.sock_info.sock.recv(n)
            if not data:
                self.close()
                data = b""
        except OSError as e:
            if e.args[0] in _DISCONNECTED:
                self.close()
                data = b""
            else:
                raise
        finally:
            self.sock_info.readable_now = False
        return data


class WriterStream(AsyncStream):
    """ Asynchronous socket writer """
    waiter_cls = AsyncWriteSockWaiter

    async def write(self, data: bytes) -> int:
        await self.waiter
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
            self.sock_info.writable_now = False

        return bytes_sent


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
        # self._callback_coros: t.Deque[ConnCallbackCoroutine] = deque()

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
            # self._callback_coros += [coro]
            _logger.debug("coro %s created", coro)

    def _epoll_poller(self, timeout=0.0) -> t.Generator[SockInfo, None, None]:
        """A poller which uses epoll(), supported on Linux 2.5.44 and newer."""
        pollster = select.epoll()

        fd: int
        info: SockInfo
        if len(self._socket_map) > 0:
            for fd, info in self._socket_map.items():
                # info.clear_ready_flags()
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

                yield info

    def serve_forever(self, timeout: float = 300.0):
        """ Server main loop """
        iter = 0
        while self._socket_map:
            iter += 1
            _logger.debug("serve_forever iteration %i, socket fds: %s",
                          iter, list(self._socket_map.keys()))

            sock_info: SockInfo
            for sock_info in self._epoll_poller(timeout):
                if sock_info.readable_now or sock_info.writable_now:
                    _logger.debug("iter %i on sock fd = %i: readable: %i, writable: %i", iter,
                                  sock_info.fd, sock_info.readable_now, sock_info.writable_now)
                    # release waiter coroutines
                    while len(sock_info.waiters):
                        # w = sock_info.waiters.pop()
                        # get the waiter with minimum timestamp
                        _, w = heapq.heappop(sock_info.waiters)
                        with contextlib.suppress(StopIteration):
                            w.coro.send(None)
                        w.waiting_for_sock = False
                        _logger.debug("coro has finished waiting on %s: %s", sock_info.fd, w)

            if len(self._callback_coros):
                cinfo: ConnCallbackCoroutine
                # get the longest awaited coroutine (with minimum timestamp)
                _, cinfo = heapq.heappop(self._callback_coros)
                try:
                    # don't unblock if the coro waits for socket
                    if not cinfo.waiting_for_sock:
                        result = cinfo.coro.send(None)

                        # interrupt manually if reader or writer closed
                        if cinfo.reader.closed or cinfo.writer.closed:
                            _logger.debug("socket closed. interrupting coroutine")
                            raise StopIteration

                        if isinstance(result, Command):
                            if result.cmd_name in (CmdName.READ, CmdName.WRITE):
                                heapq.heappush(result.sock_info.waiters, (time.time(), cinfo))
                                cinfo.waiting_for_sock = True
                                _logger.debug("coro suspended on '%s' from %s: %s",
                                              result.cmd_name.value, result.sock_info.fd, cinfo)

                    # push coroutine with the new timestamp
                    heapq.heappush(self._callback_coros, (time.time(), cinfo))
                except StopIteration:
                    _logger.debug("coro %s has finished, closing sockets", cinfo.coro)
                    self._socket_map.pop(cinfo.reader.fd, None)
                    self._socket_map.pop(cinfo.writer.fd, None)
                    cinfo.close_streams()

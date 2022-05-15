import logging
import platform
import re
import socket
from argparse import ArgumentParser
from pathlib import Path

from workerprocess import WorkerProcess

DEFAULT_WORKERS_COUNT = 2
DEFAULT_DOCUMENT_ROOT = "./"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

log_filename = None
logging.basicConfig(format="[%(asctime)s] %(name)s %(levelname).1s %(process)d %(message)s",
                    datefmt="%Y.%m.%d %H:%M:%S", filename=log_filename, level="DEBUG")

_logger = logging.getLogger(__name__)


def create_accepting_socket(host: str, port: int, listen_backlog: int = 500) -> socket.socket:
    """ Create TCP socket for accepting connections from worker processes.
    :param host:
    :param port:
    :param listen_backlog:
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((host, port))
    s.setblocking(False)
    s.listen(listen_backlog)
    s.set_inheritable(True)
    return s


if __name__ == "__main__":
    parser = ArgumentParser(description="OTUServer: simple asynchronous web server")
    parser.add_argument("-w", type=int, help="number of workers", default=DEFAULT_WORKERS_COUNT)
    parser.add_argument("-r", type=Path, help="document root", default=DEFAULT_DOCUMENT_ROOT)
    args = parser.parse_args()

    workers_count: int = args.w
    document_root: Path = args.r
    logging_filename = None

    m = re.match(r"(?P<OS>\w+)-(?P<kernel_version>[0-9\.]+)-.*", platform.platform())
    if not m or "Linux" not in m["OS"] or m["kernel_version"] < "2.5.44":
        _logger.error("Can't start: OTUServer can only run on Linux 2.5.44 or newer")
        exit(1)

    if not document_root.is_dir():
        _logger.error("Can't start: document root '%s' does not exist", document_root.resolve())
        exit(1)

    host = DEFAULT_HOST
    port = DEFAULT_PORT
    accepting_socket = create_accepting_socket(host, port)

    workers = [WorkerProcess(accepting_socket, document_root=document_root)
               for _ in range(workers_count)]
    try:
        for w in workers:
            _logger.info("starting worker process %r", w)
            w.start()

        for w in workers:
            w.join()
    finally:
        _logger.info("main process terminates")
        accepting_socket.close()

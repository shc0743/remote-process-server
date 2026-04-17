from __future__ import annotations

import base64
import errno
import json
import os
import select
import tempfile
import threading
import time
from typing import Dict, Optional, Tuple

from rmpsm_transport import safe_unlink

if os.name == 'nt':
    from rmpsm_winpipe import NamedPipeBootstrapServer, read_named_pipe_line
else:
    NamedPipeBootstrapServer = None  # type: ignore[assignment]
    read_named_pipe_line = None  # type: ignore[assignment]


def default_connection_file() -> str:
    if os.name == 'nt':
        return r"\\.\pipe\remote_process_server_bootstrap"
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return os.path.join(base, "remote_process_server.bootstrap")


def _encode_connection_info(address: Tuple[str, int], authkey: bytes) -> bytes:
    payload = {
        "address": [address[0], int(address[1])],
        "authkey": base64.b64encode(authkey).decode("ascii"),
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _decode_connection_info(payload: bytes) -> Tuple[Tuple[str, int], bytes]:
    info = json.loads(payload.decode("utf-8"))
    address = (str(info["address"][0]), int(info["address"][1]))
    authkey = base64.b64decode(str(info["authkey"]).encode("ascii"))
    return address, authkey


class _PosixFifoBootstrapServer:
    def __init__(self, path: str, payload: bytes):
        self.path = path
        self.payload = payload if payload.endswith(b"\n") else payload + b"\n"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        safe_unlink(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        os.mkfifo(self.path, 0o600)
        self._thread = threading.Thread(target=self._run, name="rmpsm-bootstrap-fifo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        safe_unlink(self.path)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.05)
                    continue
                time.sleep(0.1)
                continue

            try:
                off = 0
                while off < len(self.payload) and not self._stop_event.is_set():
                    try:
                        n = os.write(fd, self.payload[off:])
                    except OSError as e:
                        if e.errno in (errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK):
                            continue
                        break
                    if n <= 0:
                        break
                    off += n
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

            time.sleep(0.05)


def _posix_read_line(path: str, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            break
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"bootstrap endpoint not ready: {path}")
                time.sleep(0.05)
                continue
            raise

    try:
        buf = bytearray()
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if deadline is not None and remaining <= 0:
                raise TimeoutError(f"timed out waiting for bootstrap data from {path}")
            rlist, _, _ = select.select([fd], [], [], remaining)
            if not rlist:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                    continue
                raise
            if not chunk:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for bootstrap data from {path}")
                time.sleep(0.02)
                continue
            buf.extend(chunk)
            newline = buf.find(b"\n")
            if newline >= 0:
                return bytes(buf[:newline])
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


class BootstrapConnectionServer:
    def __init__(self, path: str, address: Tuple[str, int], authkey: bytes):
        self.path = path
        self.address = address
        self.authkey = authkey
        self._payload = _encode_connection_info(address, authkey)
        self._impl = NamedPipeBootstrapServer(path, self._payload) if os.name == 'nt' else _PosixFifoBootstrapServer(path, self._payload)

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()


_SERVERS: Dict[str, BootstrapConnectionServer] = {}
_SERVERS_LOCK = threading.Lock()


def write_connection_info(path: str, address: Tuple[str, int], authkey: bytes) -> None:
    server = BootstrapConnectionServer(path, address, authkey)
    with _SERVERS_LOCK:
        old = _SERVERS.pop(path, None)
        _SERVERS[path] = server
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
    server.start()


def close_connection_info(path: str) -> None:
    with _SERVERS_LOCK:
        server = _SERVERS.pop(path, None)
    if server is None:
        return
    try:
        server.stop()
    except Exception:
        pass


def read_connection_info(path: str, timeout: float = 1.0) -> Tuple[Tuple[str, int], bytes]:
    if os.name == 'nt':
        from rmpsm_winpipe import read_named_pipe_line
        payload = read_named_pipe_line(path, timeout=timeout)
    else:
        payload = _posix_read_line(path, timeout=timeout)
    return _decode_connection_info(payload)


def probe_connection_info(path: str, timeout: float = 1.0) -> bool:
    try:
        read_connection_info(path, timeout=timeout)
        return True
    except Exception:
        return False

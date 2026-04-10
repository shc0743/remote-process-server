#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import platform
import secrets
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import errno
from dataclasses import dataclass
from typing import Optional, Tuple

MAGIC = 0x961F132BDDDC19B9
VERSION = 1

TYPE_REPLY = 0
TYPE_STOP_SERVER = 1
TYPE_CREATE_TASK = 2
TYPE_KILL_TASK = 3
TYPE_TASK_END = 4
TYPE_INPUT_DATA = 5
TYPE_RECEIVE_STDOUT = 6
TYPE_RECEIVE_STDERR = 7
TYPE_QUERY_VERSION = 255

# manager-only control packet
TYPE_REGISTER_SESSION = 8

HEADER = struct.Struct("<QQQQQQ")
U64 = struct.Struct("<Q")

DEFAULT_CPP_SERVER = os.environ.get(
    "RMPSM_CPP_SERVER",
    "./rmpsm_server." + str(platform.system().lower()) + "_" + str(platform.machine()),
)


def default_manager_base() -> str:
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return os.path.join(tmpdir, "rmpsm_server")


def is_fifo_mode(mode: int) -> bool:
    return (mode & 0o170000) == 0o010000


def ensure_fifo(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if os.path.exists(path):
        st = os.stat(path)
        if not is_fifo_mode(st.st_mode):
            raise RuntimeError(f"{path} exists but is not a FIFO")
        return

    os.mkfifo(path, 0o600)


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    off = 0
    while off < len(view):
        try:
            n = os.write(fd, view[off:])
        except InterruptedError:
            continue
        except BlockingIOError:
            time.sleep(0.001)
            continue
        if n <= 0:
            raise BrokenPipeError("write returned <= 0")
        off += n


def read_exact(fd: int, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = os.read(fd, n - len(buf))
        except InterruptedError:
            continue
        except BlockingIOError:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            continue
        if not chunk:
            if not buf:
                return None
            raise EOFError("unexpected EOF")
        buf.extend(chunk)
    return bytes(buf)


def pack_packet(pkt_type: int, request_id: int, task_id: int, payload: bytes) -> bytes:
    return HEADER.pack(
        MAGIC,
        VERSION,
        pkt_type & 0xFFFFFFFFFFFFFFFF,
        request_id & 0xFFFFFFFFFFFFFFFF,
        task_id & 0xFFFFFFFFFFFFFFFF,
        len(payload),
    ) + payload


def pack_u64(v: int) -> bytes:
    return U64.pack(v & 0xFFFFFFFFFFFFFFFF)


def unpack_u64(payload: bytes, offset: int = 0) -> int:
    return U64.unpack_from(payload, offset)[0]


class PacketReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buf = bytearray()

    def read_packet(self) -> Optional[Tuple[int, int, int, bytes]]:
        while True:
            if len(self.buf) >= HEADER.size:
                magic, version, pkt_type, request_id, task_id, length = HEADER.unpack_from(self.buf, 0)
                if magic != MAGIC:
                    raise RuntimeError(f"bad magic: 0x{magic:x}")
                if version != VERSION:
                    raise RuntimeError(f"bad version: {version}")

                total = HEADER.size + length
                if len(self.buf) >= total:
                    payload = bytes(self.buf[HEADER.size:total])
                    del self.buf[:total]
                    return pkt_type, request_id, task_id, payload

            try:
                chunk = os.read(self.fd, 65536)
            except InterruptedError:
                continue
            except BlockingIOError:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if not r:
                    continue
                continue

            if not chunk:
                if not self.buf:
                    return None
                raise EOFError("unexpected EOF while reading packet")

            self.buf.extend(chunk)


def make_request_id() -> int:
    return ((time.time_ns() << 1) ^ os.getpid() ^ secrets.randbits(32)) & 0xFFFFFFFFFFFFFFFF


def default_control_path(base: str) -> str:
    return base + ".ctl"


def session_paths(base: str) -> Tuple[str, str, str]:
    sid = f"{os.getpid()}_{time.time_ns()}_{secrets.token_hex(4)}"
    req = f"{base}.req.{sid}"
    resp = f"{base}.resp.{sid}"
    return sid, req, resp


def pack_register_session(req_path: str, resp_path: str) -> bytes:
    req_b = req_path.encode("utf-8")
    resp_b = resp_path.encode("utf-8")
    return pack_u64(len(req_b)) + req_b + pack_u64(len(resp_b)) + resp_b


def unpack_register_session(payload: bytes) -> Tuple[str, str]:
    if len(payload) < 16:
        raise ValueError("bad register payload")
    req_len = unpack_u64(payload, 0)
    off = 8
    if len(payload) < off + req_len + 8:
        raise ValueError("bad register payload")
    req_path = payload[off:off + req_len].decode("utf-8", "strict")
    off += req_len
    resp_len = unpack_u64(payload, off)
    off += 8
    if len(payload) < off + resp_len:
        raise ValueError("bad register payload")
    resp_path = payload[off:off + resp_len].decode("utf-8", "strict")
    return req_path, resp_path


@dataclass
class SessionBridge:
    req_path: str
    resp_path: str
    cpp_server: str
    stop_event: threading.Event

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return t

    def run(self) -> None:
        req_fd = -1
        resp_fd = -1
        proc: Optional[subprocess.Popen] = None
        try:
            # open request fifo for reading first; client will open write end shortly after register
            req_fd = os.open(self.req_path, os.O_RDONLY | os.O_NONBLOCK)

            # response fifo needs a reader on client side first; retry until it exists
            while not self.stop_event.is_set():
                try:
                    resp_fd = os.open(self.resp_path, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError as e:
                    if e.errno in (errno.ENXIO, errno.ENOENT):
                        time.sleep(0.05)
                        continue
                    raise

            if resp_fd < 0:
                return

            proc = subprocess.Popen(
                [self.cpp_server],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None

            cpp_stdin_fd = proc.stdin.fileno()
            cpp_stdout_fd = proc.stdout.fileno()

            seen_client_data = False

            while not self.stop_event.is_set():
                if proc.poll() is not None:
                    break

                rlist = [req_fd, cpp_stdout_fd]
                r, _, _ = select.select(rlist, [], [], 0.2)

                if req_fd in r:
                    try:
                        data = os.read(req_fd, 65536)
                    except InterruptedError:
                        data = b""
                    if data:
                        seen_client_data = True
                        write_all(cpp_stdin_fd, data)
                    else:
                        # FIFO on Linux can report read-ready with no writer yet.
                        # Before we've seen any client data, keep waiting.
                        if seen_client_data:
                            break
                        time.sleep(0.05)

                if cpp_stdout_fd in r:
                    try:
                        data = os.read(cpp_stdout_fd, 65536)
                    except InterruptedError:
                        data = b""
                    if data:
                        write_all(resp_fd, data)
                    else:
                        if proc.poll() is not None:
                            break

            # drain a little more output if the process just exited
            if proc is not None and proc.stdout is not None:
                end_deadline = time.time() + 0.5
                while time.time() < end_deadline:
                    try:
                        r, _, _ = select.select([proc.stdout.fileno()], [], [], 0.05)
                    except Exception:
                        break
                    if not r:
                        break
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    try:
                        write_all(resp_fd, data)
                    except Exception:
                        break

        except BrokenPipeError:
            pass
        except Exception:
            pass
        finally:
            self.stop_event.set()
            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            try:
                if proc is not None:
                    proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if req_fd >= 0:
                    os.close(req_fd)
            except Exception:
                pass
            try:
                if resp_fd >= 0:
                    os.close(resp_fd)
            except Exception:
                pass
            safe_unlink(self.req_path)
            safe_unlink(self.resp_path)


def run_server_mode(manager_base: str) -> int:
    control_path = default_control_path(manager_base)
    ensure_fifo(control_path)

    control_fd = os.open(control_path, os.O_RDWR | os.O_NONBLOCK)
    control_reader = PacketReader(control_fd)

    sessions: list[tuple[SessionBridge, threading.Thread]] = []
    stop_all = threading.Event()

    def shutdown_all() -> None:
        stop_all.set()
        for bridge, _thr in sessions:
            bridge.stop_event.set()

    try:
        while not stop_all.is_set():
            pkt = None
            try:
                pkt = control_reader.read_packet()
            except EOFError:
                time.sleep(0.1)
                continue

            if pkt is None:
                time.sleep(0.1)
                continue

            pkt_type, request_id, task_id, payload = pkt

            if pkt_type == TYPE_STOP_SERVER:
                shutdown_all()
                break

            if pkt_type == TYPE_REGISTER_SESSION:
                try:
                    req_path, resp_path = unpack_register_session(payload)
                except Exception:
                    continue

                bridge = SessionBridge(
                    req_path=req_path,
                    resp_path=resp_path,
                    cpp_server=DEFAULT_CPP_SERVER,
                    stop_event=threading.Event(),
                )
                thr = bridge.start()
                sessions.append((bridge, thr))
                continue

            # 其他控制包先忽略
            continue

    finally:
        shutdown_all()
        for bridge, thr in sessions:
            try:
                thr.join(timeout=1.0)
            except Exception:
                pass
        try:
            os.close(control_fd)
        except Exception:
            pass

    return 0


def open_fifo_write(path: str, timeout: float = 3.0) -> int:
    deadline = time.time() + timeout
    while True:
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENXIO:
                if time.time() >= deadline:
                    raise TimeoutError(f"timed out waiting for writer on {path}")
                time.sleep(0.05)
                continue
            raise


def open_fifo_read(path: str, timeout: float = 3.0) -> int:
    deadline = time.time() + timeout
    while True:
        try:
            return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENOENT:
                if time.time() >= deadline:
                    raise TimeoutError(f"timed out waiting for fifo {path}")
                time.sleep(0.05)
                continue
            raise


def run_client_mode(manager_base: str, cmd_argv: list[str]) -> int:
    control_path = default_control_path(manager_base)
    if not os.path.exists(control_path):
        print(f"manager control fifo not found: {control_path}", file=sys.stderr)
        return 1

    sid, req_path, resp_path = session_paths(manager_base)
    ensure_fifo(req_path)
    ensure_fifo(resp_path)

    req_fd = -1
    resp_fd = -1
    control_fd = -1
    stdin_thr: Optional[threading.Thread] = None
    stop_event = threading.Event()

    try:
        # open response fifo first so the server bridge can open it for writing
        resp_fd = open_fifo_read(resp_path, timeout=3.0)

        # tell server to create a bridge for this session
        control_fd = open_fifo_write(control_path, timeout=3.0)
        register_payload = pack_register_session(req_path, resp_path)
        write_all(control_fd, pack_packet(TYPE_REGISTER_SESSION, make_request_id(), 0, register_payload))
        os.close(control_fd)
        control_fd = -1

        # open request fifo for writing; server bridge already opened the read end
        req_fd = open_fifo_write(req_path, timeout=3.0)

        req_packet_writer_lock = threading.Lock()
        resp_reader = PacketReader(resp_fd)

        # send create_task
        request_id = make_request_id()
        cmdline = shlex.join(cmd_argv).encode("utf-8")
        with req_packet_writer_lock:
            write_all(req_fd, pack_packet(TYPE_CREATE_TASK, request_id, 0, cmdline))

        task_id: Optional[int] = None

        while True:
            pkt = resp_reader.read_packet()
            if pkt is None:
                print("session closed while creating task", file=sys.stderr)
                return 1

            pkt_type, pkt_rid, pkt_tid, payload = pkt

            if pkt_type == TYPE_REPLY and pkt_rid == request_id:
                if len(payload) < 8:
                    return 1
                task_id = unpack_u64(payload, 0)
                if task_id == 0:
                    if len(payload) >= 16:
                        err = unpack_u64(payload, 8)
                        print(f"create_task failed: errno={err}", file=sys.stderr)
                    else:
                        print("create_task failed", file=sys.stderr)
                    return 1
                break

        assert task_id is not None

        def forward_stdin() -> None:
            try:
                while not stop_event.is_set():
                    try:
                        data = os.read(sys.stdin.fileno(), 65536)
                    except InterruptedError:
                        continue
                    if not data:
                        with req_packet_writer_lock:
                            write_all(req_fd, pack_packet(TYPE_INPUT_DATA, 0, task_id, b""))
                        break
                    with req_packet_writer_lock:
                        write_all(req_fd, pack_packet(TYPE_INPUT_DATA, 0, task_id, data))
            except Exception:
                stop_event.set()

        stdin_thr = threading.Thread(target=forward_stdin, daemon=True)
        stdin_thr.start()

        exit_code = 1

        while True:
            pkt = resp_reader.read_packet()
            if pkt is None:
                print("session closed unexpectedly", file=sys.stderr)
                return 1

            pkt_type, pkt_rid, pkt_tid, payload = pkt

            if pkt_type == TYPE_RECEIVE_STDOUT and pkt_tid == task_id:
                if payload:
                    write_all(sys.stdout.fileno(), payload)
                continue

            if pkt_type == TYPE_RECEIVE_STDERR and pkt_tid == task_id:
                if payload:
                    write_all(sys.stderr.fileno(), payload)
                continue

            if pkt_type == TYPE_TASK_END and pkt_tid == task_id:
                if len(payload) >= 3:
                    code = payload[0]
                    is_sig = payload[1]
                    sig = payload[2]
                    exit_code = 128 + sig if is_sig else code
                else:
                    exit_code = 1
                stop_event.set()
                break

        try:
            stdin_thr.join(timeout=0.5)
        except Exception:
            pass

        return exit_code

    finally:
        stop_event.set()
        try:
            if stdin_thr is not None:
                stdin_thr.join(timeout=0.2)
        except Exception:
            pass
        try:
            if req_fd >= 0:
                os.close(req_fd)
        except Exception:
            pass
        try:
            if resp_fd >= 0:
                os.close(resp_fd)
        except Exception:
            pass
        try:
            if control_fd >= 0:
                os.close(control_fd)
        except Exception:
            pass
        safe_unlink(req_path)
        safe_unlink(resp_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["client", "server"], default="client")
    parser.add_argument("--manager", default=default_manager_base())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if args.type == "server":
        if cmd:
            print("server mode does not accept a command", file=sys.stderr)
            return 1
        return run_server_mode(args.manager)

    if not cmd:
        print("client mode needs a command after --", file=sys.stderr)
        return 1

    return run_client_mode(args.manager, cmd)


if __name__ == "__main__":
    raise SystemExit(main())
    
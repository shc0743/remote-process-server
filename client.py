#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import platform
import queue
import secrets
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, List

# ============================================================
# Server bridge protocol (client.py <-> rmpsm_server)
# ============================================================

MAGIC = 0x961F132BDDDC19B9
VERSION = 3
PROTOCOL_VERSION_TEXT = "3.0.0"
MAX_LEN = 1 << 30

TYPE_ACK_ONLY = 18446744073709551615
MAX_APP_PAYLOAD = 32768
MAX_RELIABLE_QUEUE = 256
RETRANSMIT_TIMEOUT = datetime.timedelta(milliseconds=500) if False else None  # placeholder

# MAGIC, VERSION, TYPE, FLAGS, REQUEST_ID, TASK_ID, SEQ, ACK, LEN
HEADER = struct.Struct("<QQQQQQQQQ")

# ============================================================
# Manager <-> client binary protocol
# ============================================================

CTRL_MAGIC = b"RMPC"
CTRL_VERSION = 1
CTRL_HEADER = struct.Struct("<4sBBHI")  # magic, version, type, flags, payload_len

C2M_AUTH = 1
M2C_AUTH_OK = 2
M2C_AUTH_FAIL = 3
C2M_CREATE_SESSION = 4
M2C_CREATE_SESSION_RESP = 5
C2M_CREATE_TASK = 6
M2C_CREATE_TASK_RESP = 7
C2M_STDIN = 8
C2M_STDIN_EOF = 9
C2M_KILL = 10
C2M_CLOSE_SESSION = 11
C2M_STOP_MANAGER = 12
M2C_GENERIC_RESP = 13
M2C_STDOUT = 14
M2C_STDERR = 15
M2C_TASK_END = 16
M2C_SERVER_DEAD = 17


def default_connection_file() -> str:
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return os.path.join(base, "rmpsm_manager.conn")


def u64_to_bytes(v: int) -> bytes:
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


def bytes_to_u64(b: bytes) -> int:
    return struct.unpack("<Q", b)[0]


def u32_to_bytes(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def bytes_to_u32(b: bytes) -> int:
    return struct.unpack("<I", b)[0]


def pack_blob(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def unpack_blob(payload: bytes, offset: int) -> Tuple[bytes, int]:
    if offset + 4 > len(payload):
        raise ValueError("truncated blob length")
    (n,) = struct.unpack_from("<I", payload, offset)
    offset += 4
    if offset + n > len(payload):
        raise ValueError("truncated blob data")
    return payload[offset:offset + n], offset + n


def pack_text(text: str) -> bytes:
    return pack_blob(text.encode("utf-8"))


def unpack_text(payload: bytes, offset: int) -> Tuple[str, int]:
    data, offset = unpack_blob(payload, offset)
    return data.decode("utf-8", errors="strict"), offset


def pack_frame(msg_type: int, payload: bytes = b"", *, flags: int = 0) -> bytes:
    if len(payload) > MAX_LEN:
        raise ValueError("payload too large")
    return CTRL_HEADER.pack(
        CTRL_MAGIC,
        CTRL_VERSION,
        msg_type & 0xFF,
        flags & 0xFFFF,
        len(payload),
    ) + payload


def pack_request_id(request_id: int) -> bytes:
    return u64_to_bytes(request_id)


def pack_create_session_request(request_id: int) -> bytes:
    return pack_request_id(request_id)


def pack_create_task_request(request_id: int, cmdline: bytes) -> bytes:
    return pack_request_id(request_id) + cmdline


def pack_task_io_request(request_id: int, task_id: int, data: bytes) -> bytes:
    return pack_request_id(request_id) + u64_to_bytes(task_id) + data


def pack_task_id_request(request_id: int, task_id: int) -> bytes:
    return pack_request_id(request_id) + u64_to_bytes(task_id)


def pack_stop_manager_request(request_id: int) -> bytes:
    return pack_request_id(request_id)


def pack_create_session_resp(
    request_id: int,
    ok: bool,
    session_id: str = "",
    err: int = 0,
    message: str = "",
) -> bytes:
    return (
        u64_to_bytes(request_id)
        + struct.pack("<B", 1 if ok else 0)
        + u32_to_bytes(err)
        + pack_text(session_id)
        + pack_text(message)
    )


def pack_create_task_resp(
    request_id: int,
    ok: bool,
    task_id: int = 0,
    err: int = 0,
    message: str = "",
) -> bytes:
    return (
        u64_to_bytes(request_id)
        + struct.pack("<B", 1 if ok else 0)
        + u64_to_bytes(task_id)
        + u32_to_bytes(err)
        + pack_text(message)
    )


def pack_generic_resp(
    request_id: int,
    ok: bool,
    err: int = 0,
    message: str = "",
) -> bytes:
    return (
        u64_to_bytes(request_id)
        + struct.pack("<B", 1 if ok else 0)
        + u32_to_bytes(err)
        + pack_text(message)
    )


def pack_stdout_stderr(task_id: int, data: bytes) -> bytes:
    return u64_to_bytes(task_id) + data


def pack_task_end(task_id: int, exit_code: int, signaled: bool, signal_no: int) -> bytes:
    return (
        u64_to_bytes(task_id)
        + u32_to_bytes(exit_code)
        + struct.pack("<B", 1 if signaled else 0)
        + struct.pack("<B", signal_no & 0xFF)
    )


def unpack_u64_at(payload: bytes, offset: int) -> int:
    if offset + 8 > len(payload):
        raise ValueError("truncated u64")
    return bytes_to_u64(payload[offset:offset + 8])


def unpack_u32_at(payload: bytes, offset: int) -> int:
    if offset + 4 > len(payload):
        raise ValueError("truncated u32")
    return bytes_to_u32(payload[offset:offset + 4])


def decode_create_session_resp(payload: bytes) -> Tuple[int, bool, int, str, str]:
    off = 0
    request_id = unpack_u64_at(payload, off)
    off += 8
    if off + 1 > len(payload):
        raise ValueError("truncated create_session resp")
    ok = payload[off] != 0
    off += 1
    err = unpack_u32_at(payload, off)
    off += 4
    session_id, off = unpack_text(payload, off)
    message, off = unpack_text(payload, off)
    return request_id, ok, err, session_id, message


def decode_create_task_resp(payload: bytes) -> Tuple[int, bool, int, int, str]:
    off = 0
    request_id = unpack_u64_at(payload, off)
    off += 8
    if off + 1 > len(payload):
        raise ValueError("truncated create_task resp")
    ok = payload[off] != 0
    off += 1
    task_id = unpack_u64_at(payload, off)
    off += 8
    err = unpack_u32_at(payload, off)
    off += 4
    message, off = unpack_text(payload, off)
    return request_id, ok, task_id, err, message


def decode_generic_resp(payload: bytes) -> Tuple[int, bool, int, str]:
    off = 0
    request_id = unpack_u64_at(payload, off)
    off += 8
    if off + 1 > len(payload):
        raise ValueError("truncated generic resp")
    ok = payload[off] != 0
    off += 1
    err = unpack_u32_at(payload, off)
    off += 4
    message, off = unpack_text(payload, off)
    return request_id, ok, err, message


def decode_task_end_payload(payload: bytes) -> Tuple[int, bool, int]:
    if len(payload) >= 14:
        task_id = bytes_to_u64(payload[0:8])
        exit_code = bytes_to_u32(payload[8:12])
        signaled = payload[12] != 0
        signal_no = payload[13]
        return exit_code if task_id >= 0 else 0, signaled, signal_no  # task_id unused here

    # backward compatibility with old 3-byte format
    if len(payload) >= 3:
        exit_code = int(payload[0])
        signaled = payload[1] != 0
        signal_no = int(payload[2])
        return exit_code, signaled, signal_no

    return 0, False, 0


class BinaryFrameReader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def read_frame(self) -> Optional[Tuple[int, int, bytes]]:
        while True:
            while len(self.buf) < CTRL_HEADER.size:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buf.extend(chunk)

            magic, version, msg_type, flags, payload_len = CTRL_HEADER.unpack_from(self.buf, 0)
            if magic != CTRL_MAGIC or version != CTRL_VERSION:
                raise RuntimeError("bad control frame header")
            if payload_len > MAX_LEN:
                raise RuntimeError("control payload too large")

            frame_len = CTRL_HEADER.size + int(payload_len)
            while len(self.buf) < frame_len:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buf.extend(chunk)

            payload = bytes(self.buf[CTRL_HEADER.size:frame_len])
            del self.buf[:frame_len]
            return int(msg_type), int(flags), payload


def close_fd(fd: int) -> None:
    try:
        if fd >= 0:
            os.close(fd)
    except OSError:
        pass


def close_task_all_fds(t: "Task") -> None:
    close_fd(t.stdin_fd)
    close_fd(t.stdout_fd)
    close_fd(t.stderr_fd)
    t.stdin_fd = -1
    t.stdout_fd = -1
    t.stderr_fd = -1
    if t.hProcess is not None:
        try:
            t.hProcess.close()
        except Exception:
            pass
        t.hProcess = None


def set_nonblock(fd: int) -> None:
    if os.name == "nt":
        return
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL, 0)
    if flags >= 0:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        
def write_all(fd: int, data: bytes) -> None:
    off = 0
    while off < len(data):
        try:
            n = os.write(fd, data[off:])
        except InterruptedError:
            continue
        except BlockingIOError:
            time.sleep(0.01)
            continue
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                time.sleep(0.01)
                continue
            raise
        if n <= 0:
            raise OSError(errno.EIO, "short write")
        off += n


def safe_unlink(path: str) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Windows 上文件可能被占用，这里直接忽略即可
        pass


def read_u64_le(p: bytes) -> int:
    return bytes_to_u64(p)


def write_u64_le(p: bytearray, off: int, v: int) -> None:
    p[off:off + 8] = u64_to_bytes(v)


def read_u32_le(p: bytes) -> int:
    return bytes_to_u32(p)


def write_u32_le(p: bytearray, off: int, v: int) -> None:
    p[off:off + 4] = u32_to_bytes(v)


def parse_command_line(cmd: str, out: List[str], err: int) -> Tuple[bool, int]:
    out.clear()

    class State:
        Normal = 0
        SingleQuote = 1
        DoubleQuote = 2

    state = State.Normal
    escape = False
    cur: List[str] = []

    def flush_word() -> None:
        if cur:
            out.append("".join(cur))
            cur.clear()

    for ch in cmd:
        if ch == "\0":
            return False, errno.EINVAL

        if escape:
            cur.append(ch)
            escape = False
            continue

        if state == State.SingleQuote:
            if ch == "'":
                state = State.Normal
            else:
                cur.append(ch)
            continue

        if state == State.DoubleQuote:
            if ch == '"':
                state = State.Normal
            elif ch == "\\":
                escape = True
            else:
                cur.append(ch)
            continue

        if ch.isspace():
            flush_word()
            continue

        if ch == "'":
            state = State.SingleQuote
        elif ch == '"':
            state = State.DoubleQuote
        elif ch == "\\":
            escape = True
        else:
            cur.append(ch)

    if escape or state != State.Normal:
        return False, errno.EINVAL

    flush_word()

    if not out:
        return False, errno.EINVAL

    return True, 0


@dataclass
class Task:
    taskId: int = 0
    pid: int = -1
    hProcess: Optional[Any] = None  # Windows only

    stdin_fd: int = -1
    stdout_fd: int = -1
    stderr_fd: int = -1

    child_exited: bool = False
    task_end_sent: bool = False

    stdin_queue: List[bytes] = field(default_factory=list)
    stdin_offset: int = 0
    stdin_close_requested: bool = False

    # POSIX wait status / Windows exit code
    wait_status: int = 0
    exit_code: int = 0


@dataclass
class TxItem:
    bytes: bytes
    offset: int = 0
    reliable: bool = False
    seq: int = 0


@dataclass
class ReliablePacket:
    type: int = 0
    requestId: int = 0
    taskId: int = 0
    seq: int = 0
    payload: bytes = b""


@dataclass
class ReliableState:
    waiting: List[ReliablePacket] = field(default_factory=list)
    inflight_exists: bool = False
    inflight_on_wire: bool = False
    inflight: ReliablePacket = field(default_factory=ReliablePacket)


@dataclass
class TransportState:
    q: List[TxItem] = field(default_factory=list)


def compact_buffer(buf: bytearray, pos: int) -> int:
    if pos == 0:
        return 0
    if pos > len(buf):
        buf.clear()
        return 0
    if pos == len(buf):
        buf.clear()
        return 0
    if pos > 4096 and pos * 2 >= len(buf):
        del buf[:pos]
        return 0
    return pos


def flush_task_stdin(t: Task) -> bool:
    if t.stdin_fd < 0:
        return False

    while t.stdin_offset < len(t.stdin_queue):
        try:
            n = os.write(
                t.stdin_fd,
                t.stdin_queue[t.stdin_offset],
            )
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False

        if n > 0:
            if n < len(t.stdin_queue[t.stdin_offset]):
                t.stdin_queue[t.stdin_offset] = t.stdin_queue[t.stdin_offset][n:]
            else:
                t.stdin_offset += 1
            continue

        return False

    t.stdin_queue.clear()
    t.stdin_offset = 0

    if t.stdin_close_requested:
        close_fd(t.stdin_fd)
        t.stdin_fd = -1
        t.stdin_close_requested = False

    return True


def next_seq_wrap(v: int) -> int:
    if v == 0 or v == 0xFFFFFFFFFFFFFFFF:
        return 1
    return (v + 1) & 0xFFFFFFFFFFFFFFFF


def build_packet_bytes(
    type: int,
    requestId: int,
    taskId: int,
    seq: int,
    ack: int,
    payload: Optional[bytes],
    len_: int,
) -> bytes:
    out = bytearray(72 + len_)
    write_u64_le(out, 0, MAGIC)
    write_u64_le(out, 8, VERSION)
    write_u64_le(out, 16, type)
    write_u64_le(out, 24, 0 if type == TYPE_ACK_ONLY else 1)
    write_u64_le(out, 32, requestId)
    write_u64_le(out, 40, taskId)
    write_u64_le(out, 48, seq)
    write_u64_le(out, 56, ack)
    write_u64_le(out, 64, len_)
    if len_ > 0 and payload is not None:
        out[72:72 + len_] = payload
    return bytes(out)


def enqueue_tx_back(tx: TransportState, bytes_: bytes, reliable: bool, seq: int) -> None:
    tx.q.append(TxItem(bytes_, 0, reliable, seq))


def enqueue_tx_front(tx: TransportState, bytes_: bytes, reliable: bool, seq: int) -> None:
    tx.q.insert(0, TxItem(bytes_, 0, reliable, seq))


def flush_transport(tx: TransportState, rel: ReliableState) -> bool:
    while tx.q:
        item = tx.q[0]
        try:
            n = os.write(1, item.bytes[item.offset:])
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False

        if n > 0:
            item.offset += n
            if item.offset >= len(item.bytes):
                if item.reliable and rel.inflight_exists and not rel.inflight_on_wire and item.seq == rel.inflight.seq:
                    rel.inflight_on_wire = True
                tx.q.pop(0)
            continue

        return False

    return True


def process_peer_ack(rel: ReliableState, ack: int) -> bool:
    if not rel.inflight_exists:
        return False
    if ack == rel.inflight.seq:
        rel.inflight_exists = False
        rel.inflight_on_wire = False
        rel.inflight = ReliablePacket()
        return True
    return False


def start_next_reliable_if_idle(rel: ReliableState, tx: TransportState, peer_last_delivered_seq: int) -> bool:
    if rel.inflight_exists or not rel.waiting:
        return False

    rel.inflight = rel.waiting.pop(0)
    bytes_ = build_packet_bytes(
        rel.inflight.type,
        rel.inflight.requestId,
        rel.inflight.taskId,
        rel.inflight.seq,
        peer_last_delivered_seq,
        rel.inflight.payload if rel.inflight.payload else None,
        len(rel.inflight.payload),
    )
    enqueue_tx_back(tx, bytes_, True, rel.inflight.seq)
    rel.inflight_exists = True
    rel.inflight_on_wire = False
    return True


def maybe_retransmit(rel: ReliableState, tx: TransportState, peer_last_delivered_seq: int) -> bool:
    if not rel.inflight_exists or not rel.inflight_on_wire:
        return False

    # 500ms
    if getattr(rel, "_last_wire_ts", None) is None:
        return False
    if time.monotonic() - rel._last_wire_ts < 0.5:
        return False

    bytes_ = build_packet_bytes(
        rel.inflight.type,
        rel.inflight.requestId,
        rel.inflight.taskId,
        rel.inflight.seq,
        peer_last_delivered_seq,
        rel.inflight.payload if rel.inflight.payload else None,
        len(rel.inflight.payload),
    )
    enqueue_tx_front(tx, bytes_, True, rel.inflight.seq)
    rel.inflight_on_wire = False
    return True


def parse_payload_task_id(payload: bytes) -> Tuple[bool, int]:
    if len(payload) < 8:
        return False, 0
    return True, bytes_to_u64(payload[:8])


def spawn_task(cmdline: str, taskId: int, outTask: Task, outErr: int) -> Tuple[bool, int]:
    args: List[str] = []
    ok, err = parse_command_line(cmdline, args, 0)
    if not ok:
        return False, err

    argv: List[str] = []
    argv.extend(args)
    if not argv:
        return False, errno.EINVAL

    inpipe = [None, None]
    outpipe = [None, None]
    errpipe = [None, None]

    try:
        inpipe[0], inpipe[1] = os.pipe()
        outpipe[0], outpipe[1] = os.pipe()
        errpipe[0], errpipe[1] = os.pipe()
    except OSError as e:
        for fd in inpipe + outpipe + errpipe:
            if fd is not None:
                close_fd(fd)
        return False, e.errno

    # Ensure the child only sees its own ends
    try:
        if os.name != "nt":
            pid = os.fork()
            if pid == 0:
                try:
                    os.dup2(inpipe[0], 0)
                    os.dup2(outpipe[1], 1)
                    os.dup2(errpipe[1], 2)
                    close_fd(inpipe[0])
                    close_fd(inpipe[1])
                    close_fd(outpipe[0])
                    close_fd(outpipe[1])
                    close_fd(errpipe[0])
                    close_fd(errpipe[1])
                    os.execvp(argv[0], argv)
                except Exception:
                    os._exit(127)

            if pid < 0:
                raise OSError(errno.ECHILD, "fork failed")

            pid_child = pid
            proc_handle = None
        else:
            import msvcrt
            import ctypes
            from ctypes import wintypes

            # Windows CreateProcess path
            CREATE_NO_WINDOW = 0x08000000
            STARTF_USESTDHANDLES = 0x00000100

            class STARTUPINFOW(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD),
                    ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD),
                    ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE),
                ]

            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", wintypes.HANDLE),
                    ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            def _fd_to_handle(fd: int) -> int:
                return msvcrt.get_osfhandle(fd)

            si = STARTUPINFOW()
            pi = PROCESS_INFORMATION()
            si.cb = ctypes.sizeof(si)
            si.dwFlags |= STARTF_USESTDHANDLES
            si.hStdInput = wintypes.HANDLE(_fd_to_handle(inpipe[0]))
            si.hStdOutput = wintypes.HANDLE(_fd_to_handle(outpipe[1]))
            si.hStdError = wintypes.HANDLE(_fd_to_handle(errpipe[1]))

            cmd = subprocess.list2cmdline(argv)
            cmd_buf = ctypes.create_unicode_buffer(cmd)

            okp = kernel32.CreateProcessW(
                None,
                cmd_buf,
                None,
                None,
                True,
                CREATE_NO_WINDOW,
                None,
                None,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not okp:
                raise OSError(ctypes.get_last_error(), "CreateProcessW failed")

            pid_child = int(pi.dwProcessId)
            proc_handle = pi.hProcess
            try:
                os.close(inpipe[0])
                os.close(outpipe[1])
                os.close(errpipe[1])
            except OSError:
                pass
            try:
                kernel32.CloseHandle(pi.hThread)
            except Exception:
                pass

    except Exception as e:
        for fd in inpipe + outpipe + errpipe:
            if fd is not None:
                close_fd(fd)
        return False, getattr(e, "errno", errno.EIO)

    # parent
    if os.name != "nt":
        close_fd(inpipe[0])
        close_fd(outpipe[1])
        close_fd(errpipe[1])
        set_nonblock(inpipe[1])
        set_nonblock(outpipe[0])
        set_nonblock(errpipe[0])

    outTask.taskId = taskId
    outTask.pid = pid_child
    outTask.hProcess = proc_handle
    outTask.stdin_fd = inpipe[1]
    outTask.stdout_fd = outpipe[0]
    outTask.stderr_fd = errpipe[0]
    outTask.child_exited = False
    outTask.task_end_sent = False
    outTask.wait_status = 0
    outTask.exit_code = 0
    outTask.stdin_queue.clear()
    outTask.stdin_offset = 0
    outTask.stdin_close_requested = False
    return True, 0


def mark_task_exited(t: Task, status: int) -> None:
    t.child_exited = True
    t.wait_status = status
    if os.name == "nt":
        t.exit_code = status


def encode_exit_info(t: Task) -> Tuple[int, bool, int]:
    if os.name == "nt":
        return int(t.exit_code) & 0xFFFFFFFF, False, 0

    try:
        if os.WIFEXITED(t.wait_status):
            return int(os.WEXITSTATUS(t.wait_status)) & 0xFFFFFFFF, False, 0
        if os.WIFSIGNALED(t.wait_status):
            return 0, True, int(os.WTERMSIG(t.wait_status)) & 0xFF
    except AttributeError:
        pass
    return 0, False, 0


class SessionProxy:
    def __init__(self, manager: "Manager", session_id: str, client_socket: socket.socket, send_lock: threading.Lock):
        self.manager = manager
        self.session_id = session_id
        self.client_socket = client_socket
        self.send_lock = send_lock
        self.stop_event = threading.Event()

    def _send_frame(self, msg_type: int, payload: bytes = b"") -> None:
        if self.stop_event.is_set():
            return
        try:
            with self.send_lock:
                self.client_socket.sendall(pack_frame(msg_type, payload))
        except Exception:
            pass

    def send_create_session_resp(self, request_id: int, ok: bool, err: int = 0, message: str = "") -> None:
        self._send_frame(
            M2C_CREATE_SESSION_RESP,
            pack_create_session_resp(request_id, ok, self.session_id if ok else "", err, message),
        )

    def send_create_task_resp(self, request_id: int, ok: bool, task_id: int = 0, err: int = 0, message: str = "") -> None:
        self._send_frame(
            M2C_CREATE_TASK_RESP,
            pack_create_task_resp(request_id, ok, task_id, err, message),
        )

    def send_generic_resp(self, request_id: int, ok: bool, err: int = 0, message: str = "") -> None:
        self._send_frame(M2C_GENERIC_RESP, pack_generic_resp(request_id, ok, err, message))

    def send_stdout(self, task_id: int, data: bytes) -> None:
        self._send_frame(M2C_STDOUT, pack_stdout_stderr(task_id, data))

    def send_stderr(self, task_id: int, data: bytes) -> None:
        self._send_frame(M2C_STDERR, pack_stdout_stderr(task_id, data))

    def send_task_end(self, task_id: int, exit_code: int, signaled: bool, signal_no: int) -> None:
        self._send_frame(M2C_TASK_END, pack_task_end(task_id, exit_code, signaled, signal_no))

    def send_server_dead(self) -> None:
        self._send_frame(M2C_SERVER_DEAD, b"")

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.client_socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.client_socket.close()
        except Exception:
            pass


class ServerBridge:
    def __init__(self, manager: "Manager", server_path: str):
        self.manager = manager
        self.server_path = server_path

        args = shlex.split(server_path, posix=(os.name != "nt"))
        if not args:
            raise FileNotFoundError("empty server path")
        if not os.path.exists(args[0]):
            raise FileNotFoundError(f"Server binary not found: {args[0]}")

        self.proc = subprocess.Popen(
            args,
            executable=args[0] if os.name == "nt" else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            shell=False,
        )

        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("failed to start server process")

        self.stdin_fd = self.proc.stdin.fileno()
        self.stdout_fd = self.proc.stdout.fileno()

        self._tx_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ack_cv = threading.Condition(self._state_lock)

        self._req_lock = threading.Lock()
        self._next_request_id = 1

        self._next_seq = 1
        self._peer_expected_seq = 1
        self._peer_last_delivered_seq = 0
        self._last_acked_seq = 0

        self._pending: Dict[int, PendingCreateTask] = {}
        self._orphan_packets: Dict[int, List[Tuple[int, bytes]]] = {}
        self._task_to_session: Dict[int, str] = {}

        self._version_req_id: Optional[int] = None
        self._version_event = threading.Event()
        self._version_value: Optional[str] = None

        self.stop_event = threading.Event()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=False)
        self._reader_thread.start()

        try:
            self._check_version()
        except Exception:
            self.stop()
            raise

    def _next_req_id(self) -> int:
        with self._req_lock:
            rid = self._next_request_id
            self._next_request_id = (self._next_request_id + 1) & 0xFFFFFFFFFFFFFFFF
            if self._next_request_id == 0:
                self._next_request_id = 1
            return rid

    def _alloc_seq_locked(self) -> int:
        seq = self._next_seq
        self._next_seq = (self._next_seq + 1) & 0xFFFFFFFFFFFFFFFF
        if self._next_seq == 0:
            self._next_seq = 1
        return seq

    def _build_packet_locked(self, ptype: int, request_id: int, task_id: int, payload: bytes, seq: int) -> bytes:
        return build_packet_bytes(
            ptype,
            request_id,
            task_id,
            seq,
            self._peer_last_delivered_seq,
            payload if payload else None,
            len(payload),
        )

    def _write_packet(self, data: bytes) -> None:
        with self._tx_lock:
            write_all(self.stdin_fd, data)

    def _send_ack_only(self) -> None:
        if self.stop_event.is_set():
            return

        with self._state_lock:
            if self._peer_last_delivered_seq == 0:
                return
            pkt = build_packet_bytes(TYPE_ACK_ONLY, 0, 0, 0, self._peer_last_delivered_seq, None, 0)

        try:
            self._write_packet(pkt)
        except Exception:
            self.stop_event.set()

    def _send_packet(
        self,
        ptype: int,
        request_id: int,
        task_id: int,
        payload: bytes,
        *,
        require_ack: bool = True,
        timeout: float = 0.5,
        max_retries: int = 20,
    ) -> int:
        with self._state_lock:
            seq = self._alloc_seq_locked()
            pkt = self._build_packet_locked(ptype, request_id, task_id, payload, seq)

        attempts = 0
        while not self.stop_event.is_set():
            self._write_packet(pkt)

            if not require_ack:
                return seq

            deadline = time.monotonic() + timeout
            with self._ack_cv:
                while not self.stop_event.is_set() and self._last_acked_seq < seq:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._ack_cv.wait(timeout=remaining)

                if self._last_acked_seq >= seq:
                    return seq

                current_ack = self._peer_last_delivered_seq

            attempts += 1
            if attempts > max_retries:
                raise TimeoutError("packet ack timeout")

            with self._state_lock:
                pkt = build_packet_bytes(ptype, request_id, task_id, seq, current_ack, payload if payload else None, len(payload))

        raise RuntimeError("stopped")

    def _check_version(self) -> None:
        self._version_req_id = self._next_req_id()
        self._version_event.clear()
        self._version_value = None

        self._send_packet(255, self._version_req_id, 0, b"", require_ack=True)

        if not self._version_event.wait(5.0):
            raise TimeoutError("server version check timed out")

        if self._version_value != PROTOCOL_VERSION_TEXT:
            raise RuntimeError(
                f"protocol version mismatch: server={self._version_value!r}, client={PROTOCOL_VERSION_TEXT!r}"
            )

    def create_task(self, session_id: str, cmdline: str, timeout: float = 60.0) -> Tuple[bool, int, int]:
        req_id = self._next_req_id()
        waiter = PendingCreateTask(session_id=session_id)
        self._pending[req_id] = waiter

        try:
            self._send_packet(2, req_id, 0, cmdline.encode("utf-8"), require_ack=True)
        except Exception:
            self._pending.pop(req_id, None)
            return False, 0, errno.EIO

        if not waiter.event.wait(timeout):
            self._pending.pop(req_id, None)
            return False, 0, errno.ETIMEDOUT

        self._pending.pop(req_id, None)
        if waiter.ok:
            return True, waiter.task_id, 0
        return False, 0, waiter.err or errno.EIO

    def send_input(self, task_id: int, data: bytes) -> None:
        req_id = self._next_req_id()
        self._send_packet(5, req_id, task_id, data, require_ack=True)

    def send_eof(self, task_id: int) -> None:
        req_id = self._next_req_id()
        self._send_packet(5, req_id, task_id, b"", require_ack=True)

    def kill_task(self, task_id: int) -> None:
        req_id = self._next_req_id()
        self._send_packet(3, req_id, task_id, b"", require_ack=True)

    def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

        try:
            self._reader_thread.join(timeout=2.0)
        except Exception:
            pass

    def _route_task_packet(self, task_id: int, kind: str, payload: bytes) -> None:
        session_id = self._task_to_session.get(task_id)
        if session_id is None:
            self._orphan_packets.setdefault(task_id, []).append(
                (0 if kind == "stdout" else 1 if kind == "stderr" else 2, payload)
            )
            return

        session = self.manager.sessions.get(session_id)
        if session is None:
            self._orphan_packets.setdefault(task_id, []).append(
                (0 if kind == "stdout" else 1 if kind == "stderr" else 2, payload)
            )
            return

        if kind == "stdout":
            session.send_stdout(task_id, payload)
        elif kind == "stderr":
            session.send_stderr(task_id, payload)
        elif kind == "task_end":
            if len(payload) >= 14:
                exit_code = bytes_to_u32(payload[8:12])
                signaled = payload[12] != 0
                signal_no = int(payload[13])
            else:
                exit_code, signaled, signal_no = decode_task_end_payload(payload)
            session.send_task_end(task_id, exit_code, signaled, signal_no)

    def register_task(self, session_id: str, task_id: int) -> None:
        self._task_to_session[task_id] = session_id

        if task_id in self._orphan_packets:
            session = self.manager.sessions.get(session_id)
            if session is not None:
                for kind_id, payload in self._orphan_packets.pop(task_id):
                    if kind_id == 0:
                        session.send_stdout(task_id, payload)
                    elif kind_id == 1:
                        session.send_stderr(task_id, payload)
                    else:
                        if len(payload) >= 14:
                            exit_code = bytes_to_u32(payload[8:12])
                            signaled = payload[12] != 0
                            signal_no = int(payload[13])
                        else:
                            exit_code, signaled, signal_no = decode_task_end_payload(payload)
                        session.send_task_end(task_id, exit_code, signaled, signal_no)

    def _reader_loop(self) -> None:
        reader = BinaryFrameReader(self.stdout_fd)  # type: ignore[arg-type]
        try:
            while not self.stop_event.is_set():
                try:
                    pkt = reader.read_packet() if False else None
                except Exception as e:
                    try:
                        print(f"[manager] server reader error: {e}", file=sys.stderr)
                        sys.stderr.flush()
                    except Exception:
                        pass
                    break

                # Fallback to direct frame reader on fd-like stream
                try:
                    if not hasattr(self, "_fd_reader"):
                        self._fd_reader = _FDReader(self.stdout_fd)  # type: ignore[arg-type]
                    pkt = self._fd_reader.read_packet()
                except Exception as e:
                    try:
                        print(f"[manager] server reader error: {e}", file=sys.stderr)
                        sys.stderr.flush()
                    except Exception:
                        pass
                    break

                if pkt is None:
                    break

                ptype, request_id, task_id, seq, ack, payload = pkt

                with self._ack_cv:
                    if ack > self._last_acked_seq:
                        self._last_acked_seq = ack
                        self._ack_cv.notify_all()

                    if seq == 0:
                        accepted = False
                    elif seq == self._peer_expected_seq:
                        self._peer_expected_seq += 1
                        self._peer_last_delivered_seq = seq
                        accepted = True
                    elif seq < self._peer_expected_seq:
                        accepted = False
                    else:
                        break

                if accepted:
                    self._send_ack_only()

                    if ptype == 0:
                        if self._version_req_id is not None and request_id == self._version_req_id:
                            try:
                                self._version_value = payload.decode("utf-8", errors="strict")
                            except Exception:
                                self._version_value = None
                            self._version_event.set()

                        pending = self._pending.get(request_id)
                        if pending is not None:
                            if len(payload) == 8 and task_id != 0:
                                pending.ok = True
                                pending.task_id = task_id
                                pending.event.set()
                                self.register_task(pending.session_id, task_id)
                            elif len(payload) >= 16:
                                pending.ok = False
                                pending.err = int(bytes_to_u64(payload[8:16]))
                                pending.event.set()
                            elif len(payload) == 0 and task_id != 0:
                                pending.ok = True
                                pending.task_id = task_id
                                pending.event.set()
                                self.register_task(pending.session_id, task_id)
                            else:
                                pending.ok = False
                                pending.err = errno.EPROTO
                                pending.event.set()

                    elif ptype == 6:
                        self._route_task_packet(task_id, "stdout", payload)
                    elif ptype == 7:
                        self._route_task_packet(task_id, "stderr", payload)
                    elif ptype == 4:
                        self._route_task_packet(task_id, "task_end", payload)
        finally:
            if not self.stop_event.is_set():
                self.manager.on_server_dead()

    def send_stop(self) -> None:
        try:
            self._send_packet(1, 0, 0, b"\x01", require_ack=False)
        except Exception:
            pass


class _FDReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buf = bytearray()
        self.magic_bytes = struct.pack("<Q", MAGIC)

    def _fill(self, n: int) -> bool:
        while len(self.buf) < n:
            try:
                chunk = os.read(self.fd, 65536)
            except InterruptedError:
                continue
            except OSError:
                return False
            if not chunk:
                return False
            self.buf.extend(chunk)
        return True

    def _resync(self) -> None:
        idx = self.buf.find(self.magic_bytes, 1)
        if idx >= 0:
            if idx > 0:
                del self.buf[:idx]
            return

        keep = len(self.magic_bytes) - 1
        if len(self.buf) > keep:
            del self.buf[:-keep]

    def read_packet(self) -> Optional[Tuple[int, int, int, int, int, bytes]]:
        while True:
            if not self._fill(HEADER.size):
                return None

            if self.buf[:8] != self.magic_bytes or bytes_to_u64(self.buf[8:16]) != VERSION:
                self._resync()
                continue

            header = bytes(self.buf[:HEADER.size])
            del self.buf[:HEADER.size]

            magic, version, ptype, flags, request_id, task_id, seq, ack, length = HEADER.unpack(header)
            if magic != MAGIC or version != VERSION:
                self._resync()
                continue

            if length > MAX_LEN:
                self._resync()
                continue

            if not self._fill(int(length)):
                return None

            payload = bytes(self.buf[:length])
            del self.buf[:length]
            return int(ptype), int(request_id), int(task_id), int(seq), int(ack), payload


@dataclass
class PendingCreateTask:
    session_id: str
    event: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    task_id: int = 0
    err: int = 0


class Manager:
    def __init__(self, connection_file: str, server_path: str):
        self.connection_file = connection_file
        self.server_path = server_path

        self._base_dir = os.path.dirname(self.connection_file) or "."
        os.makedirs(self._base_dir, exist_ok=True)

        self.stop_event = threading.Event()
        self.sessions: Dict[str, SessionProxy] = {}
        self._session_lock = threading.Lock()
        self._next_session_id = 1

        self.bridge = ServerBridge(self, server_path)

    def _new_session_id(self) -> str:
        with self._session_lock:
            sid = f"{self._next_session_id:x}-{secrets.token_hex(8)}"
            self._next_session_id += 1
            return sid

    def close_session(self, session_id: str) -> None:
        with self._session_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass

    def on_server_dead(self) -> None:
        with self._session_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        for session in sessions:
            try:
                session.send_server_dead()
            except Exception:
                pass
            try:
                session.close()
            except Exception:
                pass

        self.stop_event.set()

    def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        print('Shutdowning server...', file=sys.stderr)

        try:
            self.bridge.send_stop()
        except Exception:
            pass

        try:
            self.bridge.stop()
        except Exception:
            pass

        with self._session_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

        safe_unlink(self.connection_file)

    def _write_reply_file(self, path: str, obj: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def run(self) -> None:
        def sig_handler(signum, frame):
            self.shutdown()

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(16)

        address = server_socket.getsockname()
        authkey = secrets.token_bytes(32)

        safe_unlink(self.connection_file)
        write_connection_info(self.connection_file, address, authkey)

        accept_thread = threading.Thread(target=self._accept_clients, args=(server_socket, authkey), daemon=False)
        accept_thread.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            self.stop_event.set()
            try:
                server_socket.close()
            except Exception:
                pass
            try:
                accept_thread.join(timeout=2.0)
            except Exception:
                pass
            self.shutdown()

    def _accept_clients(self, server_socket: socket.socket, expected_authkey: bytes) -> None:
        while not self.stop_event.is_set():
            try:
                server_socket.settimeout(0.5)
                try:
                    client_socket, _addr = server_socket.accept()
                except socket.timeout:
                    continue

                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, expected_authkey),
                    daemon=True,
                )
                thread.start()
            except Exception as e:
                if not self.stop_event.is_set():
                    try:
                        print(f"[manager] accept error: {e}", file=sys.stderr)
                        sys.stderr.flush()
                    except Exception:
                        pass
                break

    def _handle_client(self, client_socket: socket.socket, expected_authkey: bytes) -> None:
        send_lock = threading.Lock()
        reader = BinaryFrameReader(client_socket)
        session: Optional[SessionProxy] = None
        session_id: Optional[str] = None

        def send_raw(msg_type: int, payload: bytes = b"") -> None:
            with send_lock:
                client_socket.sendall(pack_frame(msg_type, payload))

        def send_auth_fail() -> None:
            try:
                send_raw(M2C_AUTH_FAIL, pack_generic_resp(0, False, errno.EACCES, "authentication failed"))
            except Exception:
                pass

        try:
            client_socket.settimeout(0.5)

            # AUTH handshake
            frame = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not self.stop_event.is_set():
                try:
                    frame = reader.read_frame()
                except socket.timeout:
                    continue
                except Exception:
                    frame = None
                    break
                if frame is not None:
                    break

            if frame is None:
                return

            msg_type, _flags, payload = frame
            if msg_type != C2M_AUTH or payload != expected_authkey:
                send_auth_fail()
                return

            send_raw(M2C_AUTH_OK, b"")

            while not self.stop_event.is_set():
                try:
                    frame = reader.read_frame()
                except socket.timeout:
                    continue
                except Exception:
                    break

                if frame is None:
                    break

                msg_type, _flags, payload = frame

                try:
                    if msg_type == C2M_CREATE_SESSION:
                        request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                        if session is not None:
                            session.send_create_session_resp(request_id, False, errno.EEXIST, "session already exists")
                            continue

                        session_id = self._new_session_id()
                        session = SessionProxy(self, session_id, client_socket, send_lock)
                        with self._session_lock:
                            self.sessions[session_id] = session
                        session.send_create_session_resp(request_id, True, 0, "")
                        continue

                    if msg_type == C2M_STOP_MANAGER:
                        print('Stop request received, shutdowning server...', file=sys.stderr)
                        request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                        if session is not None:
                            session.send_generic_resp(request_id, True, 0, "")
                        self.shutdown()
                        break

                    if session is None:
                        # until create_session, ignore all ops
                        continue

                    if msg_type == C2M_CREATE_TASK:
                        request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                        cmdline_bytes = payload[8:]
                        try:
                            cmdline = cmdline_bytes.decode("utf-8", errors="strict")
                        except Exception:
                            session.send_create_task_resp(request_id, False, 0, errno.EINVAL, "invalid utf-8 cmdline")
                            continue

                        ok, task_id, err = self.bridge.create_task(session.session_id, cmdline)
                        if ok:
                            session.send_create_task_resp(request_id, True, task_id, 0, "")
                        else:
                            session.send_create_task_resp(
                                request_id,
                                False,
                                0,
                                err,
                                os.strerror(err) if err else "create_task failed",
                            )
                        continue

                    if msg_type == C2M_STDIN:
                        if len(payload) < 16:
                            continue
                        request_id = bytes_to_u64(payload[:8])
                        task_id = bytes_to_u64(payload[8:16])
                        data = payload[16:]
                        self.bridge.send_input(task_id, data)
                        session.send_generic_resp(request_id, True, 0, "")
                        continue

                    if msg_type == C2M_STDIN_EOF:
                        if len(payload) < 16:
                            continue
                        request_id = bytes_to_u64(payload[:8])
                        task_id = bytes_to_u64(payload[8:16])
                        self.bridge.send_eof(task_id)
                        session.send_generic_resp(request_id, True, 0, "")
                        continue

                    if msg_type == C2M_KILL:
                        if len(payload) < 16:
                            continue
                        request_id = bytes_to_u64(payload[:8])
                        task_id = bytes_to_u64(payload[8:16])
                        self.bridge.kill_task(task_id)
                        session.send_generic_resp(request_id, True, 0, "")
                        continue

                    if msg_type == C2M_CLOSE_SESSION:
                        request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                        if session is not None:
                            session.send_generic_resp(request_id, True, 0, "")
                            sid = session.session_id
                            self.close_session(sid)
                            session = None
                            session_id = None
                        break
                    
                    #print('[DEBUG] invalid or unknown msg_type:', msg_type, file=sys.stderr)

                except Exception as e:
                    try:
                        if session is not None:
                            request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                            session.send_generic_resp(request_id, False, getattr(e, "errno", errno.EIO), str(e))
                    except Exception:
                        pass
        finally:
            if session_id is not None:
                try:
                    self.close_session(session_id)
                except Exception:
                    pass
            try:
                client_socket.close()
            except Exception:
                pass


class ClientRuntime:
    def __init__(self, connection_file: str, cmd_argv: List[str]):
        self.connection_file = connection_file
        self.cmd_argv = cmd_argv

        self.stop_event = threading.Event()
        self.task_id: Optional[int] = None
        self.exit_code: Optional[int] = None

        self._session_id: Optional[str] = None
        self._socket: Optional[socket.socket] = None
        self._reader: Optional[BinaryFrameReader] = None
        self._send_lock = threading.Lock()

        self._req_lock = threading.Lock()
        self._next_request_id = 1

        self._stdin_lock = threading.Lock()
        self._stdin_buffer: List[bytes] = []
        self._stdin_closed = False

        self._reader_thread: Optional[threading.Thread] = None
        self._stdin_thread: Optional[threading.Thread] = None
        self._stdout_writer_thread: Optional[threading.Thread] = None
        self._stderr_writer_thread: Optional[threading.Thread] = None

        self._stdout_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._stderr_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()

        self._create_task_request_id: Optional[int] = None

    def _next_req_id(self) -> int:
        with self._req_lock:
            rid = self._next_request_id
            self._next_request_id = (self._next_request_id + 1) & 0xFFFFFFFFFFFFFFFF
            if self._next_request_id == 0:
                self._next_request_id = 1
            return rid

    def _send_frame(self, msg_type: int, payload: bytes = b"", *, flags: int = 0) -> None:
        if self._socket is None:
            raise RuntimeError("manager socket not open")
        data = pack_frame(msg_type, payload, flags=flags)
        with self._send_lock:
            self._socket.sendall(data)

    def _flush_stdin_locked(self) -> None:
        if self.task_id is None or self._socket is None:
            return

        for chunk in self._stdin_buffer:
            req_id = self._next_req_id()
            self._send_frame(C2M_STDIN, pack_task_io_request(req_id, self.task_id, chunk))
        self._stdin_buffer.clear()

        if self._stdin_closed:
            req_id = self._next_req_id()
            self._send_frame(C2M_STDIN_EOF, pack_task_id_request(req_id, self.task_id))
            self._stdin_closed = False

    def _stdin_loop(self) -> None:
        fd = 0
        try:
            if os.name == "nt":
                while not self.stop_event.is_set():
                    try:
                        chunk = os.read(fd, 4096)
                    except InterruptedError:
                        continue
                    except OSError:
                        break

                    if not chunk:
                        with self._stdin_lock:
                            self._stdin_closed = True
                            self._flush_stdin_locked()
                        break

                    with self._stdin_lock:
                        if self.task_id is None:
                            self._stdin_buffer.append(chunk)
                        else:
                            req_id = self._next_req_id()
                            self._send_frame(C2M_STDIN, pack_task_io_request(req_id, self.task_id, chunk))
            else:
                while not self.stop_event.is_set():
                    try:
                        rlist, _, _ = select.select([fd], [], [], 0.1)
                    except InterruptedError:
                        continue
                    except OSError:
                        break

                    if not rlist:
                        continue

                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    except InterruptedError:
                        continue
                    except OSError:
                        break

                    if not chunk:
                        with self._stdin_lock:
                            self._stdin_closed = True
                            self._flush_stdin_locked()
                        break

                    with self._stdin_lock:
                        if self.task_id is None:
                            self._stdin_buffer.append(chunk)
                        else:
                            req_id = self._next_req_id()
                            self._send_frame(C2M_STDIN, pack_task_io_request(req_id, self.task_id, chunk))
        except Exception:
            pass

    def _writer_loop(self, fd: int, q: "queue.Queue[Optional[bytes]]") -> None:
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if item is None:
                    break

                off = 0
                while off < len(item):
                    try:
                        n = os.write(fd, item[off:])
                    except InterruptedError:
                        continue
                    except OSError:
                        return
                    if n <= 0:
                        return
                    off += n
        except Exception:
            pass

    def _handle_server_message(self, msg_type: int, payload: bytes) -> None:
        if msg_type == M2C_CREATE_TASK_RESP:
            try:
                request_id, ok, task_id, err, message = decode_create_task_resp(payload)
            except Exception:
                return

            if self._create_task_request_id is not None and request_id != self._create_task_request_id:
                return

            if ok:
                self.task_id = task_id
                with self._stdin_lock:
                    self._flush_stdin_locked()
            else:
                try:
                    sys.stderr.write(f"create_task failed: {message or 'error'} ({err})\n")
                    sys.stderr.flush()
                except Exception:
                    pass
                self.exit_code = 1
                self.stop_event.set()

        elif msg_type == M2C_STDOUT:
            if len(payload) < 8:
                return
            task_id = bytes_to_u64(payload[:8])
            data = payload[8:]
            if data:
                self._stdout_queue.put(data)

        elif msg_type == M2C_STDERR:
            if len(payload) < 8:
                return
            task_id = bytes_to_u64(payload[:8])
            data = payload[8:]
            if data:
                self._stderr_queue.put(data)

        elif msg_type == M2C_TASK_END:
            if len(payload) >= 14:
                exit_code = bytes_to_u32(payload[8:12])
                signaled = payload[12] != 0
                signal_no = int(payload[13])
                self.exit_code = (128 + signal_no) if signaled else int(exit_code)
            else:
                exit_code, signaled, signal_no = decode_task_end_payload(payload)
                self.exit_code = (128 + signal_no) if signaled else int(exit_code)
            self.stop_event.set()

        elif msg_type == M2C_SERVER_DEAD:
            self.exit_code = 1
            self.stop_event.set()

        elif msg_type == M2C_GENERIC_RESP:
            # Currently ignored unless you want to hook request tracking later.
            return

    def open_manager_session(self) -> None:
        if not os.path.exists(self.connection_file):
            raise FileNotFoundError(f"Manager connection file not found: {self.connection_file}")

        address, authkey = read_connection_info(self.connection_file)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect(address)
        self._socket = sock
        self._reader = BinaryFrameReader(sock)

        # AUTH
        self._send_frame(C2M_AUTH, authkey)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                frame = self._reader.read_frame()
            except socket.timeout:
                continue
            if frame is None:
                raise RuntimeError("manager closed during auth")
            msg_type, _flags, payload = frame
            if msg_type == M2C_AUTH_OK:
                break
            if msg_type == M2C_AUTH_FAIL:
                raise RuntimeError("authentication failed")
        else:
            raise TimeoutError("manager authentication timed out")

        # CREATE_SESSION
        req_id = self._next_req_id()
        self._send_frame(C2M_CREATE_SESSION, pack_create_session_request(req_id))

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                frame = self._reader.read_frame()
            except socket.timeout:
                continue
            if frame is None:
                raise RuntimeError("manager closed during session creation")
            msg_type, _flags, payload = frame
            if msg_type != M2C_CREATE_SESSION_RESP:
                continue

            try:
                resp_req_id, ok, err, session_id, message = decode_create_session_resp(payload)
            except Exception:
                raise RuntimeError("invalid create_session response")

            if resp_req_id != req_id:
                continue

            if not ok:
                raise RuntimeError(f"manager open failed: {message or 'unknown'} ({err})")

            self._session_id = session_id
            break
        else:
            raise TimeoutError("manager did not respond to create_session")

        self._reader_thread = threading.Thread(target=self._socket_reader_loop, daemon=False)
        self._reader_thread.start()

    def _socket_reader_loop(self) -> None:
        if self._reader is None:
            return

        try:
            while not self.stop_event.is_set():
                try:
                    frame = self._reader.read_frame()
                except socket.timeout:
                    continue
                except Exception:
                    break

                if frame is None:
                    break

                msg_type, _flags, payload = frame
                self._handle_server_message(msg_type, payload)
        finally:
            self.stop_event.set()

    def run(self) -> int:
        self.open_manager_session()

        # send create_task
        self._create_task_request_id = self._next_req_id()
        self._send_frame(
            C2M_CREATE_TASK,
            pack_create_task_request(self._create_task_request_id, shlex.join(self.cmd_argv).encode("utf-8")),
        )

        self._stdin_thread = threading.Thread(target=self._stdin_loop, daemon=(os.name == "nt"))
        self._stdout_writer_thread = threading.Thread(target=self._writer_loop, args=(1, self._stdout_queue), daemon=False)
        self._stderr_writer_thread = threading.Thread(target=self._writer_loop, args=(2, self._stderr_queue), daemon=False)

        self._stdin_thread.start()
        self._stdout_writer_thread.start()
        self._stderr_writer_thread.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(0.1)
        finally:
            try:
                if self._socket is not None and not self.stop_event.is_set():
                    req_id = self._next_req_id()
                    self._send_frame(C2M_CLOSE_SESSION, pack_request_id(req_id))
            except Exception:
                pass

            self.stop_event.set()

            try:
                self._stdout_queue.put_nowait(None)
            except Exception:
                pass
            try:
                self._stderr_queue.put_nowait(None)
            except Exception:
                pass

            for th in (self._stdin_thread, self._reader_thread, self._stdout_writer_thread, self._stderr_writer_thread):
                if th is not None:
                    try:
                        th.join(timeout=2.0)
                    except Exception:
                        pass

            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None

        return int(self.exit_code or 0)


def write_connection_info(path: str, address: Tuple[str, int], authkey: bytes) -> None:
    info = {
        "address": list(address),
        "authkey": base64.b64encode(authkey).decode("ascii"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f)


def read_connection_info(path: str) -> Tuple[Tuple[str, int], bytes]:
    with open(path, "r", encoding="utf-8") as f:
        info = json.load(f)
    address = (info["address"][0], int(info["address"][1]))
    authkey = base64.b64decode(info["authkey"].encode("ascii"))
    return address, authkey


def kill_manager(connection_file: str) -> int:
    if not os.path.exists(connection_file):
        print(f"Manager connection file not found: {connection_file}", file=sys.stderr)
        return 1

    try:
        address, authkey = read_connection_info(connection_file)
    except Exception as e:
        print(f"Failed to read connection info: {e}", file=sys.stderr)
        return 1

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(address)
        reader = BinaryFrameReader(sock)
        sock.sendall(pack_frame(C2M_AUTH, authkey))
        frame = reader.read_frame()
        if frame is None or frame[0] != M2C_AUTH_OK:
            raise RuntimeError("authentication failed")

        req_id = 1
        sock.sendall(pack_frame(C2M_STOP_MANAGER, pack_stop_manager_request(req_id)))
        print('Kill request sent to manager.')
        frame = reader.read_frame()
        return 0
    except Exception as e:
        print(f"Failed to send kill request: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["manager", "client"], default="client")
    parser.add_argument(
        "--manager",
        default=default_connection_file(),
        help="Path to Manager connection file",
    )
    parser.add_argument(
        "--server",
        default="./rmpsm_server." + str(platform.system().lower()) + "_" + str(platform.machine().lower()),
    )
    parser.add_argument("--kill", action="store_true", help="Kill the manager process (client only)")
    args, remainder = parser.parse_known_args()

    if remainder and remainder[0] == "--":
        remainder = remainder[1:]

    if args.kill and args.type == "manager":
        print("error: --kill can only be used with --type client", file=sys.stderr)
        return 2

    if args.type == "manager":
        mgr = Manager(args.manager, args.server)
        mgr.run()
        return 0

    if args.kill:
        return kill_manager(args.manager)

    if not remainder:
        print("client mode requires a command after --", file=sys.stderr)
        return 2

    client = ClientRuntime(args.manager, remainder)
    try:
        return client.run()
    except Exception as e:
        raise e


if __name__ == "__main__":
    raise SystemExit(main())

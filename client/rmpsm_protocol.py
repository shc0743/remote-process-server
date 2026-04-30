from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import tempfile
from typing import Optional, Tuple

MAGIC = 0x961F132BDDDC19B9
VERSION = 3
PROTOCOL_VERSION_TEXT = "3.0.0"
MAX_LEN = 1 << 30

TYPE_ACK_ONLY = 18446744073709551615
MAX_APP_PAYLOAD = 32768
MAX_RELIABLE_QUEUE = 256

HEADER = struct.Struct("<QQQQQQQQQ")

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
C2M_QUERY_ERROR = 18
M2C_QUERY_ERROR_RESP = 19


def _default_user_suffix() -> str:
    if os.name != "nt":
        return ""

    import getpass
    import re

    user = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or getpass.getuser()
        or "unknown"
    )
    user = hashlib.sha256(user.encode()).hexdigest()
    if not user:
        return ""
    return "." + user


def default_connection_file() -> str:
    if os.name == "nt":
        return r"\\.\pipe\remote_process_server_bootstrap" + _default_user_suffix()
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return os.path.join(base, "remote_process_server.bootstrap")


def write_connection_info(path: str, address: Tuple[str, int], authkey: bytes) -> None:
    from rmpsm_bootstrap import write_connection_info as _write_connection_info

    _write_connection_info(path, address, authkey)


def read_connection_info(path: str, timeout: float = 5.0) -> Tuple[Tuple[str, int], bytes]:
    from rmpsm_bootstrap import read_connection_info as _read_connection_info

    return _read_connection_info(path, timeout=timeout)


def probe_connection_info(path: str, timeout: float = 1.0) -> bool:
    from rmpsm_bootstrap import probe_connection_info as _probe_connection_info

    return _probe_connection_info(path, timeout=timeout)


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


def pack_query_error_request(request_id: int, error_code: int) -> bytes:
    return pack_request_id(request_id) + u32_to_bytes(error_code)


def pack_query_error_resp(request_id: int, found: bool, text: str = "") -> bytes:
    return (
        u64_to_bytes(request_id)
        + struct.pack("<B", 1 if found else 0)
        + pack_text(text)
    )


def decode_query_error_resp(payload: bytes) -> Tuple[int, bool, str]:
    off = 0
    if len(payload) < 9:
        raise ValueError("truncated query_error response")
    request_id = bytes_to_u64(payload[off:off + 8])
    off += 8
    found = payload[off] != 0
    off += 1
    text, off = unpack_text(payload, off)
    return request_id, found, text


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


def decode_create_session_resp(payload: bytes) -> Tuple[int, bool, int, str, str]:
    off = 0
    if len(payload) < 13:
        raise ValueError("truncated create_session response")
    request_id = bytes_to_u64(payload[off:off + 8])
    off += 8
    ok = payload[off] != 0
    off += 1
    err = bytes_to_u32(payload[off:off + 4])
    off += 4
    session_id, off = unpack_text(payload, off)
    message, off = unpack_text(payload, off)
    return request_id, ok, err, session_id, message


def decode_create_task_resp(payload: bytes) -> Tuple[int, bool, int, int, str]:
    off = 0
    if len(payload) < 21:
        raise ValueError("truncated create_task response")
    request_id = bytes_to_u64(payload[off:off + 8])
    off += 8
    ok = payload[off] != 0
    off += 1
    task_id = bytes_to_u64(payload[off:off + 8])
    off += 8
    err = bytes_to_u32(payload[off:off + 4])
    off += 4
    message, off = unpack_text(payload, off)
    return request_id, ok, task_id, err, message


def decode_generic_resp(payload: bytes) -> Tuple[int, bool, int, str]:
    off = 0
    if len(payload) < 13:
        raise ValueError("truncated generic response")
    request_id = bytes_to_u64(payload[off:off + 8])
    off += 8
    ok = payload[off] != 0
    off += 1
    err = bytes_to_u32(payload[off:off + 4])
    off += 4
    message, off = unpack_text(payload, off)
    return request_id, ok, err, message


def decode_task_end_payload(payload: bytes) -> Tuple[int, bool, int]:
    if len(payload) >= 14:
        exit_code = bytes_to_u32(payload[8:12])
        signaled = payload[12] != 0
        signal_no = payload[13]
        return exit_code, signaled, signal_no

    if len(payload) >= 3:
        exit_code = int(payload[0])
        signaled = payload[1] != 0
        signal_no = int(payload[2])
        return exit_code, signaled, signal_no

    return 0, False, 0


class ControlFrameReader:
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


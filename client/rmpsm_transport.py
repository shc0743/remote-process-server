from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from rmpsm_protocol import MAGIC, TYPE_ACK_ONLY, VERSION, bytes_to_u64, u64_to_bytes


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
        pass


def parse_command_line(cmd: str) -> Tuple[bool, List[str], int]:
    out: List[str] = []

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
            return False, [], errno.EINVAL

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
        return False, [], errno.EINVAL

    flush_word()

    if not out:
        return False, [], errno.EINVAL

    return True, out, 0


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

    wait_status: int = 0
    exit_code: int = 0


@dataclass
class TxItem:
    data: bytes
    offset: int = 0
    reliable: bool = False
    seq: int = 0


@dataclass
class ReliablePacket:
    msg_type: int = 0
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
    last_wire_ts: Optional[float] = None


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
            n = os.write(t.stdin_fd, t.stdin_queue[t.stdin_offset])
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
    msg_type: int,
    requestId: int,
    taskId: int,
    seq: int,
    ack: int,
    payload: Optional[bytes],
    len_: int,
) -> bytes:
    out = bytearray(72 + len_)
    out[0:8] = u64_to_bytes(MAGIC)
    out[8:16] = u64_to_bytes(VERSION)
    out[16:24] = u64_to_bytes(msg_type)
    out[24:32] = u64_to_bytes(0 if msg_type == TYPE_ACK_ONLY else 1)
    out[32:40] = u64_to_bytes(requestId)
    out[40:48] = u64_to_bytes(taskId)
    out[48:56] = u64_to_bytes(seq)
    out[56:64] = u64_to_bytes(ack)
    out[64:72] = u64_to_bytes(len_)
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
            n = os.write(1, item.data[item.offset:])
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False

        if n > 0:
            item.offset += n
            if item.offset >= len(item.data):
                if item.reliable and rel.inflight_exists and not rel.inflight_on_wire and item.seq == rel.inflight.seq:
                    rel.inflight_on_wire = True
                    rel.last_wire_ts = time.monotonic()
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
        rel.last_wire_ts = None
        return True
    return False


def start_next_reliable_if_idle(rel: ReliableState, tx: TransportState, peer_last_delivered_seq: int) -> bool:
    if rel.inflight_exists or not rel.waiting:
        return False

    rel.inflight = rel.waiting.pop(0)
    bytes_ = build_packet_bytes(
        rel.inflight.msg_type,
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
    rel.last_wire_ts = None
    return True


def maybe_retransmit(rel: ReliableState, tx: TransportState, peer_last_delivered_seq: int) -> bool:
    if not rel.inflight_exists or not rel.inflight_on_wire:
        return False

    if rel.last_wire_ts is None:
        return False
    if time.monotonic() - rel.last_wire_ts < 0.5:
        return False

    bytes_ = build_packet_bytes(
        rel.inflight.msg_type,
        rel.inflight.requestId,
        rel.inflight.taskId,
        rel.inflight.seq,
        peer_last_delivered_seq,
        rel.inflight.payload if rel.inflight.payload else None,
        len(rel.inflight.payload),
    )
    enqueue_tx_front(tx, bytes_, True, rel.inflight.seq)
    rel.inflight_on_wire = False
    rel.last_wire_ts = None
    return True


def parse_payload_task_id(payload: bytes) -> Tuple[bool, int]:
    if len(payload) < 8:
        return False, 0
    return True, bytes_to_u64(payload[:8])


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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import json
import os
import platform
import queue
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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, List, Callable

MAGIC = 0x961F132BDDDC19B9
VERSION = 2
PROTOCOL_VERSION_TEXT = "2.0.0"
MAX_LEN = 1 << 30
TYPE_ACK_ONLY = 18446744073709551615
# 
# MAGIC, VERSION, TYPE, FLAGS, REQUEST_ID, TASK_ID, SEQ, ACK, LEN
HEADER = struct.Struct("<QQQQQQQQQ")

def default_prefix() -> str:
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return os.path.join(base, "rmpsm_manager")

def u64_to_bytes(v: int) -> bytes:
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)

def bytes_to_u64(b: bytes) -> int:
    return struct.unpack("<Q", b)[0]

def pack_packet(
    ptype: int,
    request_id: int,
    task_id: int,
    payload: bytes,
    *,
    seq: int,
    ack: int,
    flags: int = 0,
) -> bytes:
    if len(payload) > MAX_LEN:
        raise ValueError("payload too large")
    return HEADER.pack(
        MAGIC,
        VERSION,
        ptype & 0xFFFFFFFFFFFFFFFF,
        flags & 0xFFFFFFFFFFFFFFFF,
        request_id & 0xFFFFFFFFFFFFFFFF,
        task_id & 0xFFFFFFFFFFFFFFFF,
        seq & 0xFFFFFFFFFFFFFFFF,
        ack & 0xFFFFFFFFFFFFFFFF,
        len(payload),
    ) + payload

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

def ensure_fifo(path: str) -> None:
    safe_unlink(path)
    os.mkfifo(path, 0o600)

def json_line(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")

def parse_json_line(raw: bytes) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
    
def set_blocking(fd: int, blocking: bool) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if blocking:
        flags &= ~os.O_NONBLOCK
    else:
        flags |= os.O_NONBLOCK
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)

def open_fifo_read_nonblock(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_NONBLOCK)

def open_fifo_write_retry(path: str, stop_event: Optional[threading.Event] = None) -> int:
    while True:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("stopped")
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.ENXIO, errno.ENOENT):
                time.sleep(0.05)
                continue
            raise

def read_json_messages_loop(
    fd: int,
    stop_event: threading.Event,
    on_message: Callable[[Dict[str, Any]], None],
) -> None:
    buf = bytearray()
    while not stop_event.is_set():
        try:
            chunk = os.read(fd, 4096)
            if not chunk:
                time.sleep(0.05)
                continue
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if not line.strip():
                    continue
                try:
                    msg = parse_json_line(line)
                except Exception:
                    continue
                if msg is not None:
                    on_message(msg)
        except BlockingIOError:
            time.sleep(0.05)
        except InterruptedError:
            continue
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                time.sleep(0.05)
                continue
            if e.errno == errno.EBADF:
                break
            raise

class PacketReader:
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


class Session:
    def __init__(self, manager: "Manager", session_id: str, req_path: str, resp_path: str):
        self.manager = manager
        self.session_id = session_id
        self.req_path = req_path
        self.resp_path = resp_path

        self.req_fd: int = -1
        self.req_keepalive_fd: int = -1
        self.resp_fd: int = -1

        self.stop_event = threading.Event()
        self.out_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()

        self.task_id: Optional[int] = None
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        self.req_fd = open_fifo_read_nonblock(self.req_path)
        self.req_keepalive_fd = open_fifo_write_retry(self.req_path, self.stop_event)

        t1 = threading.Thread(target=self._req_reader_loop, daemon=False)
        t2 = threading.Thread(target=self._resp_writer_loop, daemon=False)
        t1.start()
        t2.start()
        self._threads.extend([t1, t2])

    def _req_reader_loop(self) -> None:
        def on_msg(msg: Dict[str, Any]) -> None:
            self.manager.handle_session_message(self, msg)

        try:
            read_json_messages_loop(self.req_fd, self.stop_event, on_msg)
        finally:
            self.manager.close_session(self.session_id, from_reader=True)

    def _resp_writer_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    self.resp_fd = open_fifo_write_retry(self.resp_path, self.stop_event)
                    set_blocking(self.resp_fd, True)
                    break
                except RuntimeError:
                    return

            while not self.stop_event.is_set():
                try:
                    item = self.out_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    write_all(self.resp_fd, item)
                except OSError:
                    break
        finally:
            if self.resp_fd >= 0:
                try:
                    os.close(self.resp_fd)
                except OSError:
                    pass
                self.resp_fd = -1

    def send_json(self, obj: Dict[str, Any]) -> None:
        if self.stop_event.is_set():
            return
        self.out_queue.put(json_line(obj))

    def close(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        try:
            self.out_queue.put_nowait(None)
        except Exception:
            pass

        for fd in (self.req_fd, self.req_keepalive_fd, self.resp_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

        self.req_fd = -1
        self.req_keepalive_fd = -1
        self.resp_fd = -1

        safe_unlink(self.req_path)
        safe_unlink(self.resp_path)

class ServerBridge:
    def __init__(self, manager: "Manager", server_path: str):
        self.manager = manager
        self.server_path = server_path

        args = shlex.split(server_path)
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
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

    def _build_packet_locked(
        self,
        ptype: int,
        request_id: int,
        task_id: int,
        payload: bytes,
        seq: int,
    ) -> bytes:
        return pack_packet(
            ptype,
            request_id,
            task_id,
            payload,
            seq=seq,
            ack=self._peer_last_delivered_seq,
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
            pkt = pack_packet(
                TYPE_ACK_ONLY,
                0,
                0,
                b"",
                seq=0,  # 关键：不要消耗序号
                ack=self._peer_last_delivered_seq,
            )
    
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
            try:
                self._write_packet(pkt)
            except Exception:
                raise

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
                pkt = pack_packet(
                    ptype,
                    request_id,
                    task_id,
                    payload,
                    seq=seq,
                    ack=current_ack,
                )

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
            session.send_json(
                {
                    "type": "stdout",
                    "task_id": task_id,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }
            )
        elif kind == "stderr":
            session.send_json(
                {
                    "type": "stderr",
                    "task_id": task_id,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                }
            )
        elif kind == "task_end":
            if len(payload) >= 3:
                exit_code = int(payload[0])
                signaled = int(payload[1]) != 0
                signal_no = int(payload[2])
            else:
                exit_code = 0
                signaled = False
                signal_no = 0
            session.send_json(
                {
                    "type": "task_end",
                    "task_id": task_id,
                    "exit_code": exit_code,
                    "signaled": signaled,
                    "signal_no": signal_no,
                }
            )

    def register_task(self, session_id: str, task_id: int) -> None:
        self._task_to_session[task_id] = session_id

        if task_id in self._orphan_packets:
            session = self.manager.sessions.get(session_id)
            if session is not None:
                for kind_id, payload in self._orphan_packets.pop(task_id):
                    if kind_id == 0:
                        session.send_json(
                            {
                                "type": "stdout",
                                "task_id": task_id,
                                "data_b64": base64.b64encode(payload).decode("ascii"),
                            }
                        )
                    elif kind_id == 1:
                        session.send_json(
                            {
                                "type": "stderr",
                                "task_id": task_id,
                                "data_b64": base64.b64encode(payload).decode("ascii"),
                            }
                        )
                    else:
                        if len(payload) >= 3:
                            exit_code = int(payload[0])
                            signaled = int(payload[1]) != 0
                            signal_no = int(payload[2])
                        else:
                            exit_code = 0
                            signaled = False
                            signal_no = 0
                        session.send_json(
                            {
                                "type": "task_end",
                                "task_id": task_id,
                                "exit_code": exit_code,
                                "signaled": signaled,
                                "signal_no": signal_no,
                            }
                        )

    def _reader_loop(self) -> None:
        reader = PacketReader(self.stdout_fd)
        try:
            while not self.stop_event.is_set():
                try:
                    pkt = reader.read_packet()
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

                accepted = False
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

                # ⭐ 提前发送 ACK
                if accepted or seq < self._peer_expected_seq:
                    self._send_ack_only()
                
                if accepted:
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

                #self._send_ack_only()
        finally:
            self.stop_event.set()
            self.manager.on_server_dead()

    def send_stop(self) -> None:
        try:
            self._send_packet(1, 0, 0, b"\x01", require_ack=False)
        except Exception:
            pass

class Manager:
    def __init__(self, prefix: str, server_path: str):
        self.prefix = prefix
        self.server_path = server_path

        self.ctl_path = f"{self.prefix}.ctl"
        self._base_dir = os.path.dirname(self.ctl_path) or "."

        os.makedirs(self._base_dir, exist_ok=True)
        ensure_fifo(self.ctl_path)

        self.ctl_rfd = open_fifo_read_nonblock(self.ctl_path)
        self.ctl_keepalive_wfd = open_fifo_write_retry(self.ctl_path)
        self.stop_event = threading.Event()

        self.sessions: Dict[str, Session] = {}
        self._session_lock = threading.Lock()
        self._next_session_id = 1

        self.bridge = ServerBridge(self, server_path)

        self._ctl_thread = threading.Thread(target=self._ctl_loop, daemon=False)
        self._ctl_thread.start()

    def _new_session_id(self) -> str:
        with self._session_lock:
            sid = f"{self._next_session_id:x}-{secrets.token_hex(8)}"
            self._next_session_id += 1
            return sid

    def _session_paths(self, session_id: str) -> Tuple[str, str]:
        req = f"{self.prefix}.{session_id}.req"
        resp = f"{self.prefix}.{session_id}.resp"
        return req, resp

    def _ctl_loop(self) -> None:
        def on_msg(msg: Dict[str, Any]) -> None:
            self.handle_ctl_message(msg)

        try:
            read_json_messages_loop(self.ctl_rfd, self.stop_event, on_msg)
        finally:
            self.shutdown()

    def handle_ctl_message(self, msg: Dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "kill_manager":
            self.shutdown()
            return
        if op != "open":
            reply_fifo = msg.get("reply_fifo")
            if isinstance(reply_fifo, str):
                self._write_reply_fifo(
                    reply_fifo,
                    {
                        "ok": False,
                        "error": "unknown ctl op",
                        "errno": errno.EINVAL,
                    },
                )
            return

        reply_fifo = msg.get("reply_fifo")
        if not isinstance(reply_fifo, str) or not reply_fifo:
            return

        session_id = self._new_session_id()
        req_path, resp_path = self._session_paths(session_id)

        try:
            ensure_fifo(req_path)
            ensure_fifo(resp_path)
            session = Session(self, session_id, req_path, resp_path)
            with self._session_lock:
                self.sessions[session_id] = session
            session.start()
            self._write_reply_fifo(
                reply_fifo,
                {
                    "ok": True,
                    "session_id": session_id,
                    "req_fifo": req_path,
                    "resp_fifo": resp_path,
                },
            )
        except Exception as e:
            self._write_reply_fifo(
                reply_fifo,
                {
                    "ok": False,
                    "error": str(e),
                    "errno": getattr(e, "errno", errno.EIO),
                },
            )

    def _write_reply_fifo(self, path: str, obj: Dict[str, Any]) -> None:
        fd = -1
        try:
            fd = open_fifo_write_retry(path, self.stop_event)
            write_all(fd, json_line(obj))
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def handle_session_message(self, session: Session, msg: Dict[str, Any]) -> None:
        op = msg.get("op")

        if op == "create_task":
            cmdline = msg.get("cmdline")
            if isinstance(cmdline, list):
                cmdline = shlex.join([str(x) for x in cmdline])
            elif not isinstance(cmdline, str):
                session.send_json(
                    {
                        "type": "create_task_result",
                        "ok": False,
                        "error": "invalid cmdline",
                        "errno": errno.EINVAL,
                    }
                )
                return

            ok, task_id, err = self.bridge.create_task(session.session_id, cmdline)
            if ok:
                session.task_id = task_id
                session.send_json(
                    {
                        "type": "create_task_result",
                        "ok": True,
                        "task_id": task_id,
                        "request_id": msg.get("request_id", 0),
                    }
                )
            else:
                session.send_json(
                    {
                        "type": "create_task_result",
                        "ok": False,
                        "error": os.strerror(err) if err else "create_task failed",
                        "errno": err,
                        "request_id": msg.get("request_id", 0),
                    }
                )
            return

        if op == "stdin":
            task_id = msg.get("task_id", session.task_id)
            data_b64 = msg.get("data_b64")
            if not isinstance(task_id, int) or not isinstance(data_b64, str):
                return
            try:
                data = base64.b64decode(data_b64.encode("ascii"), validate=True)
            except Exception:
                return
            self.bridge.send_input(task_id, data)
            return

        if op == "stdin_eof":
            task_id = msg.get("task_id", session.task_id)
            if isinstance(task_id, int):
                self.bridge.send_eof(task_id)
            return

        if op == "kill":
            task_id = msg.get("task_id", session.task_id)
            if isinstance(task_id, int):
                self.bridge.kill_task(task_id)
            return

        if op == "close":
            self.close_session(session.session_id)
            return

    def close_session(self, session_id: str, from_reader: bool = False) -> None:
        with self._session_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        
        if session.task_id is not None:
            try:
                if not from_reader and not self.stop_event.is_set():
                    self.bridge.kill_task(session.task_id)
            except Exception:
                pass
            # self.bridge.unbind_task(session.task_id)
        
        session.close()

    def on_server_dead(self) -> None:
        with self._session_lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        for session in sessions:
            try:
                session.send_json(
                    {
                        "type": "server_dead",
                        "ok": False,
                    }
                )
            except Exception:
                pass
            session.close()

        self.stop_event.set()

    def shutdown(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()

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

        for fd in (self.ctl_rfd, self.ctl_keepalive_wfd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

        self.ctl_rfd = -1
        self.ctl_keepalive_wfd = -1
        safe_unlink(self.ctl_path)

    def run(self) -> None:
        def sig_handler(signum, frame):
            self.shutdown()

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            self.shutdown()

class ClientRuntime:
    def __init__(self, prefix: str, cmd_argv: List[str]):
        self.prefix = prefix
        self.cmd_argv = cmd_argv

        self.ctl_path = f"{self.prefix}.ctl"
        self.reply_fifo = f"{self.prefix}.client.{secrets.token_hex(8)}.reply"

        self.req_fifo: Optional[str] = None
        self.resp_fifo: Optional[str] = None

        self.req_wfd: int = -1
        self.resp_rfd: int = -1
        self.reply_rfd: int = -1
        self.reply_wfd: int = -1
        self.ctl_rfd: int = -1
        self.ctl_wfd: int = -1

        self.stop_event = threading.Event()
        self.task_id: Optional[int] = None
        self.exit_code: Optional[int] = None

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

    def _next_req_id(self) -> int:
        with self._req_lock:
            rid = self._next_request_id
            self._next_request_id = (self._next_request_id + 1) & 0xFFFFFFFFFFFFFFFF
            if self._next_request_id == 0:
                self._next_request_id = 1
            return rid

    def _send_json_fd(self, fd: int, obj: Dict[str, Any]) -> None:
        write_all(fd, json_line(obj))

    def _send_session_json(self, obj: Dict[str, Any]) -> None:
        if self.req_wfd < 0:
            raise RuntimeError("session request fifo not open")
        self._send_json_fd(self.req_wfd, obj)

    def _flush_stdin_locked(self) -> None:
        if self.task_id is None or self.req_wfd < 0:
            return

        for chunk in self._stdin_buffer:
            self._send_session_json(
                {
                    "op": "stdin",
                    "request_id": self._next_req_id(),
                    "task_id": self.task_id,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                }
            )
        self._stdin_buffer.clear()

        if self._stdin_closed:
            self._send_session_json(
                {
                    "op": "stdin_eof",
                    "request_id": self._next_req_id(),
                    "task_id": self.task_id,
                }
            )
            self._stdin_closed = False

    def _stdin_loop(self) -> None:
        fd = 0
        try:
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
                        self._send_session_json(
                            {
                                "op": "stdin",
                                "request_id": self._next_req_id(),
                                "task_id": self.task_id,
                                "data_b64": base64.b64encode(chunk).decode("ascii"),
                            }
                        )
        except Exception:
            pass

    def _writer_loop(self, fd: int, q: "queue.Queue[Optional[bytes]]") -> None:
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    # 不因为 stop_event 提前退出，
                    # 只等 sentinel(None) 来结束
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

    def _resp_loop(self) -> None:
        def on_msg(msg: Dict[str, Any]) -> None:
            t = msg.get("type")

            if t == "create_task_result":
                if msg.get("ok"):
                    tid = msg.get("task_id")
                    if isinstance(tid, int):
                        self.task_id = tid
                        with self._stdin_lock:
                            self._flush_stdin_locked()
                else:
                    err = msg.get("errno", errno.EIO)
                    try:
                        sys.stderr.write(f"create_task failed: {msg.get('error', 'error')} ({err})\n")
                        sys.stderr.flush()
                    except Exception:
                        pass
                    self.exit_code = 1
                    self.stop_event.set()

            elif t == "stdout":
                data_b64 = msg.get("data_b64", "")
                try:
                    data = base64.b64decode(data_b64.encode("ascii"), validate=True)
                    self._stdout_queue.put(data)
                except Exception:
                    pass

            elif t == "stderr":
                data_b64 = msg.get("data_b64", "")
                try:
                    data = base64.b64decode(data_b64.encode("ascii"), validate=True)
                    self._stderr_queue.put(data)
                except Exception:
                    pass

            elif t == "task_end":
                exit_code = int(msg.get("exit_code", 0))
                signaled = bool(msg.get("signaled", False))
                signal_no = int(msg.get("signal_no", 0))
                if signaled:
                    self.exit_code = 128 + signal_no
                else:
                    self.exit_code = exit_code
                self.stop_event.set()

            elif t == "server_dead":
                self.exit_code = 1
                self.stop_event.set()

        try:
            read_json_messages_loop(self.resp_rfd, self.stop_event, on_msg)
        finally:
            self.stop_event.set()

    def open_manager_session(self) -> None:
        if not os.path.exists(self.ctl_path):
            raise FileNotFoundError(self.ctl_path)

        ensure_fifo(self.reply_fifo)
        self.reply_rfd = open_fifo_read_nonblock(self.reply_fifo)

        self.ctl_wfd = os.open(self.ctl_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            self._send_json_fd(
                self.ctl_wfd,
                {
                    "op": "open",
                    "reply_fifo": self.reply_fifo,
                    "client_pid": os.getpid(),
                    "argv": self.cmd_argv,
                },
            )
        finally:
            try:
                os.close(self.ctl_wfd)
            except OSError:
                pass
            self.ctl_wfd = -1

        reply_buf = bytearray()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                chunk = os.read(self.reply_rfd, 4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                reply_buf.extend(chunk)
                nl = reply_buf.find(b"\n")
                if nl >= 0:
                    line = bytes(reply_buf[:nl])
                    msg = parse_json_line(line)
                    if not msg:
                        continue
                    if not msg.get("ok"):
                        raise RuntimeError(f"manager open failed: {msg.get('error', 'unknown')}")
                    self.req_fifo = msg["req_fifo"]
                    self.resp_fifo = msg["resp_fifo"]
                    break
            except BlockingIOError:
                time.sleep(0.05)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.05)
                    continue
                raise

        if self.req_fifo is None or self.resp_fifo is None:
            raise TimeoutError("manager did not respond")

        try:
            os.close(self.reply_rfd)
        except OSError:
            pass
        self.reply_rfd = -1
        safe_unlink(self.reply_fifo)

        self.resp_rfd = open_fifo_read_nonblock(self.resp_fifo)
        self.req_wfd = os.open(self.req_fifo, os.O_WRONLY | os.O_NONBLOCK)
        set_blocking(self.req_wfd, True)

    def run(self) -> int:
        self.open_manager_session()

        create_req_id = self._next_req_id()
        self._send_session_json(
            {
                "op": "create_task",
                "request_id": create_req_id,
                "cmdline": shlex.join(self.cmd_argv),
            }
        )

        self._reader_thread = threading.Thread(target=self._resp_loop, daemon=False)
        self._stdin_thread = threading.Thread(target=self._stdin_loop, daemon=False)
        self._stdout_writer_thread = threading.Thread(
            target=self._writer_loop,
            args=(1, self._stdout_queue),
            daemon=False,
        )
        self._stderr_writer_thread = threading.Thread(
            target=self._writer_loop,
            args=(2, self._stderr_queue),
            daemon=False,
        )

        self._reader_thread.start()
        self._stdin_thread.start()
        self._stdout_writer_thread.start()
        self._stderr_writer_thread.start()

        try:
            while not self.stop_event.is_set():
                time.sleep(0.1)
        finally:
            try:
                if self.req_wfd >= 0:
                    self._send_session_json(
                        {
                            "op": "close",
                            "request_id": self._next_req_id(),
                        }
                    )
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

            for fd in (self.req_wfd, self.resp_rfd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            if self._stdin_thread is not None:
                self._stdin_thread.join(timeout=2.0)
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2.0)
            if self._stdout_writer_thread is not None:
                self._stdout_writer_thread.join(timeout=2.0)
            if self._stderr_writer_thread is not None:
                self._stderr_writer_thread.join(timeout=2.0)

            self.req_wfd = -1
            self.resp_rfd = -1

            safe_unlink(self.req_fifo or "")
            safe_unlink(self.resp_fifo or "")

        return int(self.exit_code or 0)

def kill_manager(manager_prefix: str) -> int:
    ctl_path = f"{manager_prefix}.ctl"
    if not os.path.exists(ctl_path):
        print(f"Manager control FIFO not found: {ctl_path}", file=sys.stderr)
        return 1

    try:
        # 以非阻塞方式打开控制 FIFO 写入端
        fd = os.open(ctl_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f"Failed to open control FIFO: {e}", file=sys.stderr)
        return 1

    try:
        # 发送 kill_manager 操作消息
        msg = {"op": "kill_manager"}
        data = json_line(msg)
        write_all(fd, data)
        print("Kill request sent to manager.", file=sys.stderr)
    except Exception as e:
        print(f"Failed to send kill request: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["manager", "client"], default="client")
    parser.add_argument("--manager", default=default_prefix())
    parser.add_argument(
        "--server",
        default="./rmpsm_server." + str(platform.system().lower()) + "_" + str(platform.machine()),
    )
    parser.add_argument("--kill", action="store_true", help="Send kill request to manager (client only)")
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
        # print(str(e), file=sys.stderr)
        raise e
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

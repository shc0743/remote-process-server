from __future__ import annotations

import errno
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rmpsm_packet_reader import ServerPacketReader
from rmpsm_protocol import PROTOCOL_VERSION_TEXT, TYPE_ACK_ONLY, bytes_to_u64, decode_task_end_payload
from rmpsm_transport import build_packet_bytes, write_all


@dataclass
class PendingCreateTask:
    session_id: str
    event: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    task_id: int = 0
    err: int = 0


class ServerBridge:
    def __init__(self, manager: "Manager", server_path: str, stderr: str = "inherit"):
        self.manager = manager
        self.server_path = server_path

        args = shlex.split(server_path, posix=(os.name != "nt"))
        if not args:
            raise FileNotFoundError("empty server path")

        # Map stderr argument to subprocess constant
        if stderr == "ignore":
            stderr_handle = subprocess.DEVNULL
        elif stderr == "merge":
            stderr_handle = subprocess.STDOUT
        elif stderr == "inherit":
            stderr_handle = None
        else:
            raise ValueError(f"invalid stderr option: {stderr}")

        self.proc = subprocess.Popen(
            args,
            executable=args[0] if os.name == "nt" and os.path.exists(args[0]) else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            bufsize=0,
            shell=False,
        )

        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("failed to start server process")
        try:
            print('Server process started, waiting for connection...', file=sys.stderr)
        except BaseException:
            pass

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
        timeout: float = 1.5,
        max_retries: int = 60,
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
                pkt = build_packet_bytes(
                    ptype,
                    request_id,
                    task_id,
                    seq,
                    current_ack,
                    payload if payload else None,
                    len(payload),
                )

        raise RuntimeError("stopped")

    def _check_version(self) -> None:
        self._version_req_id = self._next_req_id()
        self._version_event.clear()
        self._version_value = None

        self._send_packet(
            255,
            self._version_req_id,
            0,
            b"",
            require_ack=True,
            timeout=2.0,
            max_retries=120,
        )

        if not self._version_event.wait(120.0):
            raise TimeoutError("server version check timed out")

        if self._version_value != PROTOCOL_VERSION_TEXT:
            raise RuntimeError(
                f"protocol version mismatch: server={self._version_value!r}, client={PROTOCOL_VERSION_TEXT!r}"
            )

    def create_task(self, session_id: str, cmdline: str, timeout: float = 180.0) -> Tuple[bool, int, int]:
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
                        exit_code, signaled, signal_no = decode_task_end_payload(payload)
                        session.send_task_end(task_id, exit_code, signaled, signal_no)

    def _reader_loop(self) -> None:
        reader = ServerPacketReader(self.stdout_fd)
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

                if not accepted:
                    continue

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

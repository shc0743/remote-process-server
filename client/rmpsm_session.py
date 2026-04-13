from __future__ import annotations

import socket
import threading

from rmpsm_protocol import (
    M2C_CREATE_SESSION_RESP,
    M2C_CREATE_TASK_RESP,
    M2C_GENERIC_RESP,
    M2C_SERVER_DEAD,
    M2C_STDERR,
    M2C_STDOUT,
    M2C_TASK_END,
    pack_create_session_resp,
    pack_create_task_resp,
    pack_generic_resp,
    pack_stdout_stderr,
    pack_task_end,
)


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
                self.client_socket.sendall(
                    pack_frame(msg_type, payload)  # type: ignore[name-defined]
                )
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


from rmpsm_protocol import pack_frame  # placed at end to avoid circular import noise

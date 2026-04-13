from __future__ import annotations

import errno
import os
import secrets
import signal
import socket
import sys
import threading
import time
from typing import Dict, Optional

from rmpsm_bridge import ServerBridge
from rmpsm_protocol import (
    C2M_AUTH,
    C2M_CLOSE_SESSION,
    C2M_CREATE_SESSION,
    C2M_CREATE_TASK,
    C2M_KILL,
    C2M_STDIN,
    C2M_STDIN_EOF,
    C2M_STOP_MANAGER,
    M2C_AUTH_FAIL,
    M2C_AUTH_OK,
    bytes_to_u64,
    pack_frame,
    pack_generic_resp,
    read_connection_info,
    write_connection_info,
)
from rmpsm_session import SessionProxy
from rmpsm_transport import safe_unlink


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
        print("Shutdowning server...", file=sys.stderr)

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
        reader = ControlFrameReader(client_socket)
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
                        print("Stop request received, shutdowning server...", file=sys.stderr)
                        request_id = bytes_to_u64(payload[:8]) if len(payload) >= 8 else 0
                        if session is not None:
                            session.send_generic_resp(request_id, True, 0, "")
                        self.shutdown()
                        break

                    if session is None:
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
                            self.bridge.register_task(session.session_id, task_id)
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


from rmpsm_protocol import ControlFrameReader

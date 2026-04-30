from __future__ import annotations

import errno
import os
import queue
import select
import shlex
import socket
import subprocess
import sys
import threading
import time
from typing import List, Optional

from rmpsm_protocol import (
    C2M_AUTH,
    C2M_CLOSE_SESSION,
    C2M_CREATE_SESSION,
    C2M_CREATE_TASK,
    C2M_QUERY_ERROR,
    C2M_STOP_MANAGER,
    C2M_STDIN,
    C2M_STDIN_EOF,
    C2M_KILL,
    M2C_AUTH_FAIL,
    M2C_AUTH_OK,
    M2C_CREATE_SESSION_RESP,
    M2C_CREATE_TASK_RESP,
    M2C_GENERIC_RESP,
    M2C_QUERY_ERROR_RESP,
    M2C_SERVER_DEAD,
    M2C_STDERR,
    M2C_STDOUT,
    M2C_TASK_END,
    ControlFrameReader,
    bytes_to_u32,
    decode_create_session_resp,
    decode_create_task_resp,
    decode_query_error_resp,
    decode_task_end_payload,
    pack_create_session_request,
    pack_create_task_request,
    pack_frame,
    pack_query_error_request,
    pack_request_id,
    pack_stop_manager_request,
    pack_task_id_request,
    pack_task_io_request,
    read_connection_info,
)
from rmpsm_errors import ManagerNotRunningError, ConnectionRefusedError

class ClientRuntime:
    def __init__(self, connection_file: str, cmd_argv: List[str], useCmdSyntax: bool):
        self.connection_file = connection_file
        self.cmd_argv = cmd_argv

        self.stop_event = threading.Event()
        self.task_id: Optional[int] = None
        self.exit_code: Optional[int] = None

        self._session_id: Optional[str] = None
        self._socket: Optional[socket.socket] = None
        self._reader: Optional[ControlFrameReader] = None
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
        self._query_error_code: Optional[int] = None
        self._query_error_request_id: Optional[int] = None
        self._useCmdSyntax = useCmdSyntax

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
                self._query_error_code = err
                self._query_error_request_id = self._next_req_id()
                self._send_frame(
                    C2M_QUERY_ERROR,
                    pack_query_error_request(self._query_error_request_id, err),
                )

        elif msg_type == M2C_QUERY_ERROR_RESP:
            if self._query_error_request_id is None:
                return
            try:
                resp_request_id, found, text = decode_query_error_resp(payload)
            except Exception:
                return
            if resp_request_id != self._query_error_request_id:
                return
            self._query_error_request_id = None

            err_code = self._query_error_code or 0
            desc = text if found else "Unknown error"
            try:
                sys.stderr.write(f"create_task failed: {desc} ({err_code})\n")
                sys.stderr.flush()
            except Exception:
                pass
            self.exit_code = 1
            self.stop_event.set()

        elif msg_type == M2C_STDOUT:
            if len(payload) < 8:
                return
            data = payload[8:]
            if data:
                self._stdout_queue.put(data)

        elif msg_type == M2C_STDERR:
            if len(payload) < 8:
                return
            data = payload[8:]
            if data:
                self._stderr_queue.put(data)

        elif msg_type == M2C_TASK_END:
            exit_code, signaled, signal_no = decode_task_end_payload(payload)
            self.exit_code = (128 + signal_no) if signaled else int(exit_code)
            self.stop_event.set()

        elif msg_type == M2C_SERVER_DEAD:
            self.exit_code = 1
            self.stop_event.set()

        elif msg_type == M2C_GENERIC_RESP:
            return

    def open_manager_session(self) -> None:
        startup_deadline = time.monotonic() + 10.0
        last_error: Optional[BaseException] = None

        # POSIX 上 bootstrap 载体是普通文件。文件都不存在，就说明 manager 根本没启动，
        # 这时不应该进入“等它慢慢起来”的重试逻辑。
        if os.name != "nt" and not os.path.exists(self.connection_file):
            raise FileNotFoundError(f"manager is not running")

        while time.monotonic() < startup_deadline and not self.stop_event.is_set():
            sock: Optional[socket.socket] = None
            try:
                address, authkey = read_connection_info(self.connection_file, timeout=0.5)

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(address)

                self._socket = sock
                self._reader = ControlFrameReader(sock)

                self._send_frame(C2M_AUTH, authkey)
                auth_deadline = time.monotonic() + 8.0
                while time.monotonic() < auth_deadline:
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

                req_id = self._next_req_id()
                self._send_frame(C2M_CREATE_SESSION, pack_create_session_request(req_id))

                session_deadline = time.monotonic() + 20.0
                while time.monotonic() < session_deadline:
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
                return

            except FileNotFoundError:
                raise
            except ConnectionRefusedError:
                raise
            except ManagerNotRunningError:
                raise
            except Exception as e:
                last_error = e
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                self._socket = None
                self._reader = None

                if isinstance(e, RuntimeError):
                    raise

            time.sleep(0.2)

        raise TimeoutError("manager startup timed out") from last_error

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

    def _join_cmdline_for_cmd(self, args):
        parts = []
        for arg in args:
            if not arg:
                parts.append('""')
                continue

            need_quote = any(ch in ' \t\n\v"' for ch in arg)

            if not need_quote:
                parts.append(arg)
            else:
                parts.append(f'"{arg}"')

        return ' '.join(parts)

    def run(self) -> int:
        self.open_manager_session()

        cmdline = (self._join_cmdline_for_cmd(self.cmd_argv) if self._useCmdSyntax else (subprocess.list2cmdline(self.cmd_argv) if os.name == 'nt' else shlex.join(self.cmd_argv))).encode("utf-8")

        self._create_task_request_id = self._next_req_id()
        self._send_frame(
            C2M_CREATE_TASK,
            pack_create_task_request(self._create_task_request_id, cmdline),
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

def kill_manager(connection_file: str) -> int:
    try:
        address, authkey = read_connection_info(connection_file, timeout=0.5) # Kill request doesn't wait
    except Exception as e:
        print(f"Failed to read connection info: {e}", file=sys.stderr)
        return 1

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(address)
        reader = ControlFrameReader(sock)
        sock.sendall(pack_frame(C2M_AUTH, authkey))
        frame = reader.read_frame()
        if frame is None or frame[0] != M2C_AUTH_OK:
            raise RuntimeError("authentication failed")

        req_id = 1
        sock.sendall(pack_frame(C2M_STOP_MANAGER, pack_stop_manager_request(req_id)))
        print("Kill request sent to manager.")
        try:
            _ = reader.read_frame()
        except Exception:
            pass
        return 0
    except Exception as e:
        print(f"Failed to send kill request: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass

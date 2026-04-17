from __future__ import annotations

import ctypes
import errno
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
advapi32 = ctypes.WinDLL('advapi32.dll', use_last_error=True)

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
WAIT_TIMEOUT = 258

LPVOID = ctypes.c_void_p
LPCWSTR = wintypes.LPCWSTR


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


LPSECURITY_ATTRIBUTES = ctypes.POINTER(SECURITY_ATTRIBUTES)

kernel32.CreateNamedPipeW.argtypes = [LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, LPSECURITY_ATTRIBUTES]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, LPVOID]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
kernel32.CreateFileW.argtypes = [LPCWSTR, wintypes.DWORD, wintypes.DWORD, LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ReadFile.argtypes = [wintypes.HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
kernel32.FlushFileBuffers.restype = wintypes.BOOL
kernel32.WaitNamedPipeW.argtypes = [LPCWSTR, wintypes.DWORD]
kernel32.WaitNamedPipeW.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [LPVOID]
kernel32.LocalFree.restype = LPVOID

advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [LPCWSTR, wintypes.DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

# =========================
# Security helpers
# =========================

GetNamedPipeClientProcessId = kernel32.GetNamedPipeClientProcessId
GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
GetNamedPipeClientProcessId.restype = wintypes.BOOL

OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE
GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.restype = wintypes.HANDLE

OpenProcessToken = advapi32.OpenProcessToken
OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
OpenProcessToken.restype = wintypes.BOOL

GetTokenInformation = advapi32.GetTokenInformation
GetTokenInformation.restype = wintypes.BOOL

TOKEN_QUERY = 0x0008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

TokenUser = 1
TokenIntegrityLevel = 25

advapi32.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(LPCWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

GetSidSubAuthorityCount = advapi32.GetSidSubAuthorityCount
GetSidSubAuthorityCount.argtypes = [LPVOID]
GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)

GetSidSubAuthority = advapi32.GetSidSubAuthority
GetSidSubAuthority.argtypes = [LPVOID, wintypes.DWORD]
GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

INTEGRITY_RANK = {
    0x00000000: 0,  # Untrusted
    0x00001000: 1,  # Low
    0x00002000: 2,  # Medium
    0x00002100: 3,  # Medium Plus
    0x00003000: 4,  # High
    0x00004000: 5,  # System
    0x00005000: 6,  # Protected Process
}

# =========================
# Security helpers
# =========================

ImpersonateNamedPipeClient = advapi32.ImpersonateNamedPipeClient
ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
ImpersonateNamedPipeClient.restype = wintypes.BOOL

RevertToSelf = advapi32.RevertToSelf
RevertToSelf.argtypes = []
RevertToSelf.restype = wintypes.BOOL

OpenThreadToken = advapi32.OpenThreadToken
OpenThreadToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.BOOL,
    ctypes.POINTER(wintypes.HANDLE),
]
OpenThreadToken.restype = wintypes.BOOL

GetCurrentThread = kernel32.GetCurrentThread
GetCurrentThread.restype = wintypes.HANDLE

def _integrity_rank(rid: int) -> int:
    return INTEGRITY_RANK.get(rid, -1)

def sid_to_string(sid_ptr):
    out = LPCWSTR()

    if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        _raise_last_error("ConvertSidToStringSidW")

    try:
        return out.value
    finally:
        kernel32.LocalFree(out)


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def _get_client_pid(pipe_handle: wintypes.HANDLE) -> int:
    pid = wintypes.ULONG()
    if not GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(pid)):
        _raise_last_error("GetNamedPipeClientProcessId")
    return pid.value


def _open_process_token(pid: int) -> wintypes.HANDLE:
    h_proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        _raise_last_error("OpenProcess")

    h_token = wintypes.HANDLE()
    if not OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
        kernel32.CloseHandle(h_proc)
        _raise_last_error("OpenProcessToken")

    kernel32.CloseHandle(h_proc)
    return h_token


def _open_current_process_token() -> wintypes.HANDLE:
    h_token = wintypes.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h_token)):
        _raise_last_error("OpenProcessToken(current)")
    return h_token


class WinPipeError(OSError):
    pass


@dataclass
class SecurityAttributes:
    sa: SECURITY_ATTRIBUTES
    sd: LPVOID


def _raise_last_error(prefix: str) -> None:
    err = ctypes.get_last_error()
    raise WinPipeError(err, f"{prefix} failed with error {err}")


def _get_token_user_sid(token) -> str:
    size = wintypes.DWORD(0)

    GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)

    if not GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)):
        _raise_last_error("GetTokenInformation(TokenUser)")

    tu = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
    return sid_to_string(tu.User.Sid)


def _get_token_integrity(token) -> int:
    size = wintypes.DWORD(0)
    GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))

    buf = ctypes.create_string_buffer(size.value)
    if not GetTokenInformation(token, TokenIntegrityLevel, buf, size, ctypes.byref(size)):
        _raise_last_error("GetTokenInformation(TokenIntegrityLevel)")

    til = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
    sid = til.Label.Sid

    if not sid:
        raise WinPipeError(0, "NULL integrity SID")

    count = GetSidSubAuthorityCount(sid)[0]
    if count == 0:
        raise WinPipeError(0, "Malformed integrity SID")

    rid = GetSidSubAuthority(sid, count - 1)[0]
    return int(rid)


def _check_client_allowed(pipe_handle: wintypes.HANDLE) -> bool:
    # 先拿服务器自己的 token，避免把这个步骤放进 impersonation 窗口里。
    print('pipe_handle:',pipe_handle,file=sys.stderr)
    server_token = _open_current_process_token()
    try:
        # 进入客户端身份；失败就直接拒绝。
        if not ImpersonateNamedPipeClient(pipe_handle):
            _raise_last_error("ImpersonateNamedPipeClient")
        print('2',file=sys.stderr)

        client_token = wintypes.HANDLE()
        try:
            # 直接从当前线程拿客户端 token。
            # OpenAsSelf=True 可兼容 Identification-level impersonation。
            if not OpenThreadToken(
                GetCurrentThread(),
                TOKEN_QUERY,
                True,
                ctypes.byref(client_token),
            ):
                _raise_last_error("OpenThreadToken")

            print('3',file=sys.stderr)
            try:
                csid = _get_token_user_sid(client_token)
                ssid = _get_token_user_sid(server_token)
                if csid != ssid:
                    return False

                ci = _get_token_integrity(client_token)
                si = _get_token_integrity(server_token)
                print('[DEBUG]ci',ci,'si',si,file=sys.stderr)
                irci = _integrity_rank(ci)
                irsi = _integrity_rank(si)
                print('[DEBUG]irci',irci,'irsi',irsi,file=sys.stderr)
                if irci < irsi:
                    return False

                return True
            finally:
                if client_token:
                    kernel32.CloseHandle(client_token)
        finally:
            if not RevertToSelf():
                _raise_last_error("RevertToSelf")
    finally:
        kernel32.CloseHandle(server_token)


def _create_security_attributes() -> SecurityAttributes:
    # # Allow elevated Administrators and LocalSystem. The high integrity label
    # # prevents lower-integrity processes from writing to the pipe.
    # sddl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)S:(ML;;NW;;;HI)"
    # sd = LPVOID()
    # sd_size = wintypes.DWORD()
    # if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), ctypes.byref(sd_size)):
        # _raise_last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")

    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = None # sd
    sa.bInheritHandle = False
    return SecurityAttributes(sa=sa, sd=None) #sd)


class NamedPipeBootstrapServer:
    def __init__(self, pipe_name: str, payload: bytes):
        self.pipe_name = pipe_name
        self.payload = payload if payload.endswith(b"\n") else payload + b"\n"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._security = _create_security_attributes()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="rmpsm-bootstrap-pipe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._poke()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._security.sd:
            try:
                kernel32.LocalFree(self._security.sd)
            except Exception:
                pass
            self._security.sd = None

    def _poke(self) -> None:
        h = kernel32.CreateFileW(
            self.pipe_name,
            GENERIC_READ,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h == INVALID_HANDLE_VALUE:
            return
        try:
            pass
        finally:
            kernel32.CloseHandle(h)

    def _create_instance(self) -> wintypes.HANDLE:
        flags = PIPE_ACCESS_DUPLEX
        h = kernel32.CreateNamedPipeW(
            self.pipe_name,
            flags,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES,
            4096,
            4096,
            0,
            ctypes.byref(self._security.sa),
        )
        if h == INVALID_HANDLE_VALUE:
            _raise_last_error("CreateNamedPipeW")
        return h

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                h_pipe = self._create_instance()
                connected = False
                try:
                    ok = kernel32.ConnectNamedPipe(h_pipe, None)
                    if not ok:
                        err = ctypes.get_last_error()
                        if err == ERROR_PIPE_CONNECTED:
                            connected = True
                        elif self._stop_event.is_set():
                            break
                        else:
                            continue
                    else:
                        connected = True
    
                    if connected and not self._stop_event.is_set():
                        try:
                            if not _check_client_allowed(h_pipe):
                                print('Notallowed',file=sys.stderr)
                                kernel32.DisconnectNamedPipe(h_pipe)
                                continue
                        except Exception:
                            raise
                            kernel32.DisconnectNamedPipe(h_pipe)
                            continue
                    
                        _write_all_handle(h_pipe, self.payload)
                        try:
                            kernel32.FlushFileBuffers(h_pipe)
                        except Exception:
                            pass
                finally:
                    try:
                        kernel32.DisconnectNamedPipe(h_pipe)
                    except Exception:
                        pass
                    try:
                        kernel32.CloseHandle(h_pipe)
                    except Exception:
                        pass
        except BaseException as e:
            try:
                print(e, file=sys.stderr)
                import traceback
                traceback.print_exc()
            except BaseException:
                pass
            try:
                self._stop_event.set()
            except BaseException:
                pass
            os._exit(-1)


def _write_all_handle(handle: wintypes.HANDLE, data: bytes) -> None:
    total = 0
    while total < len(data):
        written = wintypes.DWORD()
        buf = ctypes.create_string_buffer(data[total:])
        if not kernel32.WriteFile(handle, buf, len(data) - total, ctypes.byref(written), None):
            err = ctypes.get_last_error()
            if err == 232:
                return
            _raise_last_error("WriteFile")
        if written.value <= 0:
            raise WinPipeError(errno.EIO, "short write")
        total += written.value


def read_named_pipe_line(pipe_name: str, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        h = kernel32.CreateFileW(
            pipe_name,
            GENERIC_READ,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h != INVALID_HANDLE_VALUE:
            break

        err = ctypes.get_last_error()
        if err == ERROR_PIPE_BUSY:
            if deadline is None:
                kernel32.WaitNamedPipeW(pipe_name, 5000)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for bootstrap pipe {pipe_name}")
                wait_ms = max(1, min(int(remaining * 1000), 5000))
                if not kernel32.WaitNamedPipeW(pipe_name, wait_ms):
                    if ctypes.get_last_error() == WAIT_TIMEOUT:
                        continue
                    _raise_last_error("WaitNamedPipeW")
            continue
        if err == ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(f"bootstrap pipe not found: {pipe_name}")
        if err == 233:
            raise FileNotFoundError(f"bootstrap pipe was force closed by the peer: {pipe_name}")
        if err == 5:
            raise FileNotFoundError(f"Access denied: {pipe_name}")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"bootstrap pipe not ready: {pipe_name}")
        time.sleep(0.05)

    try:
        chunks = []
        buf = ctypes.create_string_buffer(4096)
        while True:
            read = wintypes.DWORD()
            if not kernel32.ReadFile(h, buf, ctypes.sizeof(buf), ctypes.byref(read), None):
                err = ctypes.get_last_error()
                if err == ERROR_BROKEN_PIPE:
                    break
                if err == 233:
                    raise FileNotFoundError(f"bootstrap pipe was force closed by the peer")
                _raise_last_error("ReadFile")
            if read.value == 0:
                break
            chunk = buf.raw[: read.value]
            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"\n" in joined:
                break
        data = b"".join(chunks)
        line = data.split(b"\n", 1)[0]
        if not line:
            raise EOFError("empty bootstrap payload")
        return line
    finally:
        kernel32.CloseHandle(h)

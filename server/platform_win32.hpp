#pragma once

#ifdef _WIN32

#include "windows1.h"

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

#include "server.hpp"

namespace rmpsm {

constexpr auto DWORD_MAX = std::numeric_limits<DWORD>::max;

inline void close_fd(platform_handle_t& h) {
    if (h && h != INVALID_HANDLE_VALUE) {
        CloseHandle(h);
        h = nullptr;
    }
}

inline void set_nonblock(platform_handle_t) {
    // Windows 版本不使用 poll / O_NONBLOCK。
}

template <class T>
inline HANDLE to_handle(T v) {
    if constexpr (std::is_pointer_v<T>) {
        return reinterpret_cast<HANDLE>(v);
    } else {
        return reinterpret_cast<HANDLE>(static_cast<intptr_t>(v));
    }
}

template <class T>
inline void store_handle(T& dst, HANDLE h) {
    if constexpr (std::is_pointer_v<T>) {
        dst = reinterpret_cast<T>(h);
    } else {
        dst = static_cast<T>(reinterpret_cast<intptr_t>(h));
    }
}

template<typename T>
inline void reset_handle_field(T& field) {
    if constexpr (std::is_pointer_v<std::decay_t<decltype(field)>>) {
        field = nullptr;
    } else {
        field = 0;
    }
}

inline std::wstring utf8_to_wide(const std::string& s) {
    if (s.empty()) return std::wstring();

    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), nullptr, 0);
    if (n <= 0) {
        n = MultiByteToWideChar(CP_ACP, 0, s.data(), (int)s.size(), nullptr, 0);
        if (n <= 0) {
            return std::wstring();
        }
        std::wstring out((size_t)n, L'\0');
        MultiByteToWideChar(CP_ACP, 0, s.data(), (int)s.size(), out.data(), n);
        return out;
    }

    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int)s.size(), out.data(), n);
    return out;
}

inline std::wstring quote_windows_arg(const std::wstring& arg) {
    if (arg.empty()) {
        return L"\"\"";
    }

    bool need_quotes = false;
    for (wchar_t ch : arg) {
        if (ch == L' ' || ch == L'\t' || ch == L'\n' || ch == L'\v' || ch == L'"') {
            need_quotes = true;
            break;
        }
    }

    if (!need_quotes) {
        return arg;
    }

    std::wstring out;
    out.push_back(L'"');

    size_t backslashes = 0;
    for (wchar_t ch : arg) {
        if (ch == L'\\') {
            ++backslashes;
            continue;
        }

        if (ch == L'"') {
            out.append(backslashes * 2 + 1, L'\\');
            out.push_back(L'"');
            backslashes = 0;
            continue;
        }

        if (backslashes > 0) {
            out.append(backslashes, L'\\');
            backslashes = 0;
        }
        out.push_back(ch);
    }

    if (backslashes > 0) {
        out.append(backslashes * 2, L'\\');
    }

    out.push_back(L'"');
    return out;
}

inline std::wstring build_windows_command_line(const std::vector<std::string>& args) {
    std::wstring cmd;
    for (size_t i = 0; i < args.size(); ++i) {
        if (i > 0) cmd.push_back(L' ');
        cmd += quote_windows_arg(utf8_to_wide(args[i]));
    }
    return cmd;
}

struct Win32TaskRuntime {
    HANDLE process = nullptr;
    HANDLE stdin_write = nullptr;
    HANDLE stdout_read = nullptr;
    HANDLE stderr_read = nullptr;

    std::atomic<bool> stop{false};
    std::atomic<bool> stdin_close_requested{false};

    std::mutex stdin_mtx;
    std::condition_variable stdin_cv;
    std::deque<std::vector<uint8_t>> stdin_chunks;

    uint64_t taskId = 0;
};

enum class AsyncEventKind {
    StdinBytes,
    StdinEof,
    ChildBytes,
    ChildEof,
    ChildExit,
};

struct AsyncEvent {
    AsyncEventKind kind = AsyncEventKind::StdinBytes;
    uint64_t taskId = 0;
    uint32_t stream = 0; // 1 = stdout, 2 = stderr
    std::vector<uint8_t> data;
    uint32_t exitCode = 0;
};

struct Win32GlobalRuntime {
    std::mutex mtx;
    std::condition_variable cv;
    std::deque<AsyncEvent> events;
    std::unordered_map<uint64_t, std::shared_ptr<Win32TaskRuntime>> tasks;
    std::atomic<bool> stdin_started{false};
    std::atomic<bool> stdin_eof{false};
    std::atomic<bool> shutdown{false};
};

inline Win32GlobalRuntime& global_runtime() {
    static Win32GlobalRuntime rt;
    return rt;
}

inline void push_event(AsyncEvent ev) {
    auto& rt = global_runtime();
    {
        std::lock_guard<std::mutex> lk(rt.mtx);
        rt.events.push_back(std::move(ev));
    }
    rt.cv.notify_one();
}

inline std::shared_ptr<Win32TaskRuntime> get_task_runtime(uint64_t taskId) {
    auto& rt = global_runtime();
    std::lock_guard<std::mutex> lk(rt.mtx);
    auto it = rt.tasks.find(taskId);
    if (it == rt.tasks.end()) return {};
    return it->second;
}

inline void register_task_runtime(const std::shared_ptr<Win32TaskRuntime>& tr) {
    auto& rt = global_runtime();
    {
        std::lock_guard<std::mutex> lk(rt.mtx);
        rt.tasks[tr->taskId] = tr;
    }
    rt.cv.notify_one();
}

inline void unregister_task_runtime(uint64_t taskId) {
    auto& rt = global_runtime();
    std::lock_guard<std::mutex> lk(rt.mtx);
    rt.tasks.erase(taskId);
}

inline void start_detached_reader_thread(std::shared_ptr<Win32TaskRuntime> tr, HANDLE h, uint32_t stream) {
    std::thread([tr = std::move(tr), h, stream]() mutable {
        uint8_t buf[MAX_APP_PAYLOAD];
        while (!tr->stop.load(std::memory_order_relaxed)) {
            DWORD got = 0;
            BOOL ok = ReadFile(h, buf, (DWORD)sizeof(buf), &got, nullptr);
            if (ok) {
                if (got > 0) {
                    AsyncEvent ev;
                    ev.kind = AsyncEventKind::ChildBytes;
                    ev.taskId = tr->taskId;
                    ev.stream = stream;
                    ev.data.assign(buf, buf + got);
                    push_event(std::move(ev));
                    continue;
                }

                AsyncEvent ev;
                ev.kind = AsyncEventKind::ChildEof;
                ev.taskId = tr->taskId;
                ev.stream = stream;
                push_event(std::move(ev));
                break;
            }

            DWORD err = GetLastError();
            if (err == ERROR_BROKEN_PIPE || err == ERROR_HANDLE_EOF) {
                AsyncEvent ev;
                ev.kind = AsyncEventKind::ChildEof;
                ev.taskId = tr->taskId;
                ev.stream = stream;
                push_event(std::move(ev));
                break;
            }
            if (err == ERROR_OPERATION_ABORTED) {
                break;
            }

            AsyncEvent ev;
            ev.kind = AsyncEventKind::ChildEof;
            ev.taskId = tr->taskId;
            ev.stream = stream;
            push_event(std::move(ev));
            break;
        }

        close_fd(h);
    }).detach();
}

inline void start_detached_process_watch_thread(std::shared_ptr<Win32TaskRuntime> tr) {
    std::thread([tr = std::move(tr)]() {
        WaitForSingleObject(tr->process, INFINITE);

        DWORD code = 0;
        if (!GetExitCodeProcess(tr->process, &code)) {
            code = 0;
        }

        AsyncEvent ev;
        ev.kind = AsyncEventKind::ChildExit;
        ev.taskId = tr->taskId;
        ev.exitCode = static_cast<uint32_t>(code);
        push_event(std::move(ev));

        CloseHandle(tr->process);
    }).detach();
}

inline void start_detached_stdin_writer_thread(std::shared_ptr<Win32TaskRuntime> tr) {
    std::thread([tr = std::move(tr)]() mutable {
        while (!tr->stop.load(std::memory_order_relaxed)) {
            std::vector<uint8_t> chunk;

            {
                std::unique_lock<std::mutex> lk(tr->stdin_mtx);
                tr->stdin_cv.wait(lk, [&] {
                    return tr->stop.load(std::memory_order_relaxed) ||
                           tr->stdin_close_requested.load(std::memory_order_relaxed) ||
                           !tr->stdin_chunks.empty();
                });

                if (tr->stop.load(std::memory_order_relaxed)) {
                    break;
                }

                if (!tr->stdin_chunks.empty()) {
                    chunk = std::move(tr->stdin_chunks.front());
                    tr->stdin_chunks.pop_front();
                } else if (tr->stdin_close_requested.load(std::memory_order_relaxed)) {
                    break;
                } else {
                    continue;
                }
            }

            if (!chunk.empty() && tr->stdin_write) {
                size_t pos = 0;
                while (pos < chunk.size() && !tr->stop.load(std::memory_order_relaxed)) {
                    DWORD written = 0;
                    BOOL ok = WriteFile(
                        tr->stdin_write,
                        chunk.data() + pos,
                        (DWORD)std::min<size_t>(chunk.size() - pos, (size_t)DWORD_MAX),
                        &written,
                        nullptr
                    );

                    if (!ok) {
                        DWORD err = GetLastError();
                        if (err == ERROR_BROKEN_PIPE || err == ERROR_NO_DATA || err == ERROR_PIPE_NOT_CONNECTED) {
                            tr->stop.store(true, std::memory_order_relaxed);
                            break;
                        }
                        if (err == ERROR_OPERATION_ABORTED) {
                            tr->stop.store(true, std::memory_order_relaxed);
                            break;
                        }
                        tr->stop.store(true, std::memory_order_relaxed);
                        break;
                    }

                    if (written == 0) {
                        break;
                    }
                    pos += written;
                }
            }

            if (tr->stdin_close_requested.load(std::memory_order_relaxed) &&
                tr->stdin_chunks.empty()) {
                break;
            }
        }

        if (tr->stdin_write) {
            CloseHandle(tr->stdin_write);
            tr->stdin_write = nullptr;
        }
    }).detach();
}

inline void start_stdin_reader_thread() {
    auto& rt = global_runtime();
    bool expected = false;
    if (!rt.stdin_started.compare_exchange_strong(expected, true)) {
        return;
    }

    std::thread([]() {
        HANDLE hIn = GetStdHandle(STD_INPUT_HANDLE);
        if (hIn == nullptr || hIn == INVALID_HANDLE_VALUE) {
            global_runtime().stdin_eof.store(true, std::memory_order_relaxed);
            push_event(AsyncEvent{AsyncEventKind::StdinEof});
            return;
        }

        uint8_t buf[8192];
        while (!global_runtime().shutdown.load(std::memory_order_relaxed)) {
            DWORD got = 0;
            BOOL ok = ReadFile(hIn, buf, (DWORD)sizeof(buf), &got, nullptr);
            if (ok) {
                if (got > 0) {
                    AsyncEvent ev;
                    ev.kind = AsyncEventKind::StdinBytes;
                    ev.data.assign(buf, buf + got);
                    push_event(std::move(ev));
                    continue;
                }

                global_runtime().stdin_eof.store(true, std::memory_order_relaxed);
                push_event(AsyncEvent{AsyncEventKind::StdinEof});
                break;
            }

            DWORD err = GetLastError();
            if (err == ERROR_BROKEN_PIPE || err == ERROR_HANDLE_EOF) {
                global_runtime().stdin_eof.store(true, std::memory_order_relaxed);
                push_event(AsyncEvent{AsyncEventKind::StdinEof});
                break;
            }

            if (err == ERROR_OPERATION_ABORTED) {
                break;
            }

            global_runtime().stdin_eof.store(true, std::memory_order_relaxed);
            push_event(AsyncEvent{AsyncEventKind::StdinEof});
            break;
        }
    }).detach();
}

inline bool flush_transport(TransportState& tx, ReliableState& rel) {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    if (hOut == nullptr || hOut == INVALID_HANDLE_VALUE) {
        return false;
    }

    while (!tx.q.empty()) {
        TxItem& item = tx.q.front();

        DWORD written = 0;
        DWORD want = (DWORD)std::min<size_t>(item.bytes.size() - item.offset, (size_t)DWORD_MAX);

        BOOL ok = WriteFile(
            hOut,
            item.bytes.data() + item.offset,
            want,
            &written,
            nullptr
        );

        if (!ok) {
            DWORD err = GetLastError();
            if (err == ERROR_BROKEN_PIPE || err == ERROR_NO_DATA || err == ERROR_PIPE_NOT_CONNECTED) {
                return false;
            }
            if (err == ERROR_OPERATION_ABORTED) {
                return false;
            }
            return false;
        }

        if (written > 0) {
            item.offset += (size_t)written;
            if (item.offset == item.bytes.size()) {
                if (item.reliable && rel.inflight_exists && !rel.inflight_on_wire && item.seq == rel.inflight.seq) {
                    rel.inflight_on_wire = true;
                    rel.inflight_last_wire = std::chrono::steady_clock::now();
                }
                tx.q.pop_front();
            }
            continue;
        }

        return true;
    }

    return true;
}

inline bool process_peer_ack(ReliableState& rel, uint64_t ack) {
    if (!rel.inflight_exists) return false;
    if (ack == rel.inflight.seq) {
        rel.inflight_exists = false;
        rel.inflight_on_wire = false;
        rel.retries = 0;
        rel.inflight = ReliablePacket{};
        return true;
    }
    return false;
}

inline bool enqueue_tx_back(TransportState& tx, std::vector<uint8_t>&& bytes, bool reliable, uint64_t seq) {
    tx.q.push_back(TxItem{std::move(bytes), 0, reliable, seq});
    return true;
}

inline bool enqueue_tx_front(TransportState& tx, std::vector<uint8_t>&& bytes, bool reliable, uint64_t seq) {
    tx.q.push_front(TxItem{std::move(bytes), 0, reliable, seq});
    return true;
}

inline bool start_next_reliable_if_idle(
    ReliableState& rel,
    TransportState& tx,
    uint64_t peer_last_delivered_seq) {
    if (rel.inflight_exists || rel.waiting.empty()) return false;

    rel.inflight = std::move(rel.waiting.front());
    rel.waiting.pop_front();

    auto bytes = build_packet_bytes(
        rel.inflight.type,
        rel.inflight.requestId,
        rel.inflight.taskId,
        rel.inflight.seq,
        peer_last_delivered_seq,
        rel.inflight.payload.empty() ? nullptr : rel.inflight.payload.data(),
        rel.inflight.payload.size()
    );

    enqueue_tx_back(tx, std::move(bytes), true, rel.inflight.seq);
    rel.inflight_exists = true;
    rel.inflight_on_wire = false;
    rel.retries = 0;
    return true;
}

inline bool maybe_retransmit(
    ReliableState& rel,
    TransportState& tx,
    uint64_t peer_last_delivered_seq) {
    if (!rel.inflight_exists || !rel.inflight_on_wire) return false;

    auto now = std::chrono::steady_clock::now();
    if (now - rel.inflight_last_wire < RETRANSMIT_TIMEOUT) {
        return false;
    }

    auto bytes = build_packet_bytes(
        rel.inflight.type,
        rel.inflight.requestId,
        rel.inflight.taskId,
        rel.inflight.seq,
        peer_last_delivered_seq,
        rel.inflight.payload.empty() ? nullptr : rel.inflight.payload.data(),
        rel.inflight.payload.size()
    );

    enqueue_tx_front(tx, std::move(bytes), true, rel.inflight.seq);
    rel.inflight_on_wire = false;
    ++rel.retries;
    return true;
}

inline bool parse_payload_task_id(const std::vector<uint8_t>& payload, uint64_t& outTaskId) {
    if (payload.size() < 8) return false;
    outTaskId = read_u64_le(payload.data());
    return true;
}

inline bool flush_task_stdin(Task& t) {
    auto rt = get_task_runtime(t.taskId);
    if (!rt) return false;
    rt->stdin_cv.notify_one();
    return true;
}

inline bool spawn_task(const std::string& cmdline, uint64_t taskId, Task& outTask, int& outErr) {
    std::vector<std::string> args;
    if (!parse_command_line(cmdline, args, outErr)) {
        return false;
    }

    if (args.empty()) {
        outErr = EINVAL;
        return false;
    }

    std::wstring cmd = build_windows_command_line(args);
    if (cmd.empty()) {
        outErr = EINVAL;
        return false;
    }

    std::vector<wchar_t> cmdMutable(cmd.begin(), cmd.end());
    cmdMutable.push_back(L'\0');

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = nullptr;
    sa.bInheritHandle = TRUE;

    HANDLE childStdInRd = nullptr;
    HANDLE childStdInWr = nullptr;
    HANDLE childStdOutRd = nullptr;
    HANDLE childStdOutWr = nullptr;
    HANDLE childStdErrRd = nullptr;
    HANDLE childStdErrWr = nullptr;

    if (!CreatePipe(&childStdInRd, &childStdInWr, &sa, 0)) {
        outErr = (int)GetLastError();
        return false;
    }
    if (!CreatePipe(&childStdOutRd, &childStdOutWr, &sa, 0)) {
        outErr = (int)GetLastError();
        close_fd(childStdInRd);
        close_fd(childStdInWr);
        return false;
    }
    if (!CreatePipe(&childStdErrRd, &childStdErrWr, &sa, 0)) {
        outErr = (int)GetLastError();
        close_fd(childStdInRd);
        close_fd(childStdInWr);
        close_fd(childStdOutRd);
        close_fd(childStdOutWr);
        return false;
    }

    // 父进程不应继承这些端点。
    SetHandleInformation(childStdInWr, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(childStdOutRd, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(childStdErrRd, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = childStdInRd;
    si.hStdOutput = childStdOutWr;
    si.hStdError = childStdErrWr;

    PROCESS_INFORMATION pi{};
    BOOL ok = CreateProcessW(
        nullptr,
        cmdMutable.data(),
        nullptr,
        nullptr,
        TRUE,
        CREATE_NO_WINDOW,
        nullptr,
        nullptr,
        &si,
        &pi
    );

    // 这些是子进程端，父进程这里不再需要。
    close_fd(childStdInRd);
    close_fd(childStdOutWr);
    close_fd(childStdErrWr);

    if (!ok) {
        outErr = (int)GetLastError();
        close_fd(childStdInWr);
        close_fd(childStdOutRd);
        close_fd(childStdErrRd);
        return false;
    }

    auto tr = std::make_shared<Win32TaskRuntime>();
    tr->process = pi.hProcess;
    tr->stdin_write = childStdInWr;
    tr->stdout_read = childStdOutRd;
    tr->stderr_read = childStdErrRd;
    tr->taskId = taskId;

    CloseHandle(pi.hThread);

    // outTask 的句柄字段在 Windows 版中应当是可容纳 HANDLE 的类型。
    store_handle(outTask.stdin_fd, tr->stdin_write);
    store_handle(outTask.stdout_fd, tr->stdout_read);
    store_handle(outTask.stderr_fd, tr->stderr_read);

    outTask.taskId = taskId;
    outTask.pid = static_cast<pid_t>(pi.dwProcessId);
    outTask.child_exited = false;
    outTask.exited_normally = false;
    outTask.signaled = false;
    outTask.exit_code = 0;
    outTask.signal_no = 0;
    outTask.task_end_sent = false;
    outTask.stdin_queue.clear();
    outTask.stdin_offset = 0;
    outTask.stdin_close_requested = false;

    register_task_runtime(tr);
    start_detached_stdin_writer_thread(tr);
    start_detached_reader_thread(tr, tr->stdout_read, 1);
    start_detached_reader_thread(tr, tr->stderr_read, 2);
    start_detached_process_watch_thread(tr);

    return true;
}

inline void Server::close_task_all_fds(Task& t) {
    auto rt = get_task_runtime(t.taskId);
    if (rt) {
        rt->stop.store(true, std::memory_order_relaxed);
        rt->stdin_close_requested.store(true, std::memory_order_relaxed);
        rt->stdin_cv.notify_all();

        if (rt->process) {
            TerminateProcess(rt->process, 1);
        }

        if (rt->stdin_write) {
            CloseHandle(rt->stdin_write);
            rt->stdin_write = nullptr;
        }
        if (rt->stdout_read) {
            CloseHandle(rt->stdout_read);
            rt->stdout_read = nullptr;
        }
        if (rt->stderr_read) {
            CloseHandle(rt->stderr_read);
            rt->stderr_read = nullptr;
        }
        if (rt->process) {
            CloseHandle(rt->process);
            rt->process = nullptr;
        }

        unregister_task_runtime(t.taskId);
    }

    close_fd(t.stdin_fd);
    close_fd(t.stdout_fd);
    close_fd(t.stderr_fd);
}

inline void Server::handle_stop_server(const std::vector<uint8_t>& payload) {
    if (payload.size() < 1) {
        return;
    }

    uint8_t bForce = payload[0];
    if (bForce) {
        _exit(0);
    }

    stopping = true;

    for (auto& kv : tasks) {
        Task& t = kv.second;
        auto rt = get_task_runtime(t.taskId);
        if (rt) {
            rt->stop.store(true, std::memory_order_relaxed);
            rt->stdin_close_requested.store(true, std::memory_order_relaxed);
            rt->stdin_cv.notify_all();
            if (rt->process) {
                TerminateProcess(rt->process, 1);
            }
        }
        close_fd(t.stdin_fd);
        t.stdin_queue.clear();
        t.stdin_offset = 0;
    }
}

inline void Server::handle_create_task(uint64_t requestId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) {
    if (stopping) {
        queue_create_task_fail(requestId, ECANCELED);
        local_app_enqueued = true;
        return;
    }

    std::string cmdline(reinterpret_cast<const char*>(payload.data()), payload.size());
    uint64_t assignedId = 0;

    if (nextTaskId == 0) {
        queue_create_task_fail(requestId, EOVERFLOW);
        local_app_enqueued = true;
        return;
    }

    assignedId = nextTaskId;
    if (nextTaskId == UINT64_MAX) nextTaskId = 1;
    else nextTaskId++;

    Task t;
    int err = 0;
    if (!spawn_task(cmdline, assignedId, t, err)) {
        queue_create_task_fail(requestId, (uint64_t)err);
        local_app_enqueued = true;
        return;
    }

    tasks.emplace(assignedId, std::move(t));
    pid_to_task[tasks[assignedId].pid] = assignedId;
    queue_reply_u64(requestId, assignedId, assignedId);
    local_app_enqueued = true;
}

inline void Server::handle_kill_task(
    uint64_t requestId,
    uint64_t headerTaskId,
    const std::vector<uint8_t>& payload,
    bool& local_app_enqueued) {
    uint64_t taskId = headerTaskId;
    if (taskId == 0) {
        if (!parse_payload_task_id(payload, taskId)) {
            queue_reply_errno(requestId, 0, EINVAL);
            local_app_enqueued = true;
            return;
        }
    }

    Task* t = nullptr;
    auto it = tasks.find(taskId);
    if (it != tasks.end()) t = &it->second;

    if (!t || t->child_exited || t->pid <= 0) {
        queue_reply_errno(requestId, taskId, ESRCH);
        local_app_enqueued = true;
        return;
    }

    auto rt = get_task_runtime(taskId);
    if (!rt || !rt->process) {
        queue_reply_errno(requestId, taskId, ESRCH);
        local_app_enqueued = true;
        return;
    }

    if (!TerminateProcess(rt->process, 1)) {
        queue_reply_errno(requestId, taskId, (uint64_t)GetLastError());
        local_app_enqueued = true;
        return;
    }

    rt->stop.store(true, std::memory_order_relaxed);
    rt->stdin_close_requested.store(true, std::memory_order_relaxed);
    rt->stdin_cv.notify_all();

    close_fd(t->stdin_fd);
    t->stdin_queue.clear();
    t->stdin_offset = 0;
    queue_reply_empty(requestId, taskId);
    local_app_enqueued = true;
}

inline void Server::handle_input_data(
    uint64_t requestId,
    uint64_t taskId,
    const std::vector<uint8_t>& payload,
    bool& local_app_enqueued) {
    Task* t = nullptr;
    auto it = tasks.find(taskId);
    if (it != tasks.end()) t = &it->second;

    if (!t || t->child_exited || t->stdin_fd == nullptr) {
        queue_reply_errno(requestId, taskId, ESRCH);
        local_app_enqueued = true;
        return;
    }

    auto rt = get_task_runtime(taskId);
    if (!rt) {
        queue_reply_errno(requestId, taskId, ESRCH);
        local_app_enqueued = true;
        return;
    }

    // EOF：payload 为空
    if (payload.empty()) {
        rt->stdin_close_requested.store(true, std::memory_order_relaxed);
        rt->stdin_cv.notify_one();
        queue_reply_empty(requestId, taskId);
        local_app_enqueued = true;
        return;
    }

    {
        std::lock_guard<std::mutex> lk(rt->stdin_mtx);
        rt->stdin_chunks.push_back(payload);
    }
    rt->stdin_cv.notify_one();

    queue_reply_empty(requestId, taskId);
    local_app_enqueued = true;
}

inline bool Server::drain_task_pipe_once(Task&, int&, uint64_t) {
    // Windows 版本不依赖 poll，这个函数保留接口但不再使用。
    return false;
}

inline bool Server::drain_exited_tasks_once() {
    // Windows 版本由后台线程直接上报输出，不需要轮询 drain。
    return false;
}

inline void Server::finalize_ready_tasks() {
    std::vector<uint64_t> to_erase;
    to_erase.reserve(tasks.size());

    for (auto& kv : tasks) {
        Task& t = kv.second;
        if (t.child_exited &&
            t.stdout_fd == nullptr &&
            t.stderr_fd == nullptr &&
            !t.task_end_sent) {
            uint32_t exitCode = 0;
            uint8_t isSig = 0, sig = 0;
            encode_exit_info(t, exitCode, isSig, sig);
            queue_task_end(t.taskId, exitCode, isSig, sig);
            t.task_end_sent = true;
            to_erase.push_back(t.taskId);
        }
    }

    for (uint64_t id : to_erase) {
        auto it = tasks.find(id);
        if (it != tasks.end()) {
            pid_to_task.erase(it->second.pid);
            close_task_all_fds(it->second);
            tasks.erase(it);
        }
    }
}

inline void Server::progress_output_state() {
    if (!flush_transport(transport, reliable)) {
        _exit(1);
    }

    if (maybe_retransmit(reliable, transport, peer_last_delivered_seq)) {
        if (!flush_transport(transport, reliable)) {
            _exit(1);
        }
    }

    if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
        if (!flush_transport(transport, reliable)) {
            _exit(1);
        }
    }
}

inline void Server::handle_stdin_event() {
    // Windows 版本中，stdin 由后台线程读取并转成事件；这里保留接口，不直接读句柄。
}

inline void Server::reap_children() {
    // Windows 版本由后台 watcher 线程上报退出事件，这里保留接口为空实现。
}

inline void process_async_events(Server& server) {
    std::deque<AsyncEvent> local;

    {
        auto& rt = global_runtime();
        std::lock_guard<std::mutex> lk(rt.mtx);
        local.swap(rt.events);
    }

    for (auto& ev : local) {
        switch (ev.kind) {
            case AsyncEventKind::StdinBytes: {
                size_t old = server.rxbuf.size();
                server.rxbuf.resize(old + ev.data.size());
                if (!ev.data.empty()) {
                    std::memcpy(server.rxbuf.data() + old, ev.data.data(), ev.data.size());
                }

                while (true) {
                    DecodedPacket pkt;
                    size_t parse_pos = server.rxpos;
                    PacketParseResult res = try_parse_packet(server.rxbuf, parse_pos, pkt);
                    if (res == PacketParseResult::NeedMore) break;
                    if (res == PacketParseResult::Invalid) _exit(1);
                    server.rxpos = parse_pos;
                    compact_buffer(server.rxbuf, server.rxpos);
                    server.dispatch_packet(pkt);
                }
                break;
            }

            case AsyncEventKind::StdinEof:
                server.stdin_eof = true;
                break;

            case AsyncEventKind::ChildBytes: {
                auto it = server.tasks.find(ev.taskId);
                if (it == server.tasks.end()) break;
                Task& t = it->second;

                uint64_t outType = (ev.stream == 1) ? 6 : 7;
                server.queue_reliable_packet(outType, 0, t.taskId, ev.data.data(), ev.data.size());

                break;
            }

            case AsyncEventKind::ChildEof: {
                auto it = server.tasks.find(ev.taskId);
                if (it == server.tasks.end()) break;
                Task& t = it->second;

                if (ev.stream == 1) {
                    close_fd(t.stdout_fd);
                    auto rt = get_task_runtime(t.taskId);
                    if (rt) rt->stdout_read = nullptr;
                } else if (ev.stream == 2) {
                    close_fd(t.stderr_fd);
                    auto rt = get_task_runtime(t.taskId);
                    if (rt) rt->stderr_read = nullptr;
                }

                break;
            }

            case AsyncEventKind::ChildExit: {
                auto it = server.tasks.find(ev.taskId);
                if (it == server.tasks.end()) break;
                Task& t = it->second;

                mark_task_exited(t, true, ev.exitCode, false, 0);

                // 退出后不再接收 stdin
                close_fd(t.stdin_fd);
                t.stdin_queue.clear();
                t.stdin_offset = 0;

                auto rt = get_task_runtime(t.taskId);
                if (rt) {
                    rt->stop.store(true, std::memory_order_relaxed);
                    rt->stdin_close_requested.store(true, std::memory_order_relaxed);
                    rt->stdin_cv.notify_all();
                }

                break;
            }
        }
    }
}

inline bool runtime_has_work() {
    auto& rt = global_runtime();
    std::lock_guard<std::mutex> lk(rt.mtx);
    return !rt.events.empty();
}

inline int Server::run() {
    // Windows 下不需要 set_nonblock，也不依赖 poll。
    start_stdin_reader_thread();

    while (true) {
        progress_output_state();

        process_async_events(*this);

        finalize_ready_tasks();

        if (stdin_eof &&
            tasks.empty() &&
            transport.q.empty() &&
            !reliable.inflight_exists &&
            reliable.waiting.empty()) {
            break;
        }

        std::unique_lock<std::mutex> lk(global_runtime().mtx);
        global_runtime().cv.wait_for(lk, std::chrono::milliseconds(20), [] {
            return global_runtime().shutdown.load(std::memory_order_relaxed) ||
                   !global_runtime().events.empty();
        });
    }

    global_runtime().shutdown.store(true, std::memory_order_relaxed);
    process_async_events(*this);
    finalize_ready_tasks();
    return 0;
}

inline int run_server() {
    Server s;
    return s.run();
}

} // namespace rmpsm

#else
#error "This header is for Windows only."
#endif

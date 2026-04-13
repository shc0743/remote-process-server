#include <algorithm>
#include <errno.h>
#include <ctype.h>
#include <chrono>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <deque>
#include <limits>
#include <signal.h>
#include <string>
#include <unordered_map>
#include <vector>
#include <sys/types.h>
#include <fcntl.h>
#include <mutex>

static constexpr uint64_t MAGIC   = 0x961f132bdddc19b9ULL;
static constexpr uint64_t VERSION  = 3;
static constexpr uint64_t MAX_LEN  = (1ULL << 30); // 1 GiB 上限，防止恶意长度撑爆内存

static constexpr uint64_t TYPE_ACK_ONLY = 18446744073709551615ULL;      // 纯 ACK，不再要求 ACK
static constexpr size_t   MAX_APP_PAYLOAD = 32768;
static constexpr size_t MAX_RELIABLE_QUEUE = 256;
static constexpr auto     RETRANSMIT_TIMEOUT = std::chrono::milliseconds(500);

// Platform specific code
#ifdef _WIN32
#include <windows.h>
#include <io.h>

#define close _close
#define dup _dup
#define dup2 _dup2

inline int pipe(int pipefd[2]) {
    return _pipe(pipefd, 4096, _O_BINARY);
}

typedef DWORD pid_t;
typedef SOCKET socket_t;
typedef int fd_t;
typedef SSIZE_T ssize_t;

constexpr int STDIN_FILENO = 0;
constexpr int STDOUT_FILENO = 1;
constexpr int STDERR_FILENO = 2;

static std::wstring s2ws(const std::string& str) {
    using namespace std;
	wstring result;
	size_t len = MultiByteToWideChar(CP_ACP, 0, str.c_str(),
		(int)(str.size()), NULL, 0);
	if (len < 0) return result;
	wchar_t* buffer = new wchar_t[len + 1];
	if (buffer == NULL) return result;
	MultiByteToWideChar(CP_ACP, 0, str.c_str(), (int)(str.size()),
		buffer, (int)len);
	buffer[len] = '\0';
	result.append(buffer);
	delete[] buffer;
	return result;
}

#else
#include <poll.h>
#include <unistd.h>
#include <sys/wait.h>
#endif
// ----

using namespace std;

#ifndef _WIN32
#pragma comment(linker, "/subsystem:windows /entry:mainCRTStartup")
#endif

static void close_fd(int& fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

#ifdef _WIN32
static void close_handle(HANDLE& h) {
    if (h != nullptr && h != INVALID_HANDLE_VALUE) {
        CloseHandle(h);
        h = nullptr;
    }
}
#endif

static void set_nonblock(int fd) {
#ifndef _WIN32
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
#else
    HANDLE handle = (HANDLE)_get_osfhandle(fd);
    if (handle != INVALID_HANDLE_VALUE) {
        DWORD mode = PIPE_NOWAIT;
        SetNamedPipeHandleState(handle, &mode, NULL, NULL);
    }
#endif
}

static uint64_t read_u64_le(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v |= (uint64_t)p[i] << (i * 8);
    }
    return v;
}

static uint32_t read_u32_le(const uint8_t* p) {
    uint32_t v = 0;
    for (int i = 0; i < 4; ++i) {
        v |= (uint32_t)p[i] << (i * 8);
    }
    return v;
}

static void write_u64_le(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)((v >> (i * 8)) & 0xFF);
    }
}

static void write_u32_le(uint8_t* p, uint32_t v) {
    for (int i = 0; i < 4; ++i) {
        p[i] = (uint8_t)((v >> (i * 8)) & 0xFF);
    }
}

static bool parse_command_line(const std::string& cmd, std::vector<std::string>& out, int& err) {
    out.clear();

    enum class State { Normal, SingleQuote, DoubleQuote };
    State state = State::Normal;
    bool escape = false;
    std::string cur;

    auto flush_word = [&]() {
        if (!cur.empty()) {
            out.push_back(cur);
            cur.clear();
        }
    };

    for (size_t i = 0; i < cmd.size(); ++i) {
        unsigned char ch = (unsigned char)cmd[i];

        if (ch == '\0') {
            err = EINVAL;
            return false;
        }

        if (escape) {
            cur.push_back((char)ch);
            escape = false;
            continue;
        }

        if (state == State::SingleQuote) {
            if (ch == '\'') {
                state = State::Normal;
            } else {
                cur.push_back((char)ch);
            }
            continue;
        }

        if (state == State::DoubleQuote) {
            if (ch == '"') {
                state = State::Normal;
            } else if (ch == '\\') {
                escape = true;
            } else {
                cur.push_back((char)ch);
            }
            continue;
        }

        if (std::isspace(ch)) {
            flush_word();
            continue;
        }

        if (ch == '\'') {
            state = State::SingleQuote;
        } else if (ch == '"') {
            state = State::DoubleQuote;
        } else if (ch == '\\') {
            escape = true;
        } else {
            cur.push_back((char)ch);
        }
    }

    if (escape || state != State::Normal) {
        err = EINVAL;
        return false;
    }

    flush_word();

    if (out.empty()) {
        err = EINVAL;
        return false;
    }

    return true;
}

struct Task {
    uint64_t taskId = 0;
    pid_t pid = -1;
#ifdef _WIN32
    HANDLE hProcess = nullptr;
    uint32_t exit_code = 0;
#else
    int wait_status = 0;
#endif

    int stdin_fd = -1;
    int stdout_fd = -1;
    int stderr_fd = -1;

    bool child_exited = false;
    bool task_end_sent = false;

    std::vector<uint8_t> stdin_queue;
    size_t stdin_offset = 0;
    bool stdin_close_requested = false;
};

struct TxItem {
    std::vector<uint8_t> bytes;
    size_t offset = 0;
    bool reliable = false;
    uint64_t seq = 0;
};

struct ReliablePacket {
    uint64_t type = 0;
    uint64_t requestId = 0;
    uint64_t taskId = 0;
    uint64_t seq = 0;
    std::vector<uint8_t> payload;
};

struct ReliableState {
    std::deque<ReliablePacket> waiting;

    bool inflight_exists = false;
    bool inflight_on_wire = false;
    ReliablePacket inflight;
    std::chrono::steady_clock::time_point inflight_last_wire{};
    int retries = 0;
};

struct TransportState {
    std::deque<TxItem> q;
};

static void compact_buffer(std::vector<uint8_t>& buf, size_t& pos) {
    if (pos == 0) return;
    if (pos > buf.size()) {
        buf.clear();
        pos = 0;
        return;
    }
    if (pos == buf.size()) {
        buf.clear();
        pos = 0;
        return;
    }
    if (pos > 4096 && pos * 2 >= buf.size()) {
        buf.erase(buf.begin(), buf.begin() + (ptrdiff_t)pos);
        pos = 0;
    }
}

static void close_task_all_fds(Task& t) {
    close_fd(t.stdin_fd);
    close_fd(t.stdout_fd);
    close_fd(t.stderr_fd);
#ifdef _WIN32
    close_handle(t.hProcess);
#endif
}

static bool flush_task_stdin(Task& t) {
    if (t.stdin_fd < 0) return false;

    while (t.stdin_offset < t.stdin_queue.size()) {
        ssize_t n = write(t.stdin_fd,
                          t.stdin_queue.data() + t.stdin_offset,
                          t.stdin_queue.size() - t.stdin_offset);
        if (n > 0) {
            t.stdin_offset += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return true;
        }
        return false;
    }

    t.stdin_queue.clear();
    t.stdin_offset = 0;

    // 真正关闭 stdin（发送 EOF）
    if (t.stdin_close_requested) {
        close_fd(t.stdin_fd);
        t.stdin_close_requested = false;
    }

    return true;
}

static uint64_t next_seq_wrap(uint64_t v) {
    if (v == UINT64_MAX) return 1;
    if (v == 0) return 1;
    return v + 1;
}

static std::vector<uint8_t> build_packet_bytes(
    uint64_t type,
    uint64_t requestId,
    uint64_t taskId,
    uint64_t seq,
    uint64_t ack,
    const uint8_t* payload,
    size_t len
) {
    std::vector<uint8_t> out(72 + len);
    write_u64_le(out.data() + 0, MAGIC);
    write_u64_le(out.data() + 8, VERSION);
    write_u64_le(out.data() + 16, type);
    write_u64_le(out.data() + 24, (type == TYPE_ACK_ONLY) ? 0 : 1); // flags：预留
    write_u64_le(out.data() + 32, requestId);
    write_u64_le(out.data() + 40, taskId);
    write_u64_le(out.data() + 48, seq);
    write_u64_le(out.data() + 56, ack);
    write_u64_le(out.data() + 64, (uint64_t)len);
    if (len > 0) {
        std::memcpy(out.data() + 72, payload, len);
    }
    return out;
}

static bool enqueue_tx_back(TransportState& tx, std::vector<uint8_t>&& bytes, bool reliable, uint64_t seq) {
    tx.q.push_back(TxItem{std::move(bytes), 0, reliable, seq});
    return true;
}

static bool enqueue_tx_front(TransportState& tx, std::vector<uint8_t>&& bytes, bool reliable, uint64_t seq) {
    tx.q.push_front(TxItem{std::move(bytes), 0, reliable, seq});
    return true;
}

static bool flush_transport(TransportState& tx, ReliableState& rel) {
    while (!tx.q.empty()) {
        TxItem& item = tx.q.front();
        ssize_t n = write(STDOUT_FILENO, item.bytes.data() + item.offset, item.bytes.size() - item.offset);
        if (n > 0) {
            item.offset += (size_t)n;
            if (item.offset == item.bytes.size()) {
                if (item.reliable && rel.inflight_exists && !rel.inflight_on_wire && item.seq == rel.inflight.seq) {
                    rel.inflight_on_wire = true;
                    rel.inflight_last_wire = std::chrono::steady_clock::now();
                }
                tx.q.pop_front();
            }
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return true;
        }
        return false;
    }
    return true;
}

static bool process_peer_ack(ReliableState& rel, uint64_t ack) {
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

static bool start_next_reliable_if_idle(ReliableState& rel,
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

static bool maybe_retransmit(ReliableState& rel,
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

    // 重传优先级高，放到最前面
    enqueue_tx_front(tx, std::move(bytes), true, rel.inflight.seq);
    rel.inflight_on_wire = false;
    ++rel.retries;
    return true;
}

static bool parse_payload_task_id(const std::vector<uint8_t>& payload, uint64_t& outTaskId) {
    if (payload.size() < 8) return false;
    outTaskId = read_u64_le(payload.data());
    return true;
}

static bool spawn_task(const std::string& cmdline, uint64_t taskId, Task& outTask, int& outErr) {
    std::vector<std::string> args;
    if (!parse_command_line(cmdline, args, outErr)) {
        return false;
    }

    std::vector<char*> argv;
    argv.reserve(args.size() + 1);
    for (auto& s : args) {
        argv.push_back(const_cast<char*>(s.c_str()));
    }
    argv.push_back(nullptr);

    int inpipe[2];
    int outpipe[2];
    int errpipe[2];

    if (pipe(inpipe) == -1) {
        outErr = errno;
        return false;
    }
    if (pipe(outpipe) == -1) {
        outErr = errno;
        close(inpipe[0]);
        close(inpipe[1]);
        return false;
    }
    if (pipe(errpipe) == -1) {
        outErr = errno;
        close(inpipe[0]);
        close(inpipe[1]);
        close(outpipe[0]);
        close(outpipe[1]);
        return false;
    }

#ifndef _WIN32
    pid_t pid = fork();
    if (pid == -1) {
        outErr = errno;
        close(inpipe[0]); close(inpipe[1]);
        close(outpipe[0]); close(outpipe[1]);
        close(errpipe[0]); close(errpipe[1]);
        return false;
    }

    if (pid == 0) {
        // child
        close(inpipe[1]);
        close(outpipe[0]);
        close(errpipe[0]);

        if (dup2(inpipe[0], STDIN_FILENO) == -1) _exit(126);
        if (dup2(outpipe[1], STDOUT_FILENO) == -1) _exit(126);
        if (dup2(errpipe[1], STDERR_FILENO) == -1) _exit(126);

        close(inpipe[0]);
        close(outpipe[1]);
        close(errpipe[1]);

        execvp(argv[0], argv.data());
        _exit(127);
    }
#else
    DWORD pid = 0;
    {
        STARTUPINFOW si{};
        PROCESS_INFORMATION pi{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
        si.wShowWindow = SW_HIDE;
        si.hStdInput = (HANDLE)_get_osfhandle(inpipe[0]);
        si.hStdOutput = (HANDLE)_get_osfhandle(outpipe[1]);
        si.hStdError = (HANDLE)_get_osfhandle(errpipe[1]);

        wstring cmd = s2ws(cmdline);
        vector<WCHAR> cmd_buf(cmd.begin(), cmd.end());
        cmd_buf.push_back(L'\0');

        wstring app = s2ws(argv[0]);
        if (!CreateProcessW(nullptr, cmd_buf.data(), nullptr, nullptr, TRUE, 0, nullptr, nullptr, &si, &pi)) {
            outErr = GetLastError();
            close(inpipe[0]); close(inpipe[1]);
            close(outpipe[0]); close(outpipe[1]);
            close(errpipe[0]); close(errpipe[1]);
            return false;
        }
        pid = pi.dwProcessId;
        outTask.hProcess = pi.hProcess;
        CloseHandle(pi.hThread);
    }
#endif

    // parent
    close(inpipe[0]);
    close(outpipe[1]);
    close(errpipe[1]);

    set_nonblock(inpipe[1]);
    set_nonblock(outpipe[0]);
    set_nonblock(errpipe[0]);

    outTask.taskId = taskId;
    outTask.pid = pid;
    outTask.stdin_fd = inpipe[1];
    outTask.stdout_fd = outpipe[0];
    outTask.stderr_fd = errpipe[0];
    outTask.child_exited = false;
    outTask.task_end_sent = false;
#ifdef _WIN32
    outTask.exit_code = 0;
#else
    outTask.wait_status = 0;
#endif
    outTask.stdin_queue.clear();
    outTask.stdin_offset = 0;
    outTask.stdin_close_requested = false;

    return true;
}

static void mark_task_exited(Task& t, int status) {
    t.child_exited = true;
#ifndef _WIN32
    t.wait_status = status;
#else
    t.exit_code = (uint32_t)status;
#endif
}

static bool encode_exit_info(const Task& t, uint32_t& exitCode, uint8_t& isSig, uint8_t& sig) {
#ifdef _WIN32
    if (!t.child_exited) {
        return false;
    }
    exitCode = t.exit_code;
    isSig = 0;
    sig = 0;
    return true;
#else
    if (WIFEXITED(t.wait_status)) {
        exitCode = (uint32_t)WEXITSTATUS(t.wait_status);
        isSig = 0;
        sig = 0;
        return true;
    }
    if (WIFSIGNALED(t.wait_status)) {
        exitCode = 0;
        isSig = 1;
        sig = (uint8_t)WTERMSIG(t.wait_status);
        return true;
    }
    exitCode = 0;
    isSig = 0;
    sig = 0;
    return false;
#endif
}

#ifdef _WIN32
#endif

#if 0
int main() {
#ifndef _WIN32
    signal(SIGPIPE, SIG_IGN);
#endif
    set_nonblock(STDIN_FILENO);
    set_nonblock(STDOUT_FILENO);

    std::unordered_map<uint64_t, Task> tasks;
    std::unordered_map<pid_t, uint64_t> pid_to_task;

    uint64_t nextTaskId = 1;
    uint64_t nextTxSeq = 1;

    uint64_t peer_expected_seq = 1;
    uint64_t peer_last_delivered_seq = 0;

    bool stdin_eof = false;
    bool stopping = false;

    std::vector<uint8_t> rxbuf;
    size_t rxpos = 0;
    std::vector<uint8_t> tmp(8192);

    TransportState transport;
    ReliableState reliable;

    auto send_protocol_error_and_exit = [&]() -> void {
        _exit(1);
    };

    auto queue_reliable_packet = [&](uint64_t type,
                                     uint64_t requestId,
                                     uint64_t taskId,
                                     const uint8_t* payload,
                                     size_t len) {
        ReliablePacket pkt;
        pkt.type = type;
        pkt.requestId = requestId;
        pkt.taskId = taskId;
        pkt.seq = nextTxSeq;
        nextTxSeq = next_seq_wrap(nextTxSeq);
        if (len > 0 && payload != nullptr) {
            pkt.payload.assign(payload, payload + len);
        }
        reliable.waiting.push_back(std::move(pkt));
    };

    auto queue_reply_empty = [&](uint64_t requestId, uint64_t taskId) {
        queue_reliable_packet(0, requestId, taskId, nullptr, 0);
    };

    auto queue_reply_errno = [&](uint64_t requestId, uint64_t taskId, uint64_t err) {
        uint8_t payload[8];
        write_u64_le(payload, err);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    };

    auto queue_reply_u64 = [&](uint64_t requestId, uint64_t taskId, uint64_t value) {
        uint8_t payload[8];
        write_u64_le(payload, value);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    };

    auto queue_create_task_fail = [&](uint64_t requestId, uint64_t err) {
        uint8_t payload[16];
        write_u64_le(payload + 0, 0);
        write_u64_le(payload + 8, err);
        queue_reliable_packet(0, requestId, 0, payload, 16);
    };

    auto queue_task_end = [&](uint64_t taskId, uint32_t exitCode, uint8_t isSignalTerminated, uint8_t signalNo) {
        uint8_t payload[6];
        write_u32_le(payload + 0, exitCode);
        payload[4] = isSignalTerminated;
        payload[5] = signalNo;
        queue_reliable_packet(4, 0, taskId, payload, 6);
    };

    auto queue_ack_only = [&]() {
        if (peer_last_delivered_seq == 0) return;
        auto bytes = build_packet_bytes(TYPE_ACK_ONLY, 0, 0, 0, peer_last_delivered_seq, nullptr, 0);
        // // ACK 包尽量优先发送；如果前面已经有“部分写入”的包，就不要插到前面破坏顺序
        // if (!transport.q.empty() && transport.q.front().offset != 0) {
            // enqueue_tx_back(transport, std::move(bytes), false, 0);
        // } else {
            // enqueue_tx_front(transport, std::move(bytes), false, 0);
        // }
        enqueue_tx_front(transport, std::move(bytes), false, 0);
    };

    auto handle_stop_server = [&](const std::vector<uint8_t>& payload) -> void {
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
            if (!t.child_exited && t.pid > 0) {
#ifndef _WIN32
                kill(t.pid, SIGKILL);
#else
                TerminateProcess(t.hProcess, 1);
#endif
            }
            close_fd(t.stdin_fd);
            t.stdin_queue.clear();
            t.stdin_offset = 0;
        }
    };

    auto handle_create_task = [&](uint64_t requestId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
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
    };

    auto handle_kill_task = [&](uint64_t requestId, uint64_t headerTaskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
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

#ifndef _WIN32
        if (kill(t->pid, SIGKILL) == -1) {
#else
        if (!TerminateProcess(t->hProcess, 1)) {
#endif
            queue_reply_errno(requestId, taskId, (uint64_t)errno);
            local_app_enqueued = true;
            return;
        }

        close_fd(t->stdin_fd);
        t->stdin_queue.clear();
        t->stdin_offset = 0;

        queue_reply_empty(requestId, taskId);
        local_app_enqueued = true;
    };

    auto handle_input_data = [&](uint64_t requestId, uint64_t taskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
        Task* t = nullptr;
        auto it = tasks.find(taskId);
        if (it != tasks.end()) t = &it->second;

        if (!t || t->child_exited || t->stdin_fd < 0) {
            queue_reply_errno(requestId, taskId, ESRCH);
            local_app_enqueued = true;
            return;
        }

        // EOF：payload 为空
        if (payload.empty()) {
            t->stdin_close_requested = true;

            if (!flush_task_stdin(*t)) {
                queue_reply_errno(requestId, taskId, (uint64_t)errno);
                local_app_enqueued = true;
                return;
            }

            queue_reply_empty(requestId, taskId);
            local_app_enqueued = true;
            return;
        }

        size_t oldSize = t->stdin_queue.size();
        t->stdin_queue.resize(oldSize + payload.size());
        std::memcpy(t->stdin_queue.data() + oldSize, payload.data(), payload.size());

        if (!flush_task_stdin(*t)) {
            queue_reply_errno(requestId, taskId, (uint64_t)errno);
            local_app_enqueued = true;
            return;
        }

        queue_reply_empty(requestId, taskId);
        local_app_enqueued = true;
    };

    auto handle_query_version = [&](uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) -> void {
        static const char ver[] = "3.0.0";
        queue_reliable_packet(0, requestId, taskId, reinterpret_cast<const uint8_t*>(ver), sizeof(ver) - 1);
        local_app_enqueued = true;
    };

    auto handle_unknown = [&](uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) -> void {
        queue_reply_errno(requestId, taskId, EINVAL);
        local_app_enqueued = true;
    };

    auto drain_task_pipe_once = [&](Task& t, int& fd, uint64_t outType) -> bool {
        if (fd < 0) return false;
    
        // 限流（防止爆队列）
        if (reliable.waiting.size() >= MAX_RELIABLE_QUEUE || reliable.inflight_exists) {
            return false;
        }
    
        uint8_t buf[MAX_APP_PAYLOAD];
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n > 0) {
            queue_reliable_packet(outType, 0, t.taskId, buf, (size_t)n);
            return true;
        }
        if (n == 0) {
            close_fd(fd);
            return false;
        }
        if (errno == EINTR) return false;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return false;
        close_fd(fd);
        return false;
    };

    auto drain_exited_tasks_once = [&]() -> bool {
        for (auto& kv : tasks) {
            Task& t = kv.second;
            if (!t.child_exited) continue;

            if (t.stdout_fd >= 0) {
                if (drain_task_pipe_once(t, t.stdout_fd, 6)) return true;
            }
            if (t.stderr_fd >= 0) {
                if (drain_task_pipe_once(t, t.stderr_fd, 7)) return true;
            }
        }
        return false;
    };

    auto finalize_ready_tasks = [&]() -> void {
        std::vector<uint64_t> to_erase;
        to_erase.reserve(tasks.size());

        for (auto& kv : tasks) {
            Task& t = kv.second;
            if (t.child_exited &&
                t.stdout_fd < 0 &&
                t.stderr_fd < 0 &&
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
    };

    auto progress_output_state = [&]() -> void {
        if (!flush_transport(transport, reliable)) {
            send_protocol_error_and_exit();
        }

        if (maybe_retransmit(reliable, transport, peer_last_delivered_seq)) {
            if (!flush_transport(transport, reliable)) {
                send_protocol_error_and_exit();
            }
        }

        if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
            if (!flush_transport(transport, reliable)) {
                send_protocol_error_and_exit();
            }
        }
    };

    while (true) {
        progress_output_state();

        reap_children:
        {
#ifdef _WIN32
            for (auto& kv : tasks)
            {
                Task& t = kv.second;
                if (t.child_exited || t.hProcess == nullptr)
                    continue;
                DWORD exitCode = 0;
                if (WaitForSingleObject(t.hProcess, 0) == WAIT_OBJECT_0)
                {
                    GetExitCodeProcess(t.hProcess, &exitCode);
                    t.child_exited = true;
                    t.exit_code = (uint32_t)exitCode;
                    CloseHandle(t.hProcess);
                    t.hProcess = nullptr;
                    close_fd(t.stdin_fd);
                    t.stdin_queue.clear();
                    t.stdin_offset = 0;
                }
            }
#else
            while (true)
            {
                int status = 0;
                pid_t pid = waitpid(-1, &status, WNOHANG);
                if (pid > 0)
                {
                    auto mp = pid_to_task.find(pid);
                    if (mp != pid_to_task.end())
                    {
                        auto it = tasks.find(mp->second);
                        if (it != tasks.end())
                        {
                            mark_task_exited(it->second, status);
                            close_fd(it->second.stdin_fd);
                            it->second.stdin_queue.clear();
                            it->second.stdin_offset = 0;
                        }
                    }
                    continue;
                }
                if (pid == 0)
                    break;
                if (pid < 0 && errno == EINTR)
                    continue;
                break;
            }
#endif
        }

        bool transport_busy = !transport.q.empty() || reliable.inflight_exists || !reliable.waiting.empty();
        if (!transport_busy) {
            // 只有在输出通道空闲时，才继续读取子进程 stdout/stderr，避免堆积太多可靠包
            while (drain_exited_tasks_once()) {
                progress_output_state();
                transport_busy = !transport.q.empty() || reliable.inflight_exists || !reliable.waiting.empty();
                if (transport_busy) break;
            }
        }

        finalize_ready_tasks();

        if (stdin_eof &&
            tasks.empty() &&
            transport.q.empty() &&
            !reliable.inflight_exists &&
            reliable.waiting.empty()) {
            break;
        }

        std::vector<pollfd> fds;
        fds.reserve(2 + tasks.size() * 3);

        enum class Kind {
            Stdin,
            ServerTx,
            TaskStdout,
            TaskStderr,
            TaskStdin
        };

        struct Item {
            Kind kind;
            uint64_t taskId;
        };

        std::vector<Item> items;
        items.reserve(2 + tasks.size() * 3);

        if (!stdin_eof) {
            pollfd p{};
            p.fd = STDIN_FILENO;
            p.events = POLLIN;
            fds.push_back(p);
            items.push_back({Kind::Stdin, 0});
        }

        if (!transport.q.empty()) {
            pollfd p{};
            p.fd = STDOUT_FILENO;
            p.events = POLLOUT | POLLERR | POLLHUP;
            fds.push_back(p);
            items.push_back({Kind::ServerTx, 0});
        }

        bool allow_task_output_read = reliable.waiting.size() < MAX_RELIABLE_QUEUE / 2;

        if (allow_task_output_read) {
            for (auto& kv : tasks) {
                Task& t = kv.second;

                if (t.stdout_fd >= 0) {
                    pollfd p{};
                    p.fd = t.stdout_fd;
                    p.events = POLLIN | POLLHUP | POLLERR;
                    fds.push_back(p);
                    items.push_back({Kind::TaskStdout, t.taskId});
                }

                if (t.stderr_fd >= 0) {
                    pollfd p{};
                    p.fd = t.stderr_fd;
                    p.events = POLLIN | POLLHUP | POLLERR;
                    fds.push_back(p);
                    items.push_back({Kind::TaskStderr, t.taskId});
                }
            }
        }

        for (auto& kv : tasks) {
            Task& t = kv.second;
            if (t.stdin_fd >= 0 && t.stdin_queue.size() > t.stdin_offset) {
                pollfd p{};
                p.fd = t.stdin_fd;
                p.events = POLLOUT | POLLERR | POLLHUP;
                fds.push_back(p);
                items.push_back({Kind::TaskStdin, t.taskId});
            }
        }

        if (fds.empty()) {
            break;
        }

        int pret =
            poll(fds.data(), fds.size(), 50);
        if (pret < 0) {
            if (errno == EINTR) continue;
            _exit(-1);
        }

        bool batch_ack_needed = false;
        bool output_generation_blocked = !allow_task_output_read;

        for (size_t i = 0; i < fds.size(); ++i) {
            if (fds[i].revents == 0) continue;
            const Item& item = items[i];

            if (item.kind == Kind::Stdin) {
                while (true) {
                    ssize_t n = read(STDIN_FILENO, tmp.data(), tmp.size());
                    if (n > 0) {
                        size_t old = rxbuf.size();
                        rxbuf.resize(old + (size_t)n);
                        std::memcpy(rxbuf.data() + old, tmp.data(), (size_t)n);
                        continue;
                    }
                    if (n == 0) {
                        stdin_eof = true;
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    _exit(1);
                }

                while (true) {
                    if (rxbuf.size() - rxpos < 72) break;

                    const uint8_t* base = rxbuf.data() + rxpos;
                    uint64_t magic = read_u64_le(base + 0);
                    uint64_t version = read_u64_le(base + 8);
                    uint64_t type = read_u64_le(base + 16);
                    uint64_t flags = read_u64_le(base + 24);
                    (void)flags;
                    uint64_t requestId = read_u64_le(base + 32);
                    uint64_t taskId = read_u64_le(base + 40);
                    uint64_t seq = read_u64_le(base + 48);
                    uint64_t ack = read_u64_le(base + 56);
                    uint64_t length = read_u64_le(base + 64);

                    if (magic != MAGIC || version != VERSION) {
                        _exit(1);
                    }
                    if (length > MAX_LEN || length > (uint64_t)(std::numeric_limits<size_t>::max)()) {
                        _exit(1);
                    }
                    if (rxbuf.size() - rxpos < 72 + (size_t)length) break;

                    std::vector<uint8_t> payload;
                    payload.resize((size_t)length);
                    if (length > 0) {
                        std::memcpy(payload.data(), base + 72, (size_t)length);
                    }

                    rxpos += 72 + (size_t)length;
                    compact_buffer(rxbuf, rxpos);

                    if (type == TYPE_ACK_ONLY) {
                        process_peer_ack(reliable, ack);
                        if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
                            progress_output_state();
                        }
                        continue;
                    }

                    bool local_app_enqueued = false;
                    bool inflight_before = reliable.inflight_exists;

                    // 先处理对端对我方包的 ACK（piggyback ACK）
                    process_peer_ack(reliable, ack);

                    bool accepted = false;
                    bool is_new = false;
                    
                    if (seq == peer_expected_seq) {
                        peer_expected_seq++;
                        peer_last_delivered_seq = seq;
                        accepted = true;
                        is_new = true;   // ⭐ 新包
                    } else if (seq < peer_expected_seq) {
                        accepted = true;  // ⭐ 旧包（重传）
                        is_new = false;
                    } else {
                        accepted = false;
                    }

                    if (accepted && is_new) {
                        switch (type) {
                            case 0:
                                // reply：客户端发来的 reply 直接忽略
                                break;

                            case 1:
                                handle_stop_server(payload);
                                break;

                            case 2:
                                handle_create_task(requestId, payload, local_app_enqueued);
                                break;

                            case 3:
                                handle_kill_task(requestId, taskId, payload, local_app_enqueued);
                                break;

                            case 5:
                                handle_input_data(requestId, taskId, payload, local_app_enqueued);
                                break;

                            case 255:
                                handle_query_version(requestId, taskId, local_app_enqueued);
                                break;

                            case 4:
                            case 6:
                            case 7:
                                // 这些是服务器发送给客户端的消息，忽略客户端发来的同类包
                                break;

                            default:
                                handle_unknown(requestId, taskId, local_app_enqueued);
                                break;
                        }
                    }

                    // 如果在这次处理里生成了新的可靠应用包，尽量马上开始发送
                    if (local_app_enqueued) {
                        if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
                            progress_output_state();
                        }
                    }

                    // // 只要收到的是有效应用包，就需要 ACK；如果当前已经有在飞的包，
                    // // 或者这次没有生成可 piggyback 的新应用包，就发一个纯 ACK。
                    // if (accepted && (inflight_before || !local_app_enqueued)) {
                        // batch_ack_needed = true;
                    // }
                    if (accepted) {
                        queue_ack_only();
                        progress_output_state();  // ⭐ 立即 flush
                    }
                }
            } else if (item.kind == Kind::ServerTx) {
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
            } else if (item.kind == Kind::TaskStdout || item.kind == Kind::TaskStderr) {
                if (output_generation_blocked) {
                    continue;
                }
                auto it = tasks.find(item.taskId);
                if (it == tasks.end()) continue;
                Task& t = it->second;
                int* target_fd = (item.kind == Kind::TaskStdout) ? &t.stdout_fd : &t.stderr_fd;
                uint64_t outType = (item.kind == Kind::TaskStdout) ? 6 : 7;

                uint8_t buf[MAX_APP_PAYLOAD];
                ssize_t n = read(*target_fd, buf, sizeof(buf));
                if (n > 0) {
                    queue_reliable_packet(outType, 0, t.taskId, buf, (size_t)n);
                    output_generation_blocked = true;
                    progress_output_state();
                } else if (n == 0) {
                    close_fd(*target_fd);
                } else if (errno == EINTR) {
                    // 忽略
                } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // 忽略
                } else {
                    close_fd(*target_fd);
                }
            } else if (item.kind == Kind::TaskStdin) {
                auto it = tasks.find(item.taskId);
                if (it == tasks.end()) continue;
                Task& t = it->second;
                if (t.stdin_fd >= 0 && t.stdin_offset < t.stdin_queue.size()) {
                    if (!flush_task_stdin(t)) {
                        // 子进程可能已经退出，直接关闭输入端即可
                        close_fd(t.stdin_fd);
                        t.stdin_queue.clear();
                        t.stdin_offset = 0;
                    }
                }
            }
        }

        // if (batch_ack_needed) {
            // queue_ack_only();
            // progress_output_state();
        // }

        finalize_ready_tasks();

        if (stdin_eof &&
            tasks.empty() &&
            transport.q.empty() &&
            !reliable.inflight_exists &&
            reliable.waiting.empty()) {
            break;
        }

        if (stopping && tasks.empty() &&
            transport.q.empty() &&
            !reliable.inflight_exists &&
            reliable.waiting.empty()) {
            break;
        }
    }

    return 0;
}
#endif

int main() {
#ifndef _WIN32
    signal(SIGPIPE, SIG_IGN);
#endif

    std::unordered_map<uint64_t, Task> tasks;
    std::unordered_map<pid_t, uint64_t> pid_to_task;

    uint64_t nextTaskId = 1;
    uint64_t nextTxSeq = 1;

    uint64_t peer_expected_seq = 1;
    uint64_t peer_last_delivered_seq = 0;

    bool stdin_eof = false;
    bool stopping = false;

    std::vector<uint8_t> rxbuf;
    size_t rxpos = 0;

    TransportState transport;
    ReliableState reliable;

    std::mutex global_lock;

    auto progress_output_state = [&]() {
        if (!flush_transport(transport, reliable)) _exit(1);

        if (maybe_retransmit(reliable, transport, peer_last_delivered_seq)) {
            if (!flush_transport(transport, reliable)) _exit(1);
        }

        if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
            if (!flush_transport(transport, reliable)) _exit(1);
        }
    };

        auto send_protocol_error_and_exit = [&]() -> void {
        _exit(1);
    };

    auto queue_reliable_packet = [&](uint64_t type,
                                     uint64_t requestId,
                                     uint64_t taskId,
                                     const uint8_t* payload,
                                     size_t len) {
        ReliablePacket pkt;
        pkt.type = type;
        pkt.requestId = requestId;
        pkt.taskId = taskId;
        pkt.seq = nextTxSeq;
        nextTxSeq = next_seq_wrap(nextTxSeq);
        if (len > 0 && payload != nullptr) {
            pkt.payload.assign(payload, payload + len);
        }
        reliable.waiting.push_back(std::move(pkt));
    };

    auto queue_reply_empty = [&](uint64_t requestId, uint64_t taskId) {
        queue_reliable_packet(0, requestId, taskId, nullptr, 0);
    };

    auto queue_reply_errno = [&](uint64_t requestId, uint64_t taskId, uint64_t err) {
        uint8_t payload[8];
        write_u64_le(payload, err);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    };

    auto queue_reply_u64 = [&](uint64_t requestId, uint64_t taskId, uint64_t value) {
        uint8_t payload[8];
        write_u64_le(payload, value);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    };

    auto queue_create_task_fail = [&](uint64_t requestId, uint64_t err) {
        uint8_t payload[16];
        write_u64_le(payload + 0, 0);
        write_u64_le(payload + 8, err);
        queue_reliable_packet(0, requestId, 0, payload, 16);
    };

    auto queue_task_end = [&](uint64_t taskId, uint32_t exitCode, uint8_t isSignalTerminated, uint8_t signalNo) {
        uint8_t payload[6];
        write_u32_le(payload + 0, exitCode);
        payload[4] = isSignalTerminated;
        payload[5] = signalNo;
        queue_reliable_packet(4, 0, taskId, payload, 6);
    };

    auto queue_ack_only = [&]() {
        if (peer_last_delivered_seq == 0) return;
        auto bytes = build_packet_bytes(TYPE_ACK_ONLY, 0, 0, 0, peer_last_delivered_seq, nullptr, 0);
        // // ACK 包尽量优先发送；如果前面已经有“部分写入”的包，就不要插到前面破坏顺序
        // if (!transport.q.empty() && transport.q.front().offset != 0) {
            // enqueue_tx_back(transport, std::move(bytes), false, 0);
        // } else {
            // enqueue_tx_front(transport, std::move(bytes), false, 0);
        // }
        enqueue_tx_front(transport, std::move(bytes), false, 0);
    };

    auto handle_stop_server = [&](const std::vector<uint8_t>& payload) -> void {
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
            if (!t.child_exited && t.pid > 0) {
#ifndef _WIN32
                kill(t.pid, SIGKILL);
#else
                TerminateProcess(t.hProcess, 1);
#endif
            }
            close_fd(t.stdin_fd);
            t.stdin_queue.clear();
            t.stdin_offset = 0;
        }
    };

    auto handle_create_task = [&](uint64_t requestId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
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
    };

    auto handle_kill_task = [&](uint64_t requestId, uint64_t headerTaskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
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

#ifndef _WIN32
        if (kill(t->pid, SIGKILL) == -1) {
#else
        if (!TerminateProcess(t->hProcess, 1)) {
#endif
            queue_reply_errno(requestId, taskId, (uint64_t)errno);
            local_app_enqueued = true;
            return;
        }

        close_fd(t->stdin_fd);
        t->stdin_queue.clear();
        t->stdin_offset = 0;

        queue_reply_empty(requestId, taskId);
        local_app_enqueued = true;
    };

    auto handle_input_data = [&](uint64_t requestId, uint64_t taskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) -> void {
        Task* t = nullptr;
        auto it = tasks.find(taskId);
        if (it != tasks.end()) t = &it->second;

        if (!t || t->child_exited || t->stdin_fd < 0) {
            queue_reply_errno(requestId, taskId, ESRCH);
            local_app_enqueued = true;
            return;
        }

        // EOF：payload 为空
        if (payload.empty()) {
            t->stdin_close_requested = true;

            if (!flush_task_stdin(*t)) {
                queue_reply_errno(requestId, taskId, (uint64_t)errno);
                local_app_enqueued = true;
                return;
            }

            queue_reply_empty(requestId, taskId);
            local_app_enqueued = true;
            return;
        }

        size_t oldSize = t->stdin_queue.size();
        t->stdin_queue.resize(oldSize + payload.size());
        std::memcpy(t->stdin_queue.data() + oldSize, payload.data(), payload.size());

        if (!flush_task_stdin(*t)) {
            queue_reply_errno(requestId, taskId, (uint64_t)errno);
            local_app_enqueued = true;
            return;
        }

        queue_reply_empty(requestId, taskId);
        local_app_enqueued = true;
    };

    auto handle_query_version = [&](uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) -> void {
        static const char ver[] = "3.0.0";
        queue_reliable_packet(0, requestId, taskId, reinterpret_cast<const uint8_t*>(ver), sizeof(ver) - 1);
        local_app_enqueued = true;
    };

    auto handle_unknown = [&](uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) -> void {
        queue_reply_errno(requestId, taskId, EINVAL);
        local_app_enqueued = true;
    };

    auto drain_task_pipe_once = [&](Task& t, int& fd, uint64_t outType) -> bool {
        if (fd < 0) return false;
    
        // 限流（防止爆队列）
        if (reliable.waiting.size() >= MAX_RELIABLE_QUEUE || reliable.inflight_exists) {
            return false;
        }
    
        uint8_t buf[MAX_APP_PAYLOAD];
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n > 0) {
            queue_reliable_packet(outType, 0, t.taskId, buf, (size_t)n);
            return true;
        }
        if (n == 0) {
            close_fd(fd);
            return false;
        }
        if (errno == EINTR) return false;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return false;
        close_fd(fd);
        return false;
    };

    auto drain_exited_tasks_once = [&]() -> bool {
        for (auto& kv : tasks) {
            Task& t = kv.second;
            if (!t.child_exited) continue;

            if (t.stdout_fd >= 0) {
                if (drain_task_pipe_once(t, t.stdout_fd, 6)) return true;
            }
            if (t.stderr_fd >= 0) {
                if (drain_task_pipe_once(t, t.stderr_fd, 7)) return true;
            }
        }
        return false;
    };

    auto finalize_ready_tasks = [&]() -> void {
        std::vector<uint64_t> to_erase;
        to_erase.reserve(tasks.size());

        for (auto& kv : tasks) {
            Task& t = kv.second;
            if (t.child_exited &&
                t.stdout_fd < 0 &&
                t.stderr_fd < 0 &&
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
    };

    // =========================
    // stdin reader thread
    // =========================

    std::thread([&]() {
        std::vector<uint8_t> tmp(8192);

        while (true) {
#ifdef _WIN32
            int n = _read(_fileno(stdin), tmp.data(), (unsigned)tmp.size());
#else
            ssize_t n = read(STDIN_FILENO, tmp.data(), tmp.size());
#endif
            if (n > 0) {
                std::lock_guard<std::mutex> lk(global_lock);
                size_t old = rxbuf.size();
                rxbuf.resize(old + (size_t)n);
                std::memcpy(rxbuf.data() + old, tmp.data(), (size_t)n);
            } else if (n == 0) {
                std::lock_guard<std::mutex> lk(global_lock);
                stdin_eof = true;
                break;
            } else {
#ifndef _WIN32
                if (errno == EINTR) continue;
#endif
                break;
            }
        }
    }).detach();

    // =========================
    // task pipe reader helper
    // =========================

    auto spawn_reader = [&](Task& t, int fd, uint64_t outType) {
        std::thread([&, fd, outType]() {
            uint8_t buf[MAX_APP_PAYLOAD];

            while (true) {
#ifdef _WIN32
                int n = _read(fd, buf, sizeof(buf));
                if (n > 0) {
                    std::lock_guard<std::mutex> lk(global_lock);
                    queue_reliable_packet(outType, 0, t.taskId, buf, (size_t)n);
                } else if (n == 0) {
                    std::lock_guard<std::mutex> lk(global_lock);
                    close_fd(const_cast<int&>(fd));
                    break;
                } else {
                    int err = errno;
                    if (err == EINTR) {
                        continue;
                    } else if (err == EAGAIN || err == EWOULDBLOCK) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(10));
                        continue;
                    } else {
                        std::lock_guard<std::mutex> lk(global_lock);
                        close_fd(const_cast<int&>(fd));
                        break;
                    }
                }
#else
                ssize_t n = read(fd, buf, sizeof(buf));
                if (n > 0) {
                    std::lock_guard<std::mutex> lk(global_lock);
                    queue_reliable_packet(outType, 0, t.taskId, buf, (size_t)n);
                } else if (n == 0) {
                    std::lock_guard<std::mutex> lk(global_lock);
                    close_fd(const_cast<int&>(fd));
                    break;
                } else {
                    int err = errno;
                    if (err == EINTR) {
                        continue;
                    } else if (err == EAGAIN || err == EWOULDBLOCK) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(10));
                        continue;
                    } else {
                        std::lock_guard<std::mutex> lk(global_lock);
                        close_fd(const_cast<int&>(fd));
                        break;
                    }
                }
#endif
            }
        }).detach();
    };

    // =========================
    // 主循环
    // =========================

    while (true) {
        {
            std::lock_guard<std::mutex> lk(global_lock);

            progress_output_state();

            // ========= reap =========
#ifdef _WIN32
            for (auto& kv : tasks) {
                Task& t = kv.second;
                if (t.child_exited || t.hProcess == nullptr) continue;

                DWORD exitCode = 0;
                if (WaitForSingleObject(t.hProcess, 0) == WAIT_OBJECT_0) {
                    GetExitCodeProcess(t.hProcess, &exitCode);
                    t.child_exited = true;
                    t.exit_code = (uint32_t)exitCode;

                    CloseHandle(t.hProcess);
                    t.hProcess = nullptr;

                    close_fd(t.stdin_fd);
                }
            }
#else
            while (true) {
                int status = 0;
                pid_t pid = waitpid(-1, &status, WNOHANG);
                if (pid <= 0) break;

                auto mp = pid_to_task.find(pid);
                if (mp != pid_to_task.end()) {
                    auto it = tasks.find(mp->second);
                    if (it != tasks.end()) {
                        mark_task_exited(it->second, status);
                    }
                }
            }
#endif

            // ========= parse stdin =========
            while (true) {
                if (rxbuf.size() - rxpos < 72) break;

                const uint8_t* base = rxbuf.data() + rxpos;

                uint64_t magic = read_u64_le(base + 0);
                uint64_t version = read_u64_le(base + 8);
                uint64_t type = read_u64_le(base + 16);
                uint64_t requestId = read_u64_le(base + 32);
                uint64_t taskId = read_u64_le(base + 40);
                uint64_t seq = read_u64_le(base + 48);
                uint64_t ack = read_u64_le(base + 56);
                uint64_t length = read_u64_le(base + 64);

                if (magic != MAGIC || version != VERSION) _exit(1);
                if (rxbuf.size() - rxpos < 72 + length) break;

                std::vector<uint8_t> payload(length);
                if (length)
                    std::memcpy(payload.data(), base + 72, length);

                rxpos += 72 + length;
                compact_buffer(rxbuf, rxpos);

                process_peer_ack(reliable, ack);

                if (seq == peer_expected_seq) {
                    peer_expected_seq++;
                    peer_last_delivered_seq = seq;

                    bool dummy = false;

                    switch (type) {
                        case 1: handle_stop_server(payload); break;
                        case 2:
                            handle_create_task(requestId, payload, dummy);
                            {
                                Task& t = tasks[nextTaskId - 1];
                                if (t.stdout_fd >= 0)
                                    spawn_reader(t, t.stdout_fd, 6);
                                if (t.stderr_fd >= 0)
                                    spawn_reader(t, t.stderr_fd, 7);
                            }
                            break;
                        case 3: handle_kill_task(requestId, taskId, payload, dummy); break;
                        case 5: handle_input_data(requestId, taskId, payload, dummy); break;
                        case 255: handle_query_version(requestId, taskId, dummy); break;
                        default: handle_unknown(requestId, taskId, dummy); break;
                    }
                }

                queue_ack_only();
            }

            finalize_ready_tasks();

            if (stdin_eof &&
                tasks.empty() &&
                transport.q.empty() &&
                !reliable.inflight_exists &&
                reliable.waiting.empty()) {
                break;
            }

            if (stopping &&
                tasks.empty() &&
                transport.q.empty() &&
                !reliable.inflight_exists &&
                reliable.waiting.empty()) {
                break;
            }
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    return 0;
}

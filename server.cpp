#include <algorithm>
#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <poll.h>
#include <signal.h>
#include <string>
#include <unordered_map>
#include <vector>
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <fcntl.h>


static constexpr uint64_t MAGIC   = 0x961f132bdddc19b9ULL;
static constexpr uint64_t VERSION  = 1;
static constexpr uint64_t MAX_LEN  = (1ULL << 30); // 1 GiB 上限，防止恶意长度撑爆内存

static void close_fd(int& fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

static void set_nonblock(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
}

static uint64_t read_u64_le(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v |= (uint64_t)p[i] << (i * 8);
    }
    return v;
}

static void write_u64_le(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)((v >> (i * 8)) & 0xFF);
    }
}

static bool write_all(int fd, const void* data, size_t len) {
    const uint8_t* p = static_cast<const uint8_t*>(data);
    size_t off = 0;
    while (off < len) {
        ssize_t n = write(fd, p + off, len - off);
        if (n > 0) {
            off += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
}

static bool send_packet(uint64_t type, uint64_t requestId, uint64_t taskId,
                        const void* payload, uint64_t len) {
    uint8_t header[48];
    write_u64_le(header + 0, MAGIC);
    write_u64_le(header + 8, VERSION);
    write_u64_le(header + 16, type);
    write_u64_le(header + 24, requestId);
    write_u64_le(header + 32, taskId);
    write_u64_le(header + 40, len);

    if (!write_all(STDOUT_FILENO, header, sizeof(header))) {
        return false;
    }
    if (len > 0) {
        if (!write_all(STDOUT_FILENO, payload, (size_t)len)) {
            return false;
        }
    }
    return true;
}

static bool send_reply_empty(uint64_t requestId, uint64_t taskId) {
    return send_packet(0, requestId, taskId, nullptr, 0);
}

static bool send_reply_errno(uint64_t requestId, uint64_t taskId, uint64_t err) {
    uint8_t payload[8];
    write_u64_le(payload, err);
    return send_packet(0, requestId, taskId, payload, 8);
}

static bool send_reply_u64(uint64_t requestId, uint64_t taskId, uint64_t value) {
    uint8_t payload[8];
    write_u64_le(payload, value);
    return send_packet(0, requestId, taskId, payload, 8);
}

static bool send_create_task_fail(uint64_t requestId, uint64_t err) {
    uint8_t payload[16];
    write_u64_le(payload + 0, 0);
    write_u64_le(payload + 8, err);
    return send_packet(0, requestId, 0, payload, 16);
}

static bool send_task_end(uint64_t taskId, uint8_t exitCode, uint8_t isSignalTerminated, uint8_t signalNo) {
    uint8_t payload[3] = { exitCode, isSignalTerminated, signalNo };
    return send_packet(4, 0, taskId, payload, 3);
}

static bool send_stdout_or_stderr(uint64_t type, uint64_t taskId, const uint8_t* data, size_t len) {
    return send_packet(type, 0, taskId, data, (uint64_t)len);
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

    int stdin_fd = -1;
    int stdout_fd = -1;
    int stderr_fd = -1;

    bool child_exited = false;
    int wait_status = 0;
    bool task_end_sent = false;

    std::vector<uint8_t> stdin_queue;
    size_t stdin_offset = 0;
    bool stdin_close_requested = false;
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

    // ⭐ 关键：真正关闭 stdin（发送 EOF）
    if (t.stdin_close_requested) {
        close_fd(t.stdin_fd);
        t.stdin_close_requested = false;
    }

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
    outTask.wait_status = 0;
    outTask.task_end_sent = false;
    outTask.stdin_queue.clear();
    outTask.stdin_offset = 0;

    return true;
}

static void mark_task_exited(Task& t, int status) {
    t.child_exited = true;
    t.wait_status = status;
}

static bool encode_exit_info(const Task& t, uint8_t& exitCode, uint8_t& isSig, uint8_t& sig) {
    if (WIFEXITED(t.wait_status)) {
        exitCode = (uint8_t)WEXITSTATUS(t.wait_status);
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
}

static void drain_task_pipe(Task& t, int& fd, uint64_t outType) {
    if (fd < 0) return;

    while (true) {
        uint8_t buf[4096];
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n > 0) {
            if (!send_stdout_or_stderr(outType, t.taskId, buf, (size_t)n)) {
                _exit(1);
            }
            continue;
        }
        if (n == 0) {
            close_fd(fd);
            return;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        close_fd(fd);
        return;
    }
}

static void drain_exited_tasks(std::unordered_map<uint64_t, Task>& tasks) {
    for (auto& kv : tasks) {
        Task& t = kv.second;
        if (!t.child_exited) continue;

        drain_task_pipe(t, t.stdout_fd, 6);
        drain_task_pipe(t, t.stderr_fd, 7);
    }
}

int main() {
    signal(SIGPIPE, SIG_IGN);
    set_nonblock(STDIN_FILENO);

    std::unordered_map<uint64_t, Task> tasks;
    std::unordered_map<pid_t, uint64_t> pid_to_task;

    uint64_t nextTaskId = 1;
    bool stdin_eof = false;
    bool stopping = false;

    std::vector<uint8_t> rxbuf;
    size_t rxpos = 0;
    std::vector<uint8_t> tmp(8192);

    auto send_protocol_error_and_exit = [&]() -> void {
        _exit(1);
    };

    auto get_task_by_id = [&](uint64_t id) -> Task* {
        auto it = tasks.find(id);
        if (it == tasks.end()) return nullptr;
        return &it->second;
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
                uint8_t exitCode = 0, isSig = 0, sig = 0;
                encode_exit_info(t, exitCode, isSig, sig);
                if (!send_task_end(t.taskId, exitCode, isSig, sig)) {
                    send_protocol_error_and_exit();
                }
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

    auto reap_children = [&]() -> void {
        while (true) {
            int status = 0;
            pid_t pid = waitpid(-1, &status, WNOHANG);
            if (pid > 0) {
                auto mp = pid_to_task.find(pid);
                if (mp != pid_to_task.end()) {
                    auto it = tasks.find(mp->second);
                    if (it != tasks.end()) {
                        mark_task_exited(it->second, status);
                        close_fd(it->second.stdin_fd);
                        it->second.stdin_queue.clear();
                        it->second.stdin_offset = 0;
                    }
                }
                continue;
            }
            if (pid == 0) break;
            if (pid < 0 && errno == EINTR) continue;
            break;
        }
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
                kill(t.pid, SIGKILL);
            }
            close_fd(t.stdin_fd);
            t.stdin_queue.clear();
            t.stdin_offset = 0;
        }
    };

    auto handle_create_task = [&](uint64_t requestId, const std::vector<uint8_t>& payload) -> void {
        if (stopping) {
            if (!send_create_task_fail(requestId, ECANCELED)) send_protocol_error_and_exit();
            return;
        }

        std::string cmdline(reinterpret_cast<const char*>(payload.data()), payload.size());

        uint64_t assignedId = 0;
        if (nextTaskId == 0) {
            if (!send_create_task_fail(requestId, EOVERFLOW)) send_protocol_error_and_exit();
            return;
        }
        assignedId = nextTaskId;
        if (nextTaskId == UINT64_MAX) nextTaskId = 0;
        else nextTaskId++;

        Task t;
        int err = 0;
        if (!spawn_task(cmdline, assignedId, t, err)) {
            if (!send_create_task_fail(requestId, (uint64_t)err)) send_protocol_error_and_exit();
            return;
        }

        tasks.emplace(assignedId, std::move(t));
        pid_to_task[tasks[assignedId].pid] = assignedId;


        if (!send_reply_u64(requestId, assignedId, assignedId)) {
            send_protocol_error_and_exit();
        }
    };

    auto handle_kill_task = [&](uint64_t requestId, uint64_t headerTaskId, const std::vector<uint8_t>& payload) -> void {
        uint64_t taskId = headerTaskId;
        if (taskId == 0) {
            if (!parse_payload_task_id(payload, taskId)) {
                if (!send_reply_errno(requestId, 0, EINVAL)) send_protocol_error_and_exit();
                return;
            }
        }

        Task* t = get_task_by_id(taskId);
        if (!t || t->child_exited || t->pid <= 0) {
            if (!send_reply_errno(requestId, taskId, ESRCH)) send_protocol_error_and_exit();
            return;
        }

        if (kill(t->pid, SIGKILL) == -1) {
            if (!send_reply_errno(requestId, taskId, (uint64_t)errno)) send_protocol_error_and_exit();
            return;
        }

        close_fd(t->stdin_fd);
        t->stdin_queue.clear();
        t->stdin_offset = 0;

        if (!send_reply_empty(requestId, taskId)) {
            send_protocol_error_and_exit();
        }
    };

    auto handle_input_data = [&](uint64_t requestId, uint64_t taskId, const std::vector<uint8_t>& payload) -> void {
        Task* t = get_task_by_id(taskId);
        if (!t || t->child_exited || t->stdin_fd < 0) {
            if (!send_reply_errno(requestId, taskId, ESRCH)) send_protocol_error_and_exit();
            return;
        }
    
        // ⭐ EOF：payload == 0
        if (payload.empty()) {
            t->stdin_close_requested = true;
    
            if (!flush_task_stdin(*t)) {
                if (!send_reply_errno(requestId, taskId, (uint64_t)errno)) send_protocol_error_and_exit();
                return;
            }
    
            if (!send_reply_empty(requestId, taskId)) send_protocol_error_and_exit();
            return;
        }
    
        size_t oldSize = t->stdin_queue.size();
        t->stdin_queue.resize(oldSize + payload.size());
        std::memcpy(t->stdin_queue.data() + oldSize, payload.data(), payload.size());
    
        if (!flush_task_stdin(*t)) {
            if (!send_reply_errno(requestId, taskId, (uint64_t)errno)) send_protocol_error_and_exit();
            return;
        }
    
        if (!send_reply_empty(requestId, taskId)) send_protocol_error_and_exit();
    };

    auto handle_query_version = [&](uint64_t requestId, uint64_t taskId) -> void {
        static const char ver[] = "1.0.0";
        if (!send_packet(0, requestId, taskId, ver, sizeof(ver) - 1)) {
            send_protocol_error_and_exit();
        }
    };

    auto handle_unknown = [&](uint64_t requestId, uint64_t taskId) -> void {
        if (!send_reply_errno(requestId, taskId, EINVAL)) {
            send_protocol_error_and_exit();
        }
    };

    while (true) {
        reap_children();
        drain_exited_tasks(tasks);
        finalize_ready_tasks();

        if (stdin_eof && tasks.empty()) {
            break;
        }

        std::vector<pollfd> fds;
        fds.reserve(1 + tasks.size() * 3);

        enum class Kind { Stdin, TaskStdout, TaskStderr, TaskStdin };
        struct Item {
            Kind kind;
            uint64_t taskId;
        };
        std::vector<Item> items;
        items.reserve(1 + tasks.size() * 3);

        if (!stdin_eof) {
            pollfd p{};
            p.fd = STDIN_FILENO;
            p.events = POLLIN;
            fds.push_back(p);
            items.push_back({Kind::Stdin, 0});
        }

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

            if (t.stdin_fd >= 0 && t.stdin_queue.size() > t.stdin_offset) {
                pollfd p{};
                p.fd = t.stdin_fd;
                p.events = POLLOUT | POLLERR | POLLHUP;
                fds.push_back(p);
                items.push_back({Kind::TaskStdin, t.taskId});
            }
        }

        if (fds.empty()) {
            // 理论上只有 stdin_eof 且 tasks 为空 才会到这里
            break;
        }

        int pret = poll(fds.data(), fds.size(), 50);
        if (pret < 0) {
            if (errno == EINTR) continue;
            _exit(1);
        }

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
                        //close_fd(*const_cast<int*>(&STDIN_FILENO)); // 不会真的关闭常量；只是避免误用
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    _exit(1);
                }

                while (true) {
                    if (rxbuf.size() - rxpos < 48) break;

                    const uint8_t* base = rxbuf.data() + rxpos;
                    uint64_t magic = read_u64_le(base + 0);
                    uint64_t version = read_u64_le(base + 8);
                    uint64_t type = read_u64_le(base + 16);
                    uint64_t requestId = read_u64_le(base + 24);
                    uint64_t taskId = read_u64_le(base + 32);
                    uint64_t length = read_u64_le(base + 40);

                    if (magic != MAGIC || version != VERSION) {
                        _exit(1);
                    }
                    if (length > MAX_LEN || length > (uint64_t)std::numeric_limits<size_t>::max()) {
                        _exit(1);
                    }
                    if (rxbuf.size() - rxpos < 48 + (size_t)length) break;

                    std::vector<uint8_t> payload;
                    payload.resize((size_t)length);
                    if (length > 0) {
                        std::memcpy(payload.data(), base + 48, (size_t)length);
                    }

                    rxpos += 48 + (size_t)length;
                    compact_buffer(rxbuf, rxpos);

                    switch (type) {
                        case 0:
                            // reply：客户端发来的 reply 直接忽略
                            break;

                        case 1:
                            handle_stop_server(payload);
                            break;

                        case 2:
                            handle_create_task(requestId, payload);
                            break;

                        case 3:
                            handle_kill_task(requestId, taskId, payload);
                            break;

                        case 5:
                            handle_input_data(requestId, taskId, payload);
                            break;

                        case 255:
                            handle_query_version(requestId, taskId);
                            break;

                        case 4:
                        case 6:
                        case 7:
                            // 这些是服务器发送给客户端的消息，忽略客户端发来的同类包
                            break;

                        default:
                            handle_unknown(requestId, taskId);
                            break;
                    }

                    if (stopping) {
                        // stopping 之后尽量不再接受新输入，继续让当前任务走向结束即可
                        // 但仍然允许已有任务的 stdout/stderr 和 waitpid 处理继续进行
                    }
                }
            } else if (item.kind == Kind::TaskStdout || item.kind == Kind::TaskStderr) {
                auto it = tasks.find(item.taskId);
                if (it == tasks.end()) continue;
                Task& t = it->second;
                int* target_fd = (item.kind == Kind::TaskStdout) ? &t.stdout_fd : &t.stderr_fd;
                uint64_t outType = (item.kind == Kind::TaskStdout) ? 6 : 7;

                while (true) {
                    uint8_t buf[4096];
                    ssize_t n = read(*target_fd, buf, sizeof(buf));
                    if (n > 0) {
                        if (!send_stdout_or_stderr(outType, t.taskId, buf, (size_t)n)) {
                            _exit(1);
                        }
                        continue;
                    }
                    if (n == 0) {
                        close_fd(*target_fd);
                        break;
                    }
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    close_fd(*target_fd);
                    break;
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

        // 处理所有 task 的结束条件
        finalize_ready_tasks();

        // 如果 stdin 已结束且没有任务，则退出
        if (stdin_eof && tasks.empty()) {
            break;
        }

        // 如果已经进入 stopping 且没有任务了，也退出
        if (stopping && tasks.empty()) {
            break;
        }
    }

    return 0;
}


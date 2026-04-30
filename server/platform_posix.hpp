#pragma once

#ifndef _WIN32

#include "server.hpp"

#include <cerrno>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <string>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace rmpsm {

inline void close_fd(platform_handle_t& fd) {
    if (fd >= 0) {
        close(fd);
        fd = -1;
    }
}

inline void set_nonblock(platform_handle_t fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
}

inline bool flush_task_stdin(Task& t) {
    if (t.stdin_fd < 0) return false;

    while (t.stdin_offset < t.stdin_queue.size()) {
        ssize_t n = write(
            t.stdin_fd,
            t.stdin_queue.data() + t.stdin_offset,
            t.stdin_queue.size() - t.stdin_offset
        );

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

inline void Server::close_task_all_fds(Task& t) {
    close_fd(t.stdin_fd);
    close_fd(t.stdout_fd);
    close_fd(t.stderr_fd);
}

inline bool flush_transport(TransportState& tx, ReliableState& rel) {
    while (!tx.q.empty()) {
        TxItem& item = tx.q.front();

        ssize_t n = write(
            STDOUT_FILENO,
            item.bytes.data() + item.offset,
            item.bytes.size() - item.offset
        );

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

    // 重传优先级高，放到最前面
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

inline bool spawn_task(const std::string& cmdline, uint64_t taskId, Task& outTask, int& outErr) {
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

        if (dup2(inpipe[0], STDIN_FILENO) == -1) {
            const char* msg = "error: dup2 stdin failed\n";
            write(errpipe[1], msg, strlen(msg));
            _exit(126);
        }
        if (dup2(outpipe[1], STDOUT_FILENO) == -1) {
            const char* msg = "error: dup2 stdout failed\n";
            write(errpipe[1], msg, strlen(msg));
            _exit(126);
        }
        if (dup2(errpipe[1], STDERR_FILENO) == -1) _exit(126);

        close(inpipe[0]);
        close(outpipe[1]);
        close(errpipe[1]);

        execvp(argv[0], argv.data());
        int exec_errno = errno;
        const char* err_desc = std::strerror(exec_errno);
        write(STDERR_FILENO, err_desc, std::strlen(err_desc));
        write(STDERR_FILENO, ": ", 2);
        write(STDERR_FILENO, argv[0], std::strlen(argv[0]));
        write(STDERR_FILENO, "\n", 1);
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
    outTask.task_end_sent = false;
    outTask.stdin_queue.clear();
    outTask.stdin_offset = 0;
    outTask.stdin_close_requested = false;
    return true;
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
        if (!t.child_exited && t.pid > 0) {
            kill(t.pid, SIGKILL);
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

    if (kill(t->pid, SIGKILL) == -1) {
        queue_reply_errno(requestId, taskId, (uint64_t)errno);
        local_app_enqueued = true;
        return;
    }

    close_fd(t->stdin_fd);
    t->stdin_queue.clear();
    t->stdin_offset = 0;
    queue_reply_empty(requestId, taskId);
    local_app_enqueued = true;
}

inline void Server::handle_query_error(uint64_t requestId, const std::vector<uint8_t>& payload, bool& local_app_enqueued) {
    if (payload.size() < 4) {
        queue_reply_errno(requestId, 0, EINVAL);
        local_app_enqueued = true;
        return;
    }

    uint32_t err_code = (uint32_t)payload[0]
                      | ((uint32_t)payload[1] << 8)
                      | ((uint32_t)payload[2] << 16)
                      | ((uint32_t)payload[3] << 24);

    const char* msg = std::strerror((int)err_code);

    std::vector<uint8_t> resp;
    resp.push_back(1);
    resp.insert(resp.end(), msg, msg + std::strlen(msg));

    queue_reliable_packet(0, requestId, 0, resp.data(), resp.size());
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
}

inline bool Server::drain_task_pipe_once(Task& t, int& fd, uint64_t outType) {
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
}

inline bool Server::drain_exited_tasks_once() {
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
}

inline void Server::finalize_ready_tasks() {
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
        DecodedPacket pkt;
        size_t parse_pos = rxpos;
        PacketParseResult res = try_parse_packet(rxbuf, parse_pos, pkt);
        if (res == PacketParseResult::NeedMore) {
            break;
        }
        if (res == PacketParseResult::Invalid) {
            _exit(1);
        }
        rxpos = parse_pos;
        compact_buffer(rxbuf, rxpos);
        dispatch_packet(pkt);
    }
}

inline void Server::reap_children() {
    while (true) {
        int status = 0;
        pid_t pid = waitpid(-1, &status, WNOHANG);

        if (pid > 0) {
            auto mp = pid_to_task.find(pid);
            if (mp != pid_to_task.end()) {
                auto it = tasks.find(mp->second);
                if (it != tasks.end()) {
                    Task& t = it->second;

                    if (WIFEXITED(status)) {
                        mark_task_exited(
                            t,
                            true,
                            (uint32_t)WEXITSTATUS(status),
                            false,
                            0
                        );
                    } else if (WIFSIGNALED(status)) {
                        mark_task_exited(
                            t,
                            false,
                            0,
                            true,
                            (uint32_t)WTERMSIG(status)
                        );
                    } else {
                        // fallback（理论上不会进来）
                        mark_task_exited(t, true, 0, false, 0);
                    }

                    close_fd(t.stdin_fd);
                    t.stdin_queue.clear();
                    t.stdin_offset = 0;
                }
            }
            continue;
        }

        if (pid == 0) break;
        if (pid < 0 && errno == EINTR) continue;
        break;
    }
}

inline int Server::run() {
    signal(SIGPIPE, SIG_IGN);
    set_nonblock(STDIN_FILENO);
    set_nonblock(STDOUT_FILENO);

    while (true) {
        progress_output_state();

        reap_children();

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

        int pret = poll(fds.data(), fds.size(), 50);
        if (pret < 0) {
            if (errno == EINTR) continue;
            _exit(1);
        }

        bool output_generation_blocked = !allow_task_output_read;

        for (size_t i = 0; i < fds.size(); ++i) {
            if (fds[i].revents == 0) continue;

            const Item& item = items[i];

            if (item.kind == Kind::Stdin) {
                handle_stdin_event();
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

inline int run_server() {
    Server s;
    return s.run();
}

} // namespace rmpsm

#else
#error "Windows platform support is not implemented in this header yet."
#endif

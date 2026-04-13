#pragma once

#include "protocol.hpp"
#include "task.hpp"

#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace rmpsm {

inline bool process_peer_ack(ReliableState& rel, uint64_t ack);
inline bool enqueue_tx_front(TransportState& tx, std::vector<uint8_t>&& bytes, bool reliable, uint64_t seq);
inline bool start_next_reliable_if_idle(ReliableState& rel, TransportState& tx, uint64_t peer_last_delivered_seq);

class Server {
public:
    int run();

private:
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
    std::vector<uint8_t> tmp = std::vector<uint8_t>(8192);

    TransportState transport;
    ReliableState reliable;

private:
    void close_task_all_fds(Task& t);

    void queue_reliable_packet(
        uint64_t type,
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
    }

    void queue_reply_empty(uint64_t requestId, uint64_t taskId) {
        queue_reliable_packet(0, requestId, taskId, nullptr, 0);
    }

    void queue_reply_errno(uint64_t requestId, uint64_t taskId, uint64_t err) {
        uint8_t payload[8];
        write_u64_le(payload, err);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    }

    void queue_reply_u64(uint64_t requestId, uint64_t taskId, uint64_t value) {
        uint8_t payload[8];
        write_u64_le(payload, value);
        queue_reliable_packet(0, requestId, taskId, payload, 8);
    }

    void queue_create_task_fail(uint64_t requestId, uint64_t err) {
        uint8_t payload[16];
        write_u64_le(payload + 0, 0);
        write_u64_le(payload + 8, err);
        queue_reliable_packet(0, requestId, 0, payload, 16);
    }

    void queue_task_end(uint64_t taskId, uint32_t exitCode, uint8_t isSignalTerminated, uint8_t signalNo) {
        uint8_t payload[6];
        payload[0] = (uint8_t)(exitCode & 0xFF);
        payload[1] = (uint8_t)((exitCode >> 8) & 0xFF);
        payload[2] = (uint8_t)((exitCode >> 16) & 0xFF);
        payload[3] = (uint8_t)((exitCode >> 24) & 0xFF);
        payload[4] = isSignalTerminated;
        payload[5] = signalNo;
        queue_reliable_packet(4, 0, taskId, payload, sizeof(payload));
    }

    void queue_ack_only() {
        if (peer_last_delivered_seq == 0) return;
        auto bytes = build_packet_bytes(TYPE_ACK_ONLY, 0, 0, 0, peer_last_delivered_seq, nullptr, 0);
        enqueue_tx_front(transport, std::move(bytes), false, 0);
    }

    void handle_stop_server(const std::vector<uint8_t>& payload);
    void handle_create_task(uint64_t requestId, const std::vector<uint8_t>& payload, bool& local_app_enqueued);
    void handle_kill_task(uint64_t requestId, uint64_t headerTaskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued);
    void handle_input_data(uint64_t requestId, uint64_t taskId, const std::vector<uint8_t>& payload, bool& local_app_enqueued);
    void handle_query_version(uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) {
        queue_reliable_packet(0, requestId, taskId, reinterpret_cast<const uint8_t*>(APP_VERSION), sizeof(APP_VERSION) - 1);
        local_app_enqueued = true;
    }

    void handle_unknown(uint64_t requestId, uint64_t taskId, bool& local_app_enqueued) {
        queue_reply_errno(requestId, taskId, EINVAL);
        local_app_enqueued = true;
    }

    bool drain_task_pipe_once(Task& t, int& fd, uint64_t outType);
    bool drain_exited_tasks_once();
    void finalize_ready_tasks();
    void progress_output_state();
    void handle_stdin_event();
    void reap_children();

    void dispatch_packet(const DecodedPacket& pkt) {
        if (pkt.type == TYPE_ACK_ONLY) {
            process_peer_ack(reliable, pkt.ack);
            if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
                progress_output_state();
            }
            return;
        }

        bool local_app_enqueued = false;

        // 先处理对端对我方包的 ACK（piggyback ACK）
        process_peer_ack(reliable, pkt.ack);

        bool accepted = false;
        bool is_new = false;

        if (pkt.seq == peer_expected_seq) {
            peer_expected_seq++;
            peer_last_delivered_seq = pkt.seq;
            accepted = true;
            is_new = true;   // ⭐ 新包
        } else if (pkt.seq < peer_expected_seq) {
            accepted = true;  // ⭐ 旧包（重传）
            is_new = false;
        } else {
            accepted = false;
        }

        if (accepted && is_new) {
            switch (pkt.type) {
                case 0:
                    // reply：客户端发来的 reply 直接忽略
                    break;
                case 1:
                    handle_stop_server(pkt.payload);
                    break;
                case 2:
                    handle_create_task(pkt.requestId, pkt.payload, local_app_enqueued);
                    break;
                case 3:
                    handle_kill_task(pkt.requestId, pkt.taskId, pkt.payload, local_app_enqueued);
                    break;
                case 5:
                    handle_input_data(pkt.requestId, pkt.taskId, pkt.payload, local_app_enqueued);
                    break;
                case 255:
                    handle_query_version(pkt.requestId, pkt.taskId, local_app_enqueued);
                    break;
                case 4:
                case 6:
                case 7:
                    // 这些是服务器发送给客户端的消息，忽略客户端发来的同类包
                    break;
                default:
                    handle_unknown(pkt.requestId, pkt.taskId, local_app_enqueued);
                    break;
            }
        }

        // 如果在这次处理里生成了新的可靠应用包，尽量马上开始发送
        if (local_app_enqueued) {
            if (start_next_reliable_if_idle(reliable, transport, peer_last_delivered_seq)) {
                progress_output_state();
            }
        }

        if (accepted) {
            queue_ack_only();
            progress_output_state();  // ⭐ 立即 flush
        }
    }
};

int run_server();

} // namespace rmpsm

#pragma once

#include <chrono>
#include <cerrno>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <limits>
#include <string>
#include <utility>
#include <vector>
#ifdef _WIN32
#include "windows1.h"
#endif

#ifdef _WIN32
using pid_t = DWORD;
#else
#include <sys/types.h>
#endif

namespace rmpsm {

static constexpr uint64_t MAGIC = 0x961f132bdddc19b9ULL;
static constexpr uint64_t VERSION = 3;
static constexpr char APP_VERSION[] = "3.0.0";
static constexpr uint64_t MAX_LEN = (1ULL << 30); // 1 GiB 上限，防止恶意长度撑爆内存
static constexpr uint64_t TYPE_ACK_ONLY = 18446744073709551615ULL; // 纯 ACK，不再要求 ACK
static constexpr size_t MAX_APP_PAYLOAD = 32768;
static constexpr size_t MAX_RELIABLE_QUEUE = 256;
static constexpr auto RETRANSMIT_TIMEOUT = std::chrono::milliseconds(5000);

#ifdef _WIN32
    using platform_handle_t = HANDLE;
    inline constexpr platform_handle_t INVALID_PLATFORM_HANDLE = nullptr;
    inline bool is_valid_platform_handle(platform_handle_t h) {
        return h != nullptr && h != INVALID_HANDLE_VALUE;
    }
#else
    using platform_handle_t = int;
    inline constexpr platform_handle_t INVALID_PLATFORM_HANDLE = -1;
    inline bool is_valid_platform_handle(platform_handle_t h) {
        return h >= 0;
    }
#endif

inline uint64_t read_u64_le(const uint8_t* p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v |= (uint64_t)p[i] << (i * 8);
    }
    return v;
}

inline void write_u64_le(uint8_t* p, uint64_t v) {
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)((v >> (i * 8)) & 0xFF);
    }
}

inline bool parse_command_line(const std::string& cmd, std::vector<std::string>& out, int& err) {
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

inline uint64_t next_seq_wrap(uint64_t v) {
    if (v == UINT64_MAX) return 1;
    if (v == 0) return 1;
    return v + 1;
}

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

struct DecodedPacket {
    uint64_t type = 0;
    uint64_t flags = 0;
    uint64_t requestId = 0;
    uint64_t taskId = 0;
    uint64_t seq = 0;
    uint64_t ack = 0;
    std::vector<uint8_t> payload;
};

enum class PacketParseResult {
    NeedMore,
    Ok,
    Invalid,
};

inline std::vector<uint8_t> build_packet_bytes(
    uint64_t type,
    uint64_t requestId,
    uint64_t taskId,
    uint64_t seq,
    uint64_t ack,
    const uint8_t* payload,
    size_t len) {
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

inline void compact_buffer(std::vector<uint8_t>& buf, size_t& pos) {
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

inline PacketParseResult try_parse_packet(
    const std::vector<uint8_t>& rxbuf,
    size_t& rxpos,
    DecodedPacket& out) {
    if (rxbuf.size() - rxpos < 72) {
        return PacketParseResult::NeedMore;
    }

    const uint8_t* base = rxbuf.data() + rxpos;
    uint64_t magic = read_u64_le(base + 0);
    uint64_t version = read_u64_le(base + 8);
    uint64_t type = read_u64_le(base + 16);
    uint64_t flags = read_u64_le(base + 24);
    uint64_t requestId = read_u64_le(base + 32);
    uint64_t taskId = read_u64_le(base + 40);
    uint64_t seq = read_u64_le(base + 48);
    uint64_t ack = read_u64_le(base + 56);
    uint64_t length = read_u64_le(base + 64);

    if (magic != MAGIC || version != VERSION) {
        return PacketParseResult::Invalid;
    }
    if (length > MAX_LEN || length > (uint64_t)std::numeric_limits<size_t>::max()) {
        return PacketParseResult::Invalid;
    }
    if (rxbuf.size() - rxpos < 72 + (size_t)length) {
        return PacketParseResult::NeedMore;
    }

    out = DecodedPacket{};
    out.type = type;
    out.flags = flags;
    out.requestId = requestId;
    out.taskId = taskId;
    out.seq = seq;
    out.ack = ack;
    out.payload.resize((size_t)length);
    if (length > 0) {
        std::memcpy(out.payload.data(), base + 72, (size_t)length);
    }

    rxpos += 72 + (size_t)length;
    return PacketParseResult::Ok;
}

} // namespace rmpsm

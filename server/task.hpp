#pragma once

#include <cstdint>
#include <cstddef>
#include <sys/types.h>
#include <sys/wait.h>
#include <vector>

namespace rmpsm {

struct Task {
    uint64_t taskId = 0;
    pid_t pid = -1;

    platform_handle_t stdin_fd = INVALID_PLATFORM_HANDLE;
    platform_handle_t stdout_fd = INVALID_PLATFORM_HANDLE;
    platform_handle_t stderr_fd = INVALID_PLATFORM_HANDLE;

    bool child_exited = false;
    bool exited_normally = false;
    bool signaled = false;
    uint32_t exit_code = 0;
    uint32_t signal_no = 0;

    bool task_end_sent = false;
    std::vector<uint8_t> stdin_queue;
    size_t stdin_offset = 0;
    bool stdin_close_requested = false;
};

inline void mark_task_exited(
    Task& t,
    bool exited_normally,
    uint32_t exit_code,
    bool signaled = false,
    uint32_t signal_no = 0) {
    t.child_exited = true;
    t.exited_normally = exited_normally;
    t.signaled = signaled;
    t.exit_code = exit_code;
    t.signal_no = signal_no;
}

inline bool encode_exit_info(const Task& t, uint32_t& exitCode, uint8_t& isSig, uint8_t& sig) {
    if (!t.child_exited) {
        exitCode = 0;
        isSig = 0;
        sig = 0;
        return false;
    }

    exitCode = t.exit_code;
    isSig = t.signaled ? 1 : 0;
    sig = (uint8_t)t.signal_no;
    return true;
}

} // namespace rmpsm

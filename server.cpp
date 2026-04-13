#include "server/server.hpp"
#ifdef _WIN32
#include "platform_win32.hpp"
#else
#include "server/platform_posix.hpp"
#endif

int main() {
    return rmpsm::run_server();
}

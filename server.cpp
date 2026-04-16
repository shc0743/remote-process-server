#include <iostream>
#include <string>
#include "server_version.h"
#include "server/server.hpp"
#include "server/platform.hpp"

using std::cout;
using std::cerr;
using std::endl;
using std::string;

int main(int argc, char* argv[]) {
    if (argc > 2) {
        cerr << "Fatal: Too many arguments" << endl;
        cerr << "Warning: The server binary is NOT for direct invoke. Please run the manager." << endl;
        return 87;
    }
    else if (argc == 2) {
        string action = argv[1];
        if (action == "-h" || action == "--help") {
            cout << "\x1b[1;4mremote-process-server\x1b[0m Server binary" << endl;
            cout << "Available commands:" << endl;
            cout << "\t-h, --help\tShow this" << endl;
            cout << "\t-V, --version, version\tShow version" << endl;
            cout << "\t-P, --protocol-version\tShow protocol version" << endl;
            cout << "\thomepage\tShow homepage" << endl;
        }
        else if (action == "homepage") {
            cout << "https://www.npmjs.com/package/remote-process-server" << endl;
        }
        else if (action == "version" || action == "-V" || action == "--version") {
            cout << "Prebuilt server binary" << endl;
            cout << SERVER_VERSION << endl;
            cout << __FILE__ << endl;
            cout << __LINE__ << endl;
            cout << __func__ << endl;
            cout << "Compile: " << __DATE__ << " " << __TIME__ << endl;
            cout << "C++: " <<
#ifdef _MSVC_LANG
                _MSVC_LANG
#else
                __cplusplus
#endif
                << endl;
        }
        else if (action == "-P" || action == "--protocol-version") {
            cout << rmpsm::VERSION << endl;
            cout << rmpsm::APP_VERSION << endl;
        }
        else {
            cerr << "Fatal: Invalid arguments" << endl;
            cerr << "Warning: The server binary is NOT for direct invoke. Please run the manager." << endl;
            return 87;
        }
        return 0;
    }
    return rmpsm::run_server();
}

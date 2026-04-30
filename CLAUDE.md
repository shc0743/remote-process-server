# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
npm run build          # Full build: type-check → rolldown bundle → obfuscate
npm run type-check     # tsc --noEmit (JS files with JSDoc type annotations)
npm run build-only     # Bundle entry.js and maintainance.js via rolldown
npm test               # Run integration test suite (starts manager + server, runs commands)
```

C++ server compilation (produces `native/bin/rmpsm_server.<arch>`):
```bash
./compile.sh                    # POSIX; requires clang++/strip, uses -static when not Android
./compile.cmd                   # Windows; requires MSVC cl.exe
./compile.sh <arch> <compiler> <stripper>  # Custom toolchain override
```

## Architecture

Three layers communicating over two protocols:

```
User → entry.js (Node.js CLI) → client.py (Python) → C++ server → OS processes
                ↓
         maintainance.js (install/uninstall/update)
```

**Layer 1 — C++ server** (`server.cpp`, `server/`): Creates and manages OS processes. Communicates via stdin/stdout using a custom binary framed protocol (magic `0x961f132bdddc19b9`, version 3, 72-byte header). Supports reliable delivery with sequence numbers, ACKs, retransmit timers. The protocol is defined in `server/protocol.hpp`. Platform abstraction: `server/platform_posix.hpp` and `server/platform_win32.hpp` (selected by `server/platform.hpp` based on `_WIN32`). Server is never invoked directly — always launched by the manager.

**Layer 2 — Python manager/client** (`client/`): Two roles in one process based on `--type`:
- `manager` mode: `rmpsm_manager.py` starts the C++ server as a subprocess via `rmpsm_bridge.py`, publishes a TCP listener + connection file, handles session multiplexing (`rmpsm_session.py`). The bootstrap protocol between client and manager uses a light framing layer (magic `RMPC`, defined in `rmpsm_protocol.py`).
- `client` mode: `rmpsm_client_runtime.py` connects to the manager's TCP socket, creates a task, and streams stdin/stdout/stderr using threads and queues. Supports both POSIX (`select.select` for stdin) and Windows (`os.read` loop).

**Layer 3 — Node.js CLI** (`entry.js`): Command dispatcher that either spawns the Python client/manager subprocess or runs JavaScript maintenance logic. `maintainance.js` handles install/update/uninstall — it copies versioned runtime directories to a system path and manages `installation.data` (JSON file pointing to the active version). The npm package is just a bootstrap; `install` extracts the full runtime to a protected directory.

## Key conventions

- Server binary filenames: `rmpsm_server.<arch>` where arch is from `sys_name.py` (e.g., `linux_x86_64`, `windows_amd64`). Binaries live in `native/bin/`.
- The `postinstall.js` script writes `package.json` version into `server_version.h` (`#define SERVER_VERSION "..."`) so the C++ binary knows its version.
- `entry.min.js` is the published bin entrypoint (`"bin"` in `package.json`). The `.min.js` files are rolldown output → obfuscated.
- `--server` argument takes a **full command line**, not just an executable path. Users must wrap paths-with-spaces in extra quotes.
- TypeScript is used only for type-checking (`checkJs: true`); files are plain JS with JSDoc annotations. `.min.js` files are excluded from type-checking.
- Test suite (`test.js`) uses eventfd (POSIX) or CreateEventW (Windows) to synchronize with server startup, then compares command output byte-for-byte against expected results.

# remote-process-server

A robust client/server system for remotely creating and managing processes.  
Version **3.0.0-alpha.1** – modern transport, cross‑platform aspirations, and a friendly CLI.

## 🚧 Alpha Status

This release is **alpha software**.  
- **POSIX (Linux, macOS, WSL)** support is fully functional.  
- **Windows** support is under active development and **not yet complete**.  
- Pre‑compiled Windows binaries are **not** provided at this time.  

We welcome testing and feedback on POSIX systems. Windows users should expect instability.

## Features

- **Process lifecycle management** – create, send input to, and kill processes on a remote machine.
- **Full‑duplex I/O streaming** – stdout and stderr are streamed back to the client in real time.
- **Reliable message delivery** – custom transport layer with sequence numbers, acknowledgements, and retransmission.
- **TCP‑based control channel** – manager and client communicate over a local TCP socket with an ephemeral port and an authentication key, replacing the earlier FIFO‑based design.
- **Cross‑platform** – core logic works on POSIX and (experimentally) Windows.
- **Simple distribution** – run directly via `npx` without manual installation.

## Architecture

```
[Client]  ──(TCP + binary protocol)──>  [Manager]  ──(Binary Protocol)──>  [Server (C++)]
                                            │                                   │
                                            │                              spawns child
                                            │                              processes
                                            +───── forwards I/O, exit codes ────+
```

- **Manager** (`client.py --type manager`)  
  Listens on a TCP socket (bound to `127.0.0.1` with a random port), accepts authenticated client connections, and bridges them to the native server process. It manages multiple concurrent sessions and tasks.

- **Server** (`rmpsm_server.*` compiled binary)  
  Spawns child processes, pipes their stdin/stdout/stderr, and communicates with the manager using a custom binary protocol. It handles reliable transmission, retransmission, and graceful shutdown.

- **Client** (`client.py --type client -- <command>`)  
  Connects to the manager, requests execution of a command, and forwards its own stdin to the remote process while printing the remote stdout/stderr locally.

## Requirements

- Python 3.7+
- A C++17 compiler (Clang or GCC on POSIX; MSVC on Windows)
- POSIX environment (Linux, macOS, WSL) for stable operation
- Node.js (optional) – only required if you prefer to run via `npx`

## Installation

### From source

```bash
git clone https://github.com/shc0743/remote-process-server.git
cd remote-process-server
```

Compile the server binary for your platform:

```bash
# On POSIX
./compile.sh

# On Windows (experimental)
compile.cmd
```

This produces `rmpsm_server.<os>_<arch>`, e.g. `rmpsm_server.linux_x86_64`.

### Using npx (no clone required)

You can run the manager directly using `npx`:

```bash
npx remote-process-server serve [--manager CONN_FILE] [--server SERVER_BIN]
```

The `serve` command starts the manager daemon. The package will download the necessary scripts automatically.

## Usage

### 1. Start the manager daemon

Run the manager in the background, specifying the path to the compiled server binary.

```bash
# Using the Python script directly
python3 client.py --type manager --server ./rmpsm_server.linux_x86_64 &

# Or using npx
npx remote-process-server serve --server ./rmpsm_server.linux_x86_64 &
```

The manager creates a connection file at `$TMPDIR/rmpsm_manager.conn` (or `/tmp`) containing the TCP port and an authentication key. It will stay alive until explicitly killed.

### 2. Execute a remote command with the client

Use the client to run any command. The client’s stdin is forwarded to the remote process, and stdout/stderr are printed locally.

```bash
python3 client.py --type client -- <command> [args...]
```

**Examples:**

```bash
# Run `ls -la` remotely and see the output locally
python3 client.py -- ls -la

# Pipe local data to the remote process
echo "Hello from client" | python3 client.py -- cat

# Interactive remote shell (bash)
python3 client.py -- bash
```

### 3. Stop the manager

To cleanly shut down the manager daemon, send a kill request:

```bash
python3 client.py --kill
```

## Command‑Line Options

### Manager mode
```
python3 client.py --type manager [--manager CONN_FILE] [--server PATH]
```
- `--manager CONN_FILE` – path to the connection info file (default: `$TMPDIR/rmpsm_manager.conn`)
- `--server PATH` – path to the compiled server binary (default: `./rmpsm_server.<os>_<arch>`)

### Client mode
```
python3 client.py --type client [--manager CONN_FILE] [--kill] -- COMMAND...
```
- `--manager CONN_FILE` – same connection file used by the manager
- `--kill` – send a termination request to the manager instead of running a command
- `COMMAND...` – the command and its arguments to execute remotely

## Protocol Overview

### Manager ↔ Client (Binary over TCP)

After a successful authentication handshake, the client and manager exchange framed messages using a simple binary protocol.

**Message types (C2M / M2C):**
- `C2M_AUTH` / `M2C_AUTH_OK` / `M2C_AUTH_FAIL`
- `C2M_CREATE_SESSION` / `M2C_CREATE_SESSION_RESP`
- `C2M_CREATE_TASK` / `M2C_CREATE_TASK_RESP`
- `C2M_STDIN` / `C2M_STDIN_EOF` / `C2M_KILL` / `C2M_CLOSE_SESSION` / `C2M_STOP_MANAGER`
- `M2C_STDOUT` / `M2C_STDERR` / `M2C_TASK_END` / `M2C_SERVER_DEAD`

Each frame is prefixed with a 10‑byte header (`CTRL_MAGIC`, version, type, flags, payload length).

### Manager ↔ Server (Binary Protocol)

A custom reliable protocol runs over the server’s stdin/stdout. Each packet has a 72‑byte header with fields for magic, version, type, flags, request ID, task ID, sequence number, acknowledgement, and payload length.

**Message types:**
- `0` – Reply / acknowledgement
- `1` – Stop server
- `2` – Create task
- `3` – Kill task
- `4` – Task end notification
- `5` – Stdin data / EOF
- `6` – Stdout data
- `7` – Stderr data
- `255` – Version query (returns `"3.0.0"`)
- `18446744073709551615` – Standalone acknowledgement

The server maintains per‑task pipes and uses platform‑specific I/O multiplexing (`poll()` on POSIX, background threads on Windows). Reliable packets are queued and retransmitted if not acknowledged within 500 ms.

## Files

- `client.py` – Manager, client runtime, and CLI entry point.
- `server.cpp` – Main entry for the native server.
- `server.hpp` – Core server logic.
- `task.hpp` – Task state definitions.
- `protocol.hpp` – Shared binary protocol structures and helpers.
- `platform_posix.hpp` / `platform_win32.hpp` – Platform‑specific implementations.
- `compile.sh` / `compile.cmd` – Build scripts.
- `sys_name.py` – Helper to generate platform‑specific binary names.
- `entry.js` – Node.js wrapper for `npx` usage.
- `package.json` – npm package manifest.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

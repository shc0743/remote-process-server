# remote-process-server

A robust client/server system for remotely creating and managing processes across FIFO-based communication channels. It consists of a long-running manager daemon, a native server that spawns and monitors child processes, and a Python client that interacts with the manager to execute commands remotely.

## Features

- **Process lifecycle management** – create, send input to, and kill processes on a remote machine.
- **Full-duplex I/O streaming** – stdout and stderr are streamed back to the client in real time.
- **Reliable message delivery** – custom reliable transport layer with sequence numbers, acknowledgements, and retransmission over FIFOs / pipes.
- **Low overhead** – written in C++ (server) and Python (manager / client), using only POSIX facilities.
- **Cross‑platform** – compiles and runs on Linux, macOS, and other Unix‑like systems.
- **Secure by design** – communication occurs through named pipes with restricted permissions (`0600`); no network exposure.

## Architecture

```
[Client]  --(JSON over FIFO)-->  [Manager]  --(Binary Protocol)-->  [Server (C++)]
                                     |                                 |
                                     |                            spawns child
                                     |                            processes
                                     +---- forwards I/O, exit codes ----+
```

- **Manager** (`client.py --type manager`)  
  Listens on a control FIFO, accepts session requests from clients, and bridges them to the server process. It manages multiple concurrent sessions and tasks.

- **Server** (`rmpsm_server.*` compiled binary)  
  Spawns child processes, pipes their stdin/stdout/stderr, and communicates with the manager using a custom binary protocol. It handles reliable transmission, retransmission, and graceful shutdown.

- **Client** (`client.py --type client -- <command>`)  
  Connects to the manager, requests execution of a command, and forwards its own stdin to the remote process while printing the remote stdout/stderr locally.

## Requirements

- Python 3.7+
- A C++17 compiler (Clang or GCC)
- POSIX environment (Linux, macOS, WSL, etc.)

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shc0743/remote-process-server.git
   cd remote-process-server
   ```

2. Compile the server binary:
   ```bash
   ./compile.sh
   ```
   This produces `rmpsm_server.$(python3 sys_name.py)`, e.g. `rmpsm_server.linux_x86_64`.

## Usage

### 1. Start the manager daemon

Run the manager in the background, specifying the path to the compiled server binary.

```bash
python3 client.py --type manager --server ./rmpsm_server.linux_x86_64 &
```

The manager creates a control FIFO at `$TMPDIR/rmpsm_manager.ctl` (or under `/tmp`). It will stay alive until explicitly killed.

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
python3 client.py --type manager [--manager PREFIX] [--server PATH]
```
- `--manager PREFIX` – prefix for FIFO paths (default: `$TMPDIR/rmpsm_manager`)
- `--server PATH` – path to the compiled server binary (default: `./rmpsm_server.$(uname -s)_$(uname -m)`)

### Client mode
```
python3 client.py --type client [--manager PREFIX] [--kill] -- COMMAND...
```
- `--manager PREFIX` – same prefix used by the manager
- `--kill` – send a termination request to the manager instead of running a command
- `COMMAND...` – the command and its arguments to execute remotely

## Protocol Overview

### Manager ↔ Client (JSON over FIFO)

| Operation          | Direction      | Description |
|--------------------|----------------|-------------|
| `open`             | Client → Mgr   | Request a new session. Returns `req_fifo` and `resp_fifo` paths. |
| `create_task`      | Client → Mgr   | Launch a command on the remote side. |
| `stdin` / `stdin_eof` | Client → Mgr | Send input data or EOF to the remote process. |
| `kill`             | Client → Mgr   | Terminate a running task. |
| `close`            | Client → Mgr   | End the session and clean up resources. |

Responses from the manager include `stdout`, `stderr`, `task_end`, and `server_dead` events.

### Manager ↔ Server (Binary Protocol)

A custom reliable protocol runs over the server’s stdin/stdout. Each packet has a 72‑byte header:

| Field      | Size (bytes) | Description |
|------------|--------------|-------------|
| magic      | 8            | `0x961f132bdddc19b9` |
| version    | 8            | `2` |
| type       | 8            | Message type (see below) |
| flags      | 8            | Reserved |
| `request_id` | 8            | Client‑generated request identifier |
| `task_id`    | 8            | Identifier of the remote task |
| seq        | 8            | Sender’s sequence number |
| ack        | 8            | Piggybacked acknowledgement of last delivered packet |
| length     | 8            | Payload length (up to 1 GiB) |

**Message types:**
- `0` – Reply / acknowledgement of a request
- `1` – Stop server
- `2` – Create task (payload: command line)
- `3` – Kill task
- `4` – Task end notification (exit code / signal)
- `5` – Stdin data / EOF
- `6` – Stdout data
- `7` – Stderr data
- `255` – Version query (returns `"2.0.0"`)
- `18446744073709551615` – Standalone acknowledgement (no payload)

The server maintains per‑task pipes and uses `poll()` to multiplex I/O. Reliable packets are queued and retransmitted if not acknowledged within 500 ms.

## Files

- `client.py` – Manager, client runtime, and CLI entry point.
- `server.cpp` – Native process launcher and protocol implementation.
- `compile.sh` – Build script for the C++ server.
- `sys_name.py` – Helper to generate platform‑specific binary names.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

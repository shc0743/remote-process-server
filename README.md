# remote-process-server

[![npm version](https://img.shields.io/npm/v/remote-process-server.svg)](https://www.npmjs.com/package/remote-process-server)
[![license](https://img.shields.io/npm/l/remote-process-server.svg)](LICENSE)

A client/server system for creating and managing processes across machines, shells, and transport layers.

`remote-process-server` v3 is still not stable. The main goal is to keep the server side small, dependency-free, and easy to launch in unusual environments.

## Table of contents

- [What it is](#what-it-is)
- [Why it exists](#why-it-exists)
- [How it is structured](#how-it-is-structured)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Typical launch patterns](#typical-launch-patterns)
- [Commands](#commands)
- [Server binary export](#server-binary-export)
- [Platform notes](#platform-notes)
- [Security notes](#security-notes)
- [Development](#development)
- [License](#license)

## What it is

This project provides a small process-management stack with three main pieces:

- a **C++ server** that actually creates and manages processes
- a **Python manager/client layer** that handles session management and higher-level orchestration
- a **Node.js CLI wrapper** that exposes the user-facing commands

The server is designed to be **zero-dependency** and to communicate through standard streams (`stdin` / `stdout`). That makes it possible to launch the server through many different kinds of environments, including:

- local execution
- SSH
- Codespaces
- `npx`
- custom launchers
- any pipe-based or RPC-like transport that can keep the server connected

## Why it exists

The core idea is to let the user choose *how the server starts* without forcing the project to depend on a heavy runtime on the target machine.

That means the `--server` argument can point to very different launch commands, for example:

```bash
remote-process-server daemon --server='ssh user@computer path/to/prebuilt/server-binary'
```

```bash
remote-process-server daemon --server="npx remote-process-server@$(remote-process-server version) run-server"
```

```bash
remote-process-server daemon --server="gh codespace ssh -c your-codespace-name -- npx remote-process-server@$(remote-process-server version) run-server"
```

In theory, it can also be pointed at something like:

```bash
sudo /path/to/server.bin
```

That may work in some setups, but it is not recommended unless you understand the security consequences.

## How it is structured

At a high level:

- `daemon` starts the manager
- the manager launches the configured server command
- `run` sends a command to an existing manager
- `stop` / `kill` requests the manager to exit
- `run-server` starts the C++ server binary directly
- `copy-server` exports the server binary to a target path

The server side is intentionally minimal so it can be copied to a target machine and run there without extra dependencies.

## Installation

Install from npm:

```bash
npm i -g remote-process-server
```

Or run it directly with `npx`:

```bash
npx remote-process-server version
```

## Quick start

Start the manager with the default local server command:

```bash
remote-process-server daemon
```

Run a command through the manager:

```bash
remote-process-server run -- echo hello
```

Stop the manager:

```bash
remote-process-server stop
```

## Typical launch patterns

### Local binary

```bash
remote-process-server daemon --server ./rmpsm_server.linux_x86_64
```

### SSH launch

```bash
remote-process-server daemon --server='ssh user@computer path/to/prebuilt/server-binary'
```

### Launch through `npx`

```bash
remote-process-server daemon --server="npx remote-process-server@$(remote-process-server version) run-server"
```

## Commands

### Manager commands

- `daemon` — start the manager process
- `serve` — deprecated alias for `daemon`

### Client commands

- `run` — run a command through the manager
- `stop` — request the manager to stop
- `kill` — alias for `stop`

### Server commands

- `run-server` — run the bundled C++ server directly
- `copy-server` — copy the bundled server binary to a target path
- `list-arch` — list supported server binary architectures

### Utility commands

- `arch` — print the current architecture
- `is-supported` — report whether the current architecture is supported by this package
- `helpclient` — show the Python client help
- `version` — print the package version

### Special forwarding

The `--` action forwards the remaining arguments directly to the Python client:

```bash
npx remote-process-server -- --help
```

That can be useful when you want to access the Python CLI more directly.

## Server binary export

If you want to copy the bundled server binary out of the package, use `copy-server`:

```bash
remote-process-server copy-server ./server.bin
```

You can also choose a specific architecture:

```bash
remote-process-server copy-server ./server.bin x86_64-linux-gnu
```

List supported architectures with:

```bash
remote-process-server list-arch
```

Check the detected current architecture with:

```bash
remote-process-server arch
```

And verify whether the current architecture is supported:

```bash
remote-process-server is-supported
```

## Platform notes

The project is designed to work across platforms, but the implementation details differ.

- POSIX platforms use standard pipe and process primitives
- Windows uses a separate bootstrap / IPC compatibility layer
- the exact bootstrap details may continue to evolve while the project is not stable now

The default manager endpoint is platform-specific and can be overridden with `--manager`.

## Security notes

This project is about process creation, IPC, and command execution. Be careful with launch commands and elevated contexts.

Recommended practices:

- prefer explicit server paths over shell-heavy command strings where possible
- avoid elevated launch commands unless you fully trust the environment
- do not expose the manager endpoint to untrusted users or processes

## Development

### Project layout

- `entry.js` — npm CLI entry point
- `client/` — Python manager, client, protocol, and transport code
- `server/` — C++ server implementation
- `server.cpp` — server entry point
- `compile.sh` / `compile.cmd` — helper scripts for building the server
- `sys_name.py` — architecture detection helper

### Useful commands

```bash
npm test
```

```bash
remote-process-server helpclient
```

```bash
remote-process-server version
```

## License

MIT License. See [LICENSE](LICENSE).

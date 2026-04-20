# remote-process-server

A client/server system for creating and managing processes over a framed process-control protocol on top of a trusted byte stream. The project is designed to keep the server side small, dependency-free, and easy to launch in many environments, while providing a flexible and controlled runtime installation model.

---

## What it is

This project provides a small process-management stack with three main pieces:

* a **C++ server** that actually creates and manages processes
* a **Python manager/client layer** that handles session management and orchestration
* a **Node.js CLI wrapper** that acts as a bootstrapper, installer, and user-facing entry point

The server communicates through standard streams (`stdin` / `stdout`) using a framed protocol, which allows it to run over many kinds of transports.

---

## Quick start

Please note that **the client needs Python 3 runtime** to run. The server binary can run in any supported platform, and you can use `copy-server` subcommand to extract the server binary for a specified platform.

Before you start, please choose whether you want to install for just you or for all users.

- Install for **just you**:

```
npm i -g remote-process-server@latest
```

- Install the runtime for **all users**:

```bash
npx remote-process-server@latest install
# or
pnpx remote-process-server@latest install
# Update an existing installation:
npx remote-process-server@latest update
```

- Please see the detailed installation guide below if you are using Windows.

---

Start the manager:

```bash
YOUR_INSTALLATION_PATH/remote-process-server daemon
# if the path is already in PATH env, or you used npm i -g:
remote-process-server daemon
```

Run a command:

```bash
remote-process-server run -- echo hello
```

Stop the manager:

```bash
remote-process-server stop
```

Uninstall:

```bash
# For just you:
npm uni -g remote-process-server
# For all users:
remote-process-server uninstall
```

---

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

**Important note**: `--server` argument specify the **full command line**, not just the path to the actual executable. This means that you **need** to wrap a group of extra quatation marks in the argument if your path has space or etc. For example:

```bash
# ❌ wrong
remote-process-server daemon --server="path with space/executable file"

# ✅ correct
remote-process-server daemon --server="\"path with space/executable file\""
```

---

## Detailed installation guide

### npm/npx installation

Install directly via `npx` without a global install:

```bash
npx remote-process-server@latest install [Destination]
```

Or install the CLI wrapper globally to the user's package directory:

```bash
npm i -g remote-process-server
```

### Windows installation

If you are using Windows, it's then strongly recommended to install the runtime to the system's Program Files directory so that low-permission processes will be unable to tamper with the runtime code:

```bash
remote-process-server install
```

You can also specify a custom installation path:

```bash
remote-process-server install /path/to/remote-process-server
```

### Detailed installation model explanation

<details>
<summary>The npm package is primarily a <b>bootstrap layer</b>. (Click to expand)</summary>

* `npm install -g remote-process-server` installs the CLI wrapper
* `remote-process-server install` installs the actual runtime into a system directory
* the runtime is stored in versioned directories: `package/<version>/`
* `installation.data` tracks the active version
* `remote-process-server uninstall` removes an installed copy cleanly

This separation allows the runtime to live in a protected location, instead of a user-writable npm directory.
</details>

---

## How it is structured

At a high level:

* `install` prepares a versioned runtime in a system directory
* `daemon` starts the manager
* the manager launches the configured server command
* `run` sends a command to the manager
* `stop` / `kill` stops the manager

The npm package itself acts as a bootstrapper.
The actual runtime lives under the installation root in versioned subdirectories.

---

## Typical launch patterns

### Local binary

```bash
remote-process-server daemon --server ./rmpsm_server.linux_x86_64 # Use a downloaded binary
```

### SSH launch

```bash
remote-process-server daemon --server='ssh user@computer path/to/prebuilt/server-binary'
```

### Launch through `npx`

```bash
remote-process-server daemon --server="npx remote-process-server@$(remote-process-server version) run-server"
```

---

## Commands

### Maintenance commands

* `install` — install or update the runtime
* `uninstall` — remove an installed runtime
* `where` — print the default installation root (if available)

### Manager commands

* `daemon` — start the manager process
* `serve` — deprecated alias for `daemon`

### Client commands

* `run` — run a command through the manager
* `stop` — request the manager to stop
* `kill` — alias for `stop`

### Server commands

* `run-server` — run the bundled C++ server directly
* `copy-server` — export the server binary to a target path
* `list-arch` — list supported server architectures

These commands are mainly intended for development, packaging, or advanced deployment scenarios.

### Utility commands

* `arch` — print the current architecture
* `is-supported` — check if the current platform is supported
* `helpclient` — show Python client help
* `version` — print the version

### Special forwarding

The `--` action forwards arguments directly to the Python client:

```bash
npx remote-process-server -- --help
```

---

## Installed layout

After installation, the directory structure looks like:

```
<install-root>/
├── remote-process-server.js
├── remote-process-server.cmd (Windows) / shell wrapper (POSIX)
├── installation.data
└── package/
    ├── <version>/
    │   ├── entry.js
    │   ├── package.json
    │   ├── client.py
    │   └── ...
```

* each version is stored separately
* `installation.data` selects the active version
* updates add new versions and switch the active pointer

---

## Server binary export

Copy the bundled server binary:

```bash
remote-process-server copy-server ./server.bin
```

Specify architecture:

```bash
remote-process-server copy-server ./server.bin x86_64-linux-gnu
```

List supported architectures:

```bash
remote-process-server list-arch
```

---

## Platform notes

* POSIX systems use standard process and pipe primitives
* Windows uses a dedicated IPC and bootstrap layer
* installation paths and wrappers differ per platform

The manager endpoint is platform-specific and can be overridden with `--manager`.

---

## Security notes

This project deals with process execution and IPC. Treat it accordingly.

* prefer explicit server paths over complex shell commands
* avoid elevated execution unless necessary
* do not expose the manager endpoint to untrusted environments

The runtime is installed into a protected directory to reduce the risk of unprivileged code modification.

Integrity verification (e.g. signatures or hashing) is not implemented yet, so the security model currently depends on filesystem permissions and trusted installation paths.

If you found a security problem, please [follow the steps in SECURITY.md](./SECURITY.md) to report it.

---

## Development

### Project layout

* `entry.js` — CLI entry point and command dispatcher
* `maintainance.js` — install / update / uninstall logic
* `client/` — Python manager and protocol
* `server/` — C++ implementation
* `server.cpp` — server entry point
* `compile.sh` / `compile.cmd` — build helpers
* `sys_name.py` — architecture detection

### Important note about pre-release versions

Pre-release versions and temporarily versions are only for development purposes and **do NOT** receive security updates. They'll also NOT be deprecated if there are some problems or bugs inside. If you are a normal user, please always use stable version and try to keep up-to-date.

The detailed relationships between dist-tags and versions are as follows:

| Version type | Dist tag | Example |
|--------------|----------|---------|
| Stable | `latest` | v3.0.0 |
| Pre-release | `alpha`, `beta`, `rc` | v3.1.0-rc.5 |
| Temporary | `hotfix` | v3.1.0-hotfix.1 |
| Internal | `internal-*` | v0.0.0 |

---

## License

MIT License. See [LICENSE](LICENSE).

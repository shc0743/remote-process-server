#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import os
import sys

from rmpsm_protocol import default_connection_file, probe_connection_info
from rmpsm_runtime import ClientRuntime, Manager, kill_manager
from rmpsm_errors import ManagerNotRunningError, ConnectionRefusedError


def main() -> int:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["manager", "client"], default="client")
    parser.add_argument(
        "--manager",
        default=default_connection_file(),
        help="Path to Manager connection file",
    )
    parser.add_argument(
        "--server",
        default=os.path.join(current_dir, "../rmpsm_server." + str(platform.system().lower()) + "_" + str(platform.machine().lower())) + ('.' if os.name == 'nt' else ''),
        help="[Manager only] specify the server startup command"
    )
    parser.add_argument("--stderr", choices=["ignore", "merge", "inherit"], default="inherit", help="[Manager only] How to handle stderr: ignore, merge to stdout, or inherit")
    parser.add_argument("--signal", type=int, default=0, help="[Manager only] Specify a eventfd(POSIX) or hEvent(Windows) and it will be set when server is ready")
    parser.add_argument("--kill", action="store_true", help="[Client only] Kill the manager process")
    parser.add_argument("--cmd-syntax", action="store_true", help="[Client only][Windows only] Use CMD's quota syntax")
    args, remainder = parser.parse_known_args()

    if remainder and remainder[0] == "--":
        remainder = remainder[1:]

    if args.kill and args.type == "manager":
        print("error: --kill can only be used with client mode", file=sys.stderr)
        return 87

    if args.stderr != 'inherit' and args.type == "client":
        print("error: --stderr can only be used with manager mode", file=sys.stderr)
        return 87

    if args.type == "manager":
        # Check if another manager with the same configuration is already running
        # by trying to read one bootstrap payload from the endpoint.
        if probe_connection_info(args.manager, timeout=1.0):
            print("Error: another manager with the same configuration is already running", file=sys.stderr)
            return 17
        mgr = Manager(args.manager, args.server, args.stderr, args.signal)
        mgr.run()
        return 0

    if args.kill:
        return kill_manager(args.manager)

    if not remainder:
        print("No input specified.", file=sys.stderr)
        return 22

    client = ClientRuntime(args.manager, remainder, args.cmd_syntax)
    try:
        return client.run()
    except ManagerNotRunningError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except ConnectionRefusedError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 5
    except TimeoutError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return -1
    except OSError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return -1
    except BaseException as e:
        try:
            import traceback
            traceback_str = traceback.format_exc()
            print(traceback_str, file=sys.stderr)
        except BaseException:
            pass
        os._exit(-1)


if __name__ == "__main__":
    raise SystemExit(main())

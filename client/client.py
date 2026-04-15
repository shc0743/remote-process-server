#!/usr/bin/env python3
from __future__ import annotations

import argparse
import platform
import sys

from rmpsm_protocol import default_connection_file
from rmpsm_runtime import ClientRuntime, Manager, kill_manager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["manager", "client"], default="client")
    parser.add_argument(
        "--manager",
        default=default_connection_file(),
        help="Path to Manager connection file",
    )
    parser.add_argument(
        "--server",
        default="./rmpsm_server." + str(platform.system().lower()) + "-" + str(platform.machine().lower()),
        help="[Manager only] specify the server startup command"
    )
    parser.add_argument("--stderr", choices=["ignore", "merge", "inherit"], default="inherit", help="[Manager only] How to handle stderr: ignore, merge to stdout, or inherit")
    parser.add_argument("--kill", action="store_true", help="[Client only] Kill the manager process")
    parser.add_argument("--cmd-syntax", action="store_true", help="[Client only][Windows only] Use CMD's quota syntax")
    args, remainder = parser.parse_known_args()

    if remainder and remainder[0] == "--":
        remainder = remainder[1:]

    if args.kill and args.type == "manager":
        print("error: --kill can only be used with client mode", file=sys.stderr)
        return 2

    if args.stderr != 'inherit' and args.type == "client":
        print("error: --stderr can only be used with manager mode", file=sys.stderr)
        return 2

    if args.type == "manager":
        # Check if another manager with same configuration is already running
        # by testing whether the connection file exists and is connectable.
        import os
        import socket
        import json
        from rmpsm_protocol import read_connection_info
        if os.path.exists(args.manager):
            try:
                address, _authkey = read_connection_info(args.manager)
                # Try to connect to the address with a short timeout
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(address)
                s.close()
                # Connection succeeded, meaning a manager is already running
                print("Error: another manager with the same configuration is already running", file=sys.stderr)
                return 1
            except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError, socket.timeout, ConnectionRefusedError):
                # Connection file is invalid or cannot connect, assume no manager is running
                pass
        mgr = Manager(args.manager, args.server, args.stderr)
        mgr.run()
        return 0

    if args.kill:
        return kill_manager(args.manager)

    if not remainder:
        print("client mode requires a command after --\nuse --help to show help information", file=sys.stderr)
        return 2

    client = ClientRuntime(args.manager, remainder, args.cmd_syntax)
    return client.run()


if __name__ == "__main__":
    raise SystemExit(main())

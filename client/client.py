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
        default="./rmpsm_server." + str(platform.system().lower()) + "_" + str(platform.machine().lower()),
    )
    parser.add_argument("--kill", action="store_true", help="Kill the manager process (client only)")
    args, remainder = parser.parse_known_args()

    if remainder and remainder[0] == "--":
        remainder = remainder[1:]

    if args.kill and args.type == "manager":
        print("error: --kill can only be used with --type client", file=sys.stderr)
        return 2

    if args.type == "manager":
        mgr = Manager(args.manager, args.server)
        mgr.run()
        return 0

    if args.kill:
        return kill_manager(args.manager)

    if not remainder:
        print("client mode requires a command after --", file=sys.stderr)
        return 2

    client = ClientRuntime(args.manager, remainder)
    return client.run()


if __name__ == "__main__":
    raise SystemExit(main())

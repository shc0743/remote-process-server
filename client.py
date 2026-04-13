#!/usr/bin/env python3

import os
import sys
import subprocess

def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    new_client_path = os.path.join(current_dir, "client", "client.py")
    
    if not os.path.exists(new_client_path):
        print(f"FATAL: core client runtime not found: {new_client_path}", file=sys.stderr)
        sys.exit(1)
    
    cmd = [sys.executable, new_client_path] + sys.argv[1:]
    
    proc = subprocess.Popen(
        cmd,
        executable=sys.executable,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise
    
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()

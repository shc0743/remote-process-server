#!/usr/bin/env bash
SYS_NAME=$(python3 ./sys_name.py)
if [[ "$SYS_NAME" == android* ]]; then
    clang++ -std=c++20 server.cpp -I. -o "rmpsm_server.$SYS_NAME"
else
    clang++ -std=c++20 -static -static-libstdc++ -static-libgcc server.cpp -I. -o "rmpsm_server.$SYS_NAME"
fi

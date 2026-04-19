#!/usr/bin/env bash
if [[ -n "$1" ]]; then
    SYS_NAME="$1"
else
    SYS_NAME=$(python3 ./sys_name.py)
fi
if [[ -n "$2" ]]; then
    COMPILER="$2"
else
    COMPILER=$(which clang++)
fi
if [[ -n "$3" ]]; then
    STRIPPER="$3"
else
    STRIPPER=$(which strip)
fi
if [[ "$SYS_NAME" == android* ]]; then
    $COMPILER -std=c++20 -O3 -flto server.cpp -I. -o "rmpsm_server.$SYS_NAME" || exit 1
else
    $COMPILER -std=c++20 -O3 -flto -static -static-libstdc++ -static-libgcc server.cpp -I. -o "rmpsm_server.$SYS_NAME" || exit 1
fi
$STRIPPER "rmpsm_server.$SYS_NAME"
mkdir -p native/bin/
mv "rmpsm_server.$SYS_NAME" native/bin/

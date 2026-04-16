#!/usr/bin/env bash
P_VER=$(jq -r .version package.json)
PL_VER=$(jq -r .version package-lock.json)
if [[ "$P_VER" != "$PL_VER" ]]; then
    echo "Fatal: Package lock file is NOT up to date!! Please check your commit and consider amend it." >&2
    exit 1
fi
exit 0

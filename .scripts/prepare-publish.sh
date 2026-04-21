#!/usr/bin/env bash
set -euo pipefail

REPO="${GITHUB_REPOSITORY:?}"
SHA="${GITHUB_SHA:?}"
TOKEN="${GITHUB_TOKEN:?}"
REF_NAME="${GITHUB_REF_NAME:?}"
WORKFLOW_FILE="${BUILD_WORKFLOW_FILE:-build-server.yml}"

BUILD_RUN_ID="$(
python - <<'PY'
import json
import os
import sys
import time
import urllib.request

repo = os.environ["GITHUB_REPOSITORY"]
sha = os.environ["GITHUB_SHA"]
token = os.environ["GITHUB_TOKEN"]
workflow_file = os.environ.get("BUILD_WORKFLOW_FILE", "build-server.yml")

def fetch_runs():
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs?head_sha={sha}&status=completed&per_page=20"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    return data.get("workflow_runs", [])

for _ in range(19):
    runs = fetch_runs()
    for run in runs:
        if run.get("conclusion") == "success":
            print(run["id"])
            sys.exit(0)
    time.sleep(15)

sys.exit(1)
PY
)"

echo "build_run_id=$BUILD_RUN_ID"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "build_run_id=$BUILD_RUN_ID" >> "$GITHUB_OUTPUT"
fi

TAG_NAME="${REF_NAME#v}"
NPM_DIST_TAG="latest"

if [[ "$TAG_NAME" == *-* ]]; then
  prerelease="${TAG_NAME#*-}"
  NPM_DIST_TAG="${prerelease%%.*}"
fi

echo "npm_dist_tag=$NPM_DIST_TAG"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "npm_dist_tag=$NPM_DIST_TAG" >> "$GITHUB_OUTPUT"
fi

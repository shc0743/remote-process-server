#!/usr/bin/env bash
set -euo pipefail

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
SHA="${GITHUB_SHA:?GITHUB_SHA is required}"
REF_NAME="${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"
GH_TOKEN="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"

WORKFLOW_FILE="${BUILD_WORKFLOW_FILE:-build-server.yml}"
ARTIFACT_DIR="${ARTIFACT_DIR:-./server-artifacts}"

mkdir -p "$ARTIFACT_DIR"

python - <<'PY' > /tmp/build_run_id.txt
import json
import os
import sys
import time
import urllib.request

repo = os.environ["REPO"]
sha = os.environ["SHA"]
token = os.environ["GH_TOKEN"]
workflow_file = os.environ["WORKFLOW_FILE"]

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

for _ in range(60):
    runs = fetch_runs()
    for run in runs:
        if run.get("conclusion") == "success":
            print(run["id"])
            sys.exit(0)
    time.sleep(20)

sys.exit(1)
PY

BUILD_RUN_ID="$(cat /tmp/build_run_id.txt)"
echo "Found successful build-server run: $BUILD_RUN_ID"

python - <<'PY'
import io
import json
import os
import pathlib
import urllib.request
import zipfile

repo = os.environ["REPO"]
token = os.environ["GH_TOKEN"]
run_id = os.environ["BUILD_RUN_ID"]
artifact_dir = pathlib.Path(os.environ["ARTIFACT_DIR"])
artifact_dir.mkdir(parents=True, exist_ok=True)

list_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts"
req = urllib.request.Request(
    list_url,
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
)
with urllib.request.urlopen(req) as resp:
    data = json.load(resp)

artifacts = data.get("artifacts", [])
if not artifacts:
    raise SystemExit(f"no artifacts found for run {run_id}")

for art in artifacts:
    name = art.get("name")
    url = art.get("archive_download_url")
    if not name or not url:
        continue

    print(f"Downloading {name}...")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        blob = resp.read()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(artifact_dir)
PY

find "$ARTIFACT_DIR" -type f -name 'rmpsm_server.*' -exec cp -f {} . \;
chmod +x rmpsm_server.* || true
ls -l rmpsm_server.* || true

TAG_NAME="${REF_NAME#v}"
NPM_DIST_TAG="latest"

if [[ "$TAG_NAME" == *-* ]]; then
  prerelease="${TAG_NAME#*-}"
  NPM_DIST_TAG="${prerelease%%.*}"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "npm_dist_tag=$NPM_DIST_TAG" >> "$GITHUB_OUTPUT"
fi
echo "npm_dist_tag=$NPM_DIST_TAG"

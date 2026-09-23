#!/usr/bin/env bash
# Isolate the smoke runner and its own Ollama server in a fresh Linux network namespace.
set -euo pipefail

if [[ $# -eq 0 || ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'USAGE'
Usage: bash backend/scripts/offline_smoke.sh AUDIO --meeting-date YYYY-MM-DD \
       --data-dir DIRECTORY --report REPORT.json [other brev_smoke.py arguments]

Required environment (already downloaded, local model directories):
  ASR_MODEL_PATH
  PYANNOTE_DIARIZATION_MODEL
  OLLAMA_MODELS
Optional:
  MEETING_PYTHON  Python executable for the existing model environment (default: python3)

Requires Linux, unshare, ip, curl, ollama, and permission to create user/network
namespaces. Failure to create a namespace is fatal; there is no networked fallback.
USAGE
    if [[ $# -eq 0 ]]; then exit 2; fi
    exit 0
fi

: "${ASR_MODEL_PATH:?Set ASR_MODEL_PATH to the downloaded ASR directory}"
: "${PYANNOTE_DIARIZATION_MODEL:?Set PYANNOTE_DIARIZATION_MODEL to the downloaded pipeline directory}"
: "${OLLAMA_MODELS:?Set OLLAMA_MODELS to the existing Ollama model store}"
MEETING_PYTHON=${MEETING_PYTHON:-python3}

for dependency in unshare ip curl ollama readlink bash "$MEETING_PYTHON"; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required executable is unavailable: %s\n' "$dependency" >&2
        exit 2
    fi
done
for directory in "$ASR_MODEL_PATH" "$PYANNOTE_DIARIZATION_MODEL" "$OLLAMA_MODELS"; do
    if [[ ! -d "$directory" || ! -r "$directory" || ! -x "$directory" ]]; then
        printf 'Model directory is absent or unreadable: %s\n' "$directory" >&2
        exit 2
    fi
done

MEETING_OFFLINE_REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
MEETING_PARENT_NETNS=$(readlink /proc/self/ns/net)
export ASR_MODEL_PATH PYANNOTE_DIARIZATION_MODEL OLLAMA_MODELS MEETING_PYTHON
export MEETING_OFFLINE_REPO_ROOT MEETING_PARENT_NETNS

meeting_offline_namespace() {
    set -euo pipefail
    local active_netns
    active_netns=$(readlink /proc/self/ns/net)
    if [[ "$active_netns" == "$MEETING_PARENT_NETNS" ]]; then
        printf 'Refusing smoke run: network namespace did not change.\n' >&2
        exit 2
    fi
    ip link set lo up
    export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
    export PYANNOTE_METRICS_ENABLED=0
    export OLLAMA_HOST=127.0.0.1:11434
    export MEETING_NETWORK_ISOLATION=linux_network_namespace
    export MEETING_NETWORK_NAMESPACE="$active_netns"
    export PYTHONPATH="$MEETING_OFFLINE_REPO_ROOT/backend"
    unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
    export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

    printf 'OS network isolation active: %s (parent %s); only loopback enabled.\n' \
        "$active_netns" "$MEETING_PARENT_NETNS" >&2

    # The host Ollama is outside this namespace. Stop only the child started here.
    ollama serve >&2 &
    local ollama_pid=$!
    # Capture the numeric PID now: local variables disappear when this function returns.
    trap "kill -- $ollama_pid 2>/dev/null || true; wait $ollama_pid 2>/dev/null || true" EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    local ready=0
    for ((attempt = 0; attempt < 60; attempt++)); do
        if ! kill -0 "$ollama_pid" 2>/dev/null; then
            printf 'The isolated Ollama server exited before becoming ready.\n' >&2
            exit 1
        fi
        if curl --noproxy '*' --fail --silent --max-time 1 \
            --output /dev/null http://127.0.0.1:11434/api/tags; then
            ready=1
            break
        fi
        sleep 0.5
    done
    if [[ "$ready" != 1 ]]; then
        printf 'The isolated Ollama server did not become ready.\n' >&2
        exit 1
    fi

    "$MEETING_PYTHON" "$MEETING_OFFLINE_REPO_ROOT/backend/scripts/brev_smoke.py" "$@"
}
export -f meeting_offline_namespace

printf 'Creating user/network namespaces; unshare failure stops this run.\n' >&2
exec unshare --user --map-root-user --net -- bash -c 'meeting_offline_namespace "$@"' offline-smoke "$@"

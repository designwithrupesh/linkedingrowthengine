#!/usr/bin/env bash
# Free CPU inference. Runtime and model assets are pinned to publisher hashes.
# Model: Unsloth's Q4_K_M quantization of Qwen/Qwen3-4B-Instruct-2507.
# Both the Qwen source and this GGUF are licensed Apache-2.0.
# Source model: https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'The bundled CPU runtime requires Linux x86_64.' >&2
  exit 1
fi

TWIN_MODEL_DIR="${TWIN_MODEL_DIR:-${HOME}/.cache/linkedin-twin}"
mkdir -p "$TWIN_MODEL_DIR"
TWIN_MODEL_DIR="$(cd "$TWIN_MODEL_DIR" && pwd)"
runtime_archive="$TWIN_MODEL_DIR/llama-b11512-bin-ubuntu-x64.tar.gz"
runtime_sha='cf4083d1e89ccce41157b096d5c9d36e2ef4c8751cf96b709ed533a3a4fe98d3'
runtime_url='https://github.com/ggml-org/llama.cpp/releases/download/b11512/llama-b11512-bin-ubuntu-x64.tar.gz'
model_file="$TWIN_MODEL_DIR/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
model_sha='3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597'
model_url='https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf'
server="$TWIN_MODEL_DIR/llama-b11512/llama-server"
pid_file="$TWIN_MODEL_DIR/server.pid"
log_file="$TWIN_MODEL_DIR/server.log"
model_marker="$TWIN_MODEL_DIR/server.model.sha256"

verify_file() {
  [[ -f "$1" ]] && printf '%s  %s\n' "$2" "$1" | sha256sum --check --status
}

download_verified() {
  local destination="$1" expected="$2" url="$3"
  if verify_file "$destination" "$expected"; then
    echo "Verified cached asset: $(basename "$destination")"
    return
  fi
  local partial="${destination}.part"
  echo "Downloading publisher asset: $(basename "$destination")"
  curl --fail --location --proto '=https' --tlsv1.2 --retry 2 \
    --connect-timeout 15 --max-time 600 --output "$partial" "$url"
  if ! verify_file "$partial" "$expected"; then
    rm -f "$partial"
    echo "Asset checksum failed: $(basename "$destination")" >&2
    exit 1
  fi
  mv "$partial" "$destination"
}

# Serialize cache preparation; the child server must not inherit this lock.
exec 9>"$TWIN_MODEL_DIR/start.lock"
flock --wait 60 9
download_verified "$runtime_archive" "$runtime_sha" "$runtime_url"
# Re-extract the verified archive so modified cached executables cannot be reused.
tar --extract --gzip --file "$runtime_archive" --directory "$TWIN_MODEL_DIR"
download_verified "$model_file" "$model_sha" "$model_url"

healthy() {
  curl --silent --fail --noproxy '*' --max-time 2 \
    'http://127.0.0.1:8080/health' >/dev/null
}

if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  if healthy; then
    if [[ ! -f "$model_marker" || "$(cat "$model_marker")" != "$model_sha" ]]; then
      echo 'An existing local model uses a different or unverified pinned model. Stop that process before starting this model.' >&2
      exit 1
    fi
    echo "Local model is ready. PID: $(cat "$pid_file"); log: $log_file"
    exit 0
  fi
  echo "An existing local model process is unhealthy. Inspect $log_file." >&2
  exit 1
fi

nohup "$server" --model "$model_file" --alias twin-local \
  --host 127.0.0.1 --port 8080 --gpu-layers 0 --ctx-size 8192 \
  --parallel 1 --threads 4 --threads-batch 4 --predict 900 \
  --jinja --reasoning off --reasoning-budget 0 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  >"$log_file" 2>&1 < /dev/null 9>&- &
model_pid=$!
printf '%s\n' "$model_pid" >"$pid_file"

deadline=$((SECONDS + 90))
while ((SECONDS < deadline)); do
  if ! kill -0 "$model_pid" 2>/dev/null; then
    echo "Local model stopped before readiness. Inspect $log_file." >&2
    exit 1
  fi
  if healthy; then
    printf '%s\n' "$model_sha" >"$model_marker"
    echo "Local model is ready. PID: $model_pid; log: $log_file"
    echo 'Endpoint: http://127.0.0.1:8080/v1/chat/completions; model: twin-local'
    exit 0
  fi
  sleep 1
done
kill "$model_pid" 2>/dev/null || true
echo "Local model readiness timed out. Inspect $log_file." >&2
exit 1

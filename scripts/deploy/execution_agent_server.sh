#!/usr/bin/env bash
#
# Deploy execution_agent_server to remote host via Docker (wss backend).
#
# Reads credentials from deploy/credentials.env (gitignored).
#
# Env handling: extracts the running container's Config.Env -> /tmp/exec_container.env
# and reuses it via --env-file (preserves DASHSCOPE_API_KEY / LLM_* etc.), overriding
# only BACKEND_WS_URL to wss. First deploy (no existing container) falls back to
# explicit -e and requires DASHSCOPE_API_KEY in the local env.
#
# Flow: preserve env + rollback tag -> clean -> tar src/+dockerfile+requirements ->
# scp -> remote build -> run (--env-file + BACKEND_WS_URL=wss) -> verify
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CRED_FILE="$REPO_ROOT/scripts/deploy/credentials.env"

if [[ ! -f "$CRED_FILE" ]]; then
  echo "ERROR: $CRED_FILE not found. Create it with SSH_HOST/SSH_USER/SSH_PASSWORD."
  exit 1
fi
# shellcheck source=/dev/null
source "$CRED_FILE"

: "${SSH_HOST:?SSH_HOST required in credentials.env}"
: "${SSH_USER:?SSH_USER required in credentials.env}"
: "${SSH_PASSWORD:?SSH_PASSWORD required in credentials.env}"

# Load local .env from execution_agent_server if present (provides defaults for LLM_*, SERVER_ID, etc.)
# Explicit env vars passed on the command line must take precedence over .env values.
LOCAL_ENV="$REPO_ROOT/src/execution_agent_server/.env"
if [[ -f "$LOCAL_ENV" ]]; then
  # Save explicitly-set vars before sourcing .env
  _saved_vars=()
  for v in SERVER_ID SERVER_NAME MAX_QUOTA HEARTBEAT_INTERVAL DASHSCOPE_API_KEY \
           LLM_BASE_URL LLM_MODEL LLM_MAX_TOKENS LLM_ENABLE_THINKING LLM_TIMEOUT; do
    if [[ -n "${!v+x}" ]]; then
      _saved_vars+=("$v=${!v}")
    fi
  done
  set -a
  # shellcheck source=/dev/null
  source "$LOCAL_ENV"
  set +a
  # Restore explicit overrides
  for sv in ${_saved_vars[@]+"${_saved_vars[@]}"}; do
    export "$sv"
  done
  echo "    loaded local env: $LOCAL_ENV"
fi

REMOTE_DIR="${REMOTE_DIR:-/home/$SSH_USER/execution_agent_server}"
IMAGE="${IMAGE:-execution-agent-server:latest}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-execution-agent-server:rollback}"
CONTAINER="${CONTAINER:-exec-server-1}"
DOCKERFILE="$REPO_ROOT/scripts/build/execution_agent_server/Dockerfile"
ENV_FILE="/tmp/exec_container.env"

# Backend WSS URL — always use production URL unless explicitly overridden via env.
# The local .env typically has ws://localhost which is wrong for remote deploy.
BACKEND_WS_URL="${DEPLOY_BACKEND_WS_URL:-wss://agent-socket-server.bdzz.com.cn:8765}"
# Only needed for first deploy (no existing container); otherwise reused via --env-file
DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}"
SERVER_ID="${SERVER_ID:-exec-server-1}"
SERVER_NAME="${SERVER_NAME:-ExecutionServer1}"
MAX_QUOTA="${MAX_QUOTA:-4}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-5}"

# WS_SERVER_API_KEY: read from agent_server_platform .env if not set explicitly
if [[ -z "${WS_SERVER_API_KEY:-}" ]]; then
  _ASP_ENV="$REPO_ROOT/src/agent_server_platform/.env"
  if [[ -f "$_ASP_ENV" ]]; then
    WS_SERVER_API_KEY=$(grep '^WS_SERVER_API_KEY=' "$_ASP_ENV" 2>/dev/null | head -1 | cut -d= -f2-)
  fi
fi

# LLM config (first deploy; otherwise preserved via --env-file)
LLM_BASE_URL="${LLM_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
LLM_MODEL="${LLM_MODEL:-qwen-plus}"
LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-65535}"
LLM_ENABLE_THINKING="${LLM_ENABLE_THINKING:-true}"
LLM_TIMEOUT="${LLM_TIMEOUT:-120}"

# expect wrappers
ssh_pw() {
  local remote_cmd="$1"
  expect <<EOF
set timeout 900
log_user 1
spawn ssh -o StrictHostKeyChecking=accept-new $SSH_USER@$SSH_HOST "$remote_cmd"
expect {
  -re "(P|p)assword:" { send "$SSH_PASSWORD\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EOF
}

scp_pw() {
  local src="$1"; local dst="$2"
  expect <<EOF
set timeout 600
log_user 1
spawn scp -o StrictHostKeyChecking=accept-new "$src" "$SSH_USER@$SSH_HOST:$dst"
expect {
  -re "(P|p)assword:" { send "$SSH_PASSWORD\r"; exp_continue }
  eof
}
catch wait result
exit [lindex \$result 3]
EOF
}

echo "==> [1/6] Preserve container env + rollback tag, then clean old container"
# use `test` (not `[ ]`) and avoid remote $var/$() - expect double-quotes make [ ] tcl
ssh_pw "if sudo docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $CONTAINER > $ENV_FILE 2>/dev/null && test -s $ENV_FILE; then echo '    reused env from $CONTAINER -> $ENV_FILE'; else rm -f $ENV_FILE; echo '    no running container; first deploy'; fi; if sudo docker tag $IMAGE $ROLLBACK_IMAGE 2>/dev/null; then echo '    tagged $IMAGE -> $ROLLBACK_IMAGE'; else echo '    (no old image to tag)'; fi; sudo docker stop $CONTAINER 2>/dev/null || true; sudo docker rm $CONTAINER 2>/dev/null || true"

echo "==> [2/6] Package build context (src/ + Dockerfile + requirements.txt)"
TARBALL="/tmp/exec_server_deploy.tgz"
STAGING=$(mktemp -d)
cp -a "$REPO_ROOT/src" "$STAGING/src"
cp "$DOCKERFILE" "$STAGING/Dockerfile"
cp "$REPO_ROOT/src/execution_agent_server/requirements.txt" "$STAGING/requirements.txt"
find "$STAGING" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -name '*.db' -o -name '*.db-journal' -o -name '.env' | xargs rm -f 2>/dev/null || true
tar -czf "$TARBALL" -C "$STAGING" .
rm -rf "$STAGING"
echo "    $(du -h "$TARBALL" | cut -f1)"

echo "==> [3/6] Upload to remote"
scp_pw "$TARBALL" "/tmp/exec_server_deploy.tgz"
rm -f "$TARBALL"

echo "==> [4/6] Remote docker build"
ssh_pw "mkdir -p $REMOTE_DIR && rm -rf $REMOTE_DIR/* && tar -xzf /tmp/exec_server_deploy.tgz -C $REMOTE_DIR && rm -f /tmp/exec_server_deploy.tgz && sudo docker build -t $IMAGE $REMOTE_DIR"

echo "==> [5/6] Run container (env preserved, BACKEND_WS_URL=$BACKEND_WS_URL)"
# Reuse path: --env-file preserves the old container's secrets; -e overrides the URL.
# First-deploy path: explicit -e (requires DASHSCOPE_API_KEY in local env).
if [[ -n "$DASHSCOPE_API_KEY" ]]; then
  ssh_pw "sudo mkdir -p /agent-data-files/$SERVER_ID && sudo docker run -d --name $CONTAINER --network host -v /agent-data-files/$SERVER_ID:/data -e SERVER_ID=$SERVER_ID -e SERVER_NAME=\\\"$SERVER_NAME\\\" -e MAX_QUOTA=$MAX_QUOTA -e HEARTBEAT_INTERVAL=$HEARTBEAT_INTERVAL -e BACKEND_WS_URL=$BACKEND_WS_URL -e DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY -e WS_SERVER_API_KEY=$WS_SERVER_API_KEY -e LLM_BASE_URL=$LLM_BASE_URL -e LLM_MODEL=$LLM_MODEL -e LLM_MAX_TOKENS=$LLM_MAX_TOKENS -e LLM_ENABLE_THINKING=$LLM_ENABLE_THINKING -e LLM_TIMEOUT=$LLM_TIMEOUT -e EVENT_PERSIST_DISABLED=1 $IMAGE && sleep 3 && sudo docker ps --filter name=$CONTAINER && echo '--- logs ---' && sudo docker logs $CONTAINER --tail 15"
else
  ssh_pw "if test -f $ENV_FILE; then SERVER_ID_VAL=\$(grep ^SERVER_ID= $ENV_FILE | cut -d= -f2- | tr -d '\\\"'); sudo mkdir -p /agent-data-files/\$SERVER_ID_VAL && sudo docker run -d --name $CONTAINER --network host -v /agent-data-files/\$SERVER_ID_VAL:/data --env-file $ENV_FILE -e BACKEND_WS_URL=$BACKEND_WS_URL -e WS_SERVER_API_KEY=$WS_SERVER_API_KEY $IMAGE; else echo 'ERROR: no env-file (no prior container) and no DASHSCOPE_API_KEY in local env; set DASHSCOPE_API_KEY for first deploy'; exit 1; fi && sleep 3 && sudo docker ps --filter name=$CONTAINER && echo '--- logs ---' && sudo docker logs $CONTAINER --tail 15"
fi

echo "==> Done: $CONTAINER -> $BACKEND_WS_URL  (rollback: $ROLLBACK_IMAGE)"

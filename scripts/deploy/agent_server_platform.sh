#!/usr/bin/env bash
#
# Deploy agent_server_platform to remote host via Docker.
#
# Reads credentials from deploy/credentials.env (gitignored).
#
# Flow: clean old container -> tar source+dockerfile -> scp -> remote docker build -> run -> health check
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

REMOTE_DIR="${REMOTE_DIR:-/home/$SSH_USER/agent_server_platform}"
IMAGE="${IMAGE:-agent-server-platform:latest}"
CONTAINER="${CONTAINER:-agent-server-platform}"
SRC_DIR="$REPO_ROOT/src/agent_server_platform"
DOCKERFILE="$REPO_ROOT/scripts/build/agent_server_platform/Dockerfile"

# expect wrappers for SSH/SCP with password auth
ssh_pw() {
  local remote_cmd="$1"
  expect <<EOF
set timeout 600
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

echo "=========================================="
echo "部署 Agent Server Platform 到 $SSH_HOST"
echo "=========================================="

echo "==> [1/6] Clean old containers on remote"
ssh_pw "sudo docker stop $CONTAINER 2>/dev/null || true; sudo docker rm $CONTAINER 2>/dev/null || true"

echo "==> [2/6] Package source (excluding __pycache__/*.db/.git/.env)"
TARBALL="/tmp/backend_deploy.tgz"
# Copy Dockerfile to source dir for docker build context
cp "$DOCKERFILE" "$SRC_DIR/Dockerfile"
tar -czf "$TARBALL" \
  --exclude='__pycache__' --exclude='*.db' --exclude='.git' --exclude='.env' \
  -C "$SRC_DIR" .
rm -f "$SRC_DIR/Dockerfile"
echo "    $(du -h "$TARBALL" | cut -f1) -> $TARBALL"

echo "==> [3/6] Upload to remote"
scp_pw "$TARBALL" "/tmp/backend_deploy.tgz"
rm -f "$TARBALL"

echo "==> [4/6] Extract on remote"
ssh_pw "mkdir -p $REMOTE_DIR && rm -rf $REMOTE_DIR/* && tar -xzf /tmp/backend_deploy.tgz -C $REMOTE_DIR && rm -f /tmp/backend_deploy.tgz"

echo "==> [5/6] Remote docker build + run"
ssh_pw "cd $REMOTE_DIR && sudo docker build -t $IMAGE -f Dockerfile . && sudo docker run -d --name $CONTAINER --network host --restart unless-stopped -e DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY:?DASHSCOPE_API_KEY must be set} -e LLM_BASE_URL=${LLM_BASE_URL:-https://llm-au9yjnfp4n2jfcyb.cn-beijing.maas.aliyuncs.com/compatible-mode/v1} -e LLM_MODEL=${LLM_MODEL:-qwen3.7-plus} -e GRADIO_SERVER_PORT=8080 -e FLASK_SERVER_PORT=5000 -e WS_SERVER_API_URL=${WS_SERVER_API_URL:-https://agent-socket-server.bdzz.com.cn:8765} -e WS_SERVER_WS_URL=${WS_SERVER_WS_URL:-wss://agent-socket-server.bdzz.com.cn:8765} -e SANDBOX_BACKEND_WS_URL=${SANDBOX_BACKEND_WS_URL:-wss://agent-socket-server.bdzz.com.cn:8765} $IMAGE && sleep 3 && sudo docker ps --filter name=$CONTAINER"

echo "==> [6/6] Health check"
ssh_pw "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/"

echo "=========================================="
echo "部署完成!"
echo "=========================================="
echo "访问地址:"
echo "  - Gradio UI: http://$SSH_HOST:8080"
echo "  - Flask API: http://$SSH_HOST:5000"
echo ""
echo "查看日志:"
echo "  ssh $SSH_USER@$SSH_HOST"
echo "  sudo docker logs -f $CONTAINER"

#!/usr/bin/env bash
#
# WebSocket Server 部署脚本
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CRED_FILE="$REPO_ROOT/scripts/deploy/credentials.env"
DOCKERFILE_SRC="$REPO_ROOT/scripts/build/websocket_server/Dockerfile"
WS_SOURCE="$REPO_ROOT/src/websocket_server"

if [[ ! -f "$CRED_FILE" ]]; then
  echo "ERROR: $CRED_FILE not found."; exit 1
fi
if [[ ! -f "$DOCKERFILE_SRC" ]]; then
  echo "ERROR: Dockerfile not found at $DOCKERFILE_SRC"; exit 1
fi
# shellcheck source=/dev/null
source "$CRED_FILE"

: "${SSH_HOST:?SSH_HOST required}"
: "${SSH_USER:?SSH_USER required}"
: "${SSH_PASSWORD:?SSH_PASSWORD required}"

CONTAINER_NAME="${WS_CONTAINER_NAME:-websocket-server}"
IMAGE_NAME="${WS_IMAGE_NAME:-websocket-server:latest}"
ROLLBACK_IMAGE="${WS_ROLLBACK_IMAGE:-websocket-server:rollback}"
REMOTE_BUILD_DIR="${WS_REMOTE_BUILD_DIR:-/tmp/ws_build}"

TAR="/tmp/websocket-server.tar.gz"
REMOTE_SH="/tmp/ws_deploy_remote.sh"

# --- expect wrappers (password auth) ---
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

echo "=========================================="
echo "部署 WebSocket Server 到 $SSH_HOST"
echo "=========================================="

# [1/5] 组装构建上下文
echo "[1/5] 组装构建上下文..."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$TAR" "$REMOTE_SH"' EXIT

# 复制 websocket_server 源码
tar -C "$WS_SOURCE" \
  --exclude='.env' --exclude='*.db' --exclude='*.db-*' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='*.log' \
  -cf - . | tar -C "$STAGE" -xf -

# 复制 Dockerfile
cp "$DOCKERFILE_SRC" "$STAGE/Dockerfile"

# 复制健康检查脚本 (Dockerfile HEALTHCHECK 依赖)
HEALTHCHECK_SRC="$(dirname "$DOCKERFILE_SRC")/healthcheck.py"
if [[ -f "$HEALTHCHECK_SRC" ]]; then
  cp "$HEALTHCHECK_SRC" "$STAGE/healthcheck.py"
fi

# [2/5] 打包
echo "[2/5] 打包代码..."
tar -czf "$TAR" -C "$STAGE" .
echo "    包大小: $(du -h "$TAR" | cut -f1)"

# 写远程部署脚本
cat > "$REMOTE_SH" <<'REMOTE_SH'
#!/bin/bash
set -euo pipefail
CONTAINER="$1"; IMAGE="$2"; ROLLBACK="$3"; BUILD_DIR="$4"
TAR="/tmp/websocket-server.tar.gz"
ENV_FILE="/tmp/ws_container.env"

echo "[1/7] 获取环境变量"
# 尝试从运行容器获取环境变量
if sudo docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" > "$ENV_FILE" 2>/dev/null && [[ -s "$ENV_FILE" ]]; then
  echo "    复用运行容器 $CONTAINER 的 env -> $ENV_FILE ($(wc -l < "$ENV_FILE") vars)"
  # 确保关键鉴权/隔离变量始终存在
  if ! grep -q '^WS_TENANT_STRICT=' "$ENV_FILE"; then
    echo "WS_TENANT_STRICT=true" >> "$ENV_FILE"
  fi
  if ! grep -q '^WS_JWT_SECRET=' "$ENV_FILE"; then
    echo "WS_JWT_SECRET=ws-platform-jwt-secret-2026" >> "$ENV_FILE"
  fi
else
  # 使用默认环境变量
  cat > "$ENV_FILE" <<'ENVEOF'
WS_HOST=0.0.0.0
WS_PORT=8765
HEARTBEAT_INTERVAL=5
HEARTBEAT_TIMEOUT=15
DB_ENGINE=sqlite
DB_NAME=/data/websocket_server.db
LOG_LEVEL=INFO
WS_JWT_SECRET=ws-platform-jwt-secret-2026
WS_TENANT_STRICT=true
ENVEOF
  echo "    使用默认环境变量 -> $ENV_FILE"
fi

echo "[2/7] 打 rollback 标签"
if sudo docker tag "$IMAGE" "$ROLLBACK" 2>/dev/null; then
  echo "    $IMAGE -> $ROLLBACK"
else
  echo "    (无旧镜像可打标签，首次部署)"
fi

echo "[3/7] 停止并移除旧容器"
sudo docker stop "$CONTAINER" 2>/dev/null || true
sudo docker rm "$CONTAINER" 2>/dev/null || true

echo "[4/7] 解压新代码 -> $BUILD_DIR"
rm -rf "$BUILD_DIR"; mkdir -p "$BUILD_DIR"
tar -xzf "$TAR" -C "$BUILD_DIR"
rm -f "$TAR"

echo "[5/7] 构建镜像"
cd "$BUILD_DIR"
sudo docker build -t "$IMAGE" .

echo "[6/7] 启动新容器"
sudo docker run -d \
  --name "$CONTAINER" \
  --network host \
  --restart unless-stopped \
  --env-file "$ENV_FILE" \
  -v /data/websocket_server:/data \
  -v /home/ubuntu/ws_certs:/certs:ro \
  "$IMAGE" >/dev/null

echo "[7/7] 等待并校验"
sleep 5
STATUS=$(sudo docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
echo "    容器状态: $STATUS"
if [[ "$STATUS" != "running" ]]; then
  echo "!!! 容器未运行，日志："
  sudo docker logs --tail 60 "$CONTAINER" 2>&1 || true
  echo "!!! 回滚命令: sudo docker rm -f $CONTAINER; sudo docker run -d --name $CONTAINER --network host --restart unless-stopped --env-file $ENV_FILE $ROLLBACK"
  exit 1
fi

echo "    最近日志："
sudo docker logs --tail 15 "$CONTAINER" 2>&1 || true
echo "=== 远程部署完成 ==="
REMOTE_SH

# [3/5] 上传
echo "[3/5] 上传代码与脚本..."
scp_pw "$TAR" "/tmp/"
scp_pw "$REMOTE_SH" "/tmp/"

# [4/5] 远程执行
echo "[4/5] 远程执行部署..."
ssh_pw "bash $REMOTE_SH '$CONTAINER_NAME' '$IMAGE_NAME' '$ROLLBACK_IMAGE' '$REMOTE_BUILD_DIR'"

# [5/5] 健康检查
echo "[5/5] 健康检查..."
sleep 3
echo "    测试 /api/health 端点..."
ssh_pw "curl -sk https://localhost:8765/api/health 2>/dev/null || curl -s http://localhost:8765/api/health || echo 'Health check failed'"

echo "=========================================="
echo "部署完成!"
echo "=========================================="
echo "服务地址:"
echo "  - WebSocket: wss://$SSH_HOST:8765/"
echo "  - REST API:  https://$SSH_HOST:8765/api/"
echo ""
echo "查看日志:"
echo "  ssh $SSH_USER@$SSH_HOST"
echo "  sudo docker logs -f $CONTAINER_NAME"
echo ""
echo "回滚（如需）:"
echo "  sudo docker stop $CONTAINER_NAME; sudo docker rm $CONTAINER_NAME"
echo "  sudo docker run -d --name $CONTAINER_NAME --network host --restart unless-stopped --env-file /tmp/ws_container.env $ROLLBACK_IMAGE"

#!/usr/bin/env bash
#
# 完整部署脚本 - 部署 agent-server-platform 到远程服务器
#
# 工作方式：
#   1. 本地组装构建上下文 = src/agent_server_platform 源码 + 仓库根的 Dockerfile
#      （Dockerfile 不在 src/agent_server_platform 下，必须单独打入构建上下文）
#   2. 打包并 scp 到远程，同时上传远程部署脚本
#   3. 远程：优先从「当前运行容器」抽取环境变量（含 LLM 密钥）通过 --env-file 复用，
#      避免密钥落盘 credentials.env 或出现在 ssh 命令行；首次部署（无运行容器）则
#      回退到 credentials.env 中的 LLM 变量（需自行补齐）
#   4. 打 rollback 标签、停旧容器、构建新镜像、启动新容器、校验
#   5. 健康检查
#
# 注意：旧版脚本 step 5 用 `ssh_pw <<'REMOTE_SCRIPT'` 把远程脚本喂到 stdin，
# 但 ssh_pw 只读 $1，导致远程构建/运行命令根本不会执行；且引号 heredoc 会阻止
# 本地变量展开，$SSH_USER/$REMOTE_DIR/$IMAGE_NAME 等在远程全为空。本版本改为
# 「本地写脚本文件 -> scp -> ssh bash 执行，配置通过参数传入」，彻底修复。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CRED_FILE="$REPO_ROOT/scripts/deploy/credentials.env"
DOCKERFILE_SRC="$REPO_ROOT/scripts/build/agent_server_platform/Dockerfile"

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

CONTAINER_NAME="${CONTAINER_NAME:-agent-server-platform}"
IMAGE_NAME="${IMAGE_NAME:-agent-server-platform:latest}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-agent-server-platform:rollback}"
REMOTE_BUILD_DIR="${REMOTE_BUILD_DIR:-/tmp/asp_build}"

TAR="/tmp/agent-server-platform.tar.gz"
REMOTE_SH="/tmp/asp_deploy_remote.sh"
SEED_ENV_LOCAL="/tmp/asp_seed.env"

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
echo "部署 Agent Server Platform 到 $SSH_HOST"
echo "=========================================="

# [1/6] 组装构建上下文（源码 + Dockerfile）
# 从本地 .env 读取 WS_SERVER_API_KEY（若存在）
ASP_ENV_FILE="$REPO_ROOT/src/agent_server_platform/.env"
WS_API_KEY=""
if [[ -f "$ASP_ENV_FILE" ]]; then
  WS_API_KEY=$(grep '^WS_SERVER_API_KEY=' "$ASP_ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
fi

echo "[1/6] 组装构建上下文..."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$SEED_ENV_LOCAL" "$TAR" "$REMOTE_SH"' EXIT
tar -C "$REPO_ROOT/src/agent_server_platform" \
  --exclude='.env' --exclude='*.db' --exclude='*.db-*' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='backend*.log' --exclude='*.log' \
  -cf - . | tar -C "$STAGE" -xf -
cp "$DOCKERFILE_SRC" "$STAGE/Dockerfile"

# [2/6] 打包
echo "[2/6] 打包代码..."
tar -czf "$TAR" -C "$STAGE" .
echo "    包大小: $(du -h "$TAR" | cut -f1)"

# [3/6] 准备 seed env（首次部署回退用；重新部署时远程会改用容器 env）
SEED_PROVIDED=0
if [[ -n "${DASHSCOPE_API_KEY:-}" && -n "${LLM_BASE_URL:-}" && -n "${LLM_MODEL:-}" ]]; then
  {
    echo "DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY"
    echo "LLM_BASE_URL=$LLM_BASE_URL"
    echo "LLM_MODEL=$LLM_MODEL"
    echo "GRADIO_SERVER_PORT=8080"
    echo "FLASK_SERVER_PORT=5000"
  } > "$SEED_ENV_LOCAL"
  SEED_PROVIDED=1
else
  echo "    (credentials.env 缺少 LLM 变量；若远程无运行容器，首次部署将失败)"
fi

# 写远程部署脚本（引号 heredoc，不做本地展开；配置通过参数 $1..$5 传入）
cat > "$REMOTE_SH" <<'REMOTE_SH'
#!/bin/bash
set -euo pipefail
CONTAINER="$1"; IMAGE="$2"; ROLLBACK="$3"; BUILD_DIR="$4"; WS_API_KEY="${5:-}"
ENV_FILE="/tmp/asp_container.env"
SEED_ENV="/tmp/asp_seed.env"
TAR="/tmp/agent-server-platform.tar.gz"

echo "[1/8] 获取环境变量"
if sudo docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" > "$ENV_FILE" 2>/dev/null && [[ -s "$ENV_FILE" ]]; then
  echo "    复用运行容器 $CONTAINER 的 env -> $ENV_FILE ($(wc -l < "$ENV_FILE") vars)"
elif [[ -f "$SEED_ENV" ]]; then
  cp "$SEED_ENV" "$ENV_FILE"
  echo "    无运行容器，使用 seed env -> $ENV_FILE ($(wc -l < "$ENV_FILE") vars)"
else
  echo "    ERROR: 远程无运行容器可抽取 env，且未提供 seed env。"
  echo "           首次部署需在 deploy/credentials.env 设置 DASHSCOPE_API_KEY/LLM_BASE_URL/LLM_MODEL"
  exit 1
fi

echo "[2/8] 打 rollback 标签"
if sudo docker tag "$IMAGE" "$ROLLBACK" 2>/dev/null; then
  echo "    $IMAGE -> $ROLLBACK"
else
  echo "    (无旧镜像可打标签，首次部署)"
fi

echo "[3/8] 停止并移除旧容器"
sudo docker stop "$CONTAINER" 2>/dev/null || true
sudo docker rm "$CONTAINER" 2>/dev/null || true

echo "[4/8] 解压新代码 -> $BUILD_DIR"
rm -rf "$BUILD_DIR"; mkdir -p "$BUILD_DIR"
tar -xzf "$TAR" -C "$BUILD_DIR"
rm -f "$TAR"

echo "[5/8] 构建镜像 (requirements.txt 未变则 pip 层缓存命中)"
cd "$BUILD_DIR"
sudo docker build -t "$IMAGE" .

DATA_DIR="${DATA_DIR:-/agent-data-files/agent-platform}"
sudo mkdir -p "$DATA_DIR"

echo "[6/8] 启动新容器 (--env-file 复用环境 + wss 覆盖)"
sudo docker run -d \
  --name "$CONTAINER" \
  --network host \
  --restart unless-stopped \
  --env-file "$ENV_FILE" \
  -v ${DATA_DIR}:/data \
  -e DB_NAME=/data/agent_server.db \
  -e WS_SERVER_API_URL=https://agent-socket-server.bdzz.com.cn:8765 \
  -e WS_SERVER_WS_URL=wss://agent-socket-server.bdzz.com.cn:8765 \
  -e SANDBOX_BACKEND_WS_URL=wss://agent-socket-server.bdzz.com.cn:8765 \
  ${WS_API_KEY:+-e WS_SERVER_API_KEY="$WS_API_KEY"} \
  "$IMAGE" >/dev/null

echo "[7/8] 等待并校验"
sleep 6
STATUS=$(sudo docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
echo "    容器状态: $STATUS"
if [[ "$STATUS" != "running" ]]; then
  echo "!!! 容器未运行，日志："
  sudo docker logs --tail 60 "$CONTAINER" 2>&1 || true
  echo "!!! 回滚: sudo docker rm -f $CONTAINER; sudo docker run -d --name $CONTAINER --network host --restart unless-stopped --env-file $ENV_FILE $ROLLBACK"
  exit 1
fi

echo "[8/8] 最近日志："
sudo docker logs --tail 20 "$CONTAINER" 2>&1 || true
rm -f "$SEED_ENV"
echo "=== 远程部署完成 ==="
REMOTE_SH

# [4/6] 上传
echo "[4/6] 上传代码与脚本..."
scp_pw "$TAR" "/tmp/"
scp_pw "$REMOTE_SH" "/tmp/"
if [[ "$SEED_PROVIDED" == "1" ]]; then
  scp_pw "$SEED_ENV_LOCAL" "/tmp/asp_seed.env"
fi

# [5/6] 远程执行（配置通过参数传入，避免命令行出现密钥）
echo "[5/6] 远程执行部署..."
ssh_pw "bash $REMOTE_SH '$CONTAINER_NAME' '$IMAGE_NAME' '$ROLLBACK_IMAGE' '$REMOTE_BUILD_DIR' '$WS_API_KEY'"

# [6/6] 健康检查
echo "[6/6] 健康检查..."
sleep 3
ssh_pw "curl -s -o /dev/null -w 'GET / -> %{http_code}\n' http://localhost:8080/ || echo 'GET / -> failed'"

echo "=========================================="
echo "部署完成!"
echo "=========================================="
echo "访问地址:"
echo "  - Gradio UI: http://$SSH_HOST:8080"
echo "  - Flask API: http://$SSH_HOST:5000"
echo ""
echo "查看日志:"
echo "  ssh $SSH_USER@$SSH_HOST"
echo "  sudo docker logs -f $CONTAINER_NAME"
echo ""
echo "回滚（如需）:"
echo "  sudo docker stop $CONTAINER_NAME; sudo docker rm $CONTAINER_NAME"
echo "  sudo docker run -d --name $CONTAINER_NAME --network host --restart unless-stopped --env-file /tmp/asp_container.env $ROLLBACK_IMAGE"

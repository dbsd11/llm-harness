#!/usr/bin/env bash
#
# Deploy websocket_server to remote host via Docker (TLS / wss).
#
# Reads credentials from deploy/credentials.env (gitignored).
# Cert+key live in deploy/certs/ (gitignored); uploaded to the remote and
# bind-mounted read-only. WS_SSL_CERT/WS_SSL_KEY enable TLS on port 8765.
#
# Flow:
#   1. Extract running container env -> /tmp/ws_container.env (preserve LLM
#      credentials etc.); tag old image as rollback; stop/rm old container.
#   2. tar source+dockerfile (exclude caches/db/.git/.env).
#   3. scp tarball to remote.
#   4. scp TLS cert+key to remote /home/$USER/ws_certs/.
#   5. remote docker build + run with --env-file + cert mount + WS_SSL_* env.
#   6. health check (https, -k self-signed) + status (logs on failure).
#   7. tail recent logs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CRED_FILE="$REPO_ROOT/scripts/deploy/credentials.env"
CERT_DIR="$REPO_ROOT/scripts/deploy/certs"

if [[ ! -f "$CRED_FILE" ]]; then
  echo "ERROR: $CRED_FILE not found. Create it with SSH_HOST/SSH_USER/SSH_PASSWORD."
  exit 1
fi
# shellcheck source=/dev/null
source "$CRED_FILE"

: "${SSH_HOST:?SSH_HOST required in credentials.env}"
: "${SSH_USER:?SSH_USER required in credentials.env}"
: "${SSH_PASSWORD:?SSH_PASSWORD required in credentials.env}"

if [[ ! -f "$CERT_DIR/server.pem" || ! -f "$CERT_DIR/server.key" ]]; then
  echo "ERROR: TLS cert missing. Generate with:"
  echo "  openssl req -x509 -newkey rsa:2048 -nodes \\"
  echo "    -keyout $CERT_DIR/server.key -out $CERT_DIR/server.pem \\"
  echo "    -days 365250 -subj \"/CN=<domain>\" -addext \"subjectAltName=DNS:<domain>,DNS:localhost,IP:127.0.0.1\""
  exit 1
fi

REMOTE_DIR="${REMOTE_DIR:-/home/$SSH_USER/websocket_server}"
REMOTE_CERT_DIR="${REMOTE_CERT_DIR:-/home/$SSH_USER/ws_certs}"
IMAGE="${IMAGE:-websocket-server:latest}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-websocket-server:rollback}"
CONTAINER="${CONTAINER:-websocket-server}"
SRC_DIR="$REPO_ROOT/src/websocket_server"
DOCKERFILE="$REPO_ROOT/scripts/build/websocket_server/Dockerfile"
ENV_FILE="/tmp/ws_container.env"
# cert bind-mount + TLS env, applied to every docker run (incl. rollback)
CERT_RUN_ARGS="-v $REMOTE_CERT_DIR:/certs:ro -e WS_SSL_CERT=/certs/server.pem -e WS_SSL_KEY=/certs/server.key"

# expect wrappers for SSH/SCP with password auth
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

echo "==> [1/7] Preserve container env + rollback tag, then clean old container"
# NOTE: use `test` (not `[ ... ]`) and avoid remote $var/$() - inside the expect
# double-quoted string, [ ] is tcl command-substitution and $var is tcl expansion.
ssh_pw "if sudo docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $CONTAINER > $ENV_FILE 2>/dev/null && test -s $ENV_FILE; then echo '    reused env from $CONTAINER -> $ENV_FILE'; else rm -f $ENV_FILE; echo '    no running container; will use WS_HOST/WS_PORT defaults'; fi; if sudo docker tag $IMAGE $ROLLBACK_IMAGE 2>/dev/null; then echo '    tagged $IMAGE -> $ROLLBACK_IMAGE'; else echo '    (no old image to tag)'; fi; sudo docker stop $CONTAINER 2>/dev/null || true; sudo docker rm $CONTAINER 2>/dev/null || true"

echo "==> [2/7] Package source (excluding __pycache__/*.db/.git/.env)"
TARBALL="/tmp/ws_server_deploy.tgz"
cp "$DOCKERFILE" "$SRC_DIR/Dockerfile"
tar -czf "$TARBALL" \
  --exclude='__pycache__' --exclude='*.db' --exclude='.git' --exclude='.env' \
  -C "$SRC_DIR" .
rm -f "$SRC_DIR/Dockerfile"
echo "    $(du -h "$TARBALL" | cut -f1) -> $TARBALL"

echo "==> [3/7] Upload source to remote"
scp_pw "$TARBALL" "/tmp/ws_server_deploy.tgz"
rm -f "$TARBALL"

echo "==> [4/7] Upload TLS cert+key (self-signed) -> $REMOTE_CERT_DIR"
ssh_pw "mkdir -p $REMOTE_CERT_DIR"
scp_pw "$CERT_DIR/server.pem" "$REMOTE_CERT_DIR/server.pem"
scp_pw "$CERT_DIR/server.key" "$REMOTE_CERT_DIR/server.key"

echo "==> [5/7] Remote docker build + run (TLS, env preserved via --env-file)"
ssh_pw "mkdir -p $REMOTE_DIR && tar -xzf /tmp/ws_server_deploy.tgz -C $REMOTE_DIR && rm -f /tmp/ws_server_deploy.tgz && sudo docker build -t $IMAGE $REMOTE_DIR && if test -f $ENV_FILE; then sudo docker run -d --name $CONTAINER --network host $CERT_RUN_ARGS --env-file $ENV_FILE $IMAGE; else sudo docker run -d --name $CONTAINER --network host $CERT_RUN_ARGS -e WS_HOST=0.0.0.0 -e WS_PORT=8765 $IMAGE; fi && sleep 3 && sudo docker ps --filter name=$CONTAINER"

echo "==> [6/7] Health check (https, -k self-signed) + status"
ssh_pw "sudo docker inspect --format '{{.State.Status}}' $CONTAINER 2>/dev/null | grep -qx running || { echo '!!! container not running, last logs:'; sudo docker logs --tail 50 $CONTAINER 2>&1; exit 1; }; curl -ksf https://localhost:8765/api/health && echo ' health=OK' || { echo '!!! health check failed, logs:'; sudo docker logs --tail 50 $CONTAINER 2>&1; exit 1; }"

echo "==> [7/7] Recent logs"
ssh_pw "sudo docker logs --tail 25 $CONTAINER 2>&1"

echo ""
echo "==> Done: wss://$SSH_HOST:8765  (image $IMAGE, rollback $ROLLBACK_IMAGE)"
echo "Rollback if needed:"
echo "  ssh $SSH_USER@$SSH_HOST"
echo "  sudo docker stop $CONTAINER; sudo docker rm $CONTAINER"
echo "  sudo docker run -d --name $CONTAINER --network host $CERT_RUN_ARGS --env-file $ENV_FILE $ROLLBACK_IMAGE"

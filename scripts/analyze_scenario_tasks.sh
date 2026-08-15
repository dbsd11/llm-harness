#!/usr/bin/env bash
# 分析场景任务执行情况

SCENARIO_ID="c23fbc80-8e9f-49cc-85ee-d1d4806e79ce"

echo "=========================================="
echo "分析场景任务执行情况"
echo "场景 ID: $SCENARIO_ID"
echo "=========================================="

# SSH 连接信息（从 credentials.env 读取）
source scripts/deploy/credentials.env

# 1. 查询场景基本信息
echo ""
echo "[1/4] 查询场景基本信息..."
expect <<EOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no $SSH_USER@$SSH_HOST
expect "password:"
send "$SSH_PASSWORD\r"
expect "$ "
send "sudo docker exec websocket-server sqlite3 /data/websocket_server.db \"SELECT scenario_id, name, state, config FROM scenarios WHERE scenario_id='$SCENARIO_ID'\"\r"
expect "$ "
send "exit\r"
expect eof
EOF

# 2. 查询所有任务及其状态
echo ""
echo "[2/4] 查询所有任务状态..."
expect <<EOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no $SSH_USER@$SSH_HOST
expect "password:"
send "$SSH_PASSWORD\r"
expect "$ "
send "sudo docker exec websocket-server sqlite3 -header -column /data/websocket_server.db \"SELECT task_id, state, goal, error, created_at, completed_at FROM tasks WHERE scenario_id='$SCENARIO_ID' ORDER BY created_at\"\r"
expect "$ "
send "exit\r"
expect eof
EOF

# 3. 查询任务依赖关系
echo ""
echo "[3/4] 查询任务依赖关系..."
expect <<EOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no $SSH_USER@$SSH_HOST
expect "password:"
send "$SSH_PASSWORD\r"
expect "$ "
send "sudo docker exec websocket-server sqlite3 -header -column /data/websocket_server.db \"SELECT task_id, depends_on FROM tasks WHERE scenario_id='$SCENARIO_ID' AND depends_on IS NOT NULL AND depends_on != ''\"\r"
expect "$ "
send "exit\r"
expect eof
EOF

# 4. 查询 dispatch 消息
echo ""
echo "[4/4] 查询 dispatch 消息..."
expect <<EOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no $SSH_USER@$SSH_HOST
expect "password:"
send "$SSH_PASSWORD\r"
expect "$ "
send "sudo docker exec websocket-server sqlite3 -header -column /data/websocket_server.db \"SELECT task_id, message_type, acked, created_at FROM messages WHERE scenario_id='$SCENARIO_ID' ORDER BY created_at\"\r"
expect "$ "
send "exit\r"
expect eof
EOF

echo ""
echo "=========================================="
echo "分析完成"
echo "=========================================="

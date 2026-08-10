# Execution Agent Server (Standalone Module)

独立封装的执行 Agent Server 模块，用于在 Docker 沙箱中运行。

## 目录结构

```
execution_agent_server/
├── execution_server/          # 执行服务器核心
│   ├── __main__.py           # 入口点
│   ├── server.py             # 服务器实现
│   ├── ws_client.py          # WebSocket 客户端
│   ├── task_runner.py        # 任务执行器
│   ├── config.py             # 配置加载
│   └── env_probe.py          # 环境探测
├── core/                     # 核心模块
│   ├── agents/               # Agent 实现
│   │   ├── base_agent.py     # Agent 基类
│   │   └── execution_agent.py # 执行 Agent
│   ├── event_bus.py          # 事件总线
│   ├── llm_client.py         # LLM 客户端
│   ├── state_machine.py      # 状态机
│   └── ws_protocol.py        # WebSocket 协议
├── database/                 # 数据库模块
│   ├── models/               # 数据模型
│   ├── repositories/         # 数据访问层
│   ├── config.py             # 数据库配置
│   └── connection.py         # 连接管理
├── logger/                   # 日志模块
│   └── __init__.py
├── Dockerfile                # Docker 镜像配置
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量示例
└── README.md                 # 本文档
```

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，配置必要的环境变量
```

关键配置项：
- `SERVER_ID`: 服务器唯一标识
- `BACKEND_WS_URL`: 后端 WebSocket 地址
- `DASHSCOPE_API_KEY`: LLM API 密钥

### 2. 本地运行

```bash
pip install -r requirements.txt
python -m execution_server
```

### 3. Docker 构建

```bash
docker build -t execution-agent-server:latest .
```

### 4. Docker 运行

```bash
docker run -d \
  --name exec-server-1 \
  -e SERVER_ID=exec-server-1 \
  -e SERVER_NAME="Execution Server 1" \
  -e MAX_QUOTA=4 \
  -e BACKEND_WS_URL=ws://your-backend:8765 \
  -e DASHSCOPE_API_KEY=your-key \
  execution-agent-server:latest
```

## 部署到远程服务器

### 方式一：直接构建

在远程服务器上：

```bash
# 克隆或复制代码到远程服务器
git clone <repo-url> execution_agent_server
cd execution_agent_server

# 构建镜像
docker build -t execution-agent-server:latest .

# 运行容器
docker run -d \
  --name exec-server-1 \
  -e SERVER_ID=exec-server-1 \
  -e SERVER_NAME="Execution Server 1" \
  -e MAX_QUOTA=4 \
  -e BACKEND_WS_URL=ws://your-backend:8765 \
  -e DASHSCOPE_API_KEY=your-key \
  execution-agent-server:latest
```

### 方式二：本地构建 + 远程推送

```bash
# 本地构建
docker build -t execution-agent-server:latest .

# 保存到文件
docker save execution-agent-server:latest | gzip > exec-server.tar.gz

# 传输到远程服务器
scp exec-server.tar.gz ubuntu@remote-server:~/

# 在远程服务器加载镜像
ssh ubuntu@remote-server
gunzip -c exec-server.tar.gz | docker load
```

## 通过调度 Agent 管理

使用调度 Agent 的工具系统动态管理执行服务器：

```python
from core.agents.scheduling_agent import SchedulingAgent

agent = SchedulingAgent()

# 创建执行服务器沙箱
result = agent.use_tool(
    "create_execution_server_sandbox",
    server_id="exec-server-1",
    server_name="Execution Server 1",
    max_quota=4
)

# 查询执行服务器列表
servers = agent.use_tool("list_execution_servers")

# 销毁执行服务器沙箱
agent.use_tool(
    "destroy_execution_server_sandbox",
    server_id="exec-server-1"
)
```

## 环境变量说明

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `SERVER_ID` | 服务器唯一标识 | `exec-server-1` |
| `SERVER_NAME` | 服务器显示名称 | `Execution Server` |
| `MAX_QUOTA` | 最大并发任务数 | `4` |
| `BACKEND_WS_URL` | 后端 WebSocket 地址 | `ws://localhost:8765` |
| `HEARTBEAT_INTERVAL` | 心跳间隔（秒） | `5` |
| `DASHSCOPE_API_KEY` | LLM API 密钥 | 无 |
| `LLM_BASE_URL` | LLM API 基础 URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | LLM 模型名称 | `qwen-plus` |
| `DB_ENGINE` | 数据库引擎 | `sqlite` |
| `DB_NAME` | 数据库名称 | `execution_server.db` |

## 依赖说明

- `python-dotenv`: 环境变量加载
- `websockets`: WebSocket 客户端
- `openai`: LLM API 调用
- `requests`: HTTP 请求

## 注意事项

1. **网络连通性**: 确保执行服务器能够访问后端的 WebSocket 端口
2. **API 密钥**: 确保 `DASHSCOPE_API_KEY` 有效且有足够配额
3. **数据库**: 默认使用 SQLite，生产环境建议切换到 MySQL
4. **日志**: 日志输出到标准输出，可通过 Docker 日志查看

## 故障排查

### 连接失败

```bash
# 检查容器是否运行
docker ps | grep exec-server

# 查看容器日志
docker logs exec-server-1
```

### LLM 调用失败

- 检查 `DASHSCOPE_API_KEY` 是否正确
- 确认 API 配额充足
- 检查网络连接

### 数据库错误

- 检查数据库文件权限
- 确认数据库路径可写
- 考虑切换到 MySQL

## 许可证

与主项目相同

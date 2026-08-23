# WebSocket Server - 独立的 WebSocket 服务器模块

## 概述

WebSocket Server 是一个独立的服务模块，负责管理执行服务器（Execution Server）的连接和任务分发。它是分布式任务调度系统的核心组件之一。

## 功能

- **连接管理**：接收和管理执行服务器的 WebSocket 连接
- **服务器注册**：处理执行服务器的注册请求，维护服务器状态
- **心跳检测**：监控执行服务器的连接状态，自动清理超时连接
- **任务分发**：将待执行的任务分发到可用的执行服务器
- **结果收集**：接收执行服务器的任务执行结果

## 架构

```
websocket_server/
├── websocket_server/     # 主模块
│   ├── __init__.py
│   ├── __main__.py      # 入口点
│   ├── config.py        # 配置加载
│   ├── server.py        # WebSocket 服务器实现
│   └── ws_protocol.py   # 协议定义
├── core/                # 核心模块
│   ├── ws_server.py
│   ├── ws_protocol.py
│   ├── central_dispatcher.py
│   ├── message_queue.py
│   ├── event_bus.py
│   └── state_machine.py
├── database/            # 数据库模块
│   ├── models/
│   └── repositories/
├── logger/              # 日志模块
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
cd websocket_server
pip install -r requirements.txt
```

### 2. 配置环境

```bash
cp .env.example .env
# 编辑 .env 文件配置参数
```

### 3. 启动服务

```bash
python -m websocket_server
```

### 4. Docker 部署

```bash
# 构建镜像
docker build -t websocket-server:latest .

# 运行容器
docker run -d \
  --name websocket-server \
  -p 8765:8765 \
  -e WS_HOST=0.0.0.0 \
  -e WS_PORT=8765 \
  -e DB_ENGINE=sqlite \
  -e DB_NAME=websocket_server.db \
  websocket-server:latest
```

## 配置说明

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `WS_HOST` | 监听地址 | `0.0.0.0` |
| `WS_PORT` | 监听端口 | `8765` |
| `HEARTBEAT_INTERVAL` | 心跳间隔（秒） | `5` |
| `HEARTBEAT_TIMEOUT` | 心跳超时（秒） | `15` |
| `DB_ENGINE` | 数据库引擎 | `sqlite` |
| `DB_NAME` | 数据库名称 | `websocket_server.db` |

## WebSocket 协议

### 帧类型

**执行服务器 → 后端:**
- `register`: 执行服务器注册
- `status`: 心跳检测（状态 + 环境 + 计数）
- `task_event`: 任务事件（execution_agent_created / task_started）
- `task_result`: 任务结果
- `ask_assistant_request`: 执行代理请求助手信息

**后端 → 执行服务器:**
- `task`: 任务分发
- `ack`: 确认响应
- `ask_assistant_response`: 助手回答返回

### 注册流程

1. 执行服务器连接到 WebSocket Server
2. 发送 `register` 帧，包含服务器信息
3. WebSocket Server 返回 `ack` 确认
4. 执行服务器定期发送 `status` 保持连接
5. WebSocket Server 分发 `task` 帧
6. 执行服务器执行完成后发送 `task_result` 帧

### Assistant Mode 帧流程

```
执行服务器                          WebSocket Server                         助手服务器
    |                                    |                                    |
    |-- ask_assistant_request --------->|                                    |
    |   (request_id, question)          |-- create task + dispatch --------->|
    |   [blocks on PendingAnswer]       |   (correlation stored)             |
    |                                   |                                    |
    |                                   |<-------- task_result -------------|
    |                                   |   (check correlation)              |
    |<-- ask_assistant_response -------|                                    |
    |   (request_id, answer)            |                                    |
    |   [PendingAnswer resolved]        |                                    |
```

## 数据库

WebSocket Server 需要访问以下数据库表：

- `execution_servers`: 执行服务器注册信息
- `messages`: 任务消息队列
- `tasks`: 任务状态

## 与执行服务器的关系

WebSocket Server 和执行服务器（Execution Server）配合工作：

1. **执行服务器**启动后连接到 **WebSocket Server**
2. **WebSocket Server** 分发任务到 **执行服务器**
3. **执行服务器** 执行任务并返回结果
4. **WebSocket Server** 收集结果并更新数据库

## 监控和日志

- 所有连接和断开事件都会记录到日志
- 心跳超时的服务器会被自动清理
- 任务分发和结果收集都有详细日志

## 故障排查

### 连接问题

- 检查防火墙是否开放 8765 端口
- 确认 WebSocket Server 正在运行
- 查看日志中的错误信息

### 心跳超时

- 检查执行服务器的网络连接
- 确认 `HEARTBEAT_INTERVAL` 和 `HEARTBEAT_TIMEOUT` 配置合理
- 查看执行服务器日志

## 下一步

- 集成到主调度系统
- 添加负载均衡
- 实现服务器健康检查
- 添加任务优先级队列

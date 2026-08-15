FROM python:3.11-slim

# git: execution agent bash tasks need git clone/pull/etc.
# bash: execution_agent.py uses executable="/bin/bash"
# curl: health checks and API calls
RUN apt-get update && \
    apt-get install -y --no-install-recommends git bash curl jq && \
    rm -rf /var/lib/apt/lists/*

# Allow git operations in any directory (container runs as root)
RUN git config --system safe.directory '*'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com

# exec-server transitively imports: execution_server/*, core/*, logger/*, database/*
COPY src/ /app/src/

ENV PYTHONPATH=/app/src:/app/src/execution_agent_server
ENV PYTHONUNBUFFERED=1
# Exec-server has no backend DB access; events go over WS.
ENV EVENT_PERSIST_DISABLED=1

RUN mkdir -p /data
WORKDIR /data

CMD ["python", "-m", "execution_server"]

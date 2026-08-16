FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

# Copy source code
COPY . .

# Set Python path
ENV PYTHONPATH=/app

# Set default environment variables
ENV PYTHONUNBUFFERED=1

# Expose port (WebSocket + REST API share one port)
EXPOSE 8765

RUN mkdir -p /data

# Health check: probes /api/health every 30s, 5s timeout, 3 consecutive failures -> unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python /app/healthcheck.py

# Run the websocket server
CMD ["python", "-m", "websocket_server"]

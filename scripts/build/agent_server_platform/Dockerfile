FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

# Copy source code
COPY . .

# Set Python path
ENV PYTHONPATH=/app/src

# Set default environment variables
ENV PYTHONUNBUFFERED=1
ENV GRADIO_PORT=8080
ENV FLASK_PORT=5000

# Expose ports
EXPOSE 8080
EXPOSE 5000

# Run the backend
CMD ["python", "-m", "src.app", "--all"]

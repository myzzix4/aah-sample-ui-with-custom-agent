# AgentCore Runtime contract: ARM64 only, port 8080, /invocations + /ping
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY services/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/agent/main.py ./
COPY services/agent/src/ ./src/

EXPOSE 8080

# AgentCore Runtime HTTP contract — long-running server, no health-check needed
# from compose (AgentCore probes /ping every 5s after start)
CMD ["python", "-u", "main.py"]

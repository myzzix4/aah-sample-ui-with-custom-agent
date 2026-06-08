# App Runner = x86_64 only (ARM64 이미지는 exec format error)
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY services/ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ui/app.py ./
COPY services/ui/templates/ ./templates/
COPY services/ui/static/ ./static/

EXPOSE 8000

# App Runner 가 PORT env로 포트 주입 (없으면 8000)
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8000} -w 2 -k gthread --threads 8 --timeout 120 app:app"]

# syntax=docker/dockerfile:1.4
# App Runner = x86_64 only (ARM64 이미지는 exec format error)
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY services/ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 표준 SDK — 사내 CodeArtifact 에 있다. Control Plane 의 CodeBuild 가 pip.conf 를
# BuildKit secret(pipconf) 으로 넘긴다. 그 밖(로컬 docker build)에선 못 받는데,
# 그래도 UI 는 떠야 하므로 실패를 삼키지 않고 로그에 남기고 계속 간다
# (healthz 의 telemetry=false 로 드러난다).
RUN --mount=type=secret,id=pipconf,target=/etc/pip.conf \
    pip install --no-cache-dir "samsunglife-agent-kit>=0.1" \
    || echo "WARN: samsunglife-agent-kit 설치 실패 — 텔레메트리(report_run) 비활성"

COPY services/ui/app.py ./
COPY services/ui/templates/ ./templates/
COPY services/ui/static/ ./static/

EXPOSE 8000

# App Runner 가 PORT env로 포트 주입 (없으면 8000)
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8000} -w 2 -k gthread --threads 8 --timeout 120 app:app"]

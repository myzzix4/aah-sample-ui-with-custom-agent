"""Flask Web UI — AgentCore Runtime 채팅 proxy.

- GET  /             : 채팅 페이지 (templates/index.html)
- GET  /healthz      : App Runner health
- POST /api/chat     : invoke_agent_runtime → 응답 forward (JSON)
- POST /api/chat-sse : invoke_agent_runtime SSE → text/event-stream forward

env:
  AGENT_RUNTIME_ARN  : 호출할 Agent ARN (필수)
  AWS_REGION         : 기본 us-east-1
"""
import json
import logging
import os
import uuid

import boto3
from botocore.exceptions import ReadTimeoutError
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

AGENT_ARN = os.getenv("AGENT_RUNTIME_ARN", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
TITLE = os.getenv("UI_TITLE", "AAH RAG Chat (Sample)").strip()

_ac = None
def _client():
    global _ac
    if _ac is None:
        _ac = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _ac


# 스트리밍을 지원하지 않는 에이전트는 text/event-stream 요청에 아예 응답하지 않는다.
# 기본 읽기 제한(60초)까지 기다리면 화면이 1분 내내 비어 있으므로 짧게 끊고 buffered
# 로 넘어간다. 실제로 흐르는 에이전트는 첫 바이트가 대개 5~10초 안에 온다.
_stream_supported = None      # None=미확인 · True=흐름 · False=미지원(확인됨)
_ac_stream = None
def _stream_client():
    global _ac_stream
    if _ac_stream is None:
        from botocore.config import Config
        _ac_stream = boto3.client(
            "bedrock-agentcore", region_name=AWS_REGION,
            config=Config(read_timeout=int(os.getenv("STREAM_FIRST_BYTE_TIMEOUT", "25")),
                          connect_timeout=10, retries={"max_attempts": 0}))
    return _ac_stream


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _invoke_buffered(payload: bytes, session_id: str):
    """accept=application/json 으로 한 번에 받는다. (output, citations) 반환."""
    r = _client().invoke_agent_runtime(
        agentRuntimeArn=AGENT_ARN, payload=payload,
        contentType="application/json", accept="application/json",
        runtimeSessionId=session_id,
    )
    raw = r["response"].read().decode("utf-8", errors="replace")
    try: parsed = json.loads(raw)
    except Exception: parsed = {"output": raw}
    out = (parsed.get("output") or parsed.get("final") or parsed.get("answer")
           or parsed.get("result") or parsed.get("text") or "")
    return out, (parsed.get("citations") or []), r.get("statusCode")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "healthy",
                       "agent_configured": bool(AGENT_ARN),
                       "region": AWS_REGION})


@app.get("/")
def index():
    return render_template("index.html",
                              title=TITLE,
                              agent_configured=bool(AGENT_ARN),
                              agent_arn_tail=AGENT_ARN.split("/")[-1] if AGENT_ARN else "",
                              scenario=os.getenv("UI_SCENARIO", "AAH Code Deploy Sample"))


@app.post("/api/chat")
def chat():
    """Buffered JSON — 응답 한방에 받음."""
    if not AGENT_ARN:
        return jsonify({"error": "AGENT_RUNTIME_ARN not configured"}), 500
    body = request.get_json(force=True, silent=True) or {}
    prompt = (body.get("input") or body.get("prompt") or "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    if not prompt:
        return jsonify({"error": "empty prompt"}), 400

    payload = json.dumps({"input": prompt, "session_id": session_id},
                              ensure_ascii=False).encode("utf-8")
    try:
        out, citations, status_code = _invoke_buffered(payload, session_id)
        return jsonify({"output": out, "citations": citations,
                        "session_id": session_id, "status_code": status_code})
    except Exception as e:
        log.error("invoke failed: %s", e)
        return jsonify({"error": str(e)[:500]}), 502


@app.post("/api/chat-sse")
def chat_sse():
    """SSE — token 단위 forward (Accept: text/event-stream)."""
    if not AGENT_ARN:
        return jsonify({"error": "AGENT_RUNTIME_ARN not configured"}), 500
    body = request.get_json(force=True, silent=True) or {}
    prompt = (body.get("input") or body.get("prompt") or "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    if not prompt:
        return jsonify({"error": "empty prompt"}), 400

    payload = json.dumps({"input": prompt, "session_id": session_id},
                              ensure_ascii=False).encode("utf-8")

    @stream_with_context
    def gen():
        global _stream_supported
        # 에이전트가 스트리밍을 지원하면 그대로 흘려보낸다.
        blocks = 0
        # 한 번 "안 흐른다"고 확인했으면 다음부터는 제한시간을 낭비하지 않는다.
        # 매 요청마다 25초를 버리면 채팅으로 못 쓴다.
        if _stream_supported is not False:
            try:
                r = _stream_client().invoke_agent_runtime(
                    agentRuntimeArn=AGENT_ARN, payload=payload,
                    contentType="application/json", accept="text/event-stream",
                    runtimeSessionId=session_id,
                )
                stream = r["response"]
                buf = b""
                while True:
                    chunk = stream.read(2048)
                    if not chunk: break
                    buf += chunk
                    while b"\n\n" in buf:
                        blk, buf = buf.split(b"\n\n", 1)
                        blocks += 1
                        yield blk + b"\n\n"
                if blocks:
                    _stream_supported = True
            except ReadTimeoutError as e:
                # 응답 자체가 안 온다 = 이 에이전트는 SSE 를 구현하지 않았다.
                # 다른 오류(스로틀링 등)는 일시적일 수 있으므로 단정하지 않는다.
                _stream_supported = False
                log.warning("streaming 미지원으로 판단 (%s) — 이후 buffered 사용", str(e)[:120])
            except Exception as e:
                log.warning("streaming 실패 (%s) — 이번 요청은 buffered 로 대체", str(e)[:160])

        # 스트리밍을 지원하지 않는 에이전트도 답은 준다 — 방식이 다를 뿐이다.
        # 여기서 포기하면 화면엔 "(빈 응답)" 만 남으므로 buffered 로 한 번 더 부른다.
        if blocks == 0:
            try:
                out, citations, _ = _invoke_buffered(payload, session_id)
                if out:
                    # 한 덩어리로 보낸다 — 쪼개서 흘리면 실제로는 안 그런 것을
                    # 토큰 스트리밍처럼 보이게 꾸미는 셈이다.
                    yield _sse("token", {"text": out})
                    if citations:
                        yield _sse("citations", {"citations": citations})
                else:
                    yield _sse("error", {"error": "에이전트가 빈 응답을 돌려줬습니다"})
            except Exception as e:
                log.error("buffered 대체도 실패: %s", e)
                yield _sse("error", {"error": str(e)[:300]})

        yield _sse("end", {"session_id": session_id})

    return Response(gen(), mimetype="text/event-stream",
                       headers={"Cache-Control": "no-cache",
                                  "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)

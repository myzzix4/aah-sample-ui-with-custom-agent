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
        r = _client().invoke_agent_runtime(
            agentRuntimeArn=AGENT_ARN, payload=payload,
            contentType="application/json", accept="application/json",
            runtimeSessionId=session_id,
        )
        raw = r["response"].read().decode("utf-8", errors="replace")
        try: parsed = json.loads(raw)
        except Exception: parsed = {"output": raw}
        return jsonify({
            "output": parsed.get("output") or parsed.get("final") or parsed.get("answer") or parsed.get("result") or parsed.get("text") or "",
            "citations": parsed.get("citations") or [],
            "session_id": session_id,
            "status_code": r.get("statusCode"),
        })
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
        try:
            r = _client().invoke_agent_runtime(
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
                    yield blk + b"\n\n"
            yield f"event: end\ndata: {json.dumps({'session_id': session_id})}\n\n".encode()
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)[:300]})}\n\n".encode()

    return Response(gen(), mimetype="text/event-stream",
                       headers={"Cache-Control": "no-cache",
                                  "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)

"""Flask Web UI — AgentCore Runtime 채팅 proxy.

- GET  /             : 채팅 페이지 (templates/index.html)
- GET  /healthz      : App Runner health
- POST /api/chat     : invoke_agent_runtime → 응답 forward (JSON)
- POST /api/chat-sse : invoke_agent_runtime SSE → text/event-stream forward

env:
  AGENT_RUNTIME_ARN  : 호출할 Agent ARN (필수)
  AWS_REGION         : 기본 us-east-1
"""
import base64
import hashlib
import socket
import json
import logging
# 표준 SDK(samsunglife-agent-kit) — 있으면 대화를 Control Plane 에 report_run 으로 남긴다.
# 없으면 채팅은 그대로 되고 텔레메트리만 빠진다(healthz 의 telemetry 로 드러낸다).
try:
    from samsunglife_kit import controlplane as _sl
    _SDK = True
except Exception:          # 패키지 없음 — CodeArtifact 없는 빌드
    _sl = None
    _SDK = False
import os
import time
import urllib.parse
import urllib.request
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
STREAM_TIMEOUT = int(os.getenv("STREAM_FIRST_BYTE_TIMEOUT", "25"))
_stream_supported = None      # None=미확인 · True=흐름 · False=미지원(확인됨)
_ac_stream = None
def _stream_client():
    global _ac_stream
    if _ac_stream is None:
        from botocore.config import Config
        _ac_stream = boto3.client(
            "bedrock-agentcore", region_name=AWS_REGION,
            config=Config(read_timeout=STREAM_TIMEOUT,
                          connect_timeout=10, retries={"max_attempts": 0}))
    return _ac_stream


def _rt_session(sid: str) -> str:
    """AgentCore 는 runtimeSessionId 를 33자 이상으로 요구한다.

    짧으면 호출이 통째로 거부되므로 결정론적으로 늘린다. 난수를 붙이면 매 호출
    새 세션이 되어 멀티턴이 깨지므로, 같은 입력은 항상 같은 값이 나와야 한다.
    """
    sid = (sid or "").strip()
    if len(sid) >= 33:
        return sid
    return (sid + "-" + hashlib.sha256(sid.encode("utf-8")).hexdigest())[:64]


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _invoke_buffered(payload: bytes, session_id: str):
    """accept=application/json 으로 한 번에 받는다. (output, citations) 반환."""
    if _use_jwt():
        raw = _invoke_jwt(payload, session_id, stream=False)
        status_code = 200
    else:
        r = _client().invoke_agent_runtime(
            agentRuntimeArn=AGENT_ARN, payload=payload,
            contentType="application/json", accept="application/json",
            runtimeSessionId=_rt_session(session_id),
        )
        raw = r["response"].read().decode("utf-8", errors="replace")
        status_code = r.get("statusCode")
    try: parsed = json.loads(raw)
    except Exception: parsed = {"output": raw}
    out = (parsed.get("output") or parsed.get("final") or parsed.get("answer")
           or parsed.get("result") or parsed.get("text") or "")
    return out, (parsed.get("citations") or []), status_code



# ── JWT(OAuth) 인바운드 ────────────────────────────────────────────────
# Studio 에서 배포한 에이전트·팀은 JWT 가 기본이라 SigV4 로 부르면 거부된다
# (AccessDeniedException: Authorization method mismatch). IdP 설정이 있으면
# client_credentials 로 토큰을 받아 Bearer 로 직접 호출한다 — AWS 자격증명이
# 없어도 되고 계정·리전을 넘어 부를 수 있다.
IDP_TOKEN_ENDPOINT = os.getenv("IDP_TOKEN_ENDPOINT", "").strip()
IDP_CLIENT_ID = os.getenv("IDP_CLIENT_ID", "").strip()
IDP_CLIENT_SECRET = os.getenv("IDP_CLIENT_SECRET", "").strip()
IDP_SCOPE = os.getenv("IDP_SCOPE", "").strip()

def _ssl_ctx():
    """CA 번들을 명시한다. 일부 환경(특히 macOS 파이썬)은 시스템 저장소를 못 봐서
    CERTIFICATE_VERIFY_FAILED 로 죽는다. certifi 가 없으면 기본값으로 둔다."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_token_cache = {"token": None, "exp": 0.0}


def _use_jwt() -> bool:
    return bool(IDP_TOKEN_ENDPOINT and IDP_CLIENT_ID and IDP_CLIENT_SECRET)


def _bearer() -> str:
    """client_credentials 토큰. 만료 60초 전까지 재사용한다."""
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now + 60:
        return _token_cache["token"]
    basic = base64.b64encode(f"{IDP_CLIENT_ID}:{IDP_CLIENT_SECRET}".encode()).decode()
    form = {"grant_type": "client_credentials"}
    if IDP_SCOPE:
        form["scope"] = IDP_SCOPE
    req = urllib.request.Request(
        IDP_TOKEN_ENDPOINT, data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    r = json.loads(urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()).read())
    _token_cache["token"] = r["access_token"]
    _token_cache["exp"] = now + float(r.get("expires_in", 3600))
    return _token_cache["token"]


def _runtime_url(accept_stream: bool = False) -> str:
    region = AGENT_ARN.split(":")[3] if AGENT_ARN.count(":") >= 4 else AWS_REGION
    enc = urllib.parse.quote(AGENT_ARN, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"


def _invoke_jwt(payload: bytes, session_id: str, stream: bool):
    """Bearer 로 직접 호출. stream=True 면 응답 객체를, 아니면 본문 문자열을 준다."""
    req = urllib.request.Request(
        _runtime_url(stream), data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json",
                 "Authorization": f"Bearer {_bearer()}",
                 "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": _rt_session(session_id)})
    resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT if stream else 180,
                                  context=_ssl_ctx())
    return resp if stream else resp.read().decode("utf-8", errors="replace")


# Control Plane 이 배포 때 넣어 주는 값 — 이 UI 가 부르는 에이전트의 레지스트리 id
CTP_AGENT_ID = os.getenv("CTP_AGENT_ID", "").strip()
CTP_AGENT_NAME = os.getenv("CTP_AGENT_NAME", "").strip() or f"{os.getenv('UI_TITLE', 'UI')} chat"


def _telemetry_on() -> bool:
    try:
        return bool(_SDK and CTP_AGENT_ID and _sl.enabled())
    except Exception:
        return False


def _report(session_id: str, prompt: str, out: str, status: str, t0: float, error: str = "") -> None:
    """대화 한 턴을 Control Plane 에 남긴다 (background · best-effort).
    세션은 실제 Runtime 세션(_rt_session)으로 — 궤적(span)의 session.id 와 같아야 이어진다."""
    if not _telemetry_on():
        return
    try:
        _sl.report_run(CTP_AGENT_ID, name=CTP_AGENT_NAME,
                       session_id=_rt_session(session_id),
                       input=prompt, output=out or "", status=status,
                       latency_ms=int((time.time() - t0) * 1000), error=error)
    except Exception as e:
        log.warning("report_run failed: %s", str(e)[:120])


@app.get("/healthz")
def healthz():
    return jsonify({"status": "healthy", "sdk": _SDK, "telemetry": _telemetry_on(),
                       "auth": "jwt" if _use_jwt() else "sigv4",
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
    t0 = time.time()
    try:
        out, citations, status_code = _invoke_buffered(payload, session_id)
        _report(session_id, prompt, out, "success" if out else "error", t0,
                "" if out else "empty response")
        return jsonify({"output": out, "citations": citations,
                        "session_id": session_id, "status_code": status_code})
    except Exception as e:
        log.error("invoke failed: %s", e)
        _report(session_id, prompt, "", "error", t0, str(e)[:300])
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
        t0 = time.time()
        acc: list = []          # 답변 본문 누적 — 대화 기록용
        err = ""
        # 에이전트가 스트리밍을 지원하면 그대로 흘려보낸다.
        blocks = 0
        # 한 번 "안 흐른다"고 확인했으면 다음부터는 제한시간을 낭비하지 않는다.
        # 매 요청마다 25초를 버리면 채팅으로 못 쓴다.
        if _stream_supported is not False:
            try:
                if _use_jwt():
                    stream = _invoke_jwt(payload, session_id, stream=True)
                else:
                    r = _stream_client().invoke_agent_runtime(
                        agentRuntimeArn=AGENT_ARN, payload=payload,
                        contentType="application/json", accept="text/event-stream",
                        runtimeSessionId=_rt_session(session_id),
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
                        try:      # token 텍스트만 모은다 (event: token / kind: text)
                            _ev, _data = "", ""
                            for _ln in blk.decode("utf-8", errors="replace").split("\n"):
                                if _ln.startswith("event:"): _ev = _ln[6:].strip()
                                elif _ln.startswith("data:"): _data += _ln[5:].strip()
                            _o = json.loads(_data) if _data else {}
                            if isinstance(_o, dict) and (_ev or _o.get("kind")) in ("token", "text") \
                                    and isinstance(_o.get("text"), str):
                                acc.append(_o["text"])
                        except Exception:
                            pass
                        yield blk + b"\n\n"
                if blocks:
                    _stream_supported = True
            except (ReadTimeoutError, socket.timeout, TimeoutError) as e:
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
                    acc.append(out)
                    # 한 덩어리로 보낸다 — 쪼개서 흘리면 실제로는 안 그런 것을
                    # 토큰 스트리밍처럼 보이게 꾸미는 셈이다.
                    yield _sse("token", {"text": out})
                    if citations:
                        yield _sse("citations", {"citations": citations})
                else:
                    err = "empty response"
                    yield _sse("error", {"error": "에이전트가 빈 응답을 돌려줬습니다"})
            except Exception as e:
                log.error("buffered 대체도 실패: %s", e)
                err = str(e)[:300]
                yield _sse("error", {"error": str(e)[:300]})

        _report(session_id, prompt, "".join(acc), "error" if err else "success", t0, err)
        yield _sse("end", {"session_id": session_id})

    return Response(gen(), mimetype="text/event-stream",
                       headers={"Cache-Control": "no-cache",
                                  "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)

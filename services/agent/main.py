"""AgentCore Runtime HTTP server.

Contract: POST /invocations  (JSON in/out, can stream SSE)
          GET  /ping          (health probe)

Entry point — payload 받아서 src.agent.run() 에 위임. 응답은 JSON 또는
text/event-stream (Accept 헤더 기반).
"""
import json
import logging
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.agent import run_invocation, run_invocation_stream

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("agent")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt % args)

    # ── health probe ──
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
            return
        self.send_response(404); self.end_headers()

    # ── invoke ──
    def do_POST(self):
        if self.path != "/invocations":
            self.send_response(404); self.end_headers(); return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            self._reply_json(400, {"error": f"bad json: {e}"})
            return

        accept = self.headers.get("Accept", "")
        prompt = payload.get("input") or payload.get("prompt") or ""
        session_id = payload.get("session_id") or "default"

        # SSE 요청 — 토큰 단위 스트리밍
        if "text/event-stream" in accept:
            try:
                self._stream(prompt, session_id)
            except Exception as e:
                log.error("stream failed: %s\n%s", e, traceback.format_exc())
            return

        # buffered JSON
        try:
            result = run_invocation(prompt, session_id)
            self._reply_json(200, result)
        except Exception as e:
            log.error("invoke failed: %s\n%s", e, traceback.format_exc())
            self._reply_json(500, {
                "error": str(e),
                "traceback": traceback.format_exc()[:2000],
            })

    # ── helpers ──
    def _reply_json(self, code: int, body):
        b = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _stream(self, prompt: str, session_id: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Content-Length 도 chunked 도 없는 HTTP/1.0 스트림이다 — keep-alive 면 앞단
        # 프록시가 응답의 끝을 몰라 전부 붙들고 있다(클라이언트는 60초 동안 0바이트).
        # 연결을 닫는 것으로 끝을 알린다. Studio handler 와 같은 헤더.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # AgentCore proxy 버퍼링 우회 — 첫 64KB padding
        self.wfile.write(b": " + b"x" * 65536 + b"\n\n")
        self.wfile.flush()

        for ev in run_invocation_stream(prompt, session_id):
            kind = ev.get("kind", "message")
            data = json.dumps(ev, ensure_ascii=False)
            self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log.info("agent runtime listening on :%d", port)
    # ThreadingHTTPServer 여야 한다 — 단일 스레드면 SSE 로 LLM 을 스트리밍하는 동안
    # AgentCore 의 /ping 헬스체크에 답을 못 해 컨테이너가 죽고 재시작된다(502).
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

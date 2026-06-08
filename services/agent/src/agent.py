"""ReAct loop — Anthropic native tool_use + Bedrock Converse stream.

구조:
  1. AgentCore Memory 에서 이전 대화 가져옴 (멀티턴)
  2. 도구 spec 빌드 (bedrock_kb / databricks_kb — enabled 한 것만)
  3. invoke_model_with_response_stream + tools 로 ReAct loop (max 5 iter)
  4. 매 iter: text → 응답 텍스트, tool_use → tool 실행 → tool_result → 다음 iter
  5. 끝나면 USER + ASSISTANT 이벤트 저장

진짜 운영 단순화 — LangChain/LangGraph 같은 의존성 없음. boto3 + native API.
"""
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import boto3

from .adapters import bedrock_kb, databricks_kb

log = logging.getLogger(__name__)

_REGION = os.getenv("AWS_REGION", "us-east-1")
_MODEL_ID = os.getenv("LLM_MODEL_ID", "anthropic.claude-sonnet-4-6")
_MEMORY_ID = (os.getenv("AGENTCORE_MEMORY_ID", "") or "").strip()
_ACTOR_ID = "agent-sample-actor"
_MAX_ITER = 5
_MAX_TOKENS = 4096

_br = None
def _bedrock():
    global _br
    if _br is None:
        _br = boto3.client("bedrock-runtime", region_name=_REGION)
    return _br

_acd = None
def _ac_data():
    global _acd
    if _acd is None and _MEMORY_ID:
        _acd = boto3.client("bedrock-agentcore", region_name=_REGION)
    return _acd


def _resolve_model(model: str) -> str:
    """Bedrock cross-region inference profile 자동 변환."""
    if model.startswith("us.") or model.startswith("eu.") or model.startswith("apac."):
        return model
    if "claude" in model.lower():
        return f"us.{model}" if "anthropic" in model else f"us.anthropic.{model}"
    return model


def _normalize_session_id(sid: str) -> str:
    """AgentCore Memory sessionId 33+ chars 요구."""
    if len(sid) >= 33:
        return sid
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:48]


def _build_tools() -> List[Dict[str, Any]]:
    """enabled 한 KB 도구만 spec 등록."""
    specs: List[Dict[str, Any]] = []
    if bedrock_kb.enabled():
        specs.append({
            "name": "retrieve_bedrock_kb",
            "description": (
                "AWS Bedrock Knowledge Base 에서 관련 문서를 검색합니다. "
                "S3 기반 RAG. 영문 자료 + 일반 도메인에 적합."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        })
    if databricks_kb.enabled():
        specs.append({
            "name": "retrieve_databricks_kb",
            "description": (
                "Databricks Vector Search (bge-m3 임베딩) 에서 관련 문서를 검색합니다. "
                "한국어 자료 + 보험 약관 / 사내 문서 검색에 강함."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색어"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        })
    return specs


def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "retrieve_bedrock_kb":
        return bedrock_kb.retrieve(args.get("query", ""), int(args.get("top_k") or 5))
    if name == "retrieve_databricks_kb":
        return databricks_kb.retrieve(args.get("query", ""), int(args.get("top_k") or 5))
    return {"error": f"unknown tool: {name}"}


def _load_history(session_id: str) -> List[Dict[str, Any]]:
    """AgentCore Memory 에서 USER/ASSISTANT 메시지 복원."""
    if not _MEMORY_ID:
        return []
    try:
        sid = _normalize_session_id(session_id)
        r = _ac_data().list_events(
            memoryId=_MEMORY_ID, sessionId=sid, actorId=_ACTOR_ID,
            includePayloads=True, maxResults=40,
        )
        events = r.get("events", [])
        msgs: List[Dict[str, Any]] = []
        for ev in events:
            for pl in (ev.get("payload") or []):
                conv = pl.get("conversational") or {}
                role = (conv.get("role") or "").lower()
                content = (conv.get("content") or {}).get("text", "")
                if role in ("user", "assistant") and content:
                    msgs.append({"role": role, "content": [{"type": "text", "text": content}]})
        # leading orphan ASSISTANT 제거
        while msgs and msgs[0]["role"] == "assistant":
            msgs.pop(0)
        return msgs
    except Exception as e:
        log.warning("memory list_events failed: %s", e)
        return []


def _save_turn(session_id: str, user_text: str, assistant_text: str):
    if not _MEMORY_ID or not assistant_text:
        return
    try:
        sid = _normalize_session_id(session_id)
        ts = int(time.time() * 1000)
        # USER
        _ac_data().create_event(
            memoryId=_MEMORY_ID, sessionId=sid, actorId=_ACTOR_ID,
            eventTimestamp=ts,
            payload=[{"conversational": {"role": "USER",
                                              "content": {"text": user_text}}}],
        )
        # ASSISTANT
        _ac_data().create_event(
            memoryId=_MEMORY_ID, sessionId=sid, actorId=_ACTOR_ID,
            eventTimestamp=ts + 1,
            payload=[{"conversational": {"role": "ASSISTANT",
                                              "content": {"text": assistant_text}}}],
        )
    except Exception as e:
        log.warning("memory create_event failed: %s", e)


# ── core ReAct loop ──────────────────────────────────────────────

def _system_prompt() -> str:
    tool_lines = []
    if bedrock_kb.enabled():
        tool_lines.append("- retrieve_bedrock_kb: AWS Bedrock KB 검색 (영문/일반)")
    if databricks_kb.enabled():
        tool_lines.append("- retrieve_databricks_kb: Databricks Vector Search 검색 (한국어/사내 강함)")
    tool_block = ""
    if tool_lines:
        tool_block = (
            "\n\n다음 도구를 사용할 수 있습니다:\n" + "\n".join(tool_lines)
            + "\n\n검색이 필요한 질문이면 적절한 도구를 호출하고, 결과의 인용을 포함해 답변하세요. "
            "도구가 빈 결과를 반환하면 도구를 다시 호출하기보다 사용자에게 명확한 답을 주세요."
        )
    return (
        "당신은 AAH 샘플 배포의 RAG 에이전트입니다. "
        "친절하고 정확하게 한국어로 답합니다."
        + tool_block
    )


def _converse_stream(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
    """Anthropic Messages API (invoke_model_with_response_stream).

    yields: {"kind": "text"|"tool_use_start"|"tool_use_delta"|"tool_use_end"|"done"|"error", ...}
    """
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": _MAX_TOKENS,
        "system": _system_prompt(),
        "messages": messages,
    }
    if tools:
        body["tools"] = tools

    try:
        r = _bedrock().invoke_model_with_response_stream(
            modelId=_resolve_model(_MODEL_ID),
            body=json.dumps(body).encode("utf-8"),
            contentType="application/json", accept="application/json",
        )
    except Exception as e:
        yield {"kind": "error", "message": str(e)[:300]}
        return

    cur_block = None  # {type, id, name, json_acc}
    assistant_content: List[Dict[str, Any]] = []
    stop_reason = ""
    usage = {}

    for ev in r["body"]:
        chunk_b = ev.get("chunk", {}).get("bytes") or b""
        if not chunk_b:
            continue
        try:
            d = json.loads(chunk_b.decode("utf-8"))
        except Exception:
            continue
        t = d.get("type")
        if t == "content_block_start":
            cb = d.get("content_block") or {}
            if cb.get("type") == "text":
                cur_block = {"type": "text", "text": ""}
            elif cb.get("type") == "tool_use":
                cur_block = {"type": "tool_use", "id": cb.get("id"),
                                "name": cb.get("name"), "json_acc": ""}
                yield {"kind": "tool_use_start", "name": cb.get("name"), "id": cb.get("id")}
        elif t == "content_block_delta":
            delta = d.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if cur_block and cur_block["type"] == "text":
                    cur_block["text"] += text
                yield {"kind": "text", "text": text}
            elif delta.get("type") == "input_json_delta":
                if cur_block and cur_block["type"] == "tool_use":
                    cur_block["json_acc"] += delta.get("partial_json", "")
        elif t == "content_block_stop":
            if cur_block:
                if cur_block["type"] == "text":
                    assistant_content.append({"type": "text", "text": cur_block["text"]})
                elif cur_block["type"] == "tool_use":
                    try: args = json.loads(cur_block["json_acc"] or "{}")
                    except Exception: args = {}
                    assistant_content.append({
                        "type": "tool_use", "id": cur_block["id"],
                        "name": cur_block["name"], "input": args,
                    })
                    yield {"kind": "tool_use_end", "id": cur_block["id"],
                              "name": cur_block["name"], "input": args}
                cur_block = None
        elif t == "message_delta":
            delta = d.get("delta") or {}
            if delta.get("stop_reason"):
                stop_reason = delta["stop_reason"]
            if d.get("usage"):
                usage.update(d["usage"])
        elif t == "message_stop":
            pass

    yield {"kind": "done", "stop_reason": stop_reason, "usage": usage,
              "assistant_content": assistant_content}


def _react_loop(user_text: str, history: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """ReAct: max _MAX_ITER 회 반복. 매 iter 끝나면 status 이벤트 emit."""
    tools = _build_tools()
    messages: List[Dict[str, Any]] = list(history) + [
        {"role": "user", "content": [{"type": "text", "text": user_text}]},
    ]

    final_text = ""
    for iter_i in range(_MAX_ITER):
        yield {"kind": "iter_start", "iter": iter_i + 1}
        assistant_content: List[Dict[str, Any]] = []
        tool_uses: List[Dict[str, Any]] = []
        stop_reason = ""
        for ev in _converse_stream(messages, tools):
            kind = ev.get("kind")
            if kind == "text":
                yield {"kind": "token", "text": ev.get("text", "")}
            elif kind == "tool_use_start":
                yield {"kind": "tool_use_start", "name": ev.get("name")}
            elif kind == "tool_use_end":
                tool_uses.append(ev)
            elif kind == "done":
                stop_reason = ev.get("stop_reason", "")
                assistant_content = ev.get("assistant_content", [])
                break
            elif kind == "error":
                yield {"kind": "error", "message": ev.get("message", "?")}
                return
        # assistant turn 누적
        messages.append({"role": "assistant", "content": assistant_content})
        # 텍스트 응답만 final 후보
        for block in assistant_content:
            if block.get("type") == "text":
                final_text = block.get("text", "")

        # tool_use 없으면 끝
        if stop_reason != "tool_use" or not tool_uses:
            yield {"kind": "iter_end", "iter": iter_i + 1, "had_tool_use": False}
            break

        # 도구 실행 후 user tool_result 메시지로 다음 iter
        tool_results = []
        for tu in tool_uses:
            name = tu.get("name", "")
            args = tu.get("input", {}) or {}
            tool_id = tu.get("id", "")
            result = _call_tool(name, args)
            yield {"kind": "tool_result", "name": name,
                      "ok": "error" not in result,
                      "count": result.get("count", 0)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps(result, ensure_ascii=False)[:8000],
            })
        messages.append({"role": "user", "content": tool_results})
        yield {"kind": "iter_end", "iter": iter_i + 1, "had_tool_use": True}

    yield {"kind": "final", "text": final_text}


# ── public API ──────────────────────────────────────────────────

def run_invocation(prompt: str, session_id: str) -> Dict[str, Any]:
    """Buffered — 전체 final 답변 반환."""
    history = _load_history(session_id)
    final_text = ""
    citations: List[Dict[str, Any]] = []
    for ev in _react_loop(prompt, history):
        if ev.get("kind") == "final":
            final_text = ev.get("text", "")
        elif ev.get("kind") == "tool_result":
            citations.append({"tool": ev.get("name"),
                                  "ok": ev.get("ok"), "count": ev.get("count", 0)})
    if final_text:
        _save_turn(session_id, prompt, final_text)
    return {
        "output": final_text,
        "answer": final_text,           # backward compat
        "session_id": session_id,
        "model_id": _MODEL_ID,
        "citations": citations,
    }


def run_invocation_stream(prompt: str, session_id: str) -> Iterator[Dict[str, Any]]:
    """SSE — token / tool_use / iter_start / iter_end / final."""
    history = _load_history(session_id)
    final_text = ""
    for ev in _react_loop(prompt, history):
        yield ev
        if ev.get("kind") == "final":
            final_text = ev.get("text", "")
    if final_text:
        _save_turn(session_id, prompt, final_text)

"""Bedrock Knowledge Base 검색 — bedrock-agent-runtime.retrieve.

BEDROCK_KB_ID 환경변수가 비어있으면 enabled=False — 도구 등록에서 제외됨.
"""
import os
import logging
from typing import Any, Dict, List

import boto3

log = logging.getLogger(__name__)

_KB_ID = os.getenv("BEDROCK_KB_ID", "").strip()
_REGION = os.getenv("BEDROCK_KB_REGION", "us-east-1").strip() or "us-east-1"


def enabled() -> bool:
    return bool(_KB_ID)


_client = None
def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agent-runtime", region_name=_REGION)
    return _client


def retrieve(query: str, top_k: int = 5) -> Dict[str, Any]:
    """KB 검색. 반환: {results: [{text, score, source}], count, error?}"""
    if not enabled():
        return {"results": [], "count": 0, "error": "BEDROCK_KB_ID not set"}
    try:
        r = _get_client().retrieve(
            knowledgeBaseId=_KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        hits: List[Dict[str, Any]] = []
        for item in r.get("retrievalResults", []):
            content = (item.get("content") or {}).get("text", "")
            score = float(item.get("score", 0.0))
            loc = (item.get("location") or {})
            source = (loc.get("s3Location") or {}).get("uri") or loc.get("type", "") or ""
            hits.append({"text": content, "score": score, "source": source})
        return {"results": hits, "count": len(hits)}
    except Exception as e:
        log.error("bedrock kb retrieve failed: %s", e)
        return {"results": [], "count": 0, "error": str(e)[:300]}

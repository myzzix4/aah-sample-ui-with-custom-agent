"""Databricks Vector Search 검색 — self-managed embedding (bge-m3 권장).

DBX_HOST / DBX_TOKEN / DBX_INDEX_NAME / DBX_EMBED_ENDPOINT 모두 있어야 활성.
하나라도 비어있으면 enabled=False — 도구 등록 X.

DBX_EMBED_ENDPOINT 가 비어있으면 auto-embed 모드 (Vector Search 자체 임베딩)
— 다만 회사 한국어 데이터는 self-managed (bge-m3) 권장.

httpx 미vendored — urllib + SSL ctx (DBX_SSL_VERIFY=false 회사망 대응).
"""
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List

log = logging.getLogger(__name__)

_HOST = (os.getenv("DBX_HOST", "") or "").rstrip("/")
_TOKEN = (os.getenv("DBX_TOKEN", "") or "").strip()
_INDEX = (os.getenv("DBX_INDEX_NAME", "") or "").strip()
_EMBED = (os.getenv("DBX_EMBED_ENDPOINT", "") or "").strip()
_SSL = (os.getenv("DBX_SSL_VERIFY", "true") or "true").lower() != "false"


def enabled() -> bool:
    return bool(_HOST and _TOKEN and _INDEX)


def _ssl_ctx():
    if _SSL:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_post(path: str, body: dict, timeout: int = 30) -> dict:
    url = f"{_HOST}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return json.loads(r.read().decode("utf-8"))


def _embed(text: str) -> List[float]:
    """bge-m3 (or any Custom Serving) embedding 호출."""
    resp = _http_post(f"/serving-endpoints/{_EMBED}/invocations",
                          {"inputs": [text]})
    preds = resp.get("predictions") or resp.get("outputs") or []
    if isinstance(preds, list) and preds and isinstance(preds[0], list):
        return preds[0]
    raise RuntimeError(f"unexpected embed response: {str(resp)[:200]}")


def retrieve(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Vector Search 검색. 반환: {results: [{text, score, source}], count, error?}"""
    if not enabled():
        return {"results": [], "count": 0, "error": "DBX_* env not fully set"}
    try:
        # self-managed embedding 모드
        if _EMBED:
            vec = _embed(query)
            body = {"num_results": top_k, "query_vector": vec, "columns": ["text", "source"]}
        else:
            # auto-embed (Vector Search 자체 호출)
            body = {"num_results": top_k, "query_text": query, "columns": ["text", "source"]}
        resp = _http_post(f"/api/2.0/vector-search/indexes/{_INDEX}/query", body)
        result = resp.get("result") or {}
        data = result.get("data_array") or []
        manifest = (result.get("manifest") or {}).get("columns") or []
        col_names = [c.get("name", "") for c in manifest]
        # score는 보통 마지막 컬럼 또는 별도 키 (`__score`)
        hits: List[Dict[str, Any]] = []
        for row in data:
            d = dict(zip(col_names, row))
            hits.append({
                "text": d.get("text", "") or "",
                "source": d.get("source", "") or "",
                "score": float(d.get("__score") or row[-1] if isinstance(row[-1], (int, float)) else 0.0),
            })
        return {"results": hits, "count": len(hits)}
    except Exception as e:
        log.error("databricks vector search failed: %s", e)
        return {"results": [], "count": 0, "error": str(e)[:300]}

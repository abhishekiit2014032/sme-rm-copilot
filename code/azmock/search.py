"""Azure AI Search mock: the policy index (REST data plane, api-version 2024-07-01).

Imitates:
  PUT  /indexes/{index}                    create or update the index definition
  POST /indexes/{index}/docs/index         {"value": [{"@search.action": "mergeOrUpload"|"upload"|"delete", ...}]}
  GET  /indexes/{index}/docs/$count
  POST /indexes/{index}/docs/search        {"search", "filter", "top", "select", "queryType": "semantic",
                                            "semanticConfiguration", "vectorQueries": [{"kind": "vector", "vector", "fields", "k"}]}
       -> {"value": [{"@search.score", "@search.rerankerScore", ...fields}]}

Ranking reuses `rm_copilot.retrieval`: BM25 for keywords, cosine for vectors, reciprocal-rank fusion (k=60) for
hybrid `@search.score`, and the local reranker for `@search.rerankerScore`. Filters support the OData subset the
tool-gateway uses for security trimming: `eq ne lt le gt ge` joined by `and`, on strings, numbers, booleans and
`Edm.DateTimeOffset` (e.g. `status eq 'current' and clearance_rank le 1 and effective_from le 2026-11-26T00:00:00Z`).

Differences: the mock reranker score is BM25 x query coverage, NOT the calibrated 0-4 scale of the semantic
ranker, so the retrieval gate values (`rmc:tool-gateway:search:candidate_floor`, `rmc:orchestrator:gate:retrieval:*`,
LLD §17.2) are published re-tuned for this scale and must be re-tuned on real Azure with the golden set
(`python3 -m evals.run_evals --tune-gate`). No `or`/`not`/`search.in`, analyzers (no `ar.microsoft`), scoring profiles, captions, answers,
synonym maps, replicas or index aliases.
"""
from __future__ import annotations

import math
import re
import threading

from rm_copilot.retrieval import bm25, rerank
from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request, Response

from ._base import Mock

RBAC = {"tool-gateway": {"read"}, "ingestion-worker": {"read", "write"}}
_TERM = re.compile(r"\s*(\w+)\s+(eq|ne|lt|le|gt|ge)\s+('(?:[^']|'')*'|[\w:.+-]+)\s*", re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")
RRF_K = 60


def _norm(v):
    if isinstance(v, str) and _DATE.match(v):
        return (v[:10] + "T" + (v[11:19] if len(v) >= 19 else "00:00:00")) + "Z"
    return v


def _literal(raw: str):
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if _DATE.match(raw):
        return raw
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            raise HttpError(400, "InvalidRequestParameter", f"Invalid expression: unsupported literal {raw}") from None


def parse_filter(expr: str | None) -> list[tuple[str, str, object]]:
    if not expr:
        return []
    terms = []
    for part in re.split(r"\s+and\s+", expr.strip(), flags=re.I):
        m = _TERM.fullmatch(part)
        if not m:
            raise HttpError(400, "InvalidRequestParameter", "Invalid expression: the mock supports 'field op literal' terms joined by 'and'")
        terms.append((m.group(1), m.group(2).lower(), _literal(m.group(3))))
    return terms


def matches(doc: dict, terms: list[tuple[str, str, object]]) -> bool:
    for field, op, lit in terms:
        a, b = _norm(doc.get(field)), _norm(lit)
        try:
            ok = {"eq": a == b, "ne": a != b}.get(op)
            if ok is None:
                if a is None or b is None:
                    return False
                ok = {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]
        except TypeError:
            return False
        if not ok:
            return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


class SearchMock(Mock):
    name = "search"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self._indexes: dict[str, dict] = {}
        self._lock = threading.Lock()
        a = self.app.route
        a("PUT", "/indexes/{index}")(self._put_index)
        a("POST", "/indexes/{index}/docs/index")(self._index_docs)
        a("GET", "/indexes/{index}/docs/$count")(lambda r: Response(200, str(len(self._index(r, "read")["docs"])), content_type="text/plain"))
        a("POST", "/indexes/{index}/docs/search")(self._search)

    def _index(self, r: Request, action: str) -> dict:
        self.require_api_version(r)
        self.simulate("search")
        self.rbac(self.caller(r, AUD["search"]), RBAC, action)
        with self._lock:
            idx = self._indexes.get(r.params["index"])
        if idx is None:
            raise HttpError(404, "IndexNotFound", f"The index '{r.params['index']}' for service was not found.")
        return idx

    def _put_index(self, r: Request) -> Response:
        self.require_api_version(r)
        self.rbac(self.caller(r, AUD["search"]), RBAC, "write")
        body = r.json()
        fields = body.get("fields") or []
        key = next((f["name"] for f in fields if f.get("key")), None)
        if not key:
            raise HttpError(400, "InvalidRequestParameter", "index needs a key field")
        with self._lock:
            created = r.params["index"] not in self._indexes
            old = self._indexes.get(r.params["index"], {}).get("docs", {})
            self._indexes[r.params["index"]] = {"definition": body, "key": key, "docs": old}
        return Response(201 if created else 200, body)

    def _index_docs(self, r: Request) -> dict:
        idx = self._index(r, "write")
        out = []
        with self._lock:
            for d in r.json().get("value", []):
                action, k = d.get("@search.action", "upload"), d.get(idx["key"])
                doc = {f: v for f, v in d.items() if not f.startswith("@")}
                if action == "delete":
                    idx["docs"].pop(k, None)
                elif action == "mergeOrUpload" and k in idx["docs"]:
                    idx["docs"][k].update(doc)
                else:
                    idx["docs"][k] = doc
                out.append({"key": k, "status": True, "errorMessage": None, "statusCode": 200 if action != "upload" else 201})
        return {"value": out}

    def _search(self, r: Request) -> dict:
        idx = self._index(r, "read")
        body = r.json()
        terms = parse_filter(body.get("filter"))
        with self._lock:
            pool = [d for d in idx["docs"].values() if matches(d, terms)]
        top = int(body.get("top") or 50)
        text = (body.get("search") or "").strip()
        keyword: list[tuple[float, dict]] = [(s, d) for s, d in bm25(text, pool) if s > 0][:50] if text and text != "*" else []
        vector: list[tuple[float, dict]] = []
        for vq in body.get("vectorQueries") or []:
            field = vq.get("fields", "contentVector")
            scored = [(_cosine(vq.get("vector") or [], d.get(field) or []), d) for d in pool]
            vector = sorted((x for x in scored if x[0] > 0), key=lambda x: x[0], reverse=True)[: int(vq.get("k") or 50)]
        if keyword and vector:  # hybrid: reciprocal-rank fusion
            fused: dict[int, list] = {}
            for ranking in (keyword, vector):
                for rank, (_, d) in enumerate(ranking, 1):
                    fused.setdefault(id(d), [0.0, d])[0] += 1 / (RRF_K + rank)
            ranked = sorted(((s, d) for s, d in fused.values()), key=lambda x: x[0], reverse=True)
        else:
            ranked = keyword or vector or [(1.0, d) for d in pool]
        results = [{"@search.score": round(s, 6), "_doc": d} for s, d in ranked]
        if body.get("queryType") == "semantic" and text:
            bm = {id(d): s for s, d in keyword}
            rr = {id(d): s for s, d in rerank(text, [(bm.get(id(x["_doc"]), 0.0), x["_doc"]) for x in results[:50]])}
            for x in results:
                x["@search.rerankerScore"] = round(rr.get(id(x["_doc"]), 0.0), 2)
            results = sorted(results[:50], key=lambda x: x["@search.rerankerScore"], reverse=True)
        select = [s.strip() for s in (body.get("select") or "").split(",") if s.strip()]
        out = []
        for x in results[:top]:
            doc = x.pop("_doc")
            fields = {k: v for k, v in doc.items() if (k in select if select else k != "contentVector")}
            out.append({**x, **fields})
        return {"@odata.context": f"{self.cfg.url('search')}/indexes('{r.params['index']}')/$metadata#docs(*)", "value": out}

"""ingestion-worker: builds the policy index (plan §3.2). Runs as a job at stack start-up.

Pipeline: document store -> (layout parsing) -> PII scan (AI Language) -> clause chunks -> embeddings (APIM ->
Azure OpenAI `embed`) -> AI Search index (`PUT /indexes/{name}`, `POST /indexes/{name}/docs/index`).
The fictional corpus in `rm_copilot.data.POLICY_PASSAGES` is already clause-segmented, so each clause is one chunk
(Document Intelligence is not mocked). Clauses with PII are skipped and reported, never indexed.
"""
from __future__ import annotations

from rm_copilot import data
from rm_copilot.retrieval import CLEARANCE_RANK

from services.common.httpkit import HttpError, RequestContext, set_request_context
from services.common.service import Service

INDEX_FIELDS = [
    {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
    {"name": "doc", "type": "Edm.String", "searchable": True},
    {"name": "ref", "type": "Edm.String"},
    {"name": "text", "type": "Edm.String", "searchable": True, "analyzer": "en.microsoft"},
    {"name": "status", "type": "Edm.String", "filterable": True},
    {"name": "classification", "type": "Edm.String", "filterable": True},
    {"name": "clearance_rank", "type": "Edm.Int32", "filterable": True},
    {"name": "effective_from", "type": "Edm.DateTimeOffset", "filterable": True},
    {"name": "contentVector", "type": "Collection(Edm.Single)", "searchable": True, "dimensions": 256, "vectorSearchProfile": "default"},
]


class IngestionWorker(Service):
    name = "ingestion-worker"

    def run(self) -> dict:
        set_request_context(RequestContext("ingestion"))
        s, base = self.cfg.settings, self.cfg.url("search")
        with self.telemetry.span("ingestion.run", **{"rmc.index": s.index_name}):
            r = self.client.request("PUT", f"{base}/indexes/{s.index_name}?api-version=2024-07-01", headers=self.auth_headers("search"),
                                    json_body={"name": s.index_name, "fields": INDEX_FIELDS,
                                               "semantic": {"configurations": [{"name": "policy-semantic", "prioritizedFields": {"contentFields": [{"fieldName": "text"}]}}]}},
                                    timeout=5.0, peer="search")
            if not r.ok:
                raise HttpError(502, "INDEX_CREATE_FAILED", f"index create failed ({r.status})")
            chunks = list(data.POLICY_PASSAGES)
            flagged = set()
            for i in range(0, len(chunks), 5):
                batch = chunks[i:i + 5]
                pii = self.client.request("POST", f"{self.cfg.url('language')}/language/:analyze-text?api-version=2024-11-01", headers=self.auth_headers("cognitive"),
                                          json_body={"kind": "PiiEntityRecognition", "parameters": {"redactionPolicy": {"policyKind": "noMask"}},
                                                     "analysisInput": {"documents": [{"id": c["id"], "language": "en", "text": c["text"]} for c in batch]}},
                                          timeout=5.0, peer="language")
                if pii.ok:
                    flagged |= {d["id"] for d in pii.json()["results"]["documents"] if d["entities"]}
            chunks = [c for c in chunks if c["id"] not in flagged]
            emb = self.client.request("POST", f"{self.cfg.url('apim')}/openai/deployments/embed/embeddings?api-version=2024-10-21",
                                      headers={**self.auth_headers("apim"), "x-use-case": "ingestion"},
                                      json_body={"input": [c["doc"] + " " + c["text"] for c in chunks], "dimensions": 256}, timeout=10.0, peer="apim")
            vectors = [d["embedding"] for d in emb.json()["data"]] if emb.ok else [None] * len(chunks)
            docs = [{"@search.action": "mergeOrUpload", "id": c["id"], "doc": c["doc"], "ref": c["ref"], "text": c["text"], "status": c["status"],
                     "classification": c["classification"], "clearance_rank": CLEARANCE_RANK[c["classification"]],
                     "effective_from": c["effective_from"] + "T00:00:00Z", **({"contentVector": v} if v else {})} for c, v in zip(chunks, vectors)]
            up = self.client.request("POST", f"{base}/indexes/{s.index_name}/docs/index?api-version=2024-07-01", headers=self.auth_headers("search"),
                                     json_body={"value": docs}, timeout=5.0, peer="search")
            if not up.ok:
                raise HttpError(502, "INDEX_UPLOAD_FAILED", f"upload failed ({up.status})")
            return {"index": s.index_name, "indexed": len(docs), "skipped_pii": sorted(flagged), "embedded": emb.ok}

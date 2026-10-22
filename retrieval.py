# retrieval.py
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer, SparseEncoder

# ---------- config (keep in sync with ingest) ----------
QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "math_hybrid_chunks"
DENSE_MODEL_NAME = "BAAI/bge-m3"
SPARSE_MODEL_NAME = "naver/splade-cocondenser-ensembledistil"

SEARCH_LIMIT = 30
MAX_OVERLAP = 150
PRACTICE_HEADING = "Practice Makes Perfect"   # compared with the stored heading_path, not the query

_dense = _sparse = _client = None

logger = logging.getLogger(__name__)

def _init():
    global _dense, _sparse, _client
    if _dense is not None and _sparse is not None and _client is not None:
        return
    _dense = SentenceTransformer(DENSE_MODEL_NAME)
    _sparse = SparseEncoder(SPARSE_MODEL_NAME)
    _client = QdrantClient(url=QDRANT_URL, api_key="")

MARGIN = 0.10   # a guess; tune it by printing the two scores

def _top_dense(query, flt):
    vec = _dense.encode(query, convert_to_numpy=True).tolist()
    r = _client.query_points(COLLECTION_NAME, query=vec, using="dense",
                             query_filter=models.Filter(must=flt), limit=1)
    return r.points[0].score if r.points else None

def pick_scope(query, sess):
    conds = []
    m = re.search(r"\bchapter\s+(\d+)\b", query, re.I)
    if m:
        conds.append(models.FieldCondition(key="chapter_no", match=models.MatchValue(value=int(m.group(1)))))
    m = re.search(r"\b(\d{1,2}\.\d{1,2})\b", query)
    if m:
        conds.append(models.FieldCondition(key="section_no", match=models.MatchValue(value=m.group(1))))
    if conds:
        return conds                                              # explicit reference wins

    body = [models.FieldCondition(key="chapter_no", range=models.Range(gte=1))]   # excludes Preface/Solutions
    if sess.last:
        cur = [models.FieldCondition(key="chapter", match=models.MatchValue(value=sess.last["chapter"])),
               models.FieldCondition(key="section", match=models.MatchValue(value=sess.last["section"]))]
        g, c = _top_dense(query, body), _top_dense(query, cur)
        logger.info("Scope check: book=%s unit=%s", g, c)
        if g is not None and c is not None and g - c <= MARGIN:
            return cur                                             # stay in the current unit
    return body
# ---------- session state (in-process; swap for a store later) ----------
@dataclass
class Session:
    delivered: dict = field(default_factory=dict)     # chunk_id -> payload
    served_text: dict = field(default_factory=dict)   # chunk_id -> text actually shown (overlap trimmed)
    last: dict | None = None                          # payload of the last chunk shown


SESSIONS: dict[str, Session] = {}


def _session(sid: str) -> Session:
    return SESSIONS.setdefault(sid, Session())


# ---------- text helpers ----------
def _strip_heading(text: str, heading_path: str) -> str:
    prefix = heading_path + "\n\n"
    return text[len(prefix):] if text.startswith(prefix) else text


def _overlap_len(prev_body: str, body: str) -> int:
    for k in range(min(MAX_OVERLAP, len(prev_body), len(body)), 19, -1):
        if prev_body.endswith(body[:k]):
            return k
    return 0


# ---------- search ----------
def chapter_scope(query: str):
    """Exact chapter number in the query -> metadata filter. Not intent detection."""
    m = re.search(r"\bchapter\s+(\d+)\b", query, re.I)
    if not m:
        return []
    return [models.FieldCondition(key="chapter_no", match=models.MatchValue(value=int(m.group(1))))]


def search(query: str, sess: Session, scope=None, debug: bool = False):
    dense_vec = _dense.encode(query, convert_to_numpy=True).tolist()
    sp = _sparse.encode([query], convert_to_tensor=True)[0].coalesce()
    sparse_vec = models.SparseVector(
        indices=sp.indices()[0].tolist(), values=sp.values().tolist()
    )
    flt = models.Filter(
        must=scope or None,
        must_not=[models.HasIdCondition(has_id=list(sess.delivered))] if sess.delivered else None,
    )
    res = _client.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            models.Prefetch(query=dense_vec, using="dense", limit=SEARCH_LIMIT, filter=flt),
            models.Prefetch(query=sparse_vec, using="sparse", limit=SEARCH_LIMIT, filter=flt),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=SEARCH_LIMIT,
        with_payload=True,
    )
    if debug:
        _print_fusion(dense_vec, sparse_vec, res.points, flt)
    return res.points


def _print_fusion(dense_vec, sparse_vec, fused_points, flt):
    d = _client.query_points(COLLECTION_NAME, query=dense_vec, using="dense",
                             query_filter=flt, limit=SEARCH_LIMIT, with_payload=False).points
    s = _client.query_points(COLLECTION_NAME, query=sparse_vec, using="sparse",
                             query_filter=flt, limit=SEARCH_LIMIT, with_payload=False).points
    d_info = {p.id: (i + 1, p.score) for i, p in enumerate(d)}
    s_info = {p.id: (i + 1, p.score) for i, p in enumerate(s)}

    def fmt(info):
        return f"#{info[0]:<2} {info[1]:.4f}" if info else "-"

    print(f"{'pos':<4}{'id':<7}{'rrf':<9}{'dense (rank, score)':<22}{'sparse (rank, score)':<22}heading")
    for pos, p in enumerate(fused_points[:10], 1):
        print(f"{pos:<4}{str(p.id):<7}{p.score:<9.4f}"
              f"{fmt(d_info.get(p.id)):<22}{fmt(s_info.get(p.id)):<22}{p.payload['heading_path']}")


# ---------- unit lookup (by chapter + section metadata) ----------
def unit_chunks(chapter: str, section: str):
    flt = models.Filter(must=[
        models.FieldCondition(key="chapter", match=models.MatchValue(value=chapter)),
        models.FieldCondition(key="section", match=models.MatchValue(value=section)),
    ])
    chunks, offset = [], None
    while True:
        pts, offset = _client.scroll(
            COLLECTION_NAME, scroll_filter=flt, limit=256,
            offset=offset, with_payload=True, with_vectors=False,
        )
        chunks += [p.payload for p in pts]
        if offset is None:
            break
    return sorted(chunks, key=lambda c: c["order"])   # scroll returns id order


# ---------- serving ----------
def _serve(sess: Session, p: dict):
    body = _strip_heading(p["text"], p["heading_path"])
    prev = sess.delivered.get(p.get("prev_id"))
    if prev:  # previous chunk was shown, so trim the repeated overlap
        body = body[_overlap_len(_strip_heading(prev["text"], prev["heading_path"]), body):]
    body = body.strip()
    sess.delivered[p["id"]] = p
    sess.served_text[p["id"]] = body
    sess.last = p
    return {"text": body, "heading_path": p["heading_path"], "order": p["order"],
            "chapter": p["chapter"], "section": p["section"],
            "page_start": p["page_start"], "page_end": p["page_end"]}


def ask(session_id: str, query: str, debug: bool = True):
    sess = _session(session_id)
    points = search(query, sess, pick_scope(query, sess), debug=debug)
    if not points:
        return {"error": "no matching chunk"}

    scores, best = defaultdict(float), {}
    for p in points:  # points arrive sorted by score
        key = (p.payload["chapter"], p.payload["section"])
        scores[key] += p.score
        best.setdefault(key, p.payload)  # first one seen = best hit in that unit
    unit = max(scores, key=scores.get)
    hit = best[unit]
    chunks = unit_chunks(hit["chapter"], hit["section"])
    if PRACTICE_HEADING in hit["heading_path"]:           # hit is practice -> practice only
        chunks = [c for c in chunks if PRACTICE_HEADING in c["heading_path"]]

    start = next((c for c in chunks if c["id"] not in sess.delivered), None)
    if start is None:
        return {"error": "everything in this unit was already shown"}
    return _serve(sess, start)


def next_chunk(session_id: str):
    sess = _session(session_id)
    if not sess.last or not sess.last.get("next_id"):
        return {"error": "nothing to continue"}
    pts = _client.retrieve(COLLECTION_NAME, ids=[sess.last["next_id"]], with_payload=True)
    if not pts:
        return {"error": "next chunk not found"}
    return _serve(sess, pts[0].payload)


def recap(session_id: str):
    sess = _session(session_id)
    ordered = sorted(sess.delivered.values(), key=lambda p: p["order"])
    groups = defaultdict(list)
    for p in ordered:
        groups[p["heading_path"]].append(sess.served_text[p["id"]])
    return "\n\n".join(f"## {h}\n\n" + "\n\n".join(t) for h, t in groups.items())

def reset(session_id: str) -> None:
    SESSIONS.pop(session_id, None)

if __name__ == "__main__":
    _init()
    sid = "local"
    print("Type a request, 'next', 'recap', or 'quit'.")
    while True:
        q = input("\n> ").strip()
        if q == "quit":
            break
        if q == "recap":
            print(recap(sid))
        elif q == "next":
            out = next_chunk(sid)
            print(out.get("text", out))
        else:
            out = ask(sid, q)
            pages = (f"p. {out['page_start']}" if out["page_start"] == out["page_end"]
                     else f"pp. {out['page_start']}-{out['page_end']}")
            print(f"[{out['chapter']} > {out['section']} | {pages} | order={out['order']}]")
            print(out["text"])
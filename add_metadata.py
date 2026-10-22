# add_metadata.py
import re
from collections import Counter, defaultdict

from qdrant_client import QdrantClient, models

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "math_hybrid_chunks"
PRACTICE_HEADING = "Practice Makes Perfect"

client = QdrantClient(url=QDRANT_URL, api_key="")

ids_by_chapter_no = defaultdict(list)
ids_by_section_no = defaultdict(list)
practice_units = Counter()
offset = None
while True:
    pts, offset = client.scroll(
        COLLECTION_NAME, limit=256, offset=offset,
        with_payload=["chapter", "section", "heading_path"], with_vectors=False,
    )
    for p in pts:
        m = re.match(r"Chapter\s+(\d+)\b", p.payload.get("chapter", ""))
        if m:
            ids_by_chapter_no[int(m.group(1))].append(p.id)
        if PRACTICE_HEADING in p.payload.get("heading_path", ""):
            practice_units[(p.payload.get("chapter"), p.payload.get("section"))] += 1
        m = re.match(r"(\d+\.\d+)\.", p.payload.get("section", ""))
        if m:
            ids_by_section_no[m.group(1)].append(p.id)
        # then: client.set_payload(COLLECTION_NAME, payload={"section_no": s}, points=ids)
    if offset is None:
        break

# add chapter_no in place; vectors and other payload fields are untouched
for n, ids in ids_by_chapter_no.items():
    client.set_payload(COLLECTION_NAME, payload={"chapter_no": n}, points=ids)

# add chapter_no in place; vectors and other payload fields are untouched
for n, ids in ids_by_section_no.items():
    client.set_payload(COLLECTION_NAME, payload={"section_no": n}, points=ids)

for field, schema in [
    ("order", models.PayloadSchemaType.INTEGER),
    ("chapter_no", models.PayloadSchemaType.INTEGER),
    ("chapter", models.PayloadSchemaType.KEYWORD),
    ("section", models.PayloadSchemaType.KEYWORD),
]:
    client.create_payload_index(COLLECTION_NAME, field, schema)

print("chapter numbers found:", sorted(ids_by_chapter_no))
print("practice chunks per (chapter, section), first 8:")
for (chap, sec), n in list(practice_units.items())[:20]:
    print(f"  {n:>3}  chapter={chap!r}  section={sec!r}")
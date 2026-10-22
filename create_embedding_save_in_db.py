import json
import re
from typing import List, Dict, Any
from tqdm import tqdm

from sentence_transformers import SentenceTransformer, SparseEncoder
from qdrant_client import QdrantClient
from qdrant_client import models

# ==============================================================================
# CONFIGURATION
# ==============================================================================
JSONL_FILE_PATH = "data/chunks.jsonl"
QDRANT_DB_URL = "http://localhost:6333"
COLLECTION_NAME = "math_hybrid_chunks"

# 1. Models
# - Dense: BGE-M3 (1024-dim, handles LaTeX, 8k context)
# - Sparse: SPLADE (Learned term-weighting for exact keyword/LaTeX symbol matching)
DENSE_MODEL_NAME = "BAAI/bge-m3"
SPARSE_MODEL_NAME = "naver/splade-cocondenser-ensembledistil"

BATCH_SIZE = 16

dense_model = None
sparse_model = None
client = None
PRACTICE_RE = re.compile(r"^[\s#*_>]*Practice Makes Perfect\b", re.I | re.M)

def initialize_ingestion():
    """Initialize models and recreate the collection for a fresh ingestion."""
    global dense_model, sparse_model, client

    print("[1/5] Loading Dense and Sparse Embedding Models...")
    dense_model = SentenceTransformer(DENSE_MODEL_NAME)
    sparse_model = SparseEncoder(SPARSE_MODEL_NAME)

    dense_dim = dense_model.get_embedding_dimension()
    print(f"      Dense Model Loaded (Dim: {dense_dim})")
    print(f"      Sparse Model Loaded ({SPARSE_MODEL_NAME})")

    print(f"[2/5] Initializing local Qdrant Client at '{QDRANT_DB_URL}'...")
    _client = QdrantClient(url=QDRANT_DB_URL, api_key="")

    print(f"[3/5] Setting up Qdrant Collection: '{COLLECTION_NAME}'...")
    if client.collection_exists(COLLECTION_NAME):
        print("::::Deleting existing collection")
        client.delete_collection(COLLECTION_NAME)

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": models.VectorParams(
                size=dense_dim,
                distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False)
            )
        }
    )

    client.create_payload_index(COLLECTION_NAME, "order", models.PayloadSchemaType.INTEGER)
    client.create_payload_index(COLLECTION_NAME, "heading_path", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(COLLECTION_NAME, "unit_index", models.PayloadSchemaType.INTEGER)
    client.create_payload_index(COLLECTION_NAME, "chapter_no", models.PayloadSchemaType.INTEGER)
    client.create_payload_index(COLLECTION_NAME, "chapter", models.PayloadSchemaType.KEYWORD)

# ==============================================================================
# 3. HELPER FUNCTIONS & INGESTION PIPELINE
# ==============================================================================
def load_jsonl(file_path: str) -> List[Dict[str, Any]]:
    """Loads JSONL file containing markdown chunk data."""
    chunks = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line.strip()))
    return chunks


def process_and_ingest():
    """Reads JSONL, computes dense/sparse embeddings, and uploads to Qdrant."""
    initialize_ingestion()
    print(f"[4/5] Loading JSONL from '{JSONL_FILE_PATH}'...")
    raw_chunks = load_jsonl(JSONL_FILE_PATH)
    print(f"      Loaded {len(raw_chunks)} chunks.")

    print("      Encoding and inserting into Qdrant...")
    for idx in tqdm(range(0, len(raw_chunks), BATCH_SIZE), desc="Ingesting Batches"):
        batch = raw_chunks[idx: idx + BATCH_SIZE]

        # Extract text content from chunks (adjust key if yours is different)
        texts = [chunk.get("text") or chunk.get("page_content", "") for chunk in batch]

        # 1. Generate Dense Vectors
        dense_embeddings = dense_model.encode(texts, show_progress_bar=False, convert_to_numpy=True)

        # 2. Generate Sparse Vectors
        sparse_embeddings = sparse_model.encode(texts, convert_to_tensor=True)

        # 3. Construct Qdrant PointStructs
        points = []
        for i, chunk in enumerate(batch):
           # point_id = str(uuid.uuid4())
            point_id = chunk["id"]
            sparse_tensor = sparse_embeddings[i].coalesce()  # Ensure indices are coalesced
            indices = sparse_tensor.indices()[0].tolist()
            values = sparse_tensor.values().tolist()

            # Format sparse vector output for Qdrant format
            sparse_vec = models.SparseVector(
                indices=indices,
                values = values
            )

            # Assemble payload
            #payload = {
            #    "text": texts[i],
            #    "metadata": chunk.get("metadata", {}),
            #    "original_id": chunk.get("id", idx + i)
            #}
            # payload = dict(chunk)

            pos = idx + i  # position in the file; assumes chunks.jsonl is written in reading order

            payload = dict(chunk)
            payload.pop("body", None)
            payload["order"] = pos
            payload["prev_id"] = raw_chunks[pos - 1]["id"] if pos > 0 else None
            payload["next_id"] = raw_chunks[pos + 1]["id"] if pos < len(raw_chunks) - 1 else None

            m = re.match(r"Chapter\s+(\d+)\.", chunk["heading_path"])
            payload["chapter_no"] = int(m.group(1)) if m else None

            # Create multi-vector Point
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector={
                        "dense": dense_embeddings[i].tolist(),
                        "sparse": sparse_vec
                    },
                    payload=payload
                )
            )

        # 4. Upsert Batch to Qdrant
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=points
        )

    print(f"\n✅ Ingestion complete! Indexed {len(raw_chunks)} chunks into '{COLLECTION_NAME}'.\n")

    print("\n testing the retrieval")
    pts, _ = client.scroll(COLLECTION_NAME, limit=5, with_payload=True, with_vectors=False)
    print(pts[0].payload.keys())
    for p in sorted(pts, key=lambda p: p.payload["order"]):
        print(p.id, p.payload["order"], p.payload["prev_id"], p.payload["next_id"], p.payload["heading_path"])





# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
if __name__ == "__main__":
    # 1. Run full ingestion pipeline
    process_and_ingest()

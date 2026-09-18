from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from core.config import settings
import uuid

# In-memory qdrant for testing if not running docker
# In production change to url=f"http://{settings.QDRANT_HOST}:{settings.QDRANT_PORT}"
client = QdrantClient(":memory:") 

COLLECTION_NAME = "rfqs"

def init_qdrant():
    if not client.collection_exists(collection_name=COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )

def add_rfq(rfq_data: dict, vector: list[float]):
    point_id = str(uuid.uuid4())
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload=rfq_data
            )
        ]
    )
    return point_id

def search_rfqs(query_vector: list[float], limit: int = 10):
    search_result = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=limit
    )
    return search_result

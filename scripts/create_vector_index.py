"""Create (or report) the Atlas Vector Search index used by question search.

Atlas builds the index asynchronously — it usually becomes queryable within a
minute or two of the vectors existing. Re-running is safe: an existing index is
reported, not duplicated.

    python scripts/create_vector_index.py
    python scripts/create_vector_index.py --wait
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo.operations import SearchIndexModel

from app.config import settings
from app.db import init_db
from app.models.question import Question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vector_index")

INDEX_NAME = "questions_vector"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true", help="poll until the index is queryable")
    args = ap.parse_args()

    await init_db()
    col = Question.get_pymongo_collection()

    cursor = await col.list_search_indexes()
    existing = await cursor.to_list(length=20)
    for idx in existing:
        if idx.get("name") == INDEX_NAME:
            logger.info("index %r already exists — status=%s queryable=%s",
                        INDEX_NAME, idx.get("status"), idx.get("queryable"))
            if not args.wait:
                return
            break
    else:
        model = SearchIndexModel(
            name=INDEX_NAME,
            type="vectorSearch",
            definition={
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": settings.embedding_dimensions,
                        "similarity": "cosine",
                    },
                    # Pre-filters: search is always scoped to a subject/exam/type.
                    {"type": "filter", "path": "subject"},
                    {"type": "filter", "path": "exam_name"},
                    {"type": "filter", "path": "question_type"},
                ]
            },
        )
        await col.create_search_index(model)
        logger.info("created index %r (Atlas will build it in the background)", INDEX_NAME)

    if args.wait:
        deadline = time.time() + 600
        while time.time() < deadline:
            cursor = await col.list_search_indexes()
            for idx in await cursor.to_list(length=20):
                if idx.get("name") == INDEX_NAME:
                    if idx.get("queryable"):
                        logger.info("index is QUERYABLE — semantic search is live")
                        return
                    logger.info("status=%s … waiting", idx.get("status"))
            await asyncio.sleep(15)
        logger.warning("timed out waiting; check the Atlas UI")


if __name__ == "__main__":
    asyncio.run(main())

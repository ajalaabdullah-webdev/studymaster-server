"""MongoDB connection + Beanie initialisation.

Beanie 2.x uses PyMongo's native async client (``AsyncMongoClient``); Motor is
deprecated/merged upstream. We connect to the SAME database the legacy Node
backend uses and map Beanie Documents onto the existing collections.
"""

import logging

from beanie import init_beanie
from pymongo import AsyncMongoClient

from app.config import settings

logger = logging.getLogger("studymaster.db")

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    if _client is None:
        raise RuntimeError("Mongo client not initialised. Call init_db() first.")
    return _client


async def init_db() -> None:
    """Open the Mongo connection and register all Beanie document models."""
    global _client

    from app.models import ALL_DOCUMENT_MODELS

    # tz_aware: datetimes come back timezone-aware (UTC). Without this they are
    # naive, serialise without a timezone marker, and browsers in UTC+1 (Nigeria)
    # read them one hour in the past ("I just did this — why does it say 1h ago?").
    _client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    database = _client[settings.mongodb_db]

    await init_beanie(database=database, document_models=ALL_DOCUMENT_MODELS)
    logger.info("Connected to MongoDB '%s' and initialised Beanie.", settings.mongodb_db)


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("Closed MongoDB connection.")

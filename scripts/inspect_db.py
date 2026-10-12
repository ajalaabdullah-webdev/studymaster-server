"""One-off DB inspection: list collections, counts, and sample doc shapes.

Run: uv run python scripts/inspect_db.py
"""

import asyncio
import json
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings


def _summarise(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        out[k] = type(v).__name__ if not isinstance(v, (str, int, float, bool)) else v
    return out


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]
    names = await db.list_collection_names()
    print("DB:", settings.mongodb_db)
    print("Collections:", sorted(names))
    print("=" * 70)

    for name in sorted(names):
        count = await db[name].estimated_document_count()
        print(f"\n### {name}  (~{count} docs)")
        sample = await db[name].find_one()
        if sample:
            sample["_id"] = str(sample.get("_id"))
            print("  keys:", list(sample.keys()))

    # Deep-dive on questions: exam distribution + options shape
    print("\n" + "=" * 70)
    print("QUESTIONS deep-dive")
    q = db["questions"]
    total = await q.estimated_document_count()
    print("  total:", total)
    for field in ("exam_name", "question_type"):
        pipeline = [{"$group": {"_id": f"${field}", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]
        dist = [d async for d in q.aggregate(pipeline)]
        print(f"  by {field}:", {d["_id"]: d["n"] for d in dist})

    # options shape sampling
    shapes = Counter()
    passage_hint = 0
    async for doc in q.find({}, {"options": 1, "question_text": 1, "correct_answer": 1}).limit(400):
        opt = doc.get("options")
        shapes[type(opt).__name__] += 1
        txt = (doc.get("question_text") or "").lower()
        if "passage" in txt or "comprehension" in txt or "underlined" in txt:
            passage_hint += 1
    print("  options types (sample 400):", dict(shapes))
    print("  passage-referencing (sample 400):", passage_hint)

    # one full objective + one theory example
    obj = await q.find_one({"question_type": "obj"})
    if obj:
        obj["_id"] = str(obj["_id"])
        print("\n  SAMPLE OBJ:", json.dumps(obj, default=str, indent=2)[:1400])

    client.close()


if __name__ == "__main__":
    asyncio.run(main())

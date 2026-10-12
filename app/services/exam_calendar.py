"""Work out when the student's exams are, to size the roadmap horizon.

Grounded by live search (dates shift yearly), with a sane fallback so roadmap
generation never blocks on flaky date lookups.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.llm import chat_json
from app.services.search import format_results_for_llm, web_search

logger = logging.getLogger("studymaster.exam_calendar")

DEFAULT_HORIZON_WEEKS = 20
MIN_HORIZON_WEEKS = 4
MAX_HORIZON_WEEKS = 40


async def get_exam_calendar(exams: list[str], now: datetime | None = None) -> dict:
    """Return {examCalendar, examDate (nearest, iso), horizonWeeks}.

    examCalendar: {"JAMB": {"date": iso|None, "label": str, "source": url|None}, ...}
    """
    now = now or datetime.now(timezone.utc)
    exams = [e.upper() for e in exams] or ["WAEC", "JAMB"]

    year = now.year
    query = (
        f"When is the {' and '.join(exams)} examination in Nigeria in {year} and {year + 1}? "
        f"Official WAEC WASSCE and JAMB UTME exam dates and timetable."
    )
    search = await web_search(query, max_results=5)

    calendar: dict = {}
    nearest: datetime | None = None
    try:
        prompt = f"""Today is {now.date().isoformat()}. Based on the research below, give the
next upcoming exam date (in the future) for each of these Nigerian exams: {", ".join(exams)}.

RESEARCH:
{format_results_for_llm(search)}

Return STRICT JSON:
{{ "exams": {{ "JAMB": {{"date": "YYYY-MM-DD", "label": "JAMB UTME 2027"}},
              "WAEC": {{"date": "YYYY-MM-DD", "label": "WAEC WASSCE 2027"}} }} }}
Only include the exams asked for. If a date is genuinely unknown, estimate the typical month
(JAMB ~ March-April, WAEC WASSCE ~ May-June) in the next cycle after today. date MUST be in the future."""
        data = await chat_json([{"role": "user", "content": prompt}], tier="smart", temperature=0.2)
        raw = data.get("exams", {}) if isinstance(data, dict) else {}
        for exam in exams:
            info = raw.get(exam) or raw.get(exam.upper()) or {}
            date_str = info.get("date")
            dt = _parse_future(date_str, now)
            calendar[exam] = {
                "date": dt.isoformat() if dt else None,
                "label": info.get("label", f"{exam} {year + 1}"),
            }
            if dt and (nearest is None or dt < nearest):
                nearest = dt
    except Exception as exc:  # noqa: BLE001
        logger.error("Exam calendar parse failed: %s", exc)

    if nearest is None:
        nearest = now + timedelta(weeks=DEFAULT_HORIZON_WEEKS)
        for exam in exams:
            calendar.setdefault(exam, {"date": nearest.isoformat(), "label": f"{exam} (estimated)"})

    horizon_weeks = max(
        MIN_HORIZON_WEEKS, min(MAX_HORIZON_WEEKS, round((nearest - now).days / 7))
    )
    return {
        "examCalendar": calendar,
        "examDate": nearest.isoformat(),
        "horizonWeeks": horizon_weeks,
    }


def _parse_future(date_str: str | None, now: datetime) -> datetime | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            return dt if dt > now else None
        except ValueError:
            continue
    return None

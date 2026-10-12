"""Canonical subject registry.

The question bank stores subjects as kebab-case slugs (e.g. ``english-language``,
``further-mathematics``, ``christian-religious-knowledge-crk``). Users, the LLM,
and the frontend speak natural names ("English", "Further Maths", "CRK"). This
module is the single source of truth that maps any of those to the real DB slug
and back to a display name, so every feature (onboarding, past questions, exams,
diagnostic) agrees on subject identity.
"""

from __future__ import annotations

import difflib

# slug -> display name. Slugs must exactly match what's stored in `questions`.
SUBJECTS: dict[str, str] = {
    "accounts-principles-of-accounts": "Principles of Accounts",
    "agricultural-science": "Agricultural Science",
    "animal-husbandry": "Animal Husbandry",
    "biology": "Biology",
    "book-keeping": "Book Keeping",
    "chemistry": "Chemistry",
    "christian-religious-knowledge-crk": "Christian Religious Knowledge (CRK)",
    "civic-education": "Civic Education",
    "commerce": "Commerce",
    "computer-studies": "Computer Studies",
    "data-processing": "Data Processing",
    "economics": "Economics",
    "english-language": "English Language",
    "french": "French",
    "further-mathematics": "Further Mathematics",
    "government": "Government",
    "history": "History",
    "home-economics": "Home Economics",
    "insurance": "Insurance",
    "islamic-religious-knowledge-irk": "Islamic Religious Knowledge (IRK)",
    "literature-in-english": "Literature in English",
    "marketing": "Marketing",
    "mathematics": "Mathematics",
    "music": "Music",
    "office-practice": "Office Practice",
    "physical-education": "Physical Education",
    "physics": "Physics",
}

# Common human/LLM aliases -> slug.
ALIASES: dict[str, str] = {
    "english": "english-language",
    "english language": "english-language",
    "use of english": "english-language",
    "eng": "english-language",
    "maths": "mathematics",
    "math": "mathematics",
    "mathematic": "mathematics",
    "further maths": "further-mathematics",
    "further math": "further-mathematics",
    "additional mathematics": "further-mathematics",
    "add maths": "further-mathematics",
    "agric": "agricultural-science",
    "agriculture": "agricultural-science",
    "agric science": "agricultural-science",
    "agricultural science": "agricultural-science",
    "crk": "christian-religious-knowledge-crk",
    "crs": "christian-religious-knowledge-crk",
    "christian religious studies": "christian-religious-knowledge-crk",
    "christian religious knowledge": "christian-religious-knowledge-crk",
    "irk": "islamic-religious-knowledge-irk",
    "irs": "islamic-religious-knowledge-irk",
    "islamic studies": "islamic-religious-knowledge-irk",
    "islamic religious studies": "islamic-religious-knowledge-irk",
    "lit": "literature-in-english",
    "literature": "literature-in-english",
    "literature in english": "literature-in-english",
    "accounts": "accounts-principles-of-accounts",
    "accounting": "accounts-principles-of-accounts",
    "principles of accounts": "accounts-principles-of-accounts",
    "poa": "accounts-principles-of-accounts",
    "computer": "computer-studies",
    "computer science": "computer-studies",
    "computer studies": "computer-studies",
    "ict": "computer-studies",
    "civic": "civic-education",
    "civic education": "civic-education",
    "data processing": "data-processing",
    "home economics": "home-economics",
    "home econs": "home-economics",
    "office practice": "office-practice",
    "physical education": "physical-education",
    "pe": "physical-education",
    "book keeping": "book-keeping",
    "bookkeeping": "book-keeping",
    "government": "government",
    "economics": "economics",
    "commerce": "commerce",
    "biology": "biology",
    "chemistry": "chemistry",
    "physics": "physics",
    "history": "history",
    "french": "french",
    "music": "music",
    "marketing": "marketing",
    "insurance": "insurance",
    "animal husbandry": "animal-husbandry",
}


def _norm(text: str) -> str:
    return " ".join(str(text).lower().strip().replace("_", " ").replace("-", " ").split())


def to_slug(name: str) -> str | None:
    """Best-effort map of any subject name to a canonical DB slug, or None."""
    if not name:
        return None
    raw = str(name).lower().strip()
    if raw in SUBJECTS:
        return raw
    norm = _norm(name)
    if norm in ALIASES:
        return ALIASES[norm]
    hyph = norm.replace(" ", "-")
    if hyph in SUBJECTS:
        return hyph
    # Fuzzy fallback against slugs and display names.
    candidates = {s: s for s in SUBJECTS}
    candidates.update({_norm(v): s for s, v in SUBJECTS.items()})
    match = difflib.get_close_matches(norm, list(candidates.keys()), n=1, cutoff=0.82)
    if match:
        return candidates[match[0]]
    return None


def normalize_many(names: list[str]) -> list[str]:
    """Map a list of subject names to unique canonical slugs (order preserved)."""
    out: list[str] = []
    for n in names:
        slug = to_slug(n)
        if slug and slug not in out:
            out.append(slug)
    return out


def display_name(slug: str) -> str:
    return SUBJECTS.get(slug, slug.replace("-", " ").title())


def all_subject_display_names() -> list[str]:
    return [SUBJECTS[s] for s in SUBJECTS]

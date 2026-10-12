# OpenAI Codex Contribution Record — StudyMaster Server

This file describes how OpenAI Codex contributed to the StudyMaster server and points reviewers to
the implementation evidence.

## Working relationship

The product owner defined the education problem, feature priorities, intended teaching experience and
acceptance criteria. StudyMaster was developed across multiple Codex sessions. Primary Codex agents
examined the system, proposed technical designs and coordinated multiple specialist subagents across
research, scraping, data cleaning, backend, frontend, testing and independent review.

## Question-bank intelligence

Codex helped build and run the end-to-end workflow that produced StudyMaster's corpus of 23,355
examination questions. Across earlier sessions, Codex agents and subagents scraped the source data,
cleaned malformed and duplicate records, normalized fields and then built the retrieval pipeline:

| Stage | Evidence in this repository |
|---|---|
| Scraping and initial cleaning | Built and run in earlier Codex sessions; the original utilities were later removed |
| Curriculum-aware labeling | `scripts/label_topics.py` |
| Semantic embedding backfill | `scripts/embed_questions.py` |
| Atlas vector-index creation | `scripts/create_vector_index.py` |
| Topic normalization | `scripts/normalize_topics.py` |
| Three-layer retrieval | `app/services/topic_search.py` |
| Question API and filtering | `app/routers/questions.py` and `app/services/questions.py` |

The search merges exact topic/text matches, semantic vector neighbours and cached AI keyword
expansion. This allows a student to find an exam question by meaning even when their words differ
from the examiner's words.

## Agentic and live-teaching work

Codex contributed to:

- previous-context-aware and versioned note generation;
- retrieval of past questions and verified diagrams before note generation;
- streaming progress and structured note/quiz output;
- OpenAI text-to-speech for read-along narration;
- lesson notebook and lesson-pack preparation;
- GPT-Live WebRTC session creation and delegation;
- Decisions-based classification for the teaching-board scribe;
- spoken mathematics and chemistry conversion to LaTeX;
- persisted board operations, pages, transcripts, quizzes, elapsed time and resumable state;
- activity tracking so lessons contribute to readiness.

Key implementation areas are `app/agents/note_agent.py`, `app/routers/notes.py`,
`app/routers/live_learning.py`, `app/services/live_learning.py`, `app/services/notebook.py`,
`app/services/lesson_pack.py`, `app/services/board_scribe.py` and `app/services/openai_speech.py`.

## Verification performed with Codex

- Python compilation across the application package
- focused Ruff checks on new live-learning modules
- authenticated API and persistence flow tests
- real OpenAI Decisions routing checks
- GPT-Live endpoint/model accessibility checks
- error-shape and fallback testing
- production frontend builds for the corresponding client changes
- published-repository integrity and secret/reference scans

The product owner completed final authenticated visual and live-audio acceptance testing.

## Custom skills and future orchestration

The owner developed reusable Codex skills and working agreements that encode how StudyMaster should be
built: phase large features, protect existing behavior, maintain design quality, verify core journeys
and explain the architecture. These instructions make development repeatable across sessions rather
than dependent on one long prompt.

Multi-agent Codex workflows were used during the build. Primary agents divided independent work among
focused subagents for research, scraping and data work, backend implementation, interface work,
testing and independent review. They then integrated the results, resolved conflicts in shared state
and verified the complete feature. The same structure remains the project's scaling mechanism for
future development.

This reflects OpenAI's documented model: skills package reusable instructions, while primary agents
delegate independent work to focused subagents and synthesize their results. See the
[OpenAI Skills documentation](https://developers.openai.com/api/docs/guides/tools-skills) and
[OpenAI Multi-agent documentation](https://developers.openai.com/api/docs/guides/agents-api/multi-agent).

## Responsibility boundary

| Product owner | OpenAI Codex |
|---|---|
| Defined the mission and student needs | Designed architecture and coordinated technical implementation |
| Prioritized and approved features | Researched OpenAI APIs and integration patterns |
| Supplied feedback from real use | Orchestrated agents and subagents across data, product and verification work |
| Performed final visual/audio testing | Ran automated and service-level verification |
| Owns deployment and product decisions | Preserved an inspectable engineering record |

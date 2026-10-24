"""Starter skills (original text). Installed once with `courier init`; the agent can patch them."""

SEEDS = {
    "research-report": """---
description: Produce a sourced research report with dates, conflicts and stated uncertainty
---
1. web_search 2-3 phrasings; open the best 3-5 pages with fetch_url (prefer official docs, papers).
2. For each claim note: source URL, published date (or "unknown"), retrieved date.
3. Separate "sources say" from "my inference". If sources disagree, list both and say which is newer/more authoritative.
4. If evidence is thin, say so plainly; do not fill gaps from memory.
5. End with a Sources list: [Title](URL) - published/retrieved dates.
""",
    "study-plan": """---
description: Build a revision schedule, quiz and flashcards from the user's own notes or syllabus
---
1. Ask for exam date, hours per day, weak topics. Read supplied notes with read_document / kb_search.
2. Split topics into sessions; put weak topics earlier; add a spaced review at +1, +3, +7 days.
3. Create to-dos with the todo tool (due dates) and a reminder job with schedule.
4. For quizzes: questions only from the supplied material; give the answer and the source location.
""",
    "lab-report-draft": """---
description: Draft a technical or lab report from supplied evidence without inventing results
---
1. Read the supplied data/notes. List exactly which measurements and observations exist.
2. Outline: aim, method, results, discussion, conclusion. Use only supplied numbers; show formulas, units and assumptions.
3. Where data is missing write "[MISSING: ...]" and ask the user; never invent values.
4. Produce the file with write_docx and read it back to confirm it was written.
""",
    "meeting-notes": """---
description: Turn a transcript or voice-note text into minutes, decisions and action items
---
1. Extract: attendees (if stated), decisions, action items with owner and deadline, open questions.
2. Mark anything inferred rather than stated. Do not invent owners or dates.
3. Offer to add action items with the todo tool and write minutes with write_docx.
""",
    "safe-file-cleanup": """---
description: Organize or clean up files without losing anything
---
1. list_dir first and report what you see. Propose the plan; wait for the user's yes before moving anything.
2. Prefer moving to a _to_review folder over deleting. Never delete without explicit confirmation.
3. After acting, list_dir again to verify the result and report exactly what changed.
""",
}

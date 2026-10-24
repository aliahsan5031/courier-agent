# Architecture
Stack: Python stdlib, SQLite (FTS5), OpenAI-compatible HTTP to NVIDIA NIM + OpenRouter. No framework, one process.
Data: ~/.courier/{memories/*.md, skills/*/SKILL.md, state.db(sessions,messages,messages_fts), config.json}.
Risks: prompt-injection into memory/skills (scanned, not signed); shell tool (approval + denylist); free-tier rate limits.
Next: cron, gateway, execute_code RPC.

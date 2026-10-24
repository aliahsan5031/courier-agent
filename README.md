# Courier Agent

A clean-room, self-improving CLI agent modelled on the *public feature set* of Nous Research's Hermes Agent
(persistent memory, skills, tool loop, subagent delegation, provider routing). No Hermes code, prompts or
assets were used. Python 3.10+, standard library only.

## Setup (keys stay in your shell, never in files)
    export NVIDIA_API_KEY="nvapi-..."          # build.nvidia.com  (several keys: comma-separated)
    export OPENROUTER_API_KEY="sk-or-..."      # openrouter.ai/keys
    python3 -m courier doctor --live           # pings each provider
    python3 -m courier models openrouter --free   # pick a current free model
    export COURIER_OPENROUTER_MODEL="<id from the list>"   # optional; same for COURIER_NVIDIA_MODEL
    python3 -m courier chat                    # or: python3 -m courier run "summarise this folder"

Route order is NVIDIA then OpenRouter (`COURIER_ROUTE=openrouter,nvidia` flips it). A 429/5xx/auth/model
error on one provider falls through to the next. Config: `~/.courier/config.json` (override `COURIER_HOME`).

## What Courier can do now (v0.3)
Tested offline with mock providers/services (107 tests). Nothing has been run against the live NVIDIA/OpenRouter/search APIs, a real
Ollama, a microphone, a display or Windows yet. `courier doctor --live` checks the providers once your keys are set.

| Area | Commands / tools |
| --- | --- |
| Chat + control | `chat`, `run`, `--plan`, `--verify`, `--style detailed`, Ctrl-C or `courier stop` = emergency stop, `resume SESSION` |
| Privacy | `--private` (local Ollama only, web tools off), `courier privacy`, `courier audit`, secrets redacted in logs |
| Memory | `--space NAME`, `memory show/export/edit/delete/pause/resume`, source/confidence/age, expiry, `sessions QUERY` |
| Research | `web_search` (Tavily, Brave, SearXNG via env keys; Wikipedia fallback), `fetch_url` (blocks private addresses), `rss_read` |
| Documents | `read_document` (txt/md/csv/docx/xlsx/pdf), `kb ingest/search`, `diff_documents`, `write_docx/xlsx/csv` |
| Organizer | `todo`, `jobs add NAME KIND "WHEN" TEXT`, `daemon`, `notifications`, `brief`; kinds: reminder, prompt, watch, disk |
| System | `system_status`, `open_app` (allowlist in config.json), `http_request` (host allowlist, idempotency keys) |
| Vision | `describe_image` (needs a vision-capable model) |
| Models | NVIDIA, OpenRouter, Ollama; light model for summaries/planning; usage tracking (`courier usage`) |
| Safety | `--safe` mode, `backup`/`restore`, config auto-backup, `selftest`, `init` (starter skills) |
| Integrations (mock-tested only) | `github`, `email_list/read/send` (IMAP read-only, SMTP with approval), `calendar_agenda/add` (local .ics + secret iCal URLs), `todoist`, `cloud_list/download/upload` (Dropbox, WebDAV), `message_send` (Telegram/Slack/Discord), `weather`, `geocode`, `distance`, `paper_search`, `public_data`, `db_query` (read-only SQLite allowlist) |
| Backup and sync | `courier sync push/pull dropbox|webdav` (manual whole-snapshot sync, safety copy, never keys or audit log), `courier backup` |
| Phone gateway | `courier telegram` (only chat ids in `telegram_allowed_chats`; approvals are denied) |
| Media | `transcribe_media` (ffmpeg + faster-whisper -> transcript, .srt, chapters, indexed for `kb_search`), `video_frames` |
| Dev extras | `security_scan` / `courier security-scan` (pattern first pass), `make_diagram` (SVG + Mermaid), workflows: `workflow_save/list/run`, `courier workflow run NAME --var k=v` |
| Search | `embeddings: {"provider": "ollama"}` in config adds semantic search (hybrid with keywords); off by default |
| Speed | `--stream` prints tokens as they arrive; `cache: {"enabled": true}` caches tool-free answers (off by default) |
| Updates | `courier update check/apply/rollback` (set `update_repo`; a SHA-256 checksum file is required) |
| Desktop control (OFF by default) | set `"desktop_enabled": true`, `pip install pyautogui`; then `keyboard_type`, `keyboard_hotkey`, `mouse_click`, `mouse_scroll`, `window_list/action`, `clipboard_read/write`, `screen_screenshot`; every action needs approval, stop flag and `desktop_max_actions` apply |
| Read aloud | `read_aloud` tool (text, clipboard, document, notifications, webpage); `courier voice --wake` for a wake word |
| Experimental, untested | `courier ui` (attach/drag-drop/copy/export/regenerate/activity panel), `courier voice` |

Config (`~/.courier/config.json`): add your apps and hosts, e.g.
`{"apps_allowlist": {"notepad": ["notepad.exe"]}, "http_allowlist": ["localhost"]}`.

Environment variables for integrations (never stored in files): `GITHUB_TOKEN`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`,
`EMAIL_USER`, `EMAIL_PASSWORD` (app password), `CALENDAR_ICS_URLS`, `TODOIST_TOKEN`, `DROPBOX_TOKEN`, `WEBDAV_URL/USER/PASSWORD`,
`TELEGRAM_BOT_TOKEN`, `SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL`.

Not built: tray/global shortcut, encryption, Google/Outlook OAuth, automatic multi-device merge, installer/licensing/commercial
pieces. Everything that touches a real account, microphone, display or speaker has only been tested against stand-ins.

## Map to Hermes features
| Hermes feature | Courier |
| --- | --- |
| MEMORY.md / USER.md, frozen snapshot | `memory.py` CuratedMemory (bounded, deduped, scanned) |
| Session search (SQLite FTS5) | `memory.py` SessionStore + `session_search` tool |
| Skills (SKILL.md, progressive disclosure) | `skills.py` + `skill_view` / `skill_manage` |
| Learns after hard tasks | `Agent.reflect()` runs after >=5 tool calls |
| Tool-use loop | `agent.py` `_loop` (budget, compaction, error-as-result) |
| delegate_task subagents | `tools.py` — 3 parallel, isolated, no memory/delegation |
| Provider routing / fallback / key pools | `providers.py` |

## Tests
    python3 -m unittest discover -s tests -v      # 107 offline tests, mock transport (media tests need ffmpeg)



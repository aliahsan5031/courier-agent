# Recon: Hermes Agent (public docs only)
Sources: hermes-agent.nousresearch.com/docs (features overview); besthub.dev "Deep dive into Hermes Agent memory";
chatbotkit.com reference architecture. No source code read, no private APIs, no accounts used.
Core loop: user task -> tool-use loop -> (hard task) save memory/skill -> next session loads them.
Flows: F01 chat+tools; F02 cross-session recall; F03 skill creation; F04 delegation; F05 provider fallback.
Skipped on purpose: Nous Portal, hosted services, trademark/branding. Size: M. Out of scope this pass: gateways, cron, voice, browser.

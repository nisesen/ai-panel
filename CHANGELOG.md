# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- A web panel for one GPU/LLM host: the dashboard (power, host, LLM, GPUs, guests, silences,
  cost now), Schedule, Cost, Activity and Settings pages, server-rendered with htmx, for phones
  and desktops, light and dark.
- Actions with live steps, one at a time, with a cooldown between power actions: power on
  (BMC), shutdown and reboot (through the host agent, with alert silences: one per matcher set
  of the site config, so unrelated groups of alerts are all covered), LLM switch,
  restart, stop and start (read back from `/props` and `/health`), guest start, shutdown and
  reboot, a read-only probe, pausing a client's checks of the LLM (user timers in a guest VM), and emergency BMC soft off, off and
  reset.
- Schedules: edge-triggered off and on rules, catch-up, holds and skips, waiting for busy
  slots and tasks (then skip, or force), protected windows.
- Running cost from Prometheus or static sources (fixed watts, a time-of-use tariff):
  now, today, 7 days and this month by period, savings, and the schedule's 30-day forecast.
- Two configuration files with one owner per setting: the site config (git, read-only,
  reloaded without a restart, the last good version kept) and the settings (the page, the JSON
  API, `aipanel settings get|set|export|import`), both with JSON Schemas.
- Sign-in through trusted headers from your proxy, with allowed groups; CSRF tokens, with a key
  kept in the state directory so open pages survive a restart; typed confirmation; an audit log; dry-run mode; DEMO mode.
- `llm-ctl`, the host agent: one Python file, fixed verbs, JSON answers, syslog.
- `/metrics` and `/healthz` on a separate port; a non-root image that runs with a read-only
  root file system.

[Unreleased]: https://github.com/nisesen/ai-panel/commits/main

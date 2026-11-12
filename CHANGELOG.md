# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- The release's smoke test runs one platform at a time (Docker refused the second one under the
  same index digest, so v0.1.1's release run failed after publishing; the image itself is fine).

## [0.1.1] - 2026-11-12

Fixes from a review of 0.1.0. **Check your configs:** `auth.trusted_proxies` is now required,
`schedule.catch_up_minutes` must be at least 1, and llm-ctl wants absolute command paths and a
root-owned config.

### Security

- `trusted_header` needs `auth.trusted_proxies` (the CIDRs are checked when the config loads);
  the server ignores `X-Forwarded-For`; docs show the NetworkPolicy that is the real control in
  Kubernetes.
- CSRF tokens are bound to a 12-hour window, and compared as bytes (a non-ASCII token is a 403,
  not a 500). Redirects after a form stay on the site.
- Request bodies over 64 KB are refused; YAML anchors and aliases are refused; at most 50 rules
  and 7 days per rule. The audit log cuts long values, rotates at 5 MB and is read from its
  tail.
- The CLI listens on 127.0.0.1 by default (the image keeps 0.0.0.0).
- llm-ctl: as root it reads only its own config, and only when root owns it and nobody else can
  write it; absolute command paths; `python3 -I`; children get a fixed environment, no stdin
  and at most 1 MB of output kept; untrusted JSON is parsed safely and cut short.

### Fixed

- No shutdown or reboot unless the BMC answered in the last two minutes: it is the only way
  back on. A BMC that stops answering stays shown as down, and the power as unknown, until it
  answers again (`aipanel_bmc_last_success_timestamp`).
- Busy fails closed: unreadable LLM slots count as busy at an off time, and the decode speed is
  read apart from them, so a Prometheus outage cannot hide a busy LLM.
- A shutdown or reboot that fails while the host is still up ends the silences it made; a
  timeout is checked against the host's real state. A schedule boundary is marked by how its
  action ended, not by its start; a BMC or SSH blip at a boundary is tried again.
- Silences follow the schedule: a skip or a hold that keeps the host off longer extends them;
  "until the next scheduled on" knows a disabled schedule and long holds, and is worked out by
  the panel; a silence time from the API must carry a time zone and end within 7 days.
- Ending silences, and the client checks, can no longer fail an action that did its work; an
  emergency action goes on without Alertmanager; `llm_stop` leaves the client checks paused.
- The last failed power action, and the failure metrics, survive a restart; a probe no longer
  closes the emergency actions after a stuck shutdown. A pending boundary can be skipped.
- One broken backend no longer stops the poller or the scheduler; failures are logged once; a
  cancelled command kills its child.
- State writes run on a background thread, so a slow NFS mount stalls nothing; the panel starts
  read-only when its state directory cannot be read; state files are 0640.
- Cost: a gap in Prometheus's data is neither "off" nor savings; history starts at
  `prometheus.retention_days`, and says so; "last week's price" is local time.
- Time zones come from the `tzdata` package, the same everywhere.
- llm-ctl: the power off still happens when the panel is gone; one changing verb at a time;
  time budgets below the panel's timeout (the panel's default is now 300 s); no shutdown or
  reboot in the first 10 minutes after a boot; a switch checks its result and reports a
  missing unit; `--timeout` for guest reboots; paused client checks resume themselves after
  `auto_resume_minutes`.

### Added

- `aipanel_poll_last_timestamp` and `aipanel_scheduler_last_tick_timestamp`.
- `docs/kubernetes.md`: Deployment, Services and NetworkPolicy examples.
- Releases publish only a commit on main whose CI passed, with an SBOM, build provenance and
  pinned build helpers; CI takes uv's version from the Dockerfile.

## [0.1.0] - 2026-11-12

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

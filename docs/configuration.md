# Configuration

ai-panel has two configuration files. **Each setting lives in exactly one of them**: there are
no overrides and no precedence rules to learn.

| | `config.yaml`, the site config | `settings.json`, the settings |
| --- | --- | --- |
| Holds | where the panel connects and what it may do | what the operator tunes |
| Owner | git (mounted read-only, e.g. from a ConfigMap) | the panel (its state directory) |
| Changed by | a commit, reviewed like any other | the settings page, the JSON API, the CLI |
| Checked by | `aipanel validate`, `schema/config.schema.json` | the same model, on every change |
| Bad version | not loaded: the last good one stays, the page says so, `aipanel_config_valid 0` | refused: nothing is saved |

The split is also the security boundary: everything that says *where* the panel connects or
*what* it may do is git-only. A stolen browser session can change a schedule (and the audit log
records it), but never a target.

Secrets are in neither file. The site config names an environment variable or a file for each
(`{env: BMC_USER}`, `{file: /secrets/bmc/password}`), and nothing shows their values.

## The site config (`config.yaml`)

[examples/config.yaml](../examples/config.yaml) has every section with comments, and
[schema/config.schema.json](../schema/config.schema.json) every key with its type and limits.
Editors that understand `# yaml-language-server: $schema=...` complete and check it as you type.

| Section | What it holds |
| --- | --- |
| `site` | the host's name (typed to confirm power actions), the time zone, header links, the emergency warning text |
| `auth` | `trusted_header` with the header names, the allowed groups and the proxy's addresses (`trusted_proxies`, required), or `none` (DEMO only); see [security.md](security.md) |
| `bmc` | the BMC's address, user and password references, cipher suite (`-C`), privilege (`-L`) |
| `host` | the host agent over SSH: address, user, key, known_hosts; the guests the panel may touch; which guest runs the LLM |
| `llm` | llama.cpp's URL; its API key reference, optional ([security.md](security.md#the-llms-api-key)); the models (`name` as in llm-ctl's config, `match`: text in `/props` that identifies it); `pause_client_checks`: pause a client's checks of the LLM (user timers in a guest VM) around planned LLM stops |
| `prometheus` | its URL; queries for the GPUs (one series per GPU) and the decode speed; `retention_days` (default 15): cost history starts no earlier, and the page says so |
| `alertmanager` | its URL, and `silences`: the matcher sets a planned stop silences, one silence per set (below) |
| `actions` | `enabled` (all when not given) and `dry_run` (read everything, only log changes) |
| `cost` | the power source and the price source, each `prometheus` (a query) or `static` (watts; a time-of-use tariff) |
| `protected_windows` | `{days, from, to, reason}`: no stop, scheduled or by hand, inside these |

The panel re-reads the file when it changes and every 30 seconds, without a restart, so a
running action is never cut short. Mount the file's directory, not the file with `subPath`:
Kubernetes updates a `subPath` mount only when the pod restarts.

### Time windows

Schedule rules, protected windows and tariff windows share one shape: days, a start and an
end, in the site's time zone. An end at or before the start crosses midnight:
`{days: [fri], from: "22:00", to: "06:00"}` runs from Friday 22:00 to Saturday 06:00.

Write times in quotes or not, as you like: the panel reads YAML with YAML 1.2's rules, so
`on`, `off`, `yes` and `no` stay text and `23:00` stays a time (YAML 1.1 would read them as
booleans and as the number 1380).

### Silences

A planned or scheduled stop silences alerts until the host should be back. Alertmanager ANDs the
matchers inside one silence, so alerts that share no label (the host's own alerts by
`instance`, its guests' by `node`, an alert about the LLM's API with neither) cannot share one
silence. `alertmanager.silences` is therefore a list of matcher sets, and the panel creates one
silence per set, all with the same end time and comment: the sets add up (OR), the matchers
inside a set must all match (AND).

```yaml
alertmanager:
  url: http://alertmanager.monitoring.svc:9093
  silences:
    - matchers: [{name: instance, value: "gpu-1.*", is_regex: true}]
    - matchers: [{name: alertname, value: GuestDown}, {name: node, value: gpu-1}]
    - matchers: [{name: alertname, value: LlmApiDown}]
```

When the host is back (a power on or a reboot), the panel expires every silence it made,
recognised by `created_by` (default `ai-panel`), and never touches anyone else's. A shutdown or
reboot that fails while the host is still up ends the silences it made at once. While the host
is off, a skip or a hold that moves the schedule's next on later extends the silences to it.
A silence time given through the API must carry a time zone and end within 7 days (or at the
schedule's next on); the dashboard's "until the next scheduled on" is worked out by the panel.

### A static tariff

```yaml
cost:
  power: {source: static, watts: 450}      # while on; on/off as the panel observed it
  price:
    source: static
    tariff:
      currency: EUR
      periods: {peak: 0.38, off-peak: 0.24, summer-peak: 0.45}
      default: off-peak
      windows:                             # the first window that matches wins
        - {days: [mon, tue, wed, thu, fri], from: "17:00", to: "21:00", period: summer-peak, months: [6, 7, 8]}
        - {days: [mon, tue, wed, thu, fri], from: "07:00", to: "23:00", period: peak}
```

With a Prometheus price source, a query that keeps the period label
(`max by (period) (electricity_price_per_kwh)`) gives the cost by period. Future prices are not
known to Prometheus: the forecast uses the same hour one week earlier, and the page says so.

## The settings (`settings.json`)

[schema/settings.schema.json](../schema/settings.schema.json) lists them all, with the
descriptions and limits the settings page shows. An empty or missing file is a working panel:
every setting has a default, and **the schedule is off by default**.

| Group | Examples |
| --- | --- |
| `schedule` | `enabled`, `rules` (`{name, days, off, on}`, at most 50), `catch_up_minutes` (at least 1), `busy_retry_minutes`, `busy_grace_minutes`, `force_after_grace`, `boot_grace_minutes` |
| `timeouts` | one llm-ctl command (default 300 s: keep it above llm-ctl's `limit_seconds`, [agent.md](agent.md)), a boot, the LLM becoming healthy, a shutdown, a guest |
| `cooldowns` | seconds between two power actions |
| `silences` | how long a manual shutdown, a reboot and an emergency action silence alerts |
| `cost` | `standby_watts` (measure it), `idle_watts` (empty: the 10th percentile of 7 days) |
| `poll`, `display` | how often the host and the BMC are read; theme, refresh, rows on the activity page |

Every change is validated by the same model, including against the site config (an off window
may not overlap a protected window), written atomically (a temporary file, then a rename), and
recorded in the audit log with who changed what, from and to. If a later change to the site
config makes the saved settings conflict with it, the pages say so, and the scheduler still
refuses any stop inside a protected window.

Holds and skips are runtime state, not settings: they live in the scheduler's own state file
beside `settings.json`, and are changed on the Schedule page or through the API.

### From the command line

The CLI works on the files directly, beside a running panel (which picks the change up within
seconds), with the same validation and audit:

```bash
aipanel settings get                              # everything, as key = value
aipanel settings get schedule
aipanel settings set schedule.enabled=true cost.standby_watts=12
aipanel settings set 'schedule.rules=[{name: nights, days: [mon, tue, wed, thu, fri], off: "23:00", on: "07:00"}]'
aipanel settings --actor my-agent set poll.interval_seconds=20   # who, for the audit log
aipanel settings export > settings.yaml           # a snapshot, e.g. for git
aipanel settings import settings.yaml             # replaces every setting; left out = default
```

Values are YAML. In Kubernetes: `kubectl exec deploy/ai-panel -- aipanel settings get`.

### Through the API

```bash
token=$(curl -s https://panel.example.com/api/session | jq -r .csrf)   # with your sign-in
curl -X PATCH https://panel.example.com/api/settings \
  -H "X-CSRF-Token: $token" -H "Content-Type: application/json" \
  -d '{"schedule.enabled": true}'
```

`PATCH` merges (nested objects or dotted keys), `PUT` replaces everything.

## How an agent edits each

- **The site config:** change `config.yaml` in its git repository and open a pull request.
  CI should run `aipanel validate config.yaml` (or validate it against the schema of the
  deployed version) so a bad key never merges. After the merge, the deployment updates the
  mounted file and the panel reloads it; a version that does not validate is not loaded, and
  `aipanel_config_valid` drops to 0 so it alerts.
- **The settings:** `aipanel settings set ...` through `kubectl exec` (or the API with a
  signed-in session). Use `--actor <name>` so the audit log says who it was.
  `aipanel settings export` gives a YAML snapshot to keep in git, and `import` restores it.

## The state directory

`AIPANEL_STATE_DIR` (default `/var/lib/aipanel`) holds:

- `settings.json`;
- `audit.jsonl`: one JSON line per event, also printed to stdout; over 5 MB it moves to
  `audit.jsonl.1`;
- `scheduler.json`: handled boundaries, holds, skips, the running boundary;
- `runner.json`: how each action last ended, for the failure metrics and the emergency actions
  after a restart;
- `power.jsonl`: power changes the panel saw, for cost history (off or a gap in the data);
- `csrf.key`: the key of the pages' CSRF tokens, made on the first start.

A few kilobytes; back it up like other apps' data. Files are `0640` (`csrf.key` `0600`), so a
backup job in the panel's group can read them. Writes happen on a background thread, so a slow
or stalled NFS mount never holds up the pages, the poller or an action. It is safe on NFS with
one panel. When it is missing, unreadable or not writable, the panel still runs, read-only, and
says so on every page.

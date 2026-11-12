# Contributing

Thank you for helping. Bug reports, documentation fixes and pull requests are all welcome. For
anything large, please open an issue first so we can agree on the approach.

## Setting up

You need [uv](https://docs.astral.sh/uv/). No GPU, BMC or Proxmox host is needed: DEMO mode
simulates all of them.

```bash
git clone https://github.com/nisesen/ai-panel.git
cd ai-panel
uv sync                                           # Python 3.14 and the dev tools
uv run aipanel --demo --state-dir /tmp/aipanel serve     # http://localhost:8080
```

Templates and the stylesheet are in `src/aipanel/web/`; there is no build step, reload the
page. In DEMO mode actions take as long as a small real machine would (a boot, about a
minute), so you can watch their steps.

## Checks

CI runs exactly these; run them before you push:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                                        # strict
uv run pytest                                      # the app's tests and llm-ctl's
uv run aipanel schema --out schema --check         # the schemas match the models
```

- The tests run every action against the DEMO fakes with a fake clock, so a boot that takes
  15 minutes takes milliseconds. `tests/conftest.py` has the fixtures.
- After changing `config/site.py` or `config/settings.py`, regenerate the schemas with
  `uv run aipanel schema --out schema` and commit them.
- `agent/llm-ctl` runs on the host's own Python: keep it to the standard library and to
  Python 3.11 (CI tests it on 3.11 and 3.13).

## Making a change

- Match the code around you: small modules with names from the domain, short functions,
  comments that say why. `ruff` formats and lints (line length 100).
- A change someone can see needs a test, and a line in `CHANGELOG.md` under "Unreleased".
- A pull request should say what changed, why, and how you checked it.
- Never commit an address, host name, key or other detail of a real installation: examples use
  the documentation ranges (192.0.2.0/24, example.com).

## Common contributions

### Another kind of BMC, host or metrics source

Each outside system is a `typing.Protocol` in `src/aipanel/backends/protocols.py` with one
implementation (`power.py`, `agent.py`, `llm.py`, `metrics.py`, `silences.py`, `energy.py`)
and a fake in `demo.py`. Another kind is one new class with the same methods, a `kind` in the
site config's model, and a line where `panel.py` builds the backends. Test it against a fake
command runner or `httpx.MockTransport`, as `tests/test_backends.py` does.

### Another action

An action is a class in `src/aipanel/actions/` with a name, a label, a confirmation level,
parameters (a pydantic model; values come from the site config's lists, never free text),
`check` (preconditions) and `run` (its steps, through `ctx.do` and `ctx.wait_for` so that
dry-run mode works). Register it with `@register`, add its name to `ActionName` in
`config/site.py`, and test its success, timeout and refusal in `tests/test_actions.py`.

## Reporting a security problem

Please do not open a public issue; see [SECURITY.md](SECURITY.md).

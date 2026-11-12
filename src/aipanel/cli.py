"""The command line: serve the panel, check a site config, write the schemas, and read or
change the settings beside a running panel (`kubectl exec deploy/ai-panel -- aipanel ...`).

Environment: AIPANEL_CONFIG (the site config), AIPANEL_STATE_DIR, AIPANEL_DEMO=1, AIPANEL_LISTEN,
AIPANEL_PORT, AIPANEL_METRICS_PORT.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import uvicorn

from aipanel import __version__, yamlio
from aipanel.audit import Audit
from aipanel.backends.demo import DEMO_RULE, demo_site_config
from aipanel.config import schema
from aipanel.config.loader import ConfigError, SiteConfigLoader, load_file
from aipanel.config.site import SiteConfig
from aipanel.config.store import SettingsError, SettingsStore, flatten

DEFAULT_STATE_DIR = "/var/lib/aipanel"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def site_loader(config: str | None, demo: bool) -> SiteConfigLoader:
    if config:
        return SiteConfigLoader(Path(config), demo)
    if demo:
        return SiteConfigLoader(None, demo, demo_site_config())
    raise ConfigError("no site config: set AIPANEL_CONFIG or --config (or AIPANEL_DEMO=1)")


def server_config(app: Any, host: str, port: int, **extra: Any) -> uvicorn.Config:
    # proxy_headers off: the client address is the TCP peer's, never X-Forwarded-For, so the
    # trusted_proxies check cannot be talked around.
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
        **extra,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from aipanel.app import create_app, create_ops_app
    from aipanel.panel import Panel

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    panel = Panel(site_loader(args.config, args.demo), Path(args.state_dir), demo=args.demo)
    if args.demo and panel.state_writable and not panel.store.path.exists():
        panel.store.update({"schedule": {"rules": [DEMO_RULE]}}, "demo")  # something to show
    app, ops = create_app(panel), create_ops_app(panel)

    class OpsServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield  # the main server handles the signals and then stops this one

    async def serve() -> None:
        main = uvicorn.Server(server_config(app, args.listen, args.port))
        side = OpsServer(server_config(ops, args.listen, args.metrics_port, lifespan="off"))
        side_task = asyncio.create_task(side.serve())
        try:
            await main.serve()
        finally:
            side.should_exit = True
            await side_task

    asyncio.run(serve())
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_file(Path(args.config), demo=args.demo)
    except ConfigError as e:
        print(f"{args.config}: not valid: {e}", file=sys.stderr)
        return 1
    print(f"{args.config}: valid (host {config.site.name})")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    directory = Path(args.out)
    if not args.check:
        for path in schema.write(directory):
            print(path)
        return 0
    stale = [
        name
        for name, content in schema.schemas().items()
        if not (directory / name).exists()
        or (directory / name).read_text(encoding="utf-8") != schema.render(content)
    ]
    for name in stale:
        print(f"{directory / name} is not current: run `aipanel schema --out {directory}`")
    return 1 if stale else 0


def settings_store(args: argparse.Namespace) -> SettingsStore:
    site: SiteConfig | None = site_loader(args.config, args.demo).current
    state = Path(args.state_dir)
    audit = Audit(state / "audit.jsonl" if os.access(state, os.W_OK) else None)
    return SettingsStore(state / "settings.json", audit, lambda: site)


def cmd_settings(args: argparse.Namespace) -> int:
    store = settings_store(args)
    actor = f"cli:{args.actor}"
    try:
        match args.verb:
            case "get":
                values = flatten(store.get().model_dump(mode="json"))
                if args.key:
                    chosen = {
                        k: v
                        for k, v in values.items()
                        if k == args.key or k.startswith(args.key + ".")
                    }
                    if not chosen:
                        print(f"no setting {args.key}", file=sys.stderr)
                        return 1
                    values = chosen
                for key, value in values.items():
                    print(f"{key} = {json.dumps(value)}")
            case "set":
                changes: dict[str, Any] = {}
                for pair in args.pairs:
                    key, sep, raw = pair.partition("=")
                    if not sep:
                        print(f"expected key=value, got {pair!r}", file=sys.stderr)
                        return 2
                    changes[key.strip()] = yamlio.load(raw) if raw.strip() else None
                store.update(changes, actor)
                print("saved")
            case "export":
                text = store.export_yaml()
                if args.file and args.file != "-":
                    Path(args.file).write_text(text, encoding="utf-8")
                else:
                    sys.stdout.write(text)
            case "import":
                source = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
                data = yamlio.load(source)
                if not isinstance(data, dict):
                    raise SettingsError("the file must hold a mapping")
                store.replace(data, actor)
                print("imported")
    except (SettingsError, ValueError, OSError) as e:
        print(f"not saved: {e}", file=sys.stderr)
        return 1
    if store.error:
        print(f"warning: {store.error}", file=sys.stderr)
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    url = f"http://127.0.0.1:{args.metrics_port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="aipanel", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", default=env("AIPANEL_CONFIG"), help="the site config (YAML)")
    p.add_argument("--state-dir", default=env("AIPANEL_STATE_DIR", DEFAULT_STATE_DIR))
    p.add_argument("--demo", action="store_true", default=env_flag("AIPANEL_DEMO"))
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the panel")
    serve.add_argument(
        "--listen", default=env("AIPANEL_LISTEN", "127.0.0.1"), help="the image sets 0.0.0.0"
    )
    serve.add_argument("--port", type=int, default=int(env("AIPANEL_PORT", "8080")))
    serve.add_argument("--metrics-port", type=int, default=int(env("AIPANEL_METRICS_PORT", "9090")))
    serve.set_defaults(func=cmd_serve)

    validate = sub.add_parser("validate", help="check a site config file")
    validate.add_argument("config")
    validate.set_defaults(func=cmd_validate)

    sch = sub.add_parser("schema", help="write the JSON Schemas of both models")
    sch.add_argument("--out", default="schema")
    sch.add_argument("--check", action="store_true", help="fail when the files are not current")
    sch.set_defaults(func=cmd_schema)

    st = sub.add_parser("settings", help="read or change the settings (settings.json)")
    st.add_argument("--actor", default=env("USER", "unknown"), help="who, for the audit log")
    verbs = st.add_subparsers(dest="verb", required=True)
    get = verbs.add_parser("get", help="print settings as key = value")
    get.add_argument("key", nargs="?")
    put = verbs.add_parser("set", help="change settings: key=value ... (values are YAML)")
    put.add_argument("pairs", nargs="+")
    exp = verbs.add_parser("export", help="write the settings as YAML")
    exp.add_argument("file", nargs="?", default="-")
    imp = verbs.add_parser("import", help="replace the settings from a YAML file (- for stdin)")
    imp.add_argument("file")
    st.set_defaults(func=cmd_settings)

    health = sub.add_parser("healthcheck", help="exit 0 when the running panel answers")
    health.add_argument(
        "--metrics-port", type=int, default=int(env("AIPANEL_METRICS_PORT", "9090"))
    )
    health.set_defaults(func=cmd_healthcheck)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code: int = args.func(args)
    except ConfigError as e:
        print(f"aipanel: {e}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())

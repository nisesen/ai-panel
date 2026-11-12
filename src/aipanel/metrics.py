"""The panel's own Prometheus metrics, served on a separate port with /healthz."""

from collections.abc import Mapping

HELP = {
    "aipanel_build_info": ("gauge", "The running version"),
    "aipanel_config_valid": ("gauge", "1 when the last read of the site config was valid"),
    "aipanel_settings_valid": ("gauge", "1 when settings.json is valid and fits the site config"),
    "aipanel_state_writable": ("gauge", "1 when the state directory can be written"),
    "aipanel_host_power_on": ("gauge", "1 on, 0 off, -1 unknown, as the BMC reports it"),
    "aipanel_host_reachable": ("gauge", "1 when the host agent answers over SSH"),
    "aipanel_action_running": ("gauge", "1 while an action runs"),
    "aipanel_action_last_success": ("gauge", "1 when the action's last run succeeded"),
    "aipanel_action_last_timestamp": ("gauge", "When the action last ended (unix time)"),
    "aipanel_schedule_enabled": ("gauge", "1 when the schedule runs"),
    "aipanel_schedule_last_boundary_timestamp": (
        "gauge",
        "When the scheduler last handled a boundary",
    ),
    "aipanel_schedule_last_boundary_success": (
        "gauge",
        "0 when the last boundary was skipped, refused or failed to start",
    ),
    "aipanel_backend_up": ("gauge", "1 when the backend answered the last poll"),
    "aipanel_bmc_last_success_timestamp": ("gauge", "When the BMC last answered (unix time)"),
    "aipanel_poll_last_timestamp": ("gauge", "When the poller last finished (unix time)"),
    "aipanel_scheduler_last_tick_timestamp": ("gauge", "When the scheduler last ran (unix time)"),
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())) + "}"


class PanelMetrics:
    def __init__(self) -> None:
        self._values: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        if name not in HELP:
            raise KeyError(f"undeclared metric {name}")
        key = tuple(sorted((labels or {}).items()))
        self._values.setdefault(name, {})[key] = float(value)

    def get(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        return self._values.get(name, {}).get(tuple(sorted((labels or {}).items())))

    def render(self) -> str:
        lines = []
        for name, series in self._values.items():
            kind, text = HELP[name]
            lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
            for key, value in series.items():
                lines.append(f"{name}{_labels(dict(key))} {value!r}")
        return "\n".join(lines) + "\n"

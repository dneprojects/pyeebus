"""Files for a probe run (``pyeebus connect --trace-file ... --report ...``).

* trace: every SPINE message, one JSON object per line with time and direction.
* report: a short text summary (SHIP states, device, entities and features,
  announced use cases, the last data per feature and function), rewritten
  whenever something changes, so it is complete whenever the run is stopped.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _now() -> str:
    return dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class _Data:
    value: Any
    classifier: str
    count: int = 0
    first: str = field(default_factory=_now)
    last: str = field(default_factory=_now)


class ProbeFiles:
    def __init__(self, trace: Path | None, report: Path | None, own_ski: str, remote_ski: str,
                 settings: str = "") -> None:
        self.trace_path, self.report_path = trace, report
        self.own_ski, self.remote_ski, self.settings = own_ski, remote_ski, settings
        self.started = _now()
        self.ship: list[str] = []
        self.device = ""
        self.use_cases = ""
        self.data: dict[tuple[str, str], _Data] = {}
        self.messages = {"in": 0, "out": 0}
        for path in (trace, report):
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
        self.write_report()

    def trace(self, direction: str, payload: dict[str, Any]) -> None:
        self.messages[direction] = self.messages.get(direction, 0) + 1
        if self.trace_path is None:
            return
        line = json.dumps({"time": _now(), "dir": direction, "msg": payload}, separators=(",", ":"))
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def ship_state(self, text: str) -> None:
        self.ship.append(f"{_now()} {text}")
        self.write_report()

    def set_device(self, text: str) -> None:
        self.device = text
        self.write_report()

    def set_use_cases(self, text: str) -> None:
        self.use_cases = text
        self.write_report()

    def data_update(self, feature: str, function: str, classifier: str, value: Any) -> None:
        key = (feature, function)
        item = self.data.get(key)
        if item is None:
            item = self.data[key] = _Data(value, classifier)
        item.value, item.classifier, item.last = value, classifier, _now()
        item.count += 1
        self.write_report()

    def write_report(self) -> None:
        if self.report_path is None:
            return
        lines = ["# pyeebus probe report", "",
                 f"started  {self.started}", f"updated  {_now()}",
                 f"our SKI  {self.own_ski}", f"device   {self.remote_ski}"]
        if self.settings:
            lines.append(f"settings {self.settings}")
        lines += [f"SPINE messages in {self.messages.get('in', 0)}, out {self.messages.get('out', 0)}",
                  "", "## SHIP", "", *(self.ship or ["(no connection yet)"]),
                  "", "## SPINE device", "", self.device or "(no detailed discovery yet)",
                  "", "## Use cases", "", self.use_cases or "(none received yet)",
                  "", "## Data (last value; count of replies/notifies; first and last time)", ""]
        for (feature, function), item in sorted(self.data.items()):
            lines.append(f"{feature} {function} ({item.classifier}, {item.count}x, "
                         f"{item.first} .. {item.last})")
            lines.append("    " + json.dumps(item.value, separators=(",", ":")))
        tmp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.report_path)

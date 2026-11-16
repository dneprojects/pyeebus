"""Probe files and the read-only CLI options."""

from __future__ import annotations

import json

import pytest

from pyeebus.cli import main
from pyeebus.probe import ProbeFiles


def test_probe_files(tmp_path):
    files = ProbeFiles(tmp_path / "spine.jsonl", tmp_path / "report.txt", "a" * 40, "b" * 40, "read-only True")
    assert "(no connection yet)" in (tmp_path / "report.txt").read_text()
    files.trace("in", {"datagram": {"header": {"cmdClassifier": "notify"}}})
    files.ship_state("[bbbbbbbb] complete")
    files.set_use_cases("Use cases announced by the device:\n  MonitoredUnit [3] ...")
    files.data_update("[3]:1 Measurement", "measurementListData", "notify", {"measurementData": []})
    files.data_update("[3]:1 Measurement", "measurementListData", "notify", {"measurementData": [{"x": 1}]})
    report = (tmp_path / "report.txt").read_text()
    assert "complete" in report and "MonitoredUnit [3]" in report
    assert "[3]:1 Measurement measurementListData (notify, 2x" in report
    assert '{"measurementData":[{"x":1}]}' in report
    line = json.loads((tmp_path / "spine.jsonl").read_text().splitlines()[0])
    assert line["dir"] == "in" and line["msg"]["datagram"]["header"]["cmdClassifier"] == "notify"


def test_read_only_refuses_writes():
    with pytest.raises(SystemExit):
        main(["connect", "c" * 40, "--read-only", "--lpc-limit", "4200"])

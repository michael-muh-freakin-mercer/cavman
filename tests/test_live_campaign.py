"""The live campaign harness, dry-run with the scripted executor (no credits)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("agents")

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap unavailable")
def test_campaign_dry_run_writes_a_report(tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("Build a booking core\nBuild a booking core #fail-validation\n")
    completed = subprocess.run(
        [sys.executable, str(REPO / "scripts/live_campaign.py"), "--executor", "scripted",
         "--prompts", str(prompts), "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")],
        capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stderr[-2000:]
    [report] = list((tmp_path / "out").glob("*.json"))
    summary = json.loads(report.read_text())
    assert summary["requests"] == 2 and summary["completed"] == 2
    assert summary["runs"][1]["failures"] == ["BAD_OUTPUT"]
    assert summary["runs"][0]["sendbacks"] == []
    [sent_back, *_] = summary["runs"][1]["sendbacks"]
    assert sent_back["by"] == "check" and sent_back["reason"].endswith("failed")
    assert summary["runs"][1]["attempts"] >= 2
    markdown = (tmp_path / "out" / report.name.replace(".json", ".md")).read_text()
    assert "| Request | State |" in markdown
    assert "## Why work was sent back" in markdown and "candidate 1, check:" in markdown


def test_campaign_refuses_to_start_without_provider_config(tmp_path, monkeypatch):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    completed = subprocess.run([sys.executable, str(REPO / "scripts/live_campaign.py"),
                                "--out", str(tmp_path / "out")],
                               capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    assert completed.returncode == 2 and "OPENROUTER_API_KEY" in completed.stderr
    assert not (tmp_path / "out").exists()


def _load_campaign():
    import importlib.util
    spec = importlib.util.spec_from_file_location("live_campaign", REPO / "scripts/live_campaign.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap unavailable")
def test_campaign_runs_every_build_on_one_event_loop(tmp_path, monkeypatch):
    """The provider client is cached with live connections, which die with their loop."""
    import asyncio

    from cavman.worker import Worker

    loops, run_once = set(), Worker.run_once

    async def recording(self):
        loops.add(asyncio.get_running_loop())
        return await run_once(self)

    monkeypatch.setattr(Worker, "run_once", recording)
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("Build a booking core\nBuild a booking core #node\n")
    assert _load_campaign().main(["--executor", "scripted", "--prompts", str(prompts),
                                  "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")]) == 0
    summary = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    assert summary["completed"] == 2
    assert len(loops) == 1 and all(loop.is_closed() for loop in loops)


def test_gate_compares_the_unrounded_completion_rate():
    campaign = _load_campaign()
    args = campaign.parse_args(["--executor", "scripted", "--min-completion", "1.0"])
    # 1999/2000 rounds to 1.0 at three places but is not every request.
    assert campaign.gate_failures(args, 1999, 2000, cost_complete=True)
    assert campaign.gate_failures(args, 2000, 2000, cost_complete=True) == []
    assert campaign.gate_failures(campaign.parse_args([]), 0, 2, cost_complete=True) == []


def test_gate_fails_when_the_provider_did_not_report_every_cost():
    campaign = _load_campaign()
    gated = campaign.parse_args(["--min-completion", "1.0"])
    [failure] = campaign.gate_failures(gated, 1, 1, cost_complete=False)
    assert "no cost" in failure
    allowed = campaign.parse_args(["--min-completion", "1.0", "--allow-unknown-cost"])
    assert campaign.gate_failures(allowed, 1, 1, cost_complete=False) == []
    # The scripted executor reports no cost by design; its dry runs still pass.
    scripted = campaign.parse_args(["--executor", "scripted", "--min-completion", "1.0"])
    assert campaign.gate_failures(scripted, 1, 1, cost_complete=False) == []


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap unavailable")
def test_campaign_min_completion_exits_nonzero(tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("Build a booking core\nBuild a booking core #approval\n")
    completed = subprocess.run(
        [sys.executable, str(REPO / "scripts/live_campaign.py"), "--executor", "scripted",
         "--prompts", str(prompts), "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data"),
         "--min-completion", "1.0"],
        capture_output=True, text=True, timeout=300)
    assert completed.returncode == 1
    assert "Completion 1/2 is below the required 100%" in completed.stderr


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap unavailable")
def test_report_is_written_after_every_build(tmp_path, monkeypatch):
    """A job timeout must not lose what earlier builds learned and spent."""
    campaign = _load_campaign()
    calls, write = [], campaign.write_report

    def recording(*args, finished, **kwargs):
        calls.append((len(args[4]), finished))
        return write(*args, finished=finished, **kwargs)

    monkeypatch.setattr(campaign, "write_report", recording)
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("Build a booking core\nBuild a booking core #node\n")
    assert campaign.main(["--executor", "scripted", "--prompts", str(prompts),
                          "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")]) == 0
    assert calls == [(1, False), (2, False), (2, True)]
    summary = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    assert summary["finished"] is True and summary["planned_requests"] == 2


def test_no_build_starts_after_the_deadline(tmp_path):
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("Build a booking core\n")
    assert _load_campaign().main(["--executor", "scripted", "--prompts", str(prompts), "--deadline-minutes", "1e-9",
                                  "--out", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")]) == 0
    [row] = json.loads(next((tmp_path / "out").glob("*.json")).read_text())["runs"]
    assert row["state"] == "skipped (campaign time limit reached)"


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap unavailable")
def test_load_test_script_runs_a_small_load(tmp_path):
    out = tmp_path / "load.json"
    completed = subprocess.run(
        [sys.executable, str(REPO / "scripts/load_test.py"), "--users", "2", "--builds-per-user", "1",
         "--streams-per-run", "1", "--workers", "1", "--worker-concurrency", "2", "--step-delay", "0",
         "--out", str(out)], capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    report = json.loads(out.read_text())
    assert report["completed"] == 2 and report["streams"]["errors"] == 0

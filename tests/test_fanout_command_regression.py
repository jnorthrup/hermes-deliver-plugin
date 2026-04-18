import importlib
import importlib.util
import sys
from pathlib import Path


def _load_plugin_package():
    pkg_dir = Path(__file__).resolve().parent.parent
    name = "hermes_deliver_plugin_testpkg"
    for key in list(sys.modules):
        if key == name or key.startswith(f"{name}."):
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        name,
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    assert spec.loader is not None
    spec.loader.exec_module(pkg)
    return name, pkg


def test_handle_fanout_new_task_creates_plan_without_transition_error(monkeypatch, tmp_path):
    pkg_name, _pkg = _load_plugin_package()
    fanout = importlib.import_module(f"{pkg_name}.fanout")

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    if hasattr(fanout, "_decompose"):
        monkeypatch.setattr(
            fanout,
            "_decompose",
            lambda task, critique="": {
                "task": task,
                "stories": [
                    {
                        "id": "001",
                        "name": "Validate across engines",
                        "description": "Run deterministic, property-based, and perf suites.",
                        "dependencies": [],
                        "acceptance": [
                            "consistent numeric results across engines",
                            "performance regression thresholds met",
                            "CI gates for precision and vectorization coverage",
                        ],
                    }
                ],
                "completed": [],
            },
        )

    result = fanout.handle_fanout(
        "Run cross-engine validation with deterministic, property-based, and perf suites"
    )

    assert "Invalid transition" not in result
    assert "Created 1 jobs" in result
    assert (tmp_path / ".fanout" / "plan.yaml").exists()
    plan = fanout._load_plan()
    assert plan is not None
    assert len(plan["jobs"]) == 1
    assert plan["jobs"][0]["status"] == "pending"


def test_handle_fanout_accept_stops_on_open_job(monkeypatch, tmp_path):
    pkg_name, _pkg = _load_plugin_package()
    fanout = importlib.import_module(f"{pkg_name}.fanout")

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    monkeypatch.setattr(
        fanout,
        "_decompose",
        lambda task, critique="": {
            "task": task,
            "jobs": [
                {
                    "id": "001",
                    "name": "First slice",
                    "description": "Implement the first change",
                    "dependencies": [],
                    "acceptance": ["first acceptance"],
                },
                {
                    "id": "002",
                    "name": "Second slice",
                    "description": "Implement the second change",
                    "dependencies": ["001"],
                    "acceptance": ["second acceptance"],
                },
            ],
            "critique_history": [],
        },
    )

    calls = []

    def _fake_run_deliver(task, max_rounds=5, job=None, structured=False):
        calls.append(job["id"])
        result = {
            "transcript": "Critic review:\n    Verdict: EDIT",
            "verdict": "EDIT",
            "summary": "first slice is still open",
            "score": None,
            "validated": [],
            "rejected": [],
            "validated_acceptance": [],
            "missing_acceptance": ["first acceptance"],
            "demands": ["critic did not accept the slice"],
        }
        return result if structured else result["transcript"]

    monkeypatch.setattr(fanout, "run_deliver", _fake_run_deliver)

    fanout.handle_fanout("Build something with two slices")
    result = fanout.handle_fanout("accept")

    assert "Fanout stopped here" in result
    assert calls == ["001"]

    plan = fanout._load_plan()
    assert plan is not None
    assert plan["jobs"][0]["status"] == "needs_attention"
    assert plan["jobs"][0]["last_verdict"] == "EDIT"
    assert plan["jobs"][1]["status"] == "pending"

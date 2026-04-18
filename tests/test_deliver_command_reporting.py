import importlib
import importlib.util
import json
import sys
from pathlib import Path


def _load_plugin_package():
    pkg_dir = Path(__file__).resolve().parent.parent
    name = "hermes_deliver_plugin_testpkg_report"
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


class _FakeCtx:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, tool_name, args):
        self.calls.append((tool_name, args))
        if "Output ONLY valid JSON" in args["goal"]:
            summary = json.dumps(
                {
                    "verdict": "COMPLETE",
                    "summary": "validated edits",
                    "critique": "looks good",
                    "score": 10,
                    "validated": [
                        {"path": "src/http.py", "lines": "12-58", "summary": "refactor accepted"}
                    ],
                }
            )
        else:
            summary = json.dumps(
                {
                    "status": "done",
                    "summary": "updated pooling logic",
                    "locations": [
                        {
                            "path": "src/http.py",
                            "lines": "12-58",
                            "summary": "adjusted connection lifecycle",
                            "snippet": "def acquire():\n    return pool.get()",
                        },
                        {
                            "path": "tests/test_http.py",
                            "lines": "1-42",
                            "summary": "added regression coverage",
                            "snippet": "assert client.reuses_socket()",
                        },
                    ],
                    "tests": ["pytest tests/test_http.py -q"],
                    "code": ["def acquire():\n    return pool.get()"],
                }
            )
        return json.dumps({"results": [{"summary": summary}]})


def test_run_deliver_reports_locations_and_verdict(monkeypatch):
    pkg_name, _pkg = _load_plugin_package()
    deliver = importlib.import_module(f"{pkg_name}.deliver")
    fake_ctx = _FakeCtx()
    monkeypatch.setattr(deliver, "get_ctx", lambda: fake_ctx)

    result = deliver.run_deliver("Implement feature X")

    assert "src/http.py" in result
    assert "tests/test_http.py" in result
    assert "Validated edits" in result
    assert "COMPLETE" in result
    assert len(fake_ctx.calls) == 2


class _SoftCompleteCtx:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, tool_name, args):
        self.calls.append((tool_name, args))
        if "rubber-stamp effort" in args["goal"]:
            summary = json.dumps(
                {
                    "verdict": "COMPLETE",
                    "summary": "looks done",
                    "score": 10,
                    "validated_acceptance": ["criterion 1"],
                }
            )
        else:
            summary = json.dumps(
                {
                    "status": "done",
                    "summary": "implemented something",
                    "completed_acceptance": ["criterion 1"],
                }
            )
        return json.dumps({"results": [{"summary": summary}]})


def test_run_deliver_downgrades_complete_without_artifacts(monkeypatch):
    pkg_name, _pkg = _load_plugin_package()
    deliver = importlib.import_module(f"{pkg_name}.deliver")
    fake_ctx = _SoftCompleteCtx()
    monkeypatch.setattr(deliver, "get_ctx", lambda: fake_ctx)

    result = deliver.run_deliver(
        "Implement feature X",
        max_rounds=1,
        job={"id": "001", "name": "Artifactful job", "acceptance": ["criterion 1"]},
        structured=True,
    )

    assert result["verdict"] == "EDIT"
    assert "critic did not cite any validated file or line artifacts" in result["demands"]
    assert "✅ COMPLETE" not in result["transcript"]
    assert len(fake_ctx.calls) == 2

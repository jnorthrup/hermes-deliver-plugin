"""Actor-critic delivery loop for /deliver command.

Spawns worker + critic subagents via dispatch_tool("delegate_task"),
loops up to max_rounds until the critic returns verdict COMPLETE.

This version also surfaces the exact files, line ranges, and code snippets
reported by the worker and critic so the CLI user can follow the edits.
"""

import json
import re
from typing import Any

from . import get_ctx
from .plugin_output import _emit

MAX_ROUNDS = 5


def _extract_json_payload(text: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if default is None:
        default = {
            "verdict": "EDIT",
            "summary": text[:500],
            "feedback": text[:500],
            "demands": ["unclear verdict from critic"],
        }
    if not text or not text.strip():
        return dict(default)

    fenced = re.sub(r"^```(?:json)?\s*", "", text.strip())
    fenced = re.sub(r"\s*```\s*$", "", fenced)

    match = re.search(r"\{.*\}", fenced, re.DOTALL)
    candidate = match.group() if match else fenced
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _coerce_location(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        path = item.strip()
        return {"path": path} if path else None
    if not isinstance(item, dict):
        return None

    path = str(item.get("path") or item.get("file") or item.get("location") or item.get("name") or "").strip()
    lines = str(item.get("lines") or item.get("line_range") or item.get("range") or item.get("span") or item.get("line") or "").strip()
    summary = str(item.get("summary") or item.get("note") or item.get("status") or item.get("result") or item.get("reason") or "").strip()
    snippet = str(item.get("snippet") or item.get("code") or item.get("patch") or item.get("diff") or "").strip()
    if not any((path, lines, summary, snippet)):
        return None
    payload: dict[str, str] = {}
    if path:
        payload["path"] = path
    if lines:
        payload["lines"] = lines
    if summary:
        payload["summary"] = summary
    if snippet:
        payload["snippet"] = snippet
    return payload


def _coerce_location_list(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]

    locations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        location = _coerce_location(item)
        if location is None:
            continue
        key = (
            location.get("path", ""),
            location.get("lines", ""),
            location.get("summary", ""),
            location.get("snippet", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        locations.append(location)
    return locations


def _coerce_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        text = str(value).strip()
        return [text] if text else []

    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(
                item.get("command")
                or item.get("summary")
                or item.get("status")
                or item.get("note")
                or item.get("result")
                or item.get("text")
                or ""
            ).strip()
            if not text:
                try:
                    text = json.dumps(item, sort_keys=True)
                except Exception:
                    text = str(item)
        else:
            text = str(item).strip()
        if text:
            items.append(text)
    return items


def _coerce_snippet_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        text = str(value).strip()
        return [text] if text else []

    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(
                item.get("snippet")
                or item.get("code")
                or item.get("patch")
                or item.get("diff")
                or item.get("text")
                or ""
            ).strip()
            if not text:
                try:
                    text = json.dumps(item, sort_keys=True)
                except Exception:
                    text = str(item)
        else:
            text = str(item).strip()
        if text:
            items.append(text)
    return items


def _normalize_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    return {
        "id": str(job.get("id") or "").strip(),
        "name": str(job.get("name") or "").strip(),
        "description": str(job.get("description") or "").strip(),
        "acceptance": _coerce_text_list(job.get("acceptance")),
        "dependencies": _coerce_text_list(job.get("dependencies")),
    }


def _job_contract_text(task: str, job: dict[str, Any] | None) -> str:
    normalized = _normalize_job(job)
    if normalized is None:
        return f"TASK:\n{task}"

    lines = [
        f"PARENT TASK:\n{task}",
        f"JOB ID: {normalized.get('id') or '?'}",
        f"JOB NAME: {normalized.get('name') or 'unnamed'}",
        "JOB DESCRIPTION:",
        normalized.get("description") or normalized.get("name") or "",
    ]
    dependencies = normalized.get("dependencies") or []
    if dependencies:
        lines.extend(["", "DEPENDENCIES:", *[f"- {item}" for item in dependencies]])
    acceptance = normalized.get("acceptance") or []
    if acceptance:
        lines.extend(["", "ACCEPTANCE CRITERIA:", *[f"- {item}" for item in acceptance]])
    return "\n".join(lines)


def _missing_acceptance(expected: list[str], actual: list[str]) -> list[str]:
    actual_map = {item.casefold(): item for item in actual}
    return [criterion for criterion in expected if criterion.casefold() not in actual_map]


def _indent_block(text: str, prefix: str = "    ", max_lines: int = 12) -> list[str]:
    lines = text.splitlines() or [text]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["..."]
    return [f"{prefix}{line}" for line in lines]


def _format_location_block(title: str, locations: list[dict[str, str]]) -> list[str]:
    if not locations:
        return []
    lines = [f"  {title}:"]
    for location in locations:
        path = location.get("path", "?") or "?"
        span = location.get("lines", "")
        summary = location.get("summary", "")
        header = path
        if span:
            header += f" [{span}]"
        if summary:
            header += f" — {summary}"
        lines.append(f"    - {header}")
        snippet = location.get("snippet", "")
        if snippet:
            lines.extend(_indent_block(snippet, prefix="      "))
    return lines


def _append_location_block(lines: list[str], title: str, locations: list[dict[str, str]]) -> None:
    lines.extend(_format_location_block(title, locations))


def _format_progress_report(report: dict[str, Any], title: str) -> list[str]:
    lines = [f"{title}:"]
    status = str(report.get("status") or "").strip()
    summary = str(report.get("summary") or report.get("message") or "").strip()
    completed_acceptance = _coerce_text_list(
        report.get("completed_acceptance") or report.get("validated_acceptance")
    )
    remaining_acceptance = _coerce_text_list(report.get("remaining_acceptance") or report.get("missing_acceptance"))
    locations = _coerce_location_list(
        report.get("locations") or report.get("files") or report.get("changed") or report.get("changes") or report.get("edits")
    )
    tests = _coerce_text_list(report.get("tests") or report.get("checks") or report.get("verification"))
    code = _coerce_snippet_list(report.get("code") or report.get("patch") or report.get("diff"))
    if status:
        lines.append(f"  Status: {status}")
    if summary:
        lines.append(f"  Summary: {summary}")
    if completed_acceptance:
        lines.append("  Completed acceptance:")
        for item in completed_acceptance:
            lines.append(f"    - {item}")
    if remaining_acceptance:
        lines.append("  Remaining acceptance:")
        for item in remaining_acceptance:
            lines.append(f"    - {item}")
    if locations:
        lines.extend(_format_location_block("Locations", locations))
    if tests:
        lines.append("  Tests:")
        for test in tests:
            lines.append(f"    - {test}")
    if code:
        lines.append("  Code:")
        for index, snippet in enumerate(code, start=1):
            if index > 1:
                lines.append("    ---")
            lines.extend(_indent_block(snippet, prefix="    "))
    return lines


def _parse_progress_report(text: str) -> dict[str, Any] | None:
    payload = _extract_json_payload(text, default={})
    if not payload:
        return None
    report = {
        "status": str(payload.get("status") or "").strip(),
        "summary": str(payload.get("summary") or payload.get("message") or "").strip(),
        "completed_acceptance": _coerce_text_list(
            payload.get("completed_acceptance") or payload.get("validated_acceptance")
        ),
        "remaining_acceptance": _coerce_text_list(
            payload.get("remaining_acceptance") or payload.get("missing_acceptance")
        ),
        "locations": _coerce_location_list(
            payload.get("locations") or payload.get("files") or payload.get("changed") or payload.get("changes") or payload.get("edits")
        ),
        "tests": _coerce_text_list(payload.get("tests") or payload.get("checks") or payload.get("verification")),
        "code": _coerce_snippet_list(payload.get("code") or payload.get("patch") or payload.get("diff")),
        "raw": text,
    }
    if not any((
        report["status"],
        report["summary"],
        report["completed_acceptance"],
        report["remaining_acceptance"],
        report["locations"],
        report["tests"],
        report["code"],
    )):
        return None
    return report


def _parse_verdict(critic_output: str) -> dict[str, Any]:
    """Extract verdict JSON from critic response."""
    payload = _extract_json_payload(critic_output)
    verdict_text = str(payload.get("verdict", "EDIT")).upper()
    if verdict_text not in {"COMPLETE", "EDIT", "RESTART"}:
        verdict_text = "EDIT"

    score_raw = payload.get("score")
    score = score_raw if isinstance(score_raw, int) else None
    demands = _coerce_text_list(payload.get("demands"))
    summary = str(payload.get("summary") or payload.get("message") or payload.get("status") or "").strip()
    validated_acceptance = _coerce_text_list(payload.get("validated_acceptance") or payload.get("completed_acceptance"))
    missing_acceptance = _coerce_text_list(payload.get("missing_acceptance") or payload.get("remaining_acceptance"))
    validated = _coerce_location_list(
        payload.get("validated")
        or payload.get("validated_locations")
        or payload.get("accepted")
        or payload.get("accepted_locations")
        or (payload.get("locations") if verdict_text == "COMPLETE" else None)
    )
    rejected = _coerce_location_list(
        payload.get("rejected")
        or payload.get("rejected_locations")
        or payload.get("blocked")
        or payload.get("blocked_locations")
    )
    return {
        "verdict": verdict_text,
        "summary": summary,
        "feedback": str(payload.get("feedback", "")),
        "demands": demands,
        "critique": str(payload.get("critique", "")),
        "reason": str(payload.get("reason", "")),
        "guidance": str(payload.get("guidance", "")),
        "score": score,
        "validated_acceptance": validated_acceptance,
        "missing_acceptance": missing_acceptance,
        "validated": validated,
        "rejected": rejected,
        "raw": critic_output,
    }


def _enforce_success_guardrails(
    verdict: dict[str, Any],
    worker_report: dict[str, Any] | None,
    job: dict[str, Any] | None,
) -> dict[str, Any]:
    if verdict.get("verdict") != "COMPLETE":
        return verdict

    problems: list[str] = []
    expected_acceptance = (_normalize_job(job) or {}).get("acceptance", [])
    missing_acceptance = _missing_acceptance(expected_acceptance, verdict.get("validated_acceptance", []))
    if missing_acceptance:
        problems.append(
            "critic did not validate every acceptance criterion: "
            + "; ".join(missing_acceptance)
        )

    if not verdict.get("validated"):
        problems.append("critic did not cite any validated file or line artifacts")

    if worker_report is None:
        problems.append("worker did not return a structured artifact report")
    else:
        has_artifacts = bool(worker_report.get("locations") or worker_report.get("tests") or worker_report.get("code"))
        if not has_artifacts:
            problems.append("worker report contains no concrete deliverable artifacts")

    if not problems:
        return verdict

    updated = dict(verdict)
    updated["verdict"] = "EDIT"
    updated["feedback"] = updated.get("feedback") or "Critic claimed COMPLETE without enough concrete evidence."
    updated["demands"] = _coerce_text_list(updated.get("demands")) + problems
    updated["missing_acceptance"] = missing_acceptance or updated.get("missing_acceptance", [])
    return updated


def _dispatch(goal: str, context: str, toolsets: list, max_iterations: int = 50) -> str:
    """Call delegate_task through the plugin's dispatch_tool interface."""
    ctx = get_ctx()
    if ctx is None:
        return "Error: plugin context not initialized"

    result_json = ctx.dispatch_tool("delegate_task", {
        "goal": goal,
        "context": context,
        "toolsets": toolsets,
        "max_iterations": max_iterations,
    })

    try:
        data = json.loads(result_json)
        if "results" in data:
            parts = []
            for r in data["results"]:
                val = r.get("summary")
                if val:
                    parts.append(val)
                else:
                    parts.append(r.get("error") or "no result")
            return "\n\n".join(parts)
        return data.get("final_response", data.get("error", result_json))
    except (json.JSONDecodeError, TypeError):
        return result_json


def run_deliver(
    task: str,
    max_rounds: int = MAX_ROUNDS,
    job: dict[str, Any] | None = None,
    structured: bool = False,
) -> str | dict[str, Any]:
    """Run the actor-critic delivery loop."""
    previous_output = None
    feedback = None
    next_action = "RESTART"
    lines = []
    normalized_job = _normalize_job(job)
    header = (normalized_job or {}).get("name") or task
    contract_text = _job_contract_text(task, normalized_job)

    _emit(lines, f"  🔀 Deliver: {header[:80]}{'...' if len(header) > 80 else ''}", f"Deliver: {header[:80]}{'...' if len(header) > 80 else ''}")
    _emit(lines, f"  Max rounds: {max_rounds}", f"Max rounds: {max_rounds}")

    final_result: dict[str, Any] = {
        "verdict": "MAX_ROUNDS",
        "summary": "",
        "score": None,
        "validated": [],
        "rejected": [],
        "validated_acceptance": [],
        "missing_acceptance": (normalized_job or {}).get("acceptance", []),
        "demands": [],
    }

    for rnd in range(1, max_rounds + 1):
        _emit(lines, f"\n  {'─' * 2} Round {rnd}/{max_rounds} {'─' * 2}", f"\n── Round {rnd}/{max_rounds} ──")

        if next_action == "RESTART" or previous_output is None:
            worker_goal = (
                f"{contract_text}\n\n"
                "Implement the job now. Write complete code. No TODOs, no stubs, no placeholders.\n"
                "Produce concrete deliverable artifacts: exact file paths, exact line ranges, code snippets or diff hunks, and test commands you actually ran.\n"
                "Do not claim completion without artifacts.\n"
                "Return a compact JSON report with keys summary, completed_acceptance, remaining_acceptance, locations, tests, and code.\n"
                "Each location entry must include the exact file path, line range, and a short code snippet or diff hunk."
            )
        else:
            worker_goal = (
                f"{contract_text}\n\n"
                f"PREVIOUS IMPLEMENTATION:\n{previous_output}\n\n"
                f"CRITIC FEEDBACK — address ALL of these:\n{feedback}\n\n"
                "Edit the previous implementation. Do not regress on anything working.\n"
                "Return the updated JSON report with completed_acceptance, remaining_acceptance, exact files, line ranges, and code snippets you changed."
            )

        _emit(lines, "  Deliver starting...", "Deliver starting...")
        worker_result = _dispatch(
            goal=worker_goal,
            context=(
                "You are a senior implementation agent. Do the work — write code, run tests, fix errors. "
                "No describing, only doing. Report exact file paths, line ranges, acceptance coverage, and concrete artifacts in your final response."
            ),
            toolsets=["terminal", "file", "web"],
        )
        _emit(lines, f"  Worker done ({len(worker_result)} chars)", f"Worker done ({len(worker_result)} chars)")

        worker_report = _parse_progress_report(worker_result)
        if worker_report:
            lines.extend(_format_progress_report(worker_report, "Worker report"))
            first_location = worker_report.get("locations", [])[:1]
            if first_location:
                loc = first_location[0]
                live = loc.get("path", "?")
                span = loc.get("lines", "")
                if span:
                    live += f" [{span}]"
                _emit(lines, f"  Worker report captured: {live}", f"Worker report captured: {live}")
        else:
            lines.append("Worker report:")
            lines.extend(_indent_block(worker_result, prefix="  "))

        _emit(lines, "  Critic reviewing...", "Critic reviewing...")
        critic_goal = (
            f"{contract_text}\n\n"
            f"WORKER TRANSCRIPT:\n{worker_result}\n\n"
            "Read the files. Run the tests yourself. Output ONLY valid JSON — no markdown, no commentary outside JSON.\n"
            "You may not rubber-stamp effort, intent, or plausibility. A COMPLETE verdict requires concrete deliverable artifacts and explicit acceptance coverage.\n"
            'If fundamentally wrong: {"verdict": "RESTART", "reason": "...", "guidance": "...", "missing_acceptance": ["..."], "rejected": [{"path": "...", "lines": "...", "reason": "..."}]}\n'
            'If right but incomplete: {"verdict": "EDIT", "summary": "...", "feedback": "...", "demands": ["..."], "validated_acceptance": ["criterion already met"], "missing_acceptance": ["criterion still missing"], "validated": [{"path": "...", "lines": "...", "summary": "..."}], "rejected": [{"path": "...", "lines": "...", "reason": "..."}]}\n'
            'If fully complete: {"verdict": "COMPLETE", "summary": "...", "critique": "...", "score": <1-10>, "validated_acceptance": ["criterion 1", "criterion 2"], "validated": [{"path": "...", "lines": "...", "summary": "..."}]}'
        )

        critic_result = _dispatch(
            goal=critic_goal,
            context=(
                "You are a code reviewer. Read the files the worker touched. Run the tests yourself. "
                "Form your own verdict from what you see, not what the transcript claims. "
                "Verify exact file paths, line ranges, and acceptance coverage. Output ONLY valid JSON."
            ),
            toolsets=["terminal", "file"],
            max_iterations=15,
        )

        verdict = _enforce_success_guardrails(_parse_verdict(critic_result), worker_report, normalized_job)
        v = verdict.get("verdict", "EDIT").upper()
        _emit(lines, f"  Verdict: {v}", f"Verdict: {v}")

        lines.append("  Critic review:")
        lines.append(f"    Verdict: {v}")
        if verdict.get("summary"):
            lines.append(f"    Summary: {verdict.get('summary', '')}")
        if verdict.get("score") is not None:
            lines.append(f"    Score: {verdict.get('score')}/10")
        if verdict.get("critique"):
            lines.append(f"    Critique: {verdict.get('critique', '')}")
        if verdict.get("reason"):
            lines.append(f"    Reason: {verdict.get('reason', '')}")
        if verdict.get("guidance"):
            lines.append(f"    Guidance: {verdict.get('guidance', '')}")
        if verdict.get("validated_acceptance"):
            lines.append("    Validated acceptance:")
            for item in verdict.get("validated_acceptance", []):
                lines.append(f"      - {item}")
        if verdict.get("missing_acceptance"):
            lines.append("    Missing acceptance:")
            for item in verdict.get("missing_acceptance", []):
                lines.append(f"      - {item}")
        _append_location_block(lines, "Validated edits", verdict.get("validated", []))
        _append_location_block(lines, "Rejected edits", verdict.get("rejected", []))
        if verdict.get("feedback"):
            lines.append(f"    Feedback: {verdict.get('feedback', '')}")
        if verdict.get("demands"):
            lines.append("    Demands:")
            for demand in verdict.get("demands", []):
                lines.append(f"      - {demand}")

        final_result = dict(verdict)

        if v == "COMPLETE":
            score = verdict.get("score", "?")
            critique = verdict.get("critique", "")
            lines.append(f"\n  ✅ COMPLETE — score: {score}/10")
            if critique:
                lines.append(f"  Critique: {critique}")
            final_result["transcript"] = "\n".join(lines)
            return final_result if structured else final_result["transcript"]

        if v == "RESTART":
            previous_output = None
            feedback = verdict.get("guidance") or verdict.get("reason") or verdict.get("feedback", "")
            next_action = "RESTART"
            lines.append(f"  RESTART — {verdict.get('reason', '')[:100]}")
        else:
            previous_output = worker_result
            feedback = json.dumps(verdict, indent=2)
            next_action = "EDIT"
            demands = verdict.get("demands", [])
            lines.append(f"  EDIT — {len(demands)} demand(s)")

    lines.append(f"\n  ⚠️  Max rounds ({max_rounds}) hit. Spec may need work.")
    final_result["transcript"] = "\n".join(lines)
    final_result["verdict"] = final_result.get("verdict") or "MAX_ROUNDS"
    return final_result if structured else final_result["transcript"]


def handle_deliver(raw_args: str) -> str:
    """Slash command handler for /deliver."""
    task = raw_args.strip()
    if not task:
        return (
            "Usage: /deliver <task description>\n"
            "Example: /deliver Implement connection pooling in src/http.py"
        )
    return run_deliver(task)

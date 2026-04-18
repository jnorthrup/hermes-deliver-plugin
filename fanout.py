"""Job decomposition and dependency-aware execution for /fanout command.

Decomposes a task into ordered jobs via a subagent, persists the plan
to .fanout/ on disk, and executes each job through the /deliver loop.

The review gate uses subcommands instead of TUI modals, making it work
in both CLI and gateway:

    /fanout Build a web scraper     → decompose + show plan
    /fanout accept                  → execute the plan
    /fanout critique <feedback>     → re-decompose with critique
    /fanout status                  → show current plan + progress
    /fanout abort                   → clean up
    /fanout clear                   → remove .fanout/ directory
"""

import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from . import get_ctx
from .deliver import run_deliver
from .fanout_fsm import FanoutReviewFSM, FanoutState, FanoutStory, FanoutTransitionError

# Module-level FSM instance — persists across subcommand calls within a session.
_fsm = FanoutReviewFSM()
PLAN_VERSION = 2
_DONE_JOB_STATUSES = {"done"}
_OPEN_JOB_STATUSES = {"pending", "running", "blocked", "needs_attention"}

# Try YAML, fall back to JSON
try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _yaml = None
    _HAS_YAML = False


def _feedback(msg: str) -> None:
    """Print immediate feedback to the terminal — Terrapin Station lyrics.

    Uses hermes' _cprint (prompt_toolkit ANSI renderer) so colors actually
    render instead of showing raw escape codes through patch_stdout's proxy.
    """
    try:
        from cli import _cprint, _DIM, _RST
        _cprint(f"  {_DIM}{msg}{_RST}")
    except Exception:
        # Absolute fallback — no color, just text
        print(f"  {msg}", flush=True)


def _emit(lines: list, persistent: str, live: str = None) -> None:
    """Dual output: append to lines (persistent) and print immediate feedback.

    If `live` is omitted, `persistent` is used for both channels.
    """
    lines.append(persistent)
    _feedback(live if live is not None else persistent)


def _workdir() -> Path:
    return Path(os.getenv("TERMINAL_CWD", os.getcwd()))


def _fanout_dir() -> Path:
    return _workdir() / ".fanout"


def _plan_path() -> Path:
    return _fanout_dir() / "plan.yaml"


def _jobs_dir() -> Path:
    return _fanout_dir() / "jobs"


def _stories_dir() -> Path:
    """Legacy alias kept for compatibility with older helper names."""
    return _jobs_dir()


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_job(job: dict[str, Any], index: int, completed: set[str]) -> dict[str, Any]:
    job_id = str(job.get("id") or f"{index + 1:03d}").strip() or f"{index + 1:03d}"
    dependencies = [
        str(dep).strip()
        for dep in (job.get("dependencies") or [])
        if str(dep).strip()
    ]
    acceptance = [
        str(item).strip()
        for item in (job.get("acceptance") or [])
        if str(item).strip()
    ]

    status = str(job.get("status") or "").strip().lower()
    if status in {"complete", "completed"}:
        status = "done"
    elif status in {"todo", "queued", "open"}:
        status = "pending"
    elif not status:
        status = "done" if job_id in completed else "pending"

    if status not in (_DONE_JOB_STATUSES | _OPEN_JOB_STATUSES):
        status = "done" if job_id in completed else "pending"

    history = job.get("history") or []
    if not isinstance(history, list):
        history = [{"event": "legacy_history", "detail": str(history)}]

    return {
        "id": job_id,
        "name": str(job.get("name") or f"Job {job_id}"),
        "description": str(job.get("description") or ""),
        "dependencies": dependencies,
        "acceptance": acceptance,
        "status": status,
        "attempts": _coerce_int(job.get("attempts"), 0),
        "last_summary": str(job.get("last_summary") or job.get("summary") or ""),
        "last_verdict": str(job.get("last_verdict") or ""),
        "last_run_at": str(job.get("last_run_at") or ""),
        "completed_at": str(job.get("completed_at") or ""),
        "history": history,
    }


def _normalize_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None

    jobs_raw = plan.get("jobs") or plan.get("stories") or []
    completed = {
        str(item).strip()
        for item in (plan.get("completed") or [])
        if str(item).strip()
    }
    jobs = [
        _normalize_job(job if isinstance(job, dict) else {}, index=index, completed=completed)
        for index, job in enumerate(jobs_raw)
    ]
    return {
        "version": PLAN_VERSION,
        "task": str(plan.get("task") or ""),
        "jobs": jobs,
        "critique_history": [
            str(item)
            for item in (plan.get("critique_history") or [])
            if str(item).strip()
        ],
    }


def _load_plan() -> dict | None:
    """Load plan from disk."""
    pp = _plan_path()
    if not pp.exists():
        return None
    try:
        raw = pp.read_text()
        if _HAS_YAML:
            return _normalize_plan(_yaml.safe_load(raw))
        if raw.strip().startswith("{"):
            return _normalize_plan(json.loads(raw))
        return None
    except Exception:
        return None


def _save_plan(plan: dict) -> None:
    """Save plan to disk."""
    normalized = _normalize_plan(plan)
    if normalized is None:
        raise ValueError("plan must be a mapping")
    pp = _plan_path()
    pp.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_YAML:
        pp.write_text(_yaml.dump(normalized, default_flow_style=False))
    else:
        pp.write_text(json.dumps(normalized, indent=2))


def _safe_slug(name: str) -> str:
    """Sanitize story name into a flat filename slug."""
    slug = name.replace(" ", "-").lower()
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    return slug or "untitled"


def _save_jobs(jobs: list[dict[str, Any]]) -> None:
    """Write individual job files to .fanout/jobs/."""
    jd = _jobs_dir()
    jd.mkdir(parents=True, exist_ok=True)
    for existing in jd.glob("*"):
        if existing.is_file():
            existing.unlink()

    for index, job in enumerate(jobs):
        normalized = _normalize_job(job, index=index, completed=set())
        slug = _safe_slug(normalized.get("name", "untitled"))
        fname = f"{normalized.get('id', '000')}-{slug}"
        if _HAS_YAML:
            (jd / f"{fname}.yaml").write_text(_yaml.dump(normalized, default_flow_style=False))
        else:
            (jd / f"{fname}.json").write_text(json.dumps(normalized, indent=2))


def _save_stories(stories: list) -> None:
    """Legacy alias for the old stories-based persistence API."""
    _save_jobs([story if isinstance(story, dict) else {} for story in stories])


def _format_plan(plan: dict) -> str:
    """Format the plan for display."""
    jobs = plan.get("jobs", [])
    task = plan.get("task", "?")
    completed = [job for job in jobs if job.get("status") in _DONE_JOB_STATUSES]

    lines = [
        f"{'=' * 60}",
        f"FANOUT PLAN",
        f"{'=' * 60}",
        f"Task: {task}",
        f"Jobs: {len(jobs)} ({len(completed)} done, {len(jobs) - len(completed)} open)",
        f"{'=' * 60}",
    ]

    for job in jobs:
        sid = job.get("id", "?")
        sname = job.get("name", "unnamed")
        status = str(job.get("status") or "pending").upper()
        deps = job.get("dependencies", [])
        dep_str = f"  (after: {', '.join(deps)})" if deps else ""
        lines.append(f"\n[{status}] Job {sid}: {sname}{dep_str}")
        lines.append(f"{'─' * 40}")
        desc = job.get("description", "")
        if desc:
            for line in desc.split("\n"):
                lines.append(f"  {line}")
        acceptance = job.get("acceptance", [])
        if acceptance:
            lines.append("  Acceptance:")
            for a in acceptance:
                lines.append(f"    - {a}")
        attempts = _coerce_int(job.get("attempts"), 0)
        if attempts:
            lines.append(f"  Attempts: {attempts}")
        if job.get("last_verdict"):
            lines.append(f"  Last verdict: {job.get('last_verdict')}")
        if job.get("last_summary"):
            lines.append(f"  Last summary: {job.get('last_summary')}")

    lines.append(f"\n{'=' * 60}")
    lines.append(f"Plan file: {_plan_path()}")
    return "\n".join(lines)


def _extract_subagent_summary(result_json: str) -> str:
    """Extract text from delegate_task's JSON return format.

    delegate_task returns: {"results": [{"summary": "...", ...}], "total_duration_seconds": N}
    Falls back to raw string if parsing fails.
    """
    try:
        data = json.loads(result_json)
        # Error-only response (no results key)
        if "error" in data and not data.get("results"):
            return ""
        if "results" in data:
            parts = []
            for r in data["results"]:
                val = r.get("summary")
                if val:
                    parts.append(val)
                else:
                    parts.append(r.get("error") or "")
            return "\n\n".join(parts)
        # Fallback for unexpected shapes
        return data.get("final_response", data.get("error", ""))
    except (json.JSONDecodeError, TypeError):
        return result_json if isinstance(result_json, str) else ""


def _parse_jobs_json(text: str) -> list | None:
    """Extract jobs array from subagent text output.

    Handles markdown fences, conversational wrapper text, etc.
    Returns list of job dicts or None.
    """
    if not text or not text.strip():
        return None
    # Strip markdown code fences
    clean = re.sub(r"^```(?:json)?\s*", "", text.strip())
    clean = re.sub(r"\s*```\s*$", "", clean)
    # Find the first JSON object
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return data.get("jobs") or data.get("stories") or None


def _decompose(task: str, critique: str = "") -> dict | None:
    """Run decomposition via subagent. Returns normalized plan dict or None."""
    ctx = get_ctx()
    _log = open("/tmp/fanout_debug.log", "a")
    _log.write(f"\n=== _decompose {datetime.datetime.now()} ===\n")
    _log.write(f"ctx={ctx} (type={type(ctx).__name__})\n")
    if ctx is None:
        _log.write("FATAL: ctx is None — plugin not registered?\n")
        _log.close()
        return None

    crit_note = f"\n\nUSER CRITIQUE FROM PREVIOUS ROUND (address this):\n{critique}" if critique else ""

    _log.write(f"calling dispatch_tool(delegate_task, ...) task={task[:80]}\n")
    _log.write(f"  _manager._cli_ref={ctx._manager._cli_ref}\n")
    if ctx._manager._cli_ref:
        _log.write(f"  _cli_ref.agent={getattr(ctx._manager._cli_ref, 'agent', 'NO_ATTR')}\n")
    _log.flush()
    _feedback("Let my inspiration flow in token lines, suggesting rhythm")
    result_json = ctx.dispatch_tool("delegate_task", {
        "goal": (
            f"DECOMPOSE ONLY — do NOT execute or implement anything.\n"
            f"Do NOT create files, run commands, or write code.\n"
            f"Your ONLY job is to output a JSON decomposition.\n\n"
            f"Decompose this task into 3-7 ordered jobs. Each job must be "
            f"narrow enough to complete in one session and concrete enough to review independently.\n"
            f"Do not emit umbrella jobs like 'polish', 'cleanup', or 'wire everything together' "
            f"unless they are unavoidable and still testable.\n\n"
            f"CRITICAL: Your entire response must be ONLY this JSON object, "
            f"no markdown fences, no commentary, no extra text:\n"
            f'{{"jobs": [{{"id": "001", "name": "...", "description": "...", '
            f'"dependencies": [], "acceptance": ["criterion 1", "criterion 2"]}}]}}\n\n'
            f"TASK:\n{task}{crit_note}"
        ),
        "context": (
            "You are a senior architect doing ONLY decomposition. "
            "You MUST NOT execute the task — only break it into concrete jobs. "
            "Each job needs a crisp, observable acceptance contract. "
            "Output ONLY a raw JSON object with no markdown fences. "
            "Do not use any tools. Do not create any files."
        ),
        "toolsets": [],
        "max_iterations": 2,
    })
    _log.write(f"dispatch_tool returned, type={type(result_json).__name__}, len={len(result_json) if result_json else 0}\n")
    _log.write(f"result_json[:500] = {repr(result_json)[:500]}\n")
    _log.flush()

    summary = _extract_subagent_summary(result_json)
    _log.write(f"summary type={type(summary).__name__}, len={len(summary)}, repr[:200]={repr(summary)[:200]}\n")
    _log.flush()

    jobs = _parse_jobs_json(summary)
    _log.write(f"jobs = {repr(jobs)[:300] if jobs else 'None'}\n")
    _log.close()

    if not jobs:
        return None

    return _normalize_plan({"task": task, "jobs": jobs, "critique_history": []})


def _sync_fsm_to_plan(task: str, plan: dict) -> None:
    """Drive the FSM from IDLE to PLAN_READY using an existing plan."""
    global _fsm
    if _fsm.state != FanoutState.IDLE:
        _fsm.reset()
    _fsm.start(task)
    fsm_stories = [
        FanoutStory(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description", ""),
            dependencies=s.get("dependencies", []),
            acceptance=s.get("acceptance", []),
        )
        for s in plan.get("jobs", [])
    ]
    _fsm.decomposition_done(fsm_stories)


# ── Subcommand handlers ──────────────────────────────────────────────────


def _handle_new_task(task: str) -> str:
    """Decompose a new task into jobs."""
    global _fsm
    _fsm = FanoutReviewFSM()

    # Check for existing plan
    plan = _load_plan()
    if plan and plan.get("jobs"):
        existing_task = plan.get("task", "").strip()
        if existing_task == task.strip():
            # Same task — show existing plan
            _sync_fsm_to_plan(task, plan)
            return (
                f"Found existing .fanout/ plan for this task.\n\n"
                f"{_format_plan(plan)}\n\n"
                f"Commands: /fanout accept | critique <text> | abort | clear"
            )
        else:
            return (
                f"Found .fanout/ for a different task:\n"
                f"  Existing: {existing_task[:60]}\n"
                f"  New: {task[:60]}\n\n"
                f"Run /fanout clear first, then try again."
            )

    # Decompose
    _fsm.start(task)
    plan = _normalize_plan(_decompose(task))
    if plan is None:
        try:
            _fsm.decomposition_fail()
        except FanoutTransitionError:
            pass
        return "Decomposition failed — subagent didn't return valid jobs."

    # Save to disk
    _save_plan(plan)
    _save_jobs(plan["jobs"])

    # Build FSM stories and transition
    fsm_stories = [
        FanoutStory(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description", ""),
            dependencies=s.get("dependencies", []),
            acceptance=s.get("acceptance", []),
        )
        for s in plan["jobs"]
    ]
    _fsm.decomposition_done(fsm_stories)

    return (
        f"Created {len(plan['jobs'])} jobs.\n\n"
        f"{_format_plan(plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _append_job_history(job: dict[str, Any], event: str, **details: Any) -> None:
    entry = {"event": event, "at": datetime.datetime.now().isoformat()}
    entry.update({key: value for key, value in details.items() if value not in (None, "", [], {})})
    history = job.setdefault("history", [])
    if isinstance(history, list):
        history.append(entry)


def _build_job_task(parent_task: str, job: dict[str, Any]) -> str:
    acceptance = job.get("acceptance", [])
    deps = job.get("dependencies", [])
    lines = [
        f"PARENT TASK:\n{parent_task}",
        f"JOB ID: {job.get('id', '?')}",
        f"JOB NAME: {job.get('name', 'unnamed')}",
        "JOB DESCRIPTION:",
        str(job.get("description") or job.get("name") or "").strip(),
    ]
    if deps:
        lines.extend(["", "DEPENDENCIES ALREADY SATISFIED:", *[f"- {dep}" for dep in deps]])
    if acceptance:
        lines.extend(["", "ACCEPTANCE CRITERIA (must all pass):", *[f"- {item}" for item in acceptance]])
    return "\n".join(lines)


def _handle_accept() -> str:
    """Accept the plan and execute jobs."""
    global _fsm

    # If FSM is idle but plan exists on disk, sync up
    if _fsm.state == FanoutState.IDLE:
        plan = _load_plan()
        if plan and plan.get("jobs"):
            _sync_fsm_to_plan(plan["task"], plan)
        else:
            return "No plan to accept. Run /fanout <task> first."

    try:
        _fsm.accept()
        _fsm.execute()
    except FanoutTransitionError as e:
        return f"Cannot accept from state '{_fsm.state.value}': {e}"

    plan = _load_plan()
    if not plan:
        return "Error: plan file missing."

    jobs = plan.get("jobs", [])
    completed = {
        job.get("id", "?")
        for job in jobs
        if str(job.get("status") or "").lower() in _DONE_JOB_STATUSES
    }
    journal_path = _fanout_dir() / "journal.md"
    pending = [job for job in jobs if job.get("id", "?") not in completed]

    lines = [
        f"{'=' * 50}",
        f"FANOUT: {len(jobs)} jobs, {len(completed)} already done",
        f"{'=' * 50}",
    ]

    _emit(lines, "", f"Executing {len(pending)} open jobs ({len(completed)} already done)")
    _emit(lines, "  While the story teller speaks, a door within the fire creaks")
    _emit(lines, "  Suddenly flies open, and a girl is standing there")
    _emit(lines, "  She takes her fan and throws it, in the lion's den")

    stalled_job = None

    for job in jobs:
        sid = job.get("id", "?")
        sname = job.get("name", "unnamed")
        deps = job.get("dependencies", [])
        status = str(job.get("status") or "pending").lower()

        if status in _DONE_JOB_STATUSES:
            lines.append(f"\nJob {sid} ({sname}) — already complete, skipping")
            try:
                _fsm.story_done(sid)
            except FanoutTransitionError:
                pass
            continue

        blocked = [d for d in deps if d not in completed]
        if blocked:
            job["status"] = "blocked"
            _append_job_history(job, "blocked", unmet_dependencies=blocked)
            _save_plan(plan)
            _save_jobs(jobs)
            lines.append(f"\nJob {sid} ({sname}) — BLOCKED by {blocked}")
            continue

        _emit(lines, "", f"--- JOB {sid}: {sname} ---")
        _emit(lines, "  Which of you to gain me, tell, will risk uncertain pains of hell?")
        _emit(lines, "  I will not forgive you if you will not take the chance")

        lines.append(f"\n{'─' * 50}")
        lines.append(f"JOB {sid}: {sname}")
        lines.append(f"{'─' * 50}")

        job["status"] = "running"
        job["attempts"] = _coerce_int(job.get("attempts"), 0) + 1
        job["last_run_at"] = datetime.datetime.now().isoformat()
        _append_job_history(job, "started", attempt=job["attempts"])
        _save_plan(plan)
        _save_jobs(jobs)

        deliver_result = run_deliver(_build_job_task(plan["task"], job), job=job, structured=True)
        lines.append(deliver_result["transcript"])

        verdict = str(deliver_result.get("verdict") or "MAX_ROUNDS").upper()
        acceptance = job.get("acceptance", [])
        job["last_verdict"] = verdict
        job["last_summary"] = str(deliver_result.get("summary") or "")

        if verdict == "COMPLETE":
            completed.add(sid)
            job["status"] = "done"
            job["completed_at"] = datetime.datetime.now().isoformat()
            _append_job_history(
                job,
                "complete",
                score=deliver_result.get("score"),
                validated_acceptance=deliver_result.get("validated_acceptance"),
            )
            _emit(lines, "  The sailor gave at least a try", f"JOB {sid} PASS -- The sailor gave at least a try")
            try:
                _fsm.story_done(sid)
            except FanoutTransitionError:
                pass
            with open(journal_path, "a") as jf:
                jf.write(
                    f"\n## Job {sid}: {sname} — COMPLETE\n"
                    f"- Completed at: {job['completed_at']}\n"
                    f"- Acceptance: {len(acceptance)} criteria\n"
                    f"- Last verdict: {verdict}\n"
                )
        else:
            missing = deliver_result.get("missing_acceptance") or deliver_result.get("demands") or []
            job["status"] = "needs_attention"
            _append_job_history(
                job,
                "needs_attention",
                verdict=verdict,
                missing_acceptance=missing,
            )
            _emit(
                lines,
                "  The soldier being much too wise, strategy was his strength, and not disaster",
                f"JOB {sid} OPEN -- The soldier being much too wise",
            )
            with open(journal_path, "a") as jf:
                jf.write(
                    f"\n## Job {sid}: {sname} — OPEN\n"
                    f"- Updated at: {datetime.datetime.now().isoformat()}\n"
                    f"- Last verdict: {verdict}\n"
                    f"- Missing acceptance: {len(missing)}\n"
                )
            stalled_job = job
            _save_plan(plan)
            _save_jobs(jobs)
            lines.append(f"\nJob {sid} remains open. Fanout stopped here so the next pass can focus on this job.")
            break

        _save_plan(plan)
        _save_jobs(jobs)

    if stalled_job is None and completed >= {job.get("id", "?") for job in jobs}:
        try:
            _fsm.all_stories_done()
        except FanoutTransitionError:
            pass

    if stalled_job is None:
        _emit(lines, f"\n✓ Fanout complete: {len(completed)}/{len(jobs)} jobs done",
              f"Fanout complete: {len(completed)}/{len(jobs)} jobs done")
    else:
        _emit(lines, f"\n! Fanout paused on job {stalled_job.get('id', '?')}",
              f"Fanout paused on job {stalled_job.get('id', '?')}")
    lines.append(f"Journal: {journal_path}")
    _emit(lines, "  The sailor, coming out again, the lady fairly leapt at him!")
    return "\n".join(lines)


def _handle_critique(text: str) -> str:
    """Re-decompose with user critique."""
    global _fsm

    if not text.strip():
        return "Usage: /fanout critique <your feedback>"

    # Sync FSM if needed
    if _fsm.state == FanoutState.IDLE:
        plan = _load_plan()
        if plan and plan.get("jobs"):
            _sync_fsm_to_plan(plan["task"], plan)
        else:
            return "No plan to critique. Run /fanout <task> first."

    try:
        _fsm.critique(text)
        _fsm.re_decompose()
    except FanoutTransitionError as e:
        return f"Cannot critique from state '{_fsm.state.value}': {e}"

    plan = _load_plan()
    if not plan:
        return "Error: lost the plan during critique."

    task = plan["task"]
    accumulated = _fsm.accumulated_critique
    new_plan = _normalize_plan(_decompose(task, critique=accumulated))
    if new_plan is None:
        try:
            _fsm.decomposition_fail()
        except FanoutTransitionError:
            pass
        return "Re-decomposition failed. Try /fanout critique with different feedback."

    _save_plan(new_plan)
    _save_jobs(new_plan["jobs"])

    fsm_stories = [
        FanoutStory(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description", ""),
            dependencies=s.get("dependencies", []),
            acceptance=s.get("acceptance", []),
        )
        for s in new_plan["jobs"]
    ]
    _fsm.decomposition_done(fsm_stories)

    return (
        f"Re-decomposed with critique ({len(_fsm.plan.critique_history)} rounds).\n\n"
        f"{_format_plan(new_plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_status() -> str:
    """Show current plan status."""
    plan = _load_plan()
    if not plan:
        return "No .fanout/ plan found. Run /fanout <task> to start."

    _feedback("That's how it stands today, you decide if he was wise")
    return (
        f"FSM state: {_fsm.state.value}\n\n"
        f"{_format_plan(plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_abort() -> str:
    """Abort the current fanout."""
    global _fsm
    try:
        _fsm.abort()
    except FanoutTransitionError:
        pass
    _fsm = FanoutReviewFSM()
    return "Fanout aborted. Plan files kept in .fanout/ — use /fanout clear to remove."


def _handle_clear() -> str:
    """Remove .fanout/ directory."""
    global _fsm
    _feedback("The storyteller makes no choice, soon you will not hear his voice")
    _feedback("His job is to shed light, and not to master")
    fd = _fanout_dir()
    if fd.exists():
        archive = Path("/tmp") / f"fanout_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.move(str(fd), str(archive))
            msg = f"Archived .fanout/ to {archive}"
        except Exception:
            shutil.rmtree(fd, ignore_errors=True)
            msg = "Removed .fanout/"
    else:
        msg = "No .fanout/ directory found."

    _fsm = FanoutReviewFSM()
    return msg


# ── Main router ──────────────────────────────────────────────────────────


_SUBCOMMANDS = {
    "accept": lambda args: _handle_accept(),
    "critique": lambda args: _handle_critique(args),
    "status": lambda args: _handle_status(),
    "abort": lambda args: _handle_abort(),
    "clear": lambda args: _handle_clear(),
}


def handle_fanout(raw_args: str) -> str:
    """Slash command handler for /fanout."""
    raw_args = raw_args.strip()

    if not raw_args:
        return (
            "Usage:\n"
            "  /fanout <task description>   — decompose into jobs\n"
            "  /fanout accept               — execute the plan\n"
            "  /fanout critique <feedback>   — re-decompose with feedback\n"
            "  /fanout status               — show current plan\n"
            "  /fanout abort                — abort execution\n"
            "  /fanout clear                — remove .fanout/ directory"
        )

    # Check for subcommand
    first_word = raw_args.split(maxsplit=1)[0].lower()
    if first_word in _SUBCOMMANDS:
        remaining = raw_args[len(first_word):].strip()
        return _SUBCOMMANDS[first_word](remaining)

    # Otherwise it's a new task
    return _handle_new_task(raw_args)

"""Story decomposition and dependency-aware execution for /fanout command.

Decomposes a task into ordered stories via a subagent, persists the plan
to .fanout/ on disk, and executes each story through the /deliver loop.

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

from . import get_ctx
from .deliver import run_deliver
from .fanout_fsm import FanoutReviewFSM, FanoutState, FanoutStory, FanoutTransitionError

# Module-level FSM instance — persists across subcommand calls within a session.
_fsm = FanoutReviewFSM()

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


def _stories_dir() -> Path:
    return _fanout_dir() / "stories"


def _load_plan() -> dict | None:
    """Load plan from disk."""
    pp = _plan_path()
    if not pp.exists():
        return None
    try:
        raw = pp.read_text()
        if _HAS_YAML:
            return _yaml.safe_load(raw)
        if raw.strip().startswith("{"):
            return json.loads(raw)
        return None
    except Exception:
        return None


def _save_plan(plan: dict) -> None:
    """Save plan to disk."""
    pp = _plan_path()
    pp.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_YAML:
        pp.write_text(_yaml.dump(plan, default_flow_style=False))
    else:
        pp.write_text(json.dumps(plan, indent=2))


def _safe_slug(name: str) -> str:
    """Sanitize story name into a flat filename slug."""
    slug = name.replace(" ", "-").lower()
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    return slug or "untitled"


def _save_stories(stories: list) -> None:
    """Write individual story files to .fanout/stories/."""
    sd = _stories_dir()
    sd.mkdir(parents=True, exist_ok=True)
    for s in stories:
        slug = _safe_slug(s.get("name", "untitled"))
        fname = f"{s.get('id', '000')}-{slug}"
        if _HAS_YAML:
            (sd / f"{fname}.yaml").write_text(_yaml.dump(s, default_flow_style=False))
        else:
            (sd / f"{fname}.json").write_text(json.dumps(s, indent=2))


def _format_plan(plan: dict) -> str:
    """Format the plan for display."""
    stories = plan.get("stories", [])
    task = plan.get("task", "?")
    completed = set(plan.get("completed", []))

    lines = [
        f"{'=' * 60}",
        f"FANOUT PLAN",
        f"{'=' * 60}",
        f"Task: {task}",
        f"Stories: {len(stories)} ({len(completed)} done)",
        f"{'=' * 60}",
    ]

    for i, s in enumerate(stories, 1):
        sid = s.get("id", "?")
        sname = s.get("name", "unnamed")
        status = "DONE" if sid in completed else f"  {i}/{len(stories)}"
        deps = s.get("dependencies", [])
        dep_str = f"  (after: {', '.join(deps)})" if deps else ""
        lines.append(f"\n[{status}] Story {sid}: {sname}{dep_str}")
        lines.append(f"{'─' * 40}")
        desc = s.get("description", "")
        if desc:
            for line in desc.split("\n"):
                lines.append(f"  {line}")
        acceptance = s.get("acceptance", [])
        if acceptance:
            lines.append("  Acceptance:")
            for a in acceptance:
                lines.append(f"    - {a}")

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


def _parse_stories_json(text: str) -> list | None:
    """Extract stories array from subagent text output.

    Handles markdown fences, conversational wrapper text, etc.
    Returns list of story dicts or None.
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
    return data.get("stories") or None


def _decompose(task: str, critique: str = "") -> dict | None:
    """Run decomposition via subagent. Returns plan dict or None."""
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
            f"Decompose this task into 3-7 ordered stories. Each story must be "
            f"narrow enough to complete in one session.\n\n"
            f"CRITICAL: Your entire response must be ONLY this JSON object, "
            f"no markdown fences, no commentary, no extra text:\n"
            f'{{"stories": [{{"id": "001", "name": "...", "description": "...", '
            f'"dependencies": [], "acceptance": ["criterion 1", "criterion 2"]}}]}}\n\n'
            f"TASK:\n{task}{crit_note}"
        ),
        "context": (
            "You are a senior architect doing ONLY decomposition. "
            "You MUST NOT execute the task — only break it into stories. "
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

    stories = _parse_stories_json(summary)
    _log.write(f"stories = {repr(stories)[:300] if stories else 'None'}\n")
    _log.close()

    if not stories:
        return None

    return {"task": task, "stories": stories, "completed": []}


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
        for s in plan.get("stories", [])
    ]
    _fsm.decomposition_done(fsm_stories)


# ── Subcommand handlers ──────────────────────────────────────────────────


def _handle_new_task(task: str) -> str:
    """Decompose a new task into stories."""
    global _fsm
    _fsm = FanoutReviewFSM()

    # Check for existing plan
    plan = _load_plan()
    if plan and plan.get("stories"):
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
    plan = _decompose(task)
    if plan is None:
        try:
            _fsm.decomposition_fail()
        except FanoutTransitionError:
            pass
        return "Decomposition failed — subagent didn't return valid stories."

    # Save to disk
    _save_plan(plan)
    _save_stories(plan["stories"])

    # Build FSM stories and transition
    fsm_stories = [
        FanoutStory(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description", ""),
            dependencies=s.get("dependencies", []),
            acceptance=s.get("acceptance", []),
        )
        for s in plan["stories"]
    ]
    _fsm.decomposition_done(fsm_stories)

    return (
        f"Created {len(plan['stories'])} stories.\n\n"
        f"{_format_plan(plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_accept() -> str:
    """Accept the plan and execute stories."""
    global _fsm

    # If FSM is idle but plan exists on disk, sync up
    if _fsm.state == FanoutState.IDLE:
        plan = _load_plan()
        if plan and plan.get("stories"):
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

    completed = set(plan.get("completed", []))
    stories = plan.get("stories", [])
    journal_path = _fanout_dir() / "journal.md"
    pending = [s for s in stories if s.get("id", "?") not in completed]

    lines = [
        f"{'=' * 50}",
        f"FANOUT: {len(stories)} stories, {len(completed)} already done",
        f"{'=' * 50}",
    ]

    _emit(lines, "", f"Executing {len(pending)} pending stories ({len(completed)} already done)")
    _emit(lines, "  While the story teller speaks, a door within the fire creaks")
    _emit(lines, "  Suddenly flies open, and a girl is standing there")
    _emit(lines, "  She takes her fan and throws it, in the lion's den")

    for idx, story in enumerate(stories, 1):
        sid = story.get("id", "?")
        sname = story.get("name", "unnamed")
        deps = story.get("dependencies", [])

        if sid in completed:
            lines.append(f"\nStory {sid} ({sname}) — already complete, skipping")
            try:
                _fsm.story_done(sid)
            except FanoutTransitionError:
                pass
            continue

        blocked = [d for d in deps if d not in completed]
        if blocked:
            lines.append(f"\nStory {sid} ({sname}) — BLOCKED by {blocked}")
            continue

        _emit(lines, "", f"--- STORY {sid}: {sname} ---")
        _emit(lines, "  Which of you to gain me, tell, will risk uncertain pains of hell?")
        _emit(lines, "  I will not forgive you if you will not take the chance")

        lines.append(f"\n{'─' * 50}")
        lines.append(f"STORY {sid}: {sname}")
        lines.append(f"{'─' * 50}")

        acceptance = story.get("acceptance", [])
        desc = story.get("description", sname)
        story_task = (
            f"{desc}\n\n"
            f"ACCEPTANCE CRITERIA (you must satisfy ALL):\n"
            + "\n".join(f"  - {a}" for a in acceptance)
        )

        deliver_result = run_deliver(story_task)
        lines.append(deliver_result)

        completed.add(sid)
        plan["completed"] = sorted(completed)
        _save_plan(plan)

        # Terrapin verdict — sailor tried, or soldier played it safe
        if "COMPLETE" in deliver_result:
            _emit(lines, "  The sailor gave at least a try", f"STORY {sid} PASS -- The sailor gave at least a try")
        else:
            _emit(lines, "  The soldier being much too wise, strategy was his strength, and not disaster",
                  f"STORY {sid} INCOMPLETE -- The soldier being much too wise")

        try:
            _fsm.story_done(sid)
        except FanoutTransitionError:
            pass

        entry = (
            f"\n## Story {sid}: {sname} — COMPLETE\n"
            f"- Completed at: {datetime.datetime.now().isoformat()}\n"
            f"- Acceptance: {len(acceptance)} criteria\n"
        )
        with open(journal_path, "a") as jf:
            jf.write(entry)

    try:
        _fsm.all_stories_done()
    except FanoutTransitionError:
        pass

    _emit(lines, f"\n✓ Fanout complete: {len(completed)}/{len(stories)} stories done",
          f"Fanout complete: {len(completed)}/{len(stories)} stories done")
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
        if plan and plan.get("stories"):
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
    new_plan = _decompose(task, critique=accumulated)
    if new_plan is None:
        try:
            _fsm.decomposition_fail()
        except FanoutTransitionError:
            pass
        return "Re-decomposition failed. Try /fanout critique with different feedback."

    _save_plan(new_plan)
    _save_stories(new_plan["stories"])

    fsm_stories = [
        FanoutStory(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description", ""),
            dependencies=s.get("dependencies", []),
            acceptance=s.get("acceptance", []),
        )
        for s in new_plan["stories"]
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
            "  /fanout <task description>   — decompose into stories\n"
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

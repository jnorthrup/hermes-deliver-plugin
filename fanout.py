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


def _decompose(task: str, critique: str = "") -> dict | None:
    """Run decomposition via subagent. Returns plan dict or None."""
    ctx = get_ctx()
    if ctx is None:
        return None

    crit_note = f"\n\nUSER CRITIQUE FROM PREVIOUS ROUND (address this):\n{critique}" if critique else ""

    result_json = ctx.dispatch_tool("delegate_task", {
        "goal": (
            f"Decompose this task into 3-7 ordered stories. Each story must be "
            f"narrow enough to complete in one session.\n\n"
            f"Output ONLY valid JSON (no markdown):\n"
            f'{{"stories": [{{"id": "001", "name": "...", "description": "...", '
            f'"dependencies": [], "acceptance": ["criterion 1", "criterion 2"]}}]}}\n\n'
            f"TASK:\n{task}{crit_note}"
        ),
        "context": "You are a senior architect. Output ONLY valid JSON. No commentary.",
        "toolsets": ["terminal", "file"],
        "max_iterations": 10,
    })

    # Extract the response from delegate_task
    try:
        data = json.loads(result_json)
        decomp_result = data.get("final_response", data.get("error", ""))
    except (json.JSONDecodeError, TypeError):
        decomp_result = result_json

    if not decomp_result or not decomp_result.strip():
        return None

    # Strip markdown fences and extract JSON
    clean = re.sub(r"^```(?:json)?\s*", "", decomp_result.strip())
    clean = re.sub(r"\s*```\s*$", "", clean)
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if not match:
        return None

    try:
        decomp_data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    stories = decomp_data.get("stories", [])
    if not stories:
        return None

    plan = {"task": task, "stories": stories, "completed": []}
    return plan


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
    lines = [
        f"{'=' * 50}",
        f"FANOUT: {len(stories)} stories, {len(completed)} already done",
        f"{'=' * 50}",
    ]

    for story in stories:
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

    lines.append(f"\n✓ Fanout complete: {len(completed)}/{len(stories)} stories done")
    lines.append(f"Journal: {journal_path}")
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

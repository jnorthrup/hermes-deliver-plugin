"""Finite State Machine for the /fanout command review workflow.

Models the lifecycle of a fanout plan from decomposition through review,
critique, confirmation, and story execution.

States:
    IDLE          - Initial state, no plan exists
    DECOMPOSING   - Subagent is decomposing the task into stories
    PLAN_READY    - Plan has been generated, awaiting user review
    EDITING       - User is editing the plan file (external to FSM)
    CRITIQUING    - User has provided critique; feedback accumulated
    CONFIRMED     - User has accepted the plan
    EXECUTING     - Stories are being executed
    COMPLETE      - All stories finished
    ABORTED       - User aborted or fatal error

Transitions:
    start(task)             IDLE       → DECOMPOSING
    decomposition_done()    DECOMPOSING → PLAN_READY
    decomposition_fail()    DECOMPOSING → IDLE
    critique(text)          PLAN_READY → CRITIQUING
    re_decompose()          CRITIQUING  → DECOMPOSING
    accept()                PLAN_READY → CONFIRMED
    edit()                  PLAN_READY → EDITING
    resume_review()         EDITING    → PLAN_READY
    execute()               CONFIRMED  → EXECUTING
    story_done(sid)         EXECUTING  → EXECUTING (progress)
    all_stories_done()      EXECUTING  → COMPLETE
    abort()                 *          → ABORTED
    retry()                 ABORTED    → IDLE
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# String resources used by the /fanout plugin.
FANOUT_ACCEPT_LYRICS = (
    "  While the story teller speaks, a door within the fire creaks",
    "  Suddenly flies open, and a girl is standing there",
    "  She takes her fan and throws it, in the lion's den",
)
FANOUT_STORY_LYRICS = (
    "  Which of you to gain me, tell, will risk uncertain pains of hell?",
    "  I will not forgive you if you will not take the chance",
)
FANOUT_STORY_PASS = "  The sailor gave at least a try"
FANOUT_STORY_INCOMPLETE = (
    "  The soldier being much too wise, strategy was his strength, and not disaster"
)
FANOUT_STORY_END = "  The sailor, coming out again, the lady fairly leapt at him!"
FANOUT_STATUS = "That's how it stands today, you decide if he was wise"
FANOUT_CLEAR_LYRICS = (
    "The storyteller makes no choice, soon you will not hear his voice",
    "His job is to shed light, and not to master",
)
FANOUT_DECOMPOSE_ONLY = "DECOMPOSE ONLY — do NOT execute or implement anything."
FANOUT_DECOMPOSE_CONSTRAINTS = (
    "Do NOT create files, run commands, or write code. Your ONLY job is to output a JSON decomposition."
)
FANOUT_DECOMPOSE_CONTEXT = (
    "You are a senior architect doing ONLY decomposition. You MUST NOT execute the task — "
    "only break it into stories. Output ONLY a raw JSON object with no markdown fences. "
    "Do not use any tools. Do not create any files."
)
FANOUT_DECOMPOSE_EXAMPLE = (
    '{"stories": [{"id": "001", "name": "...", "description": "...", '
    '"dependencies": [], "acceptance": ["criterion 1", "criterion 2"]}]}'
)


class FanoutState(Enum):
    """States of the fanout review FSM."""
    IDLE = "idle"
    DECOMPOSING = "decomposing"
    PLAN_READY = "plan_ready"
    EDITING = "editing"
    CRITIQUING = "critiquing"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ABORTED = "aborted"


class FanoutTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: FanoutState, action: str, allowed: list[str]):
        self.current = current
        self.action = action
        self.allowed = allowed
        super().__init__(
            f"Cannot '{action}' from state '{current.value}'. "
            f"Allowed actions: {', '.join(allowed)}"
        )


@dataclass(frozen=True)
class FanoutStory:
    """A single story in a fanout plan."""
    id: str
    name: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "acceptance": list(self.acceptance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FanoutStory":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "unnamed")),
            description=str(data.get("description", "")),
            dependencies=list(data.get("dependencies", [])),
            acceptance=list(data.get("acceptance", [])),
        )


@dataclass
class FanoutPlan:
    """Complete fanout plan with stories and tracking."""
    task: str
    stories: list[FanoutStory] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)
    critique_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "stories": [s.to_dict() for s in self.stories],
            "completed": sorted(self.completed),
            "critique_history": list(self.critique_history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FanoutPlan":
        stories = [FanoutStory.from_dict(s) for s in data.get("stories", [])]
        completed = set(data.get("completed", []))
        critique_history = list(data.get("critique_history", []))
        return cls(
            task=str(data.get("task", "")),
            stories=stories,
            completed=completed,
            critique_history=critique_history,
        )

    @property
    def is_complete(self) -> bool:
        """All stories have been completed."""
        return bool(self.stories) and self.completed >= {s.id for s in self.stories}

    @property
    def remaining_stories(self) -> list[FanoutStory]:
        """Stories not yet completed, respecting dependency order."""
        return [s for s in self.stories if s.id not in self.completed]

    @property
    def ready_stories(self) -> list[FanoutStory]:
        """Stories whose dependencies are all satisfied and not yet done."""
        ready = []
        for s in self.remaining_stories:
            if all(d in self.completed for d in s.dependencies):
                ready.append(s)
        return ready

    @property
    def blocked_stories(self) -> list[FanoutStory]:
        """Stories blocked by unmet dependencies."""
        blocked = []
        for s in self.remaining_stories:
            unmet = [d for d in s.dependencies if d not in self.completed]
            if unmet:
                blocked.append(s)
        return blocked


_ALLOWED_TRANSITIONS: dict[FanoutState, dict[str, FanoutState]] = {
    FanoutState.IDLE: {
        "start": FanoutState.DECOMPOSING,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.DECOMPOSING: {
        "decomposition_done": FanoutState.PLAN_READY,
        "decomposition_fail": FanoutState.IDLE,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.PLAN_READY: {
        "accept": FanoutState.CONFIRMED,
        "edit": FanoutState.EDITING,
        "critique": FanoutState.CRITIQUING,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.EDITING: {
        "resume_review": FanoutState.PLAN_READY,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.CRITIQUING: {
        "re_decompose": FanoutState.DECOMPOSING,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.CONFIRMED: {
        "execute": FanoutState.EXECUTING,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.EXECUTING: {
        "story_done": FanoutState.EXECUTING,
        "all_stories_done": FanoutState.COMPLETE,
        "abort": FanoutState.ABORTED,
    },
    FanoutState.COMPLETE: {
        "retry": FanoutState.IDLE,
    },
    FanoutState.ABORTED: {
        "retry": FanoutState.IDLE,
    },
}


class FanoutReviewFSM:
    """State machine for the /fanout command review workflow.

    Usage::

        fsm = FanoutReviewFSM()
        fsm.start("Build a web scraper")
        # ... subagent decomposes ...
        fsm.decomposition_done(stories=[...])
        # ... user reviews plan ...
        fsm.accept()
        fsm.execute()
        fsm.story_done("001")
        fsm.story_done("002")
        fsm.all_stories_done()
        assert fsm.state == FanoutState.COMPLETE
    """

    def __init__(self) -> None:
        self._state: FanoutState = FanoutState.IDLE
        self._plan: FanoutPlan | None = None
        self._history: list[tuple[str, FanoutState, FanoutState]] = []

    @property
    def state(self) -> FanoutState:
        """Current FSM state."""
        return self._state

    @property
    def plan(self) -> FanoutPlan | None:
        """Current plan, or None if not yet decomposed."""
        return self._plan

    @property
    def history(self) -> list[tuple[str, FanoutState, FanoutState]]:
        """Transition log: (action, from_state, to_state)."""
        return list(self._history)

    # ------------------------------------------------------------------ #
    #  Internal transition helper
    # ------------------------------------------------------------------ #

    def _transition(self, action: str) -> None:
        """Attempt a state transition. Raises FanoutTransitionError if invalid."""
        allowed = _ALLOWED_TRANSITIONS.get(self._state, {})
        if action not in allowed:
            raise FanoutTransitionError(
                self._state, action, sorted(allowed.keys())
            )
        old = self._state
        self._state = allowed[action]
        self._history.append((action, old, self._state))

    # ------------------------------------------------------------------ #
    #  Transitions
    # ------------------------------------------------------------------ #

    def start(self, task: str) -> None:
        """Begin decomposition of a task. IDLE → DECOMPOSING."""
        self._transition("start")
        self._plan = FanoutPlan(task=task)

    def decomposition_done(self, stories: list[FanoutStory]) -> None:
        """Decomposition succeeded; plan is ready for review. DECOMPOSING → PLAN_READY."""
        self._transition("decomposition_done")
        if self._plan is None:
            raise RuntimeError("No plan exists — call start() first")
        self._plan.stories = stories

    def decomposition_fail(self) -> None:
        """Decomposition failed. DECOMPOSING → IDLE."""
        self._transition("decomposition_fail")

    def accept(self) -> None:
        """User accepted the plan. PLAN_READY → CONFIRMED."""
        self._transition("accept")

    def edit(self) -> None:
        """User wants to edit the plan file. PLAN_READY → EDITING."""
        self._transition("edit")

    def resume_review(self) -> None:
        """User finished editing; return to review. EDITING → PLAN_READY."""
        self._transition("resume_review")

    def critique(self, text: str) -> None:
        """User provided critique feedback. PLAN_READY → CRITIQUING."""
        self._transition("critique")
        if self._plan is not None:
            self._plan.critique_history.append(text)

    def re_decompose(self) -> None:
        """Re-decompose with accumulated critique. CRITIQUING → DECOMPOSING."""
        self._transition("re_decompose")

    def execute(self) -> None:
        """Begin story execution. CONFIRMED → EXECUTING."""
        self._transition("execute")

    def story_done(self, story_id: str) -> None:
        """Mark a story as completed. EXECUTING → EXECUTING."""
        self._transition("story_done")
        if self._plan is not None:
            self._plan.completed.add(story_id)

    def all_stories_done(self) -> None:
        """All stories completed. EXECUTING → COMPLETE."""
        self._transition("all_stories_done")

    def abort(self) -> None:
        """Abort the fanout from any state. * → ABORTED."""
        if self._state in (FanoutState.COMPLETE, FanoutState.ABORTED, FanoutState.IDLE):
            # Already terminal or idle — just set aborted
            old = self._state
            self._state = FanoutState.ABORTED
            self._history.append(("abort", old, self._state))
            return
        self._transition("abort")

    def retry(self) -> None:
        """Reset to IDLE for a new attempt. ABORTED/COMPLETE → IDLE."""
        self._transition("retry")
        self._plan = None

    # ------------------------------------------------------------------ #
    #  Queries
    # ------------------------------------------------------------------ #

    @property
    def accumulated_critique(self) -> str:
        """Return all accumulated critique as a single string."""
        if self._plan is None:
            return ""
        return "\n\n".join(self._plan.critique_history)

    @property
    def is_terminal(self) -> bool:
        """True when in a terminal state (COMPLETE, ABORTED)."""
        return self._state in (FanoutState.COMPLETE, FanoutState.ABORTED)

    @property
    def pending_story_ids(self) -> list[str]:
        """Story IDs not yet completed."""
        if self._plan is None:
            return []
        return [s.id for s in self._plan.remaining_stories]

    @property
    def ready_story_ids(self) -> list[str]:
        """Story IDs ready to execute (dependencies satisfied)."""
        if self._plan is None:
            return []
        return [s.id for s in self._plan.ready_stories]

    @property
    def blocked_story_ids(self) -> list[str]:
        """Story IDs blocked by unmet dependencies."""
        if self._plan is None:
            return []
        return [s.id for s in self._plan.blocked_stories]

    def story(self, story_id: str) -> FanoutStory | None:
        """Look up a story by ID."""
        if self._plan is None:
            return None
        for s in self._plan.stories:
            if s.id == story_id:
                return s
        return None

    def reset(self) -> None:
        """Hard reset to initial state, discarding all data."""
        self._state = FanoutState.IDLE
        self._plan = None
        self._history.clear()


def _get_ctx():
    """Best-effort plugin context lookup.

    The module is also imported directly by tests as ``fanout_fsm`` rather
    than as a package module, so the import has to tolerate both layouts.
    """
    try:
        from . import get_ctx as _pkg_get_ctx
        return _pkg_get_ctx()
    except Exception:
        return None


def _workdir():
    from pathlib import Path
    import os
    return Path(os.getenv("TERMINAL_CWD", os.getcwd()))


def _fanout_dir():
    return _workdir() / ".fanout"


def _plan_path():
    return _fanout_dir() / "plan.yaml"


def _stories_dir():
    return _fanout_dir() / "stories"


def _load_plan():
    import json
    try:
        import yaml as _yaml
        has_yaml = True
    except Exception:
        _yaml = None
        has_yaml = False

    pp = _plan_path()
    if not pp.exists():
        return None
    try:
        raw = pp.read_text()
        if has_yaml:
            return _yaml.safe_load(raw)
        if raw.strip().startswith("{"):
            return json.loads(raw)
        return None
    except Exception:
        return None


def _save_plan(plan: dict) -> None:
    import json
    try:
        import yaml as _yaml
        has_yaml = True
    except Exception:
        _yaml = None
        has_yaml = False

    pp = _plan_path()
    pp.parent.mkdir(parents=True, exist_ok=True)
    if has_yaml:
        pp.write_text(_yaml.dump(plan, default_flow_style=False))
    else:
        pp.write_text(json.dumps(plan, indent=2))


def _safe_slug(name: str) -> str:
    import re
    slug = name.replace(" ", "-").lower()
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    return slug or "untitled"


def _save_stories(stories: list) -> None:
    import json
    try:
        import yaml as _yaml
        has_yaml = True
    except Exception:
        _yaml = None
        has_yaml = False

    sd = _stories_dir()
    sd.mkdir(parents=True, exist_ok=True)
    for s in stories:
        slug = _safe_slug(s.get("name", "untitled"))
        fname = f"{s.get('id', '000')}-{slug}"
        if has_yaml:
            (sd / f"{fname}.yaml").write_text(_yaml.dump(s, default_flow_style=False))
        else:
            (sd / f"{fname}.json").write_text(json.dumps(s, indent=2))


def _format_plan(plan: dict) -> str:
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
    import json
    try:
        data = json.loads(result_json)
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
        return data.get("final_response", data.get("error", ""))
    except Exception:
        return result_json if isinstance(result_json, str) else ""


def _parse_stories_json(text: str) -> list | None:
    import json
    import re
    if not text or not text.strip():
        return None
    clean = re.sub(r"^```(?:json)?\s*", "", text.strip())
    clean = re.sub(r"\s*```\s*$", "", clean)
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return data.get("stories") or None


def _decompose(task: str, critique: str = "") -> dict | None:
    ctx = _get_ctx()
    if ctx is None:
        return None

    crit_note = f"\n\nUSER CRITIQUE FROM PREVIOUS ROUND (address this):\n{critique}" if critique else ""
    result_json = ctx.dispatch_tool("delegate_task", {
        "goal": (
            f"{FANOUT_DECOMPOSE_ONLY}\n"
            f"{FANOUT_DECOMPOSE_CONSTRAINTS}\n\n"
            f"Decompose this task into 3-7 ordered stories. Each story must be "
            f"narrow enough to complete in one session.\n\n"
            f"Output ONLY valid JSON (no markdown):\n"
            f"{FANOUT_DECOMPOSE_EXAMPLE}\n\n"
            f"TASK:\n{task}{crit_note}"
        ),
        "context": FANOUT_DECOMPOSE_CONTEXT,
        "toolsets": [],
        "max_iterations": 2,
    })

    summary = _extract_subagent_summary(result_json)
    stories = _parse_stories_json(summary)
    if not stories:
        return None
    return {"task": task, "stories": stories, "completed": []}


def _sync_fsm_to_plan(task: str, plan: dict, fsm: FanoutReviewFSM) -> None:
    if fsm.state != FanoutState.IDLE:
        fsm.reset()
    fsm.start(task)
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
    fsm.decomposition_done(fsm_stories)


def _handle_new_task(task: str, fsm: FanoutReviewFSM) -> str:
    fsm.reset()
    plan = _load_plan()
    if plan and plan.get("stories"):
        existing_task = plan.get("task", "").strip()
        if existing_task == task.strip():
            _sync_fsm_to_plan(task, plan, fsm)
            return (
                f"Found existing .fanout/ plan for this task.\n\n"
                f"{_format_plan(plan)}\n\n"
                f"Commands: /fanout accept | critique <text> | abort | clear"
            )
        return (
            f"Found .fanout/ for a different task:\n"
            f"  Existing: {existing_task[:60]}\n"
            f"  New: {task[:60]}\n\n"
            f"Run /fanout clear first, then try again."
        )

    fsm.start(task)
    plan = _decompose(task)
    if plan is None:
        try:
            fsm.decomposition_fail()
        except FanoutTransitionError:
            pass
        return "Decomposition failed — subagent didn't return valid stories."

    _save_plan(plan)
    _save_stories(plan["stories"])

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
    fsm.decomposition_done(fsm_stories)

    return (
        f"Created {len(plan['stories'])} stories.\n\n"
        f"{_format_plan(plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_accept(fsm: FanoutReviewFSM) -> str:
    if fsm.state == FanoutState.IDLE:
        plan = _load_plan()
        if plan and plan.get("stories"):
            _sync_fsm_to_plan(plan["task"], plan, fsm)
        else:
            return "No plan to accept. Run /fanout <task> first."

    try:
        fsm.accept()
        fsm.execute()
    except FanoutTransitionError as e:
        return f"Cannot accept from state '{fsm.state.value}': {e}"

    plan = _load_plan()
    if not plan:
        return "Error: plan file missing."

    import datetime
    import shutil
    from pathlib import Path

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
    for lyric in FANOUT_ACCEPT_LYRICS:
        _emit(lines, lyric)

    for story in stories:
        sid = story.get("id", "?")
        sname = story.get("name", "unnamed")
        deps = story.get("dependencies", [])

        if sid in completed:
            lines.append(f"\nStory {sid} ({sname}) — already complete, skipping")
            try:
                fsm.story_done(sid)
            except FanoutTransitionError:
                pass
            continue

        blocked = [d for d in deps if d not in completed]
        if blocked:
            lines.append(f"\nStory {sid} ({sname}) — BLOCKED by {blocked}")
            continue

        _emit(lines, "", f"--- STORY {sid}: {sname} ---")
        for lyric in FANOUT_STORY_LYRICS:
            _emit(lines, lyric)

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

        from .deliver import run_deliver
        deliver_result = run_deliver(story_task)
        lines.append(deliver_result)

        completed.add(sid)
        plan["completed"] = sorted(completed)
        _save_plan(plan)

        if "COMPLETE" in deliver_result:
            _emit(lines, FANOUT_STORY_PASS, f"STORY {sid} PASS -- The sailor gave at least a try")
        else:
            _emit(lines, FANOUT_STORY_INCOMPLETE, f"STORY {sid} INCOMPLETE -- The soldier being much too wise")

        try:
            fsm.story_done(sid)
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
        fsm.all_stories_done()
    except FanoutTransitionError:
        pass

    _emit(lines, f"\n✓ Fanout complete: {len(completed)}/{len(stories)} stories done",
          f"Fanout complete: {len(completed)}/{len(stories)} stories done")
    lines.append(f"Journal: {journal_path}")
    _emit(lines, FANOUT_STORY_END)
    return "\n".join(lines)


def _handle_critique(text: str, fsm: FanoutReviewFSM) -> str:
    if not text.strip():
        return "Usage: /fanout critique <your feedback>"

    if fsm.state == FanoutState.IDLE:
        plan = _load_plan()
        if plan and plan.get("stories"):
            _sync_fsm_to_plan(plan["task"], plan, fsm)
        else:
            return "No plan to critique. Run /fanout <task> first."

    try:
        fsm.critique(text)
        fsm.re_decompose()
    except FanoutTransitionError as e:
        return f"Cannot critique from state '{fsm.state.value}': {e}"

    plan = _load_plan()
    if not plan:
        return "Error: lost the plan during critique."

    task = plan["task"]
    accumulated = fsm.accumulated_critique
    new_plan = _decompose(task, critique=accumulated)
    if new_plan is None:
        try:
            fsm.decomposition_fail()
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
    fsm.decomposition_done(fsm_stories)

    return (
        f"Re-decomposed with critique ({len(fsm.plan.critique_history)} rounds).\n\n"
        f"{_format_plan(new_plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_status(fsm: FanoutReviewFSM) -> str:
    plan = _load_plan()
    if not plan:
        return "No .fanout/ plan found. Run /fanout <task> to start."

    _feedback(FANOUT_STATUS)
    return (
        f"FSM state: {fsm.state.value}\n\n"
        f"{_format_plan(plan)}\n\n"
        f"Commands: /fanout accept | critique <text> | abort | clear"
    )


def _handle_abort(fsm: FanoutReviewFSM) -> str:
    try:
        fsm.abort()
    except FanoutTransitionError:
        pass
    fsm.reset()
    return "Fanout aborted. Plan files kept in .fanout/ — use /fanout clear to remove."


def _handle_clear(fsm: FanoutReviewFSM) -> str:
    import datetime
    import shutil
    from pathlib import Path

    _feedback(FANOUT_CLEAR_LYRICS[0])
    _feedback(FANOUT_CLEAR_LYRICS[1])
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

    fsm.reset()
    return msg


_SUBCOMMANDS = {
    "accept": _handle_accept,
    "critique": _handle_critique,
    "status": _handle_status,
    "abort": _handle_abort,
    "clear": _handle_clear,
}


def handle_fanout(raw_args: str) -> str:
    """Slash command handler for /fanout."""
    raw_args = raw_args.strip()
    fsm = FanoutReviewFSM()

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

    first_word = raw_args.split(maxsplit=1)[0].lower()
    if first_word in _SUBCOMMANDS:
        remaining = raw_args[len(first_word):].strip()
        return _SUBCOMMANDS[first_word](remaining, fsm)

    return _handle_new_task(raw_args, fsm)

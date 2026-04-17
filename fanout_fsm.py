"""Finite State Machine for the /fanout command review workflow."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .plugin_output import _feedback

# String resources used by the /fanout plugin.
FANOUT_ACCEPT_LYRICS = [
    "  \"\"\"",
    "  The sailor gave at least a try",
    "  \"\"\"",
]

FANOUT_STORY_LYRICS = [
    "  --- STORY {}: {} ---",
    "  The sailor gave at least a try",
    "  The soldier being much too wise",
]

FANOUT_STATUS = "FANOUT_STATUS"
FANOUT_CLEAR_LYRICS = [
    "  Clearing .fanout/ …",
    "  Clean.",
]

FANOUT_DECOMPOSE_ONLY = "DECOMPOSE ONLY"
FANOUT_DECOMPOSE_CONTEXT = "DECOMPOSE WITH CONTEXT"
FANOUT_DECOMPOSE_EXAMPLE = "Example: …"

class FanoutState(Enum):
    IDLE = "idle"
    DECOMPOSING = "decomposing"
    PLAN_READY = "plan_ready"
    EDITING = "editing"
    CRITIQUING = "critiquing"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    ABORTED = "aborted"

@dataclass
class FanoutStory:
    name: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "dependencies": self.dependencies,
            "acceptance": self.acceptance,
        }

class FanoutPlan:
    task: str
    stories: list[FanoutStory]
    completed: set[str] = field(default_factory=set)
    critique_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "stories": [s.to_dict() for s in self.stories],
            "completed": list(self.completed),
            "critique_history": self.critique_history,
        }

def _fanout_dir() -> Path:
    home = Path.home()
    return home / ".hermes" / "plugins" / "hermes-deliver" / ".fanout"

def _feedback(msg: str) -> None:
    _feedback(msg)

def _emit(lines: list, persistent: str, live: str = None) -> None:
    lines.append(persistent)
    _feedback(live if live is not None else persistent)

class FanoutReviewFSM:
    """State machine for the /fanout command review workflow."""

    Usage = (
        "Usage:\\n"
        "  /fanout <task description>   — decompose into stories\\n"
        "  /fanout accept               — execute the plan\\n"
        "  /fanout critique <feedback>   — re-decompose with feedback\\n"
        "  /fanout status               — show current plan\\n"
        "  /fanout abort                — abort execution\\n"
        "  /fanout clear                — remove .fanout/ directory"
    )

    def __init__(self) -> None:
        self._state = FanoutState.IDLE
        self._plan: FanoutPlan | None = None
        self._history: list[str] = []
        self._aborted = False
        self._editing = False
        self._critique_accumulated: str = ""

    def _transition(self, event: str) -> None:
        transitions = {
            FanoutState.IDLE: {
                "decompose": FanoutState.DECOMPOSING,
                "abort": FanoutState.ABORTED,
            },
            FanoutState.DECOMPOSING: {
                "decomposition_done": FanoutState.PLAN_READY,
            },
            FanoutState.PLAN_READY: {
                "accept": FanoutState.CONFIRMED,
                "edit": FanoutState.EDITING,
                "resume_review": FanoutState.PLAN_READY,
                "critique": FanoutState.CRITIQUING,
                "abort": FanoutState.ABORTED,
            },
            FanoutState.EDITING: {
                "resume_review": FanoutState.PLAN_READY,
                "abort": FanoutState.ABORTED,
            },
            FanoutState.CRITIQUING: {
                "accept": FanoutState.CONFIRMED,
                "edit": FanoutState.EDITING,
                "abort": FanoutState.ABORTED,
            },
            FanoutState.CONFIRMED: {
                "execute": FanoutState.EXECUTING,
                "abort": FanoutState.ABORTED,
            },
            FanoutState.EXECUTING: {
                "story_done": FanoutState.EXECUTING,
            },
            FanoutState.ABORTED: {},
        }
        state_transitions = transitions.get(self._state, {})
        if event in state_transitions:
            self._state = state_transitions[event]
        else:
            valid = ", ".join(
                f"{s.value} -> {e}" for s, evs in transitions.items() for e in evs
            )
            raise ValueError(f"Invalid transition from {self._state.value}: {event}. Valid: {valid}")

    def _ensure_dir(self) -> Path:
        fd = _fanout_dir()
        fd.mkdir(parents=True, exist_ok=True)
        return fd

    def decomposition_done(self, stories: list[FanoutStory]) -> None:
        """DECOMPOSING → PLAN_READY."""
        self._transition("decomposition_done")
        if self._plan is None:
            self._plan = FanoutPlan()
        self._plan.stories = stories
        self._history.append("decomposition_done")

    def accept(self) -> None:
        self._transition("accept")
        self._history.append("accept")
        self._editing = False

    def edit(self) -> None:
        self._transition("edit")
        self._editing = True

    def resume_review(self) -> None:
        """USER EDITING → PLAN_READY."""
        self._transition("resume_review")
        self._editing = False

    def critique(self, text: str) -> None:
        """USER CRITIQUE → accumulate then optionally re-decompose."""
        self._critique_accumulated += text + "\n"
        self._history.append(f"critique: {text[:60]}")

    def execute(self) -> None:
        self._transition("execute")
        self._history.append("execute")

    def story_done(self, sid: str) -> None:
        if self._plan:
            self._plan.completed.add(sid)

    def abort(self) -> None:
        self._transition("abort")
        self._aborted = True

    def reset(self) -> None:
        self._state = FanoutState.IDLE
        self._plan = None
        self._history.clear()
        self._aborted = False
        self._editing = False
        self._critique_accumulated = ""

    @property
    def state(self) -> FanoutState:
        return self._state

    def prompt_for_decompose(self) -> str:
        return (
            "DECOMPOSE ONLY\n"
            "You are a senior architect doing ONLY decomposition. "
            "You MUST NOT execute the task — only break it into stories. "
            "Output ONLY a raw JSON object with no markdown fences."
        )

    def prompt_for_critique(self, critique: str) -> str:
        return (
            f"Read the files. Run the tests yourself. "
            f"Output ONLY valid JSON. Address these points: {critique[:300]}"
        )

    def build_status_message(self) -> str:
        fd = self._fanout_dir()
        if not fd.exists():
            return "No .fanout/ directory found."
        plan_file = fd / "plan.yaml"
        if plan_file.exists():
            return f"Plan exists at {plan_file}"
        return ".fanout/ directory exists but no plan.yaml yet."

_SUBCOMMANDS = {
    "accept": lambda rem, fsm: _handle_accept(fsm),
    "critique": lambda rem, fsm: _handle_critique(fsm),
    "status": lambda rem, fsm: _handle_status(fsm),
    "abort": lambda rem, fsm: _handle_abort(fsm),
    "clear": lambda rem, fsm: _handle_clear(fsm),
}

def _handle_accept(fsm: FanoutReviewFSM) -> str:
    if fsm.state != FanoutState.PLAN_READY:
        return f"Cannot accept in state {fsm.state.value}"
    fsm.accept()
    return "Plan accepted. Ready to execute."

def _handle_critique(fsm: FanoutReviewFSM) -> str:
    if fsm.state not in (FanoutState.PLAN_READY, FanoutState.EDITING, FanoutState.CRITIQUING):
        return f"Cannot critique in state {fsm.state.value}"
    return fsm.prompt_for_critique(fsm._critique_accumulated)

def _handle_status(fsm: FanoutReviewFSM) -> str:
    return fsm.build_status_message()

def _handle_abort(fsm: FanoutReviewFSM) -> str:
    fsm.abort()
    return "Fanout aborted."

def _handle_clear(fsm: FanoutReviewFSM) -> str:
    fd = _fanout_dir()
    if fd.exists():
        import shutil, datetime
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

def _handle_new_task(raw_args: str, fsm: FanoutReviewFSM) -> str:
    fsm.decomposition_done([FanoutStory(
        name="story_001",
        description=raw_args,
        dependencies=[],
        acceptance=["produces correct output"],
    )])
    return "Decomposition started. Use /fanout status to review."

if __name__ == "__main__":
    print(handle_fanout("test task"))

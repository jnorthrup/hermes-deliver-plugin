"""Actor-critic delivery loop for /deliver command.

Spawns worker + critic subagents via dispatch_tool("delegate_task"),
loops up to max_rounds until the critic returns verdict COMPLETE.
"""

import json
import re

from . import get_ctx

MAX_ROUNDS = 5


def _feedback(msg: str) -> None:
    """Print immediate feedback through prompt_toolkit's ANSI renderer."""
    try:
        from cli import _cprint, _DIM, _RST
        _cprint(f"  {_DIM}{msg}{_RST}")
    except Exception:
        print(f"  {msg}", flush=True)


def _emit(lines: list, persistent: str, live: str = None) -> None:
    """Dual output: append to lines (persistent) and print immediate feedback."""
    lines.append(persistent)
    _feedback(live if live is not None else persistent)


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


def _parse_verdict(critic_output: str) -> dict:
    """Extract verdict JSON from critic response."""
    match = re.search(r'\{[^{}]*"verdict"[^{}]*\}', critic_output, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(critic_output.strip())
    except (json.JSONDecodeError, TypeError):
        return {"verdict": "EDIT", "feedback": critic_output[:500],
                "demands": ["unclear verdict from critic"]}


def run_deliver(task: str, max_rounds: int = MAX_ROUNDS) -> str:
    """Run the actor-critic delivery loop. Returns a summary string."""
    previous_output = None
    feedback = None
    action = "RESTART"
    lines = []

    lines.append(f"Deliver: {task[:80]}{'...' if len(task) > 80 else ''}")
    lines.append(f"Max rounds: {max_rounds}")

    for rnd in range(1, max_rounds + 1):
        lines.append(f"\n── Round {rnd}/{max_rounds} ──")

        # --- WORKER ---
        if action == "RESTART" or previous_output is None:
            worker_goal = (
                f"TASK:\n{task}\n\n"
                f"Implement now. Write complete code. No TODOs, no stubs, no placeholders.\n"
                f"Include tests if the task implies correctness requirements.\n"
                f"Run your tests before finishing."
            )
        else:
            worker_goal = (
                f"TASK:\n{task}\n\n"
                f"PREVIOUS IMPLEMENTATION:\n{previous_output}\n\n"
                f"CRITIC FEEDBACK — address ALL of these:\n{feedback}\n\n"
                f"Edit the previous implementation. Do not regress on anything working."
            )

        _emit(lines, "  Worker starting...", f"  [{rnd}/{max_rounds}] Worker starting...")
        worker_result = _dispatch(
            goal=worker_goal,
            context="You are a senior implementation agent. Do the work — write code, run tests, fix errors. No describing, only doing.",
            toolsets=["terminal", "file", "web"],
        )
        _emit(lines, f"  Worker done ({len(worker_result)} chars)", f"  [{rnd}/{max_rounds}] Worker done ({len(worker_result)} chars)")

        # --- CRITIC ---
        _emit(lines, "  Critic reviewing...", f"  [{rnd}/{max_rounds}] Critic reviewing...")
        critic_goal = (
            f"TASK:\n{task}\n\n"
            f"WORKER TRANSCRIPT:\n{worker_result}\n\n"
            f"Read the files. Run the tests. Output ONLY valid JSON — no markdown, no commentary outside JSON.\n"
            f'If fundamentally wrong: {{"verdict": "RESTART", "reason": "...", "guidance": "..."}}\n'
            f'If right but incomplete: {{"verdict": "EDIT", "feedback": "...", "demands": ["..."]}}\n'
            f'If fully complete: {{"verdict": "COMPLETE", "critique": "...", "score": <1-10>}}'
        )

        critic_result = _dispatch(
            goal=critic_goal,
            context="You are a code reviewer. Read the files the worker touched. Run the tests yourself. Form your own verdict from what you see, not what the transcript claims. Output ONLY valid JSON.",
            toolsets=["terminal", "file"],
            max_iterations=15,
        )

        verdict = _parse_verdict(critic_result)
        v = verdict.get("verdict", "EDIT").upper()
        _emit(lines, f"  Verdict: {v}", f"  [{rnd}/{max_rounds}] Verdict: {v}")

        if v == "COMPLETE":
            score = verdict.get("score", "?")
            critique = verdict.get("critique", "")
            lines.append(f"\n✓ COMPLETE — score: {score}/10")
            lines.append(f"  Critique: {critique}")
            return "\n".join(lines)

        elif v == "RESTART":
            previous_output = None
            feedback = verdict.get("guidance", verdict.get("reason", ""))
            lines.append(f"  RESTART — {verdict.get('reason', '')[:100]}")
        else:  # EDIT
            previous_output = worker_result
            feedback = json.dumps(verdict, indent=2)
            demands = verdict.get("demands", [])
            lines.append(f"  EDIT — {len(demands)} demand(s)")

    lines.append(f"\n⚠ Max rounds ({max_rounds}) hit. Spec may need work.")
    return "\n".join(lines)


def handle_deliver(raw_args: str) -> str:
    """Slash command handler for /deliver."""
    task = raw_args.strip()
    if not task:
        return (
            "Usage: /deliver <task description>\n"
            "Example: /deliver Implement connection pooling in src/http.py"
        )
    return run_deliver(task)

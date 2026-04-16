"""hermes-deliver-plugin — /deliver and /fanout slash commands for Hermes Agent.

/deliver <task>  — Actor-critic delivery loop. Spawns a worker subagent to
    implement and a critic subagent to review, looping until the critic says
    COMPLETE (or max rounds hit).

/fanout <task>   — Decomposes a task into dependency-ordered stories, presents
    a plan for review, then executes each story through /deliver.
    Subcommands: /fanout accept | critique <text> | status | abort | clear

Both commands run *outside* the agent conversation loop — they orchestrate
subagents imperatively via dispatch_tool("delegate_task") without touching
the parent agent's context or iteration budget.

Install:
    git clone https://github.com/NousResearch/hermes-deliver-plugin ~/.hermes/plugins/hermes-deliver

Original concept by @jnorthrup (PR #10240).
"""

_ctx = None


def register(ctx):
    """Called by the Hermes plugin system during discovery."""
    global _ctx
    _ctx = ctx

    from .deliver import handle_deliver
    from .fanout import handle_fanout

    ctx.register_command(
        "deliver",
        handler=handle_deliver,
        description="Actor-critic delivery loop — worker implements, critic reviews, loops until COMPLETE",
    )
    ctx.register_command(
        "fanout",
        handler=handle_fanout,
        description="Decompose task into stories, review plan, execute with /deliver per story",
    )


def get_ctx():
    """Return the plugin context (captured during register())."""
    return _ctx

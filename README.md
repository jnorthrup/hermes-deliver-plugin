# hermes-deliver-plugin

Actor-critic delivery loop (`/deliver`) and story decomposition with dependency-aware execution (`/fanout`) for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## What it does

**`/deliver <task>`** — Spawns a worker subagent to implement the task and a critic subagent to review it. Loops until the critic returns COMPLETE or max rounds (5) are hit. Both agents have full access to terminal, files, and web — they write real code, run real tests.

**`/fanout <task>`** — Breaks a large task into 3-7 dependency-ordered stories via a decomposition subagent, lets you review/critique the plan, then executes each story through `/deliver`. State persists to `.fanout/` on disk for resume across sessions.

### Key design

Both commands run *outside* the agent conversation loop. They orchestrate subagents imperatively via `dispatch_tool("delegate_task")` — the parent agent's context window, message history, and iteration budget are untouched.

## Install

```bash
git clone https://github.com/NousResearch/hermes-deliver-plugin ~/.hermes/plugins/hermes-deliver
```

The plugin is auto-discovered on next Hermes launch. Verify with `hermes plugins`.

## Usage

### /deliver

```
/deliver Implement connection pooling in src/http.py
```

Runs up to 5 rounds of worker → critic → feedback loop. Output shows each round's verdict and final score.

### /fanout

```
/fanout Build a web scraper with HTTP client, HTML parser, rate limiter, storage layer, and CLI
```

This decomposes the task and shows a plan. Then use subcommands:

| Subcommand | Description |
|---|---|
| `/fanout accept` | Execute all stories in dependency order |
| `/fanout critique <text>` | Re-decompose with your feedback |
| `/fanout status` | Show current plan and progress |
| `/fanout abort` | Stop execution (keeps files) |
| `/fanout clear` | Remove `.fanout/` directory |

The `.fanout/` directory contains:
- `plan.yaml` — the full plan with completion tracking
- `stories/` — individual story files
- `journal.md` — completion log with timestamps

### Resume

If you close Hermes mid-execution, the `.fanout/` state persists. Run `/fanout accept` again to resume from where you left off — completed stories are skipped automatically.

## Requirements

- Hermes Agent ≥ 0.30.0 (needs `register_command()` and `dispatch_tool()` on plugin context)
- No additional dependencies (YAML is optional — falls back to JSON)

## Development

Run the FSM tests:

```bash
cd hermes-deliver-plugin
python -m pytest tests/ -q
```

## Credits

Original concept and implementation by [@jnorthrup](https://github.com/jnorthrup) ([PR #10240](https://github.com/NousResearch/hermes-agent/pull/10240)). Adapted into plugin form by the Hermes team.

 * The Grateful Dead, Terrapin Station 1977
    - a love story around getting a *fan out*
    
## License

MIT

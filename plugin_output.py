"""Shared plugin output helpers for Hermes Deliver.

These are intentionally tiny: live progress goes through Hermes' ANSI-aware
renderer when available, while persistent text is collected separately.
"""


def _feedback(msg: str) -> None:
    """Print immediate feedback through Hermes' ANSI-aware renderer."""
    try:
        from cli import _cprint, _DIM, _RST
        _cprint(f"  {_DIM}{msg}{_RST}")
    except Exception:
        print(f"  {msg}", flush=True)


def _emit(lines: list, persistent: str, live: str = None) -> None:
    """Append persistent text and emit live feedback."""
    lines.append(persistent)
    _feedback(live if live is not None else persistent)

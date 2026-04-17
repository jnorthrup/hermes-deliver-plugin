"""Thin wrapper for /fanout.

The full fanout FSM, string resources, plan persistence, and execution logic
live in fanout_fsm.py so the plugin scope stays small and the hard-capture
prompt resources stay centralized.
"""

from .fanout_fsm import handle_fanout

"""The guided search conversation (spec §6.2-§6.4).

Split out of handlers/search_flow.py, which had grown to 764 lines and
held four of the whole-branch review's findings. Layer 3b moves
run_and_report here too, alongside the results rewrite that has to touch
its tests anyway.
"""
from handlers.search.builder import build_search_conversation

__all__ = ["build_search_conversation"]

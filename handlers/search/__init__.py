"""The guided search conversation (spec §6.2-§6.4).

Split out of the old handlers/search_flow.py, which had grown to 764 lines
and held four of the whole-branch review's findings. Its engine-facing half,
run_and_report, now lives in handlers/results.py.

The re-export below is lazy on purpose. Importing it eagerly would run
builder.py -- and therefore telegram -- on any `import
handlers.search.draft`, which would make the telegram-free boundary
that draft.py and dates.py are built around impossible to verify.
"""

__all__ = ["build_search_conversation"]


def __getattr__(name):
    if name == "build_search_conversation":
        from handlers.search.builder import build_search_conversation
        return build_search_conversation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

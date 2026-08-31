"""SQLite with loadable-extension support (needed for sqlite-vec).

Apple's system Python ships sqlite3 without enable_load_extension; uv-managed
CPython has it. Prefer pysqlite3 when importable (always extension-capable),
else verify the stdlib module can load extensions and fail loudly if not.
"""

try:
    import pysqlite3 as sqlite3  # type: ignore[import-not-found]
except ImportError:
    import sqlite3

    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        raise ImportError(
            "this Python's sqlite3 cannot load extensions; "
            "run mem under a uv-managed Python (uv tool install) "
            "or install pysqlite3-binary"
        ) from None

__all__ = ["sqlite3"]

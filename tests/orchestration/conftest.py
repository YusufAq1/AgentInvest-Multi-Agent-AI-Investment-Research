"""Event-loop setup for tests/orchestration/.

WHY: LangGraph's AsyncPostgresSaver uses psycopg 3, whose async mode refuses
to run on Windows' default ProactorEventLoop. The application works around
this in scripts/run_research.py with a SelectorEventLoop, and this hook
does the same here so test_checkpointer_postgres.py can talk to the real
checkpointer on a Windows dev machine.

WHY every test in this directory, not just that one module: once this
hook exists, pytest-asyncio 1.4 requires it to return a factory for EVERY
test it covers (None is rejected with a UsageError). The graph tests don't
care which loop they get, and a selector loop is what the real CLI uses on
Windows anyway. On Linux (CI) and macOS this returns the standard loop.
"""

import asyncio
import sys
from collections.abc import Mapping

import pytest
from pytest_asyncio.plugin import LoopFactory


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, LoopFactory]:
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}

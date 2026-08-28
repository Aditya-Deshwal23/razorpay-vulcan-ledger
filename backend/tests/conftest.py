"""
Shared pytest fixtures for the Razorpay Vulcan Ledger backend suite.

The one thing that genuinely needs to be shared is engine lifecycle. Production
runs one asyncio event loop for the life of the process, so a module-level
AsyncEngine with a long-lived connection pool is exactly right there. pytest is
the opposite: pytest-asyncio (auto mode) gives every test its own event loop,
tears it down at the end of the test, and the pooled asyncpg connections created
under the previous loop are then unusable -- the second database test in a module
fails with `RuntimeError: Event loop is closed` from deep inside asyncpg's
connection teardown.

The symptom looks like a bug in whatever test happens to run second, which is
why this is worth a fixture and a comment rather than a per-module workaround:
every test here passed in isolation before this file existed, and failed in
sequence.

Disposing the engine after each test costs one new connection per database test
(a few milliseconds against local Postgres) and keeps every test honest about
starting from a clean pool.
"""
import pytest

from config.database import engine


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """
    Return the shared AsyncEngine's pooled connections before the test's event
    loop is closed.

    Autouse and async, so it applies to every test in the suite -- including the
    purely synchronous ones, where disposing an already-empty pool is a no-op --
    without any test having to opt in or remember why.
    """
    yield
    await engine.dispose()

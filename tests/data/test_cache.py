"""Tests for backend.data.cache."""

from datetime import date
from pathlib import Path

from backend.data.cache import Cache, make_args_hash


def test_args_hash_is_stable_regardless_of_key_order() -> None:
    a = make_args_hash({"ticker": "AAPL", "cik": "0000320193"})
    b = make_args_hash({"cik": "0000320193", "ticker": "AAPL"})

    assert a == b


async def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache.sqlite3")
    as_of = date(2024, 6, 30)

    assert await cache.get(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of) is None

    await cache.set(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of, payload={"hello": "world"})
    result = await cache.get(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of)

    assert result == {"hello": "world"}


async def test_cache_keys_are_distinct_by_source_args_and_as_of(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache.sqlite3")
    as_of = date(2024, 6, 30)

    await cache.set(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of, payload={"v": 1})
    await cache.set(source="xbrl", args={"ticker": "MSFT"}, as_of=as_of, payload={"v": 2})
    await cache.set(
        source="xbrl", args={"ticker": "AAPL"}, as_of=date(2024, 1, 1), payload={"v": 3}
    )
    await cache.set(source="sec_8k", args={"ticker": "AAPL"}, as_of=as_of, payload={"v": 4})

    assert await cache.get(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of) == {"v": 1}
    assert await cache.get(source="xbrl", args={"ticker": "MSFT"}, as_of=as_of) == {"v": 2}
    assert await cache.get(source="xbrl", args={"ticker": "AAPL"}, as_of=date(2024, 1, 1)) == {
        "v": 3
    }
    assert await cache.get(source="sec_8k", args={"ticker": "AAPL"}, as_of=as_of) == {"v": 4}


async def test_cache_set_overwrites_existing_entry(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache.sqlite3")
    as_of = date(2024, 6, 30)

    await cache.set(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of, payload={"v": 1})
    await cache.set(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of, payload={"v": 2})

    assert await cache.get(source="xbrl", args={"ticker": "AAPL"}, as_of=as_of) == {"v": 2}

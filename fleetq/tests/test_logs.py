"""The numpi log cache (§6.4): offsets, generations, eviction gaps, completeness."""

from __future__ import annotations

import base64

import pytest

from fleetq.logs import LogCache

A = "att_0123456789abcdef01234567"


def text(chunk):
    return base64.b64decode(chunk["data_b64"])


def test_appends_and_reads_by_byte_offset(tmp_path):
    cache = LogCache(tmp_path)
    cache.append(A, "stdout", offset=0, data=b"hello ", remote_size=6, final=False)
    cache.append(A, "stdout", offset=6, data=b"w\xc3\xa9rld", remote_size=12, final=False)
    whole = cache.read(A, "stdout", offset=None, max_bytes=100)
    assert text(whole) == b"hello w\xc3\xa9rld" and whole["next_offset"] == 12 and not whole["complete"]
    # A chunk boundary may split a UTF-8 sequence: bytes are returned as they are.
    part = cache.read(A, "stdout", offset=8, max_bytes=2)
    assert text(part) == b"\xa9r" and part["next_offset"] == 10


def test_a_re_read_of_the_same_range_is_not_duplicated(tmp_path):
    cache = LogCache(tmp_path)
    cache.append(A, "stdout", offset=0, data=b"abc", remote_size=3, final=False)
    cache.append(A, "stdout", offset=0, data=b"abc", remote_size=3, final=False)
    cache.append(A, "stdout", offset=1, data=b"bcd", remote_size=4, final=False)   # not at our end: ignored
    assert text(cache.read(A, "stdout", offset=0, max_bytes=10)) == b"abc"


def test_complete_only_after_the_final_read_reaches_the_remote_size(tmp_path):
    cache = LogCache(tmp_path)
    cache.append(A, "stdout", offset=0, data=b"12345", remote_size=10, final=True)
    assert not cache.meta(A, "stdout")["complete"]
    cache.append(A, "stdout", offset=5, data=b"67890", remote_size=10, final=True)
    assert cache.read(A, "stdout", offset=0, max_bytes=0)["complete"]


def test_eviction_past_the_cap_is_an_explicit_gap(tmp_path):
    cache = LogCache(tmp_path, cap_bytes=100)
    for i in range(3):
        cache.append(A, "stdout", offset=i * 60, data=bytes([65 + i]) * 60, remote_size=(i + 1) * 60, final=False)
    meta = cache.meta(A, "stdout")
    assert meta["end"] == 180 and meta["base"] == 130 and meta["evicted"] == 130
    chunk = cache.read(A, "stdout", offset=0, max_bytes=1000)
    assert chunk["gap"] == {"from": 0, "to": 130, "reason": "evicted"}
    # 180 bytes written, 50 kept: only the newest 50 (all "C") remain.
    assert chunk["offset"] == 130 and text(chunk) == b"C" * 50


def test_eviction_keeps_the_newest_bytes(tmp_path):
    cache = LogCache(tmp_path, cap_bytes=100)
    cache.append(A, "stdout", offset=0, data=b"a" * 80, remote_size=80, final=False)
    cache.append(A, "stdout", offset=80, data=b"b" * 40, remote_size=120, final=False)
    chunk = cache.read(A, "stdout", offset=None, max_bytes=1000)
    assert chunk["gap"] is None and chunk["offset"] == 70
    assert text(chunk) == b"a" * 10 + b"b" * 40


def test_a_remote_file_that_shrinks_starts_a_new_generation(tmp_path):
    cache = LogCache(tmp_path)
    cache.append(A, "stderr", offset=0, data=b"first run\n", remote_size=10, final=False)
    old = cache.read(A, "stderr", offset=0, max_bytes=100)
    cache.append(A, "stderr", offset=0, data=b"new\n", remote_size=4, final=False)
    meta = cache.meta(A, "stderr")
    assert meta["generation"] == old["generation"] + 1 and meta["resets"] == 1
    again = cache.read(A, "stderr", offset=10, max_bytes=100, generation=old["generation"])
    assert again["reset"] is True and again["offset"] == 0 and text(again) == b"new\n"


def test_a_vanished_source_completes_what_we_have(tmp_path):
    cache = LogCache(tmp_path)
    cache.append(A, "stdout", offset=0, data=b"partial", remote_size=7, final=False)
    cache.mark_source_gone(A, "stdout")
    chunk = cache.read(A, "stdout", offset=0, max_bytes=100)
    assert chunk["complete"] and chunk["source_available"] is False and text(chunk) == b"partial"


def test_nothing_cached_reads_empty(tmp_path):
    chunk = LogCache(tmp_path).read(A, "stdout", offset=5, max_bytes=100)
    assert chunk["data_b64"] == "" and chunk["next_offset"] == 0 and chunk["age_s"] is None


@pytest.mark.parametrize("attempt,stream", [("../etc", "stdout"), ("att_x/../../y", "stdout"), (A, "passwd")])
def test_keys_cannot_escape_the_cache(tmp_path, attempt, stream):
    with pytest.raises(ValueError):
        LogCache(tmp_path).read(attempt, stream, offset=0, max_bytes=1)

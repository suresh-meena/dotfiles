"""Bundle upload serialization, quota, and publication race regressions."""

from __future__ import annotations

import asyncio
import gzip
import httpx
import pytest

from fleetq import auth, bundles
from fleetq.api import app as app_module
from fleetq.api.app import Runtime, create_app
from fleetq.executors.fake import FakeNode
from harness import Harness

RESERVE = 2 * 1024**3


@pytest.fixture()
def upload_env(tmp_path):
    h = Harness(tmp_path, [FakeNode("n1", gpus=["GPU-a"])])
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    controller = h.start()
    runtime = Runtime(store=h.store, controller=controller, bundle_dir=bundle_dir,
                      bundle_limits=bundles.BundleLimits(), clock_ok=lambda: True)
    app = create_app(runtime)
    yield h, runtime, app
    h.close()


def _request(app, owner_token, digest, body):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq") as client:
            return await client.put(f"/api/v1/bundles/{digest}", content=body,
                                    headers={"Authorization": f"Bearer {owner_token}"})
    return asyncio.run(run())


def _alternate_encoding(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "model.bin").write_bytes(b"weights" * 1000)
    canonical = tmp_path / "canonical.tar.gz"
    info = bundles.bundle_build(source, canonical)
    # Keep the canonical uncompressed tar bytes/digest while changing only the
    # gzip header/encoding. bundle_validate intentionally accepts this input.
    with gzip.open(canonical, "rb") as stream:
        tar_bytes = stream.read()
    alternate = gzip.compress(tar_bytes, compresslevel=0, mtime=1)
    assert len(alternate) != len(canonical.read_bytes())
    assert bundles.bundle_validate(tmp_path / "canonical.tar.gz").digest == info.digest
    altpath = tmp_path / "alternate.tar.gz"
    altpath.write_bytes(alternate)
    assert bundles.bundle_validate(altpath, expected_digest=info.digest).digest == info.digest
    return info.digest, canonical.read_bytes(), alternate


def test_concurrent_equivalent_encodings_keep_the_published_size_consistent(upload_env, tmp_path):
    h, runtime, app = upload_env
    digest, first, second = _alternate_encoding(tmp_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq") as client:
            headers = {"Authorization": f"Bearer {h.token}"}
            return await asyncio.gather(
                client.put(f"/api/v1/bundles/{digest}", content=first, headers=headers),
                client.put(f"/api/v1/bundles/{digest}", content=second, headers=headers),
            )

    responses = asyncio.run(run())
    assert [r.status_code for r in responses] == [200, 200]
    path = runtime.bundle_dir / f"{digest.split(':', 1)[1]}.tar.gz"
    row = h.store.run_sync(lambda c: c.execute(
        "SELECT compressed_bytes FROM bundles WHERE digest=?", (digest,)).fetchone())
    assert path.stat().st_size == row["compressed_bytes"]
    assert all(r.json()["compressed_bytes"] == row["compressed_bytes"] for r in responses)
    head = _request_head(app, h.token, digest)
    assert head.status_code == 200


def _request_head(app, token, digest):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq") as client:
            return await client.head(f"/api/v1/bundles/{digest}", headers={"Authorization": f"Bearer {token}"})
    return asyncio.run(run())


@pytest.mark.parametrize("scope", ["owner", "global", "token"])
def test_parallel_new_uploads_cannot_overrun_logical_byte_cap(upload_env, tmp_path, scope):
    h, runtime, app = upload_env
    source = tmp_path / "payload"
    source.mkdir()
    (source / "data").write_bytes(b"z" * 2048)
    bundle_path = tmp_path / "payload.tar.gz"
    built = bundles.bundle_build(source, bundle_path)
    body = bundle_path.read_bytes()
    # Make two independent content digests so the winner cannot be hidden by dedup.
    other = tmp_path / "other"
    other.mkdir()
    (other / "data").write_bytes(b"q" * 2048)
    other_path = tmp_path / "other.tar.gz"
    other_built = bundles.bundle_build(other, other_path)
    other_body = other_path.read_bytes()
    cap = built.compressed_bytes
    runtime.bundle_limits = bundles.BundleLimits(max_owner_bytes=cap if scope == "owner" else 10**9,
                                                  max_global_bytes=cap if scope == "global" else 10**9)
    token = h.token
    if scope == "token":
        _token_id, token = h.store.run_sync(lambda c: auth.create_token(
            c, owner="suresh", kind="human", label="limited-upload", quota={"bundle_bytes": cap}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq") as client:
            headers = {"Authorization": f"Bearer {token}"}
            return await asyncio.gather(
                client.put(f"/api/v1/bundles/{built.digest}", content=body, headers=headers),
                client.put(f"/api/v1/bundles/{other_built.digest}", content=other_body, headers=headers),
            )

    responses = asyncio.run(run())
    assert sorted(r.status_code for r in responses) == [200, 429]
    used = h.store.run_sync(lambda c: c.execute(
        "SELECT COALESCE(SUM(compressed_bytes),0) FROM bundles").fetchone()[0])
    assert used <= cap


def test_streaming_upload_keeps_two_gib_free_reserve(upload_env, tmp_path, monkeypatch):
    h, runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)

    class Stat:
        f_bavail = RESERVE + len(data)
        f_frsize = 1

    monkeypatch.setattr(app_module.os, "statvfs", lambda _path: Stat())
    response = _request(app, h.token, digest, data)
    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"
    assert h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]) == 0


def test_corrupt_existing_digest_fails_closed_instead_of_overwriting(upload_env, tmp_path):
    h, runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)
    assert _request(app, h.token, digest, data).status_code == 200
    path = runtime.bundle_dir / f"{digest.split(':', 1)[1]}.tar.gz"
    path.write_bytes(b"corrupt")

    retry = _request(app, h.token, digest, data)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "conflict"
    assert path.read_bytes() == b"corrupt"
    assert _request_head(app, h.token, digest).status_code == 404


def test_reupload_reactivates_a_released_owner_reference(upload_env, tmp_path):
    h, _runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)
    assert _request(app, h.token, digest, data).status_code == 200
    h.store.run_sync(lambda c: c.execute(
        "UPDATE bundle_refs SET released_at='2026-01-01T00:00:00Z' WHERE digest=? AND ref_id=?",
        (digest, h.token_id)))
    assert _request_head(app, h.token, digest).status_code == 404
    assert _request(app, h.token, digest, data).status_code == 200
    assert _request_head(app, h.token, digest).status_code == 200


def test_actual_stream_size_is_capped_without_content_length(upload_env, tmp_path):
    h, runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)
    runtime.bundle_limits = bundles.BundleLimits(max_compressed=len(data) - 1)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data

        async def aclose(self):
            pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fq") as client:
            return await client.put(f"/api/v1/bundles/{digest}", content=Body(),
                                    headers={"Authorization": f"Bearer {h.token}"})

    response = asyncio.run(run())
    assert response.status_code == 413 and response.json()["error"]["code"] == "bundle_too_large"
    assert h.store.run_sync(lambda c: c.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]) == 0


def test_symlinked_upload_tmp_directory_is_rejected_without_writing_external_target(upload_env, tmp_path):
    h, runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    sentinel = external / "keep"
    sentinel.write_text("untouched")
    (runtime.bundle_dir / "tmp").symlink_to(external, target_is_directory=True)

    response = _request(app, h.token, digest, data)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "unsafe_storage"
    assert sentinel.read_text() == "untouched"
    assert list(external.iterdir()) == [sentinel]


def test_owned_legacy_upload_tmp_directory_is_tightened(upload_env, tmp_path):
    h, runtime, app = upload_env
    digest, data, _alternate = _alternate_encoding(tmp_path)
    tmp_dir = runtime.bundle_dir / "tmp"
    tmp_dir.mkdir(mode=0o755)
    tmp_dir.chmod(0o755)

    response = _request(app, h.token, digest, data)

    assert response.status_code == 200
    assert tmp_dir.stat().st_mode & 0o777 == 0o700

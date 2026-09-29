"""Client transfer boundaries: no buffered bundle or unverified published output."""

from __future__ import annotations

import hashlib
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from fleetq import client


@pytest.mark.parametrize("url", [
    "http://fleetq.example", "http://10.0.0.2:8089", "http://localhost:8089",
    "https://user:pass@fleetq.example", "https://fleetq.example/path",
])
def test_client_refuses_unsafe_daemon_origin(tmp_path, monkeypatch, url):
    monkeypatch.setenv("FQ_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("FQ_URL", url)
    with pytest.raises(client.ClientError) as exc:
        client.load_client_config()
    assert exc.value.exit_code == 2


def test_client_accepts_https_and_literal_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("FQ_CONFIG", str(tmp_path / "absent.toml"))
    for url in ("https://fleetq.example/", "http://127.0.0.1:8089", "http://[::1]:8089"):
        monkeypatch.setenv("FQ_URL", url)
        assert client.load_client_config()["url"] == url.rstrip("/")


def test_bundle_upload_streams_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "train.py").write_text("print('ok')\n")

    class Api:
        def call(self, method, path):
            assert method == "HEAD" and path.startswith("/api/v1/bundles/sha256:")
            return 404, {}

        def ok(self, method, path, **kw):
            assert method == "PUT" and path.startswith("/api/v1/bundles/sha256:")
            stream = kw["data"]
            assert hasattr(stream, "read") and not isinstance(stream, bytes)
            assert int(kw["headers"]["Content-Length"]) == os.fstat(stream.fileno()).st_size
            assert stream.read(1)
            return {"ok": True}

    assert client._upload_bundle(Api(), source, []).startswith("sha256:")


def test_download_preserves_existing_file_until_hash_matches(tmp_path):
    dest = tmp_path / "result.bin"
    dest.write_bytes(b"original")
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    old_partial = tmp_path / "result.bin.fq-partial"
    old_partial.symlink_to(victim)
    api = client.Api("https://fleetq.example", "test-token")

    class Opener:
        def open(self, _request, *, timeout):
            return io.BytesIO(b"new payload")

    api.opener = Opener()
    with pytest.raises(client.ClientError) as exc:
        api.download("/artifact", dest, expected_sha256="0" * 64)
    assert exc.value.exit_code == 1
    assert dest.read_bytes() == b"original"
    assert victim.read_bytes() == b"untouched" and old_partial.is_symlink()
    assert not list(tmp_path.glob(".result.bin.fq-*"))

    digest = hashlib.sha256(b"new payload").hexdigest()
    with pytest.raises(client.ClientError, match="use --overwrite") as exc:
        api.download("/artifact", dest, expected_sha256=digest)
    assert exc.value.exit_code == 2
    assert dest.read_bytes() == b"original"
    assert not list(tmp_path.glob(".result.bin.fq-*"))

    assert api.download("/artifact", dest, expected_sha256=digest, overwrite=True) == digest
    assert dest.read_bytes() == b"new payload"
    assert victim.read_bytes() == b"untouched" and old_partial.is_symlink()


def test_download_no_clobber_refuses_symlink_and_overwrite_replaces_link(tmp_path):
    dest = tmp_path / "result.bin"
    victim = tmp_path / "important.bin"
    victim.write_bytes(b"keep me")
    dest.symlink_to(victim)
    api = client.Api("https://fleetq.example", "test-token")

    class Opener:
        def open(self, _request, *, timeout):
            return io.BytesIO(b"new payload")

    api.opener = Opener()
    digest = hashlib.sha256(b"new payload").hexdigest()
    with pytest.raises(client.ClientError, match="already exists") as exc:
        api.download("/artifact", dest, expected_sha256=digest)
    assert exc.value.exit_code == 2
    assert dest.is_symlink() and dest.resolve() == victim
    assert victim.read_bytes() == b"keep me"

    api.download("/artifact", dest, expected_sha256=digest, overwrite=True)
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"new payload"
    assert victim.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".result.bin.fq-*"))


def test_fetch_passes_explicit_overwrite_to_download(tmp_path):
    digest = hashlib.sha256(b"payload").hexdigest()

    class Api:
        def ok(self, method, path):
            return {"state": "COMPLETE", "files": [{
                "state": "COMPLETE", "attempt": 1, "relpath": "nested/result.bin",
                "size": 7, "sha256": digest}]}

        def download(self, path, dest, *, expected_sha256, overwrite):
            assert path.endswith("nested/result.bin?attempt=1")
            assert dest == tmp_path / "nested" / "result.bin"
            assert expected_sha256 == digest
            assert overwrite is True
            return digest

    args = SimpleNamespace(job=1, attempt=None, paths=None, output=str(tmp_path), json=False, overwrite=True)
    assert client.cmd_fetch(Api(), args) == 0


def test_fetch_rejects_completed_file_without_a_hash(tmp_path):
    class Api:
        def ok(self, method, path):
            return {"state": "COMPLETE", "files": [
                {"state": "COMPLETE", "attempt": 1, "relpath": "result.bin", "size": 3, "sha256": None}]}

        def download(self, *_args, **_kwargs):
            pytest.fail("an unverified artifact must not be downloaded or published")

    args = SimpleNamespace(job=1, attempt=None, paths=None, output=str(tmp_path), json=False)
    with pytest.raises(client.ClientError) as exc:
        client.cmd_fetch(Api(), args)
    assert exc.value.exit_code == 1
    assert not (tmp_path / "result.bin").exists()


def test_client_never_follows_a_redirect_with_bearer_token():
    forwarded = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            forwarded.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    destination = HTTPServer(("127.0.0.1", 0), Destination)

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/stolen")
            self.end_headers()

        def log_message(self, *_args):
            pass

    source = HTTPServer(("127.0.0.1", 0), Source)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (source, destination)]
    for thread in threads:
        thread.start()
    try:
        api = client.Api(f"http://127.0.0.1:{source.server_port}", "secret")
        status, _ = api.call("GET", "/redirect")
        assert status == 302
        assert forwarded == []
    finally:
        for server in (source, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()

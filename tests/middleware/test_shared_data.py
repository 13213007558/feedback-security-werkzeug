import email.utils
import os
import threading
from contextlib import closing

from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.test import create_environ
from werkzeug.test import run_wsgi_app


def test_get_file_loader():
    app = SharedDataMiddleware(None, {})
    assert callable(app.get_file_loader("foo"))


def _null_application(environ, start_response):
    start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
    yield b"NOT FOUND"


def _request(app, path, **kwargs):
    return run_wsgi_app(app, create_environ(path, **kwargs))


def test_shared_data_middleware(tmpdir):
    def null_application(environ, start_response):
        start_response("404 NOT FOUND", [("Content-Type", "text/plain")])
        yield b"NOT FOUND"

    test_dir = str(tmpdir)

    with open(os.path.join(test_dir, "äöü"), "w") as test_file:
        test_file.write("FOUND")

    for t in [list, dict]:
        app = SharedDataMiddleware(
            null_application,
            t(
                [
                    ("/", os.path.join(os.path.dirname(__file__), "..", "res")),
                    ("/sources", os.path.join(os.path.dirname(__file__), "..", "res")),
                    ("/pkg", ("werkzeug.debug", "shared")),
                    ("/foo", test_dir),
                ]
            ),
        )

        for p in "/test.txt", "/sources/test.txt", "/foo/äöü":
            app_iter, status, headers = run_wsgi_app(app, create_environ(p))
            assert status == "200 OK"

            if p.endswith(".txt"):
                content_type = next(v for k, v in headers if k == "Content-Type")
                assert content_type == "text/plain; charset=utf-8"

            with closing(app_iter) as app_iter:
                data = b"".join(app_iter).strip()

            assert data == b"FOUND"

        app_iter, status, headers = run_wsgi_app(
            app, create_environ("/pkg/debugger.js")
        )

        with closing(app_iter) as app_iter:
            contents = b"".join(app_iter)

        assert b"docReady(() =>" in contents

        for path in ("/missing", "/pkg", "/pkg/", "/pkg/missing.txt"):
            app_iter, status, headers = run_wsgi_app(app, create_environ(path))
            assert status == "404 NOT FOUND"
            assert b"".join(app_iter).strip() == b"NOT FOUND"


def _make_strict_app(root, opened=None):
    app = SharedDataMiddleware(_null_application, {"/static": str(root)}, strict=True)

    if opened is not None:
        strict_open = app._strict_open

        def tracked(directory, path):
            file, mtime, size = strict_open(directory, path)
            opened.append(file)
            return file, mtime, size

        app._strict_open = tracked

    return app


def test_strict_serves_file_and_nested_directory(tmp_path):
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "a.txt").write_text("top")
    (sub / "b.txt").write_text("nested")

    app = _make_strict_app(root)

    for path, expected in (
        ("/static/a.txt", b"top"),
        ("/static/sub/b.txt", b"nested"),
    ):
        app_iter, status, headers = _request(app, path)
        assert status == "200 OK"
        with closing(app_iter):
            assert b"".join(app_iter) == expected
        assert headers["ETag"]
        assert headers["Last-Modified"]


def test_strict_disabled_follows_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SECRET")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(secret, root / "link.txt")

    # The legacy loader follows the link and serves the external file.
    app = SharedDataMiddleware(_null_application, {"/static": str(root)})
    app_iter, status, _ = _request(app, "/static/link.txt")
    assert status == "200 OK"
    with closing(app_iter):
        assert b"".join(app_iter) == b"SECRET"


def test_strict_rejects_symlink_in_any_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    root = tmp_path / "root"
    root.mkdir()
    (root / "regular.txt").write_text("ok")

    # Symlink as the final segment.
    os.symlink(outside / "secret.txt", root / "file_link.txt")
    # Symlink as an intermediate directory segment.
    os.symlink(outside, root / "dir_link")

    app = _make_strict_app(root)

    for path in (
        "/static/file_link.txt",
        "/static/dir_link/secret.txt",
        "/static/missing.txt",
        "/static/",
    ):
        app_iter, status, headers = _request(app, path)
        assert status == "404 NOT FOUND"
        assert b"".join(app_iter) == b"NOT FOUND"
        assert headers.get("ETag") is None


def test_strict_rejects_directory_segment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "subdir").mkdir()

    app = _make_strict_app(root)

    app_iter, status, _ = _request(app, "/static/subdir")
    assert status == "404 NOT FOUND"
    with closing(app_iter):
        assert b"".join(app_iter) == b"NOT FOUND"


def test_strict_rejects_traversal_and_invalid_names(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("A")
    (root / "äöü.txt").write_text("UNICODE")

    app = _make_strict_app(root)

    # Path traversal is rejected by safe_join before the open.
    for path in ("/static/../a.txt", "/static/sub/../../a.txt"):
        app_iter, status, _ = _request(app, path)
        assert status == "404 NOT FOUND"
        with closing(app_iter):
            assert b"".join(app_iter) == b"NOT FOUND"

    # An embedded NUL byte fails the open but is still a 404, not a 500.
    app_iter, status, _ = _request(app, "/static/a.txt%00")
    assert status == "404 NOT FOUND"
    with closing(app_iter):
        assert b"".join(app_iter) == b"NOT FOUND"

    # Percent-encoded Unicode names still resolve to the real entry.
    app_iter, status, _ = _request(app, "/static/%C3%A4%C3%B6%C3%BC.txt")
    assert status == "200 OK"
    with closing(app_iter):
        assert b"".join(app_iter) == b"UNICODE"


def test_strict_toctou_symlink_swap_never_reads_external_file(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "f.txt"
    target.write_text("ORIGINAL")
    secret = outside / "secret.txt"
    secret.write_text("SECRET-CANARY")

    app = _make_strict_app(root)
    # Two rendezvous points: the request thread waits after the path lookup
    # (inside is_allowed) while the entry is replaced, then proceeds to the
    # race-safe open.
    barrier = threading.Barrier(2)

    def is_allowed(filename):
        os.unlink(target)
        os.symlink(secret, target)
        barrier.wait(timeout=5)
        barrier.wait(timeout=5)
        return True

    app.is_allowed = is_allowed  # type: ignore[method-assign]
    result = {}

    def request():
        app_iter, status, _ = _request(app, "/static/f.txt")
        result["status"] = status
        with closing(app_iter):
            result["body"] = b"".join(app_iter)

    thread = threading.Thread(target=request)
    thread.start()
    barrier.wait(timeout=5)
    barrier.wait(timeout=5)
    thread.join()

    assert result["status"] == "404 NOT FOUND"
    assert result["body"] == b"NOT FOUND"
    assert secret.read_text() == "SECRET-CANARY"


def test_strict_rename_after_open_keeps_old_descriptor_consistent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "f.txt"
    target.write_text("OLD-BYTES" * 4)
    old_stat = os.stat(target)
    opened = []

    app = _make_strict_app(root, opened)
    app_iter, status, headers = _request(app, "/static/f.txt")

    # Replace the directory entry (new inode) before the body is streamed.
    replacement = tmp_path / "new.txt"
    replacement.write_text("NEW-BYTES-DIFFERENT")
    os.replace(replacement, target)

    with closing(app_iter):
        body = b"".join(app_iter)

    assert status == "200 OK"
    # The held descriptor still refers to the original file.
    assert body == b"OLD-BYTES" * 4
    assert opened[0].closed
    # The caching metadata describes that same original file.
    assert headers["ETag"].endswith(f'-{old_stat.st_size}"')
    last_modified = email.utils.parsedate_to_datetime(headers["Last-Modified"])
    assert abs(last_modified.timestamp() - old_stat.st_mtime) < 1


def test_strict_if_none_match_returns_304_and_releases_descriptor(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("content")
    opened = []

    app = _make_strict_app(root, opened)
    app_iter, _, headers = _request(app, "/static/f.txt")
    etag = headers["ETag"]
    last_modified = headers["Last-Modified"]
    app_iter.close()

    app_iter, status, headers = _request(
        app, "/static/f.txt", headers={"If-None-Match": etag}
    )
    assert status == "304 Not Modified"
    assert b"".join(app_iter) == b""
    assert "Content-Length" not in headers
    # The descriptor opened for the conditional check is released.
    assert opened[-1].closed

    # A date-based conditional request takes the same 304 path.
    app_iter, status, _ = _request(
        app, "/static/f.txt", headers={"If-Modified-Since": last_modified}
    )
    assert status == "304 Not Modified"
    assert b"".join(app_iter) == b""
    assert opened[-1].closed


def test_strict_head_releases_descriptor_on_wsgi_close(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("content")
    opened = []

    app = _make_strict_app(root, opened)
    app_iter, status, headers = _request(app, "/static/f.txt", method="HEAD")

    assert status == "200 OK"
    assert headers["ETag"]
    with closing(app_iter) as iterator:
        assert b"".join(iterator) == b""
    assert opened[-1].closed


def test_strict_interrupted_iteration_releases_descriptor(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("x" * 300_000)
    opened = []

    app = _make_strict_app(root, opened)
    app_iter, status, _ = _request(app, "/static/f.txt")
    assert status == "200 OK"

    next(iter(app_iter))
    app_iter.close()

    assert opened[-1].closed


def test_strict_in_place_rewrite_is_not_a_snapshot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "f.txt"
    target.write_text("X" * 300_000)

    app = _make_strict_app(root)
    app_iter, _, _ = _request(app, "/static/f.txt")

    # Rewrite the same opened file (same inode) in place.
    with open(target, "r+b") as file:
        file.seek(131_072)
        file.write(b"Z" * 168_928)
        file.flush()
        os.fsync(file.fileno())

    with closing(app_iter):
        body = b"".join(app_iter)

    # The later bytes reflect the in-place rewrite; strict mode only pins the
    # descriptor, it does not snapshot the contents.
    assert body[:1] == b"X"
    assert b"Z" in body


def test_strict_does_not_apply_to_package_or_single_file(tmp_path):
    # Package exports keep using the resource loader regardless of strict mode.
    app = SharedDataMiddleware(
        _null_application,
        {"/pkg": ("werkzeug.debug", "shared")},
        strict=True,
    )
    app_iter, status, _ = _request(app, "/pkg/debugger.js")
    assert status == "200 OK"
    with closing(app_iter):
        assert b"docReady(() =>" in b"".join(app_iter)

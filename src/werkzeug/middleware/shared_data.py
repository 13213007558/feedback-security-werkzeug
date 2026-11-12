"""
Serve Shared Static Files
=========================

.. autoclass:: SharedDataMiddleware
    :members: is_allowed
"""

from __future__ import annotations

import collections.abc as cabc
import datetime as dt
import importlib.util
import os
import posixpath
import stat
import typing as t
import wsgiref.types as wt
from fnmatch import fnmatch
from io import BytesIO

from ..security import safe_join
from ..utils import send_file
from ..wrappers import Request

_TOpener = t.Callable[[], tuple[t.IO[bytes], dt.datetime, int]]
_TLoader = t.Callable[[str | None], tuple[str | None, _TOpener | None]]


class SharedDataMiddleware:
    """Serve static files during development. Use the HTTP server in front of
    the application to serve static files in production.

    Given a map of base URL paths to directories, this will match a requested
    file under a path to a file in a directory. Unmatched paths will be
    forwarded to the wrapped application.

    - ``"/static": "static"`` matches paths under the "static" directory.
    - ``"/robots.txt": "generated/robots.txt"`` matches a single path and file.
    - ``"/toolbar": ("toolbar", "static")`` matches paths under the "static"
      directory within the importable "toolbar" Python package.

    :param app: The application to use for paths not handled by the middleware.
        Using :exc:`.NotFound` will return 404 for any other path.
    :param exports: Map URL paths to a folder with static files. A ``str`` is a
        filesystem path to a directory or single file, relative to the current
        directory. A ``(package, path)`` tuple is a path within an importable
        Python package. URL path keys are matched in order.
    :param disallow: A pattern for :func:`~fnmatch.fnmatch`. If it matches the
        loaded filename, the file is not served.
    :param cache_timeout: Set the ``Cache-Control`` ``max-age`` directive. If
        not set, conditional caching is used, which is typically what you want.
    :param strict: Only applies to directory exports. Open the requested file
        relative to the stable export root without following symbolic links in
        any path segment, so a directory entry cannot be replaced to escape the
        root. The path check, the metadata used for caching (``ETag`` and
        ``Last-Modified``), and the bytes sent all refer to the single file
        descriptor produced by that open. Conditional responses are enabled.
        This guards against entry replacement and symlinks, not against an
        already open file being rewritten in place; an in-place modification is
        not an immutable snapshot and may be observed.

    .. versionchanged:: 3.2
        The ``cache`` and ``fallback_mimetype`` parameters are deprecated and
        will be removed in Werkzeug 4.0.

        The ``cache_timeout`` parameter is disabled by default, enabling
        conditional caching.

        Conditional and range requests are supported.

        Added the ``strict`` parameter for race-safe directory exports.

    .. versionchanged:: 1.0
        The default ``fallback_mimetype`` is ``application/octet-stream``
        If a filename looks like a text mimetype, the ``utf-8`` charset
        is added to it.

    .. versionadded:: 0.6
        Added ``fallback_mimetype``.

    .. versionchanged:: 0.5
        Added ``cache_timeout``.
    """

    def __init__(
        self,
        app: wt.WSGIApplication,
        exports: (
            cabc.Mapping[str, str | tuple[str, str]]
            | t.Iterable[tuple[str, str | tuple[str, str]]]
        ),
        disallow: str | None = None,
        cache: None = None,
        cache_timeout: int | None = None,
        fallback_mimetype: None = None,
        strict: bool = False,
    ) -> None:
        self.app = app
        self.exports: list[tuple[str, _TLoader]] = []
        self.cache = cache
        self.cache_timeout = cache_timeout
        self.fallback_mimetype = fallback_mimetype
        self.strict = strict

        if cache is not None:
            import warnings

            warnings.warn(
                "The 'cache' parameter is deprecated and will be removed"
                " in Werkzeug 4.0.",
                DeprecationWarning,
                stacklevel=2,
            )

        if fallback_mimetype is not None:
            import warnings

            warnings.warn(
                "The 'fallback_mimetype' parameter is deprecated and will be"
                " removed in Werkzeug 4.0.",
                DeprecationWarning,
                stacklevel=2,
            )

        if isinstance(exports, cabc.Mapping):
            exports = exports.items()

        for key, value in exports:
            if isinstance(value, tuple):
                loader = self.get_package_loader(*value)
            elif isinstance(value, str):
                if os.path.isfile(value):
                    loader = self.get_file_loader(value)
                else:
                    loader = self.get_directory_loader(value, strict=strict)
            else:
                raise TypeError(f"unknown def {value!r}")

            self.exports.append((key, loader))

        if disallow is not None:
            self.is_allowed = lambda x: x is not None and not fnmatch(x, disallow)  # type: ignore[method-assign,assignment]

    def is_allowed(self, filename: str | None) -> bool:
        """Subclasses can override this method to disallow the access to
        certain files.  However by providing `disallow` in the constructor
        this method is overwritten.
        """
        return True

    def _opener(self, filename: str) -> _TOpener:
        return lambda: (
            open(filename, "rb"),
            dt.datetime.fromtimestamp(os.path.getmtime(filename), tz=dt.UTC),
            os.path.getsize(filename),
        )

    def get_file_loader(self, filename: str) -> _TLoader:
        return lambda x: (os.path.basename(filename), self._opener(filename))

    def get_package_loader(self, package: str, package_path: str) -> _TLoader:
        load_time = dt.datetime.now(dt.UTC)
        spec = importlib.util.find_spec(package)
        reader = spec.loader.get_resource_reader(package)  # type: ignore[union-attr]

        def loader(
            path: str | None,
        ) -> tuple[str | None, _TOpener | None]:
            if path is None:
                return None, None

            path = safe_join(package_path, path)

            if path is None:
                return None, None

            basename = posixpath.basename(path)

            try:
                resource = reader.open_resource(path)
            except OSError:
                return None, None

            if isinstance(resource, BytesIO):
                return (
                    basename,
                    lambda: (resource, load_time, len(resource.getvalue())),
                )

            return (
                basename,
                lambda: (
                    resource,
                    dt.datetime.fromtimestamp(
                        os.path.getmtime(resource.name), tz=dt.UTC
                    ),
                    os.path.getsize(resource.name),
                ),
            )

        return loader

    def _strict_open(
        self, directory: str, path: str
    ) -> tuple[t.IO[bytes], dt.datetime, int]:
        """Open ``path`` under the trusted ``directory`` without following a
        symbolic link in any segment and without escaping the directory.

        Every segment is opened relative to a file descriptor for the previous
        directory, so a concurrent replacement of a directory entry (including
        swapping in a symlink to an outside file) cannot redirect the open.
        The returned metadata and bytes therefore describe the same open file
        descriptor.

        Only entry replacement and symlinks are covered. Rewriting the opened
        file in place is not prevented and does not produce an immutable
        snapshot.
        """
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        intermediate_fds: list[int] = []

        try:
            parent_fd = directory_fd
            parts = [part for part in path.split("/") if part]

            for part in parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | no_follow | os.O_DIRECTORY, dir_fd=parent_fd
                )
                intermediate_fds.append(next_fd)
                parent_fd = next_fd

            file_no = os.open(parts[-1], os.O_RDONLY | no_follow, dir_fd=parent_fd)
        finally:
            for fd in reversed(intermediate_fds):
                os.close(fd)

            os.close(directory_fd)

        file = t.cast(t.IO[bytes], os.fdopen(file_no, "rb"))

        try:
            file_stat = os.fstat(file_no)

            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("not a regular file")

            mtime = dt.datetime.fromtimestamp(file_stat.st_mtime, tz=dt.UTC)
            return file, mtime, file_stat.st_size
        except BaseException:
            file.close()
            raise

    def get_directory_loader(self, directory: str, strict: bool = False) -> _TLoader:
        def loader(
            path: str | None,
        ) -> tuple[str | None, _TOpener | None]:
            if path is not None:
                path = safe_join(directory, path)

                if path is None:
                    return None, None
            else:
                path = directory

            if strict:
                if path is None:
                    return None, None

                relative = os.path.relpath(path, directory)

                if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                    return None, None

                filename = os.path.basename(path)

                def opener() -> tuple[t.IO[bytes], dt.datetime, int]:
                    return self._strict_open(directory, relative)

                return filename, opener

            if os.path.isfile(path):
                return os.path.basename(path), self._opener(path)

            return None, None

        return loader

    @Request.application
    def __call__(self, request: Request) -> wt.WSGIApplication:
        path = request.path

        for search_path, loader in self.exports:
            if search_path == path:
                filename, opener = loader(None)

                if opener is not None:
                    break

            if not search_path.endswith("/"):
                search_path += "/"

            if path.startswith(search_path):
                filename, opener = loader(path[len(search_path) :])

                if opener is not None:
                    break
        else:
            return self.app

        if not self.is_allowed(filename):
            return self.app

        try:
            f, mtime, size = opener()
        except (OSError, ValueError):
            # In strict mode the entry may have been removed or replaced by a
            # symlink between lookup and the race-safe open. An invalid name
            # such as an embedded NUL byte fails the open as well. Treat these
            # as a missing resource rather than an unhandled server error.
            return self.app

        response = send_file(
            f,
            request.environ,
            download_name=filename,
            conditional=bool(self.cache) or self.strict,
            last_modified=mtime,
            max_age=self.cache_timeout,
        )

        if response.content_length is None:
            response.content_length = size

        if (
            self.fallback_mimetype is not None
            and response.mimetype == "application/octet-stream"
        ):
            response.mimetype = self.fallback_mimetype

        return response

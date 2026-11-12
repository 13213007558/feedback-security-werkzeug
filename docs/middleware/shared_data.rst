.. automodule:: werkzeug.middleware.shared_data

Strict Directory Exports
------------------------

A publishing directory may have its entries replaced atomically while a
request is being handled. Pass ``strict=True`` to make directory exports open
the requested file in a way that is safe against that race:

-   No segment of the requested path is followed as a symbolic link, so an
    entry cannot be swapped for a link that points outside the configured
    export root.
-   Each segment is opened relative to the file descriptor of the directory
    above it. The access check, the open, and the bytes read therefore cannot
    be redirected by replacing a directory entry in between; there is no
    separate "check the real path, then open the path" window.
-   The ``ETag``, ``Last-Modified``, size, and response body are all derived
    from that one open file descriptor. Conditional (``304 Not Modified``)
    and range responses are evaluated after the open, and every response,
    including a ``304`` or one that is only partially read, releases the
    descriptor it opened.

The export root itself comes from application configuration and is assumed
stable. ``strict`` only applies to directory exports; package resource and
single-file exports keep their existing behavior.

The guarantee is about *which file* is opened. Replacing a directory entry
cannot make a request read a different file, and after a rename the response
continues to use the descriptor it already opened. It is not an immutable
snapshot of the contents: if the same open file is rewritten in place (the
inode is kept, its bytes change), those changes may be observed while the
response is streamed. Treat publishing as "write a new file, then atomically
rename it into place" rather than mutating an exported file in place.

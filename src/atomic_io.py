"""Atomic JSON writes.

The bots persist state by rewriting a whole JSON document in place. Done with a
plain `write_text()` that is truncate-then-write, so any reader touching the file
mid-write sees a partial document — and these files are 17-27KB, several pages,
so the window is real rather than theoretical. `tools/status_json.py` hit exactly
this and has to defend against it on the read side.

Write to a sibling temp file, fsync it, then `os.replace()` into position.
`os.replace` is atomic on POSIX, so a reader sees either the whole previous
document or the whole new one — never a torn one. The temp file is a sibling so
the rename stays within one filesystem, which is what makes it atomic.

Two properties worth knowing before reusing this elsewhere:

  Permissions come from the temp file, not the destination. The replacement is
  a newly created file, so its mode is the process umask (0644 here, matching
  the existing bot:bot state files) rather than whatever the old file had. If a
  state file is ever tightened to 0600, this would silently widen it back on
  the next save.

  One writer per destination. The temp name is keyed on PID only, so two
  threads in the same process writing the same path would collide on one temp
  file and race. That is safe for the bots, which are single-process service
  loops with one writer per state file, but this is not a general-purpose
  concurrency-safe primitive. Multiple writers would need mkstemp-style unique
  names or external synchronisation.
"""
import json
import os
from pathlib import Path
from typing import Union


def write_json_atomic(path: Union[str, Path], obj, **dumps_kwargs) -> None:
    """Serialize `obj` to `path` atomically. Raises on failure, leaving any
    existing file untouched."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh, **dumps_kwargs)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Serialization or write failed — drop the partial temp file and let the
        # existing document stand rather than replacing it with garbage.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # Durability of the rename itself across power loss. Not required for reader
    # atomicity, and not supported everywhere, so failure here is not fatal.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass

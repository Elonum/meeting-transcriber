"""Sandbox compatibility shim (environment-only, not application code).

Why this exists
---------------
CPython 3.14 on Windows applies a real security descriptor when ``os.mkdir`` is
called with ``mode``. ``mode=0o700`` produces a protected DACL that grants the
current user only; any process running under a restricted token (AppContainer /
sandbox) then gets "access denied" for paths *inside* that directory, even
though it just created them. ``tempfile.mkdtemp`` uses ``0o700``, so ``pip``,
``ensurepip`` and every tool that unpacks into a temp directory die with
``PermissionError: [WinError 5]``.

This module is installed as ``sitecustomize.py`` inside the project virtualenv
so that directories inherit the parent ACL instead. It changes nothing about how
the application behaves on a normal machine, and it is never imported by the
application code itself — only by the interpreter at startup, inside this venv.

Delete ``<venv>/Lib/site-packages/sitecustomize.py`` if the venv is ever used
outside this sandbox.
"""

from __future__ import annotations

import os

_real_mkdir = os.mkdir


def _mkdir(path, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
    # Dropping ``mode`` keeps Windows inheritance intact; chmod is a no-op on
    # NTFS anyway, so no meaningful behaviour is lost.
    if dir_fd is not None:
        return _real_mkdir(path, dir_fd=dir_fd)
    return _real_mkdir(path)


os.mkdir = _mkdir  # type: ignore[assignment]

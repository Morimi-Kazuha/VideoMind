"""Response-owned media files and shared, expiring playback grants."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import time


async def joined_thread(function):
    """Cancellation joins the writer before its owner may delete the output."""
    task = asyncio.create_task(asyncio.to_thread(function))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve failures without replacing cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def _lock(handle):
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


class ResponseFiles:
    def __init__(self, parent: Path):
        self.root = (parent / "responses").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._handles = {}
        self.scavenge()

    def _remove(self, directory):
        # Never follow a foreign directory or a symlink out of this workspace.
        resolved = directory.resolve()
        if directory.is_symlink() or resolved.parent != self.root:
            raise ValueError("response directory is outside its owner")
        shutil.rmtree(resolved, ignore_errors=False)

    def scavenge(self):
        for directory in self.root.glob("response-*"):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                if time.time() - directory.stat().st_mtime < 86400:
                    continue
                with (directory / "owner.lock").open("r+b") as handle:
                    if not _lock(handle):
                        continue
                self._remove(directory)
            except (OSError, ValueError):
                continue  # An active or inaccessible owner is never guessed dead.

    def allocate(self, suffix):
        directory = self.root / ("response-" + secrets.token_hex(16))
        directory.mkdir()
        handle = (directory / "owner.lock").open("w+b")
        handle.write(b"1")
        handle.flush()
        if not _lock(handle):
            handle.close()
            raise OSError("response owner lock unavailable")
        path = directory / ("media" + suffix)
        self._handles[path] = handle
        return path

    def release(self, path):
        handle = self._handles.pop(path, None)
        if handle is not None:
            handle.close()
            self._remove(path.parent)

    def close(self):
        for path in tuple(self._handles):
            try:
                self.release(path)
            except OSError:
                pass  # Scavenged after this process releases its owner lock.


class RedisPlaybackGrants:
    TTL_SECONDS = 15 * 60

    def __init__(self, client):
        self.client = client

    @staticmethod
    def key(token):
        return "media:playback:" + hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, media_id, user_id):
        token = secrets.token_urlsafe(24)
        await asyncio.to_thread(self.client.set, self.key(token),
                                json.dumps([media_id, user_id]), ex=self.TTL_SECONDS)
        return token

    async def owner(self, media_id, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        raw = await asyncio.to_thread(self.client.get, self.key(token))
        if raw is None:
            return None
        stored_media, user = json.loads(raw)
        return user if stored_media == media_id and type(user) is int else None

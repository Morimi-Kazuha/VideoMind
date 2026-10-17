import asyncio
from unittest.mock import AsyncMock

import pytest
from dovideo.infrastructure.media.runner import AsyncSubprocessRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 60])
async def test_repeated_cancellation_joins_process_and_pipe_readers(monkeypatch, timeout):
    started, reaping, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Process:
        returncode = None
        drained = False
        def kill(self): pass
        async def wait(self):
            reaping.set()
            await exited.wait()
            self.returncode = -9
        async def communicate(self):
            started.set()
            await exited.wait()
            self.drained = True
            return b"", b""
    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(AsyncSubprocessRunner(default_timeout=timeout).run(("ffmpeg",)))
    await started.wait()
    task.cancel()
    await reaping.wait()
    task.cancel()
    await asyncio.sleep(.01)
    completed_before_exit = task.done()
    exited.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not completed_before_exit, "caller must not remove files until process and readers exit"
    assert process.returncode == -9 and process.drained

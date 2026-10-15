from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from dovideo.presentation.api.r4_runtime import ProductionR4Services
from dovideo.domain import TaskStatusState


@pytest.mark.asyncio
async def test_r4_transcript_reads_persisted_context_after_ownership_check():
    service=ProductionR4Services.__new__(ProductionR4Services)
    service.media=SimpleNamespace(require_owned=AsyncMock())
    service.checkpoint=SimpleNamespace(load_context=AsyncMock(return_value=SimpleNamespace(transcript_text=lambda:'Real stored ASR')))
    result=await service.transcription_status(3,4)
    service.media.require_owned.assert_awaited_once_with(3,4)
    assert result.state is TaskStatusState.COMPLETED and result.result=='Real stored ASR'


@pytest.mark.asyncio
async def test_no_context_never_reports_completed_transcription():
    service=ProductionR4Services.__new__(ProductionR4Services)
    service.media=SimpleNamespace(require_owned=AsyncMock())
    service.checkpoint=SimpleNamespace(load_context=AsyncMock(return_value=None))
    result=await service.transcription_status(3,4)
    assert result.state is TaskStatusState.NOT_STARTED and result.result is None


@pytest.mark.asyncio
async def test_unauthorized_transcript_never_loads_other_user_context():
    service=ProductionR4Services.__new__(ProductionR4Services)
    service.media=SimpleNamespace(require_owned=AsyncMock(side_effect=PermissionError('not owned')))
    service.checkpoint=SimpleNamespace(load_context=AsyncMock())
    with pytest.raises(PermissionError): await service.transcription_status(3,4)
    service.checkpoint.load_context.assert_not_awaited()

import hashlib
import json
from pathlib import Path
import pytest
from dovideo.presentation.api.app import create_app
from dovideo.presentation.api.runtime import LocalR1Services


@pytest.mark.asyncio
async def test_public_api_and_feature_defaults_match_pre_governance_baseline(tmp_path):
    baseline = json.loads(Path('tests/fixtures/governance/api-contract.json').read_text(encoding='utf-8'))
    service = LocalR1Services(work_dir=tmp_path)
    try:
        schema = create_app(services=service).openapi()
        digest = hashlib.sha256(json.dumps(schema,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        assert digest == baseline['openapi_sha256']
    finally:
        await service.shutdown()
    values = dict(line.split('=',1) for line in Path('.env.example').read_text(encoding='utf-8').splitlines()
                  if line and not line.startswith('#') and '=' in line)
    assert {k:values[k] for k in baseline['feature_defaults']} == baseline['feature_defaults']

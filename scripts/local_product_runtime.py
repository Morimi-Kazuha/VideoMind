"""Local launcher support. Read approved env files without printing secrets."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class PreflightError(RuntimeError):
    """Only safe component identifiers cross the diagnostic boundary."""
    def __init__(self, component: str, category: str):
        self.component = component
        self.category = category
        super().__init__('local product preflight failed')


def runtime_environment(settings: dict) -> dict[str, str]:
    env = dict(os.environ)
    for filename in settings['env_files']:
        path = Path(filename)
        if not path.is_absolute(): path = ROOT/path
        if not path.is_file():
            raise RuntimeError('private configuration file missing')
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if not match:
                raise RuntimeError('unsupported env syntax; use simple KEY=value lines')
            value = match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[match[1]] = value
    if env.get('DOVIDEO_PROFILE','').lower() != 'production':
        raise RuntimeError('local product requires explicit production profile')
    extras = [str(ROOT/'src'), *(str(Path(path) if Path(path).is_absolute() else ROOT/path) for path in settings.get('python_paths', []))]
    if env.get('PYTHONPATH'):
        extras.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(extras)
    defaults = settings.get('tool_settings', {})
    for name, value in defaults.items():
        if name in {'DOVIDEO_FFMPEG_DIR','DOVIDEO_TESSERACT_PATH','DOVIDEO_WHISPER_MODEL_ROOT'}:
            path=Path(value)
            value=str(path if path.is_absolute() else ROOT/path)
        env.setdefault(name, str(value))
    env.setdefault('DOVIDEO_R2_RUNTIME_ROOT',str(ROOT/'work/r2-runtime'))
    # Explicit local operator settings; never rewrite private provider files.
    for name, value in settings.get('runtime_overrides', {}).items():
        env[name] = str(value)
    ffmpeg_dir = env.get('DOVIDEO_FFMPEG_DIR')
    if ffmpeg_dir:
        env['PATH'] = ffmpeg_dir + os.pathsep + env.get('PATH','')
    env['PYTHONUNBUFFERED'] = '1'
    env['VITE_DEV_PROXY_TARGET'] = 'http://127.0.0.1:' + str(settings.get('api_port',8000))
    return env


def apply_environment(settings: dict) -> dict[str,str]:
    env = runtime_environment(settings)
    os.environ.update(env)
    for path in reversed(env['PYTHONPATH'].split(os.pathsep)):
        if path and path not in sys.path:
            sys.path.insert(0,path)
    return env


async def check(settings: dict) -> dict:
    from sqlalchemy import text
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from dovideo.infrastructure import create_r2_infrastructure
    from dovideo.infrastructure.celery_transport import CeleryTransportSettings, RabbitMQTopology
    from dovideo.infrastructure.providers import ProviderConfig
    from dovideo.presentation.composition import AnalysisSettings, embedding_provider_config_from_environment
    import urllib.request

    checks = {}
    infra = None
    component = 'production_configuration'
    try:
        infra = create_r2_infrastructure()
        component = 'mysql'
        with infra.engine.connect() as connection:
            checks['mysql'] = connection.execute(text('SELECT 1')).scalar() == 1
            from sqlalchemy import inspect
            revision = connection.execute(text('SELECT version_num FROM alembic_version')).scalar() if inspect(connection).has_table('alembic_version') else None
        component = 'database_migration'
        config = Config(str(ROOT/'alembic.ini'))
        config.set_main_option('script_location',str(ROOT/'alembic'))
        head = ScriptDirectory.from_config(config).get_current_head()
        checks['migration'] = {'current':revision,'head':head,'at_head':revision==head}
        if revision != head:
            raise RuntimeError('database migration required; ordinary start never upgrades schema')
        component = 'redis'
        checks['redis'] = bool(infra.redis_client.ping())
        if not checks['redis']: raise RuntimeError('Redis ping failed')
        component = 'minio'
        checks['minio'] = bool(infra.object_storage.client.bucket_exists(infra.settings.minio_bucket))
        if not checks['minio']: raise RuntimeError('MinIO bucket unavailable')
        component = 'qdrant'
        request = urllib.request.Request(infra.settings.qdrant_url.rstrip('/')+'/readyz')
        if infra.settings.qdrant_api_key:
            request.add_header('api-key',infra.settings.qdrant_api_key)
        with urllib.request.urlopen(request,timeout=5) as response:
            checks['qdrant'] = response.status == 200
        component = 'rabbitmq'
        broker = CeleryTransportSettings.from_environment(require_production=True)
        checks['rabbitmq'] = RabbitMQTopology(broker).queue_stats()
        component = 'media_configuration'
        tools = AnalysisSettings.from_environment(embedding_mode='remote')
        for name, executable in [('ffmpeg',tools.ffmpeg_executable),('ffprobe',tools.ffprobe_executable),('tesseract',tools.tesseract_executable)]:
            component = name
            found = shutil.which(executable) or (executable if Path(executable).is_file() else None)
            if not found: raise RuntimeError(name+' executable missing')
            probe = subprocess.run([found,'--version' if name=='tesseract' else '-version'],capture_output=True,timeout=10)
            checks[name] = probe.returncode == 0
            if not checks[name]: raise RuntimeError('media tool version probe failed')
        for name in ['whisper','torch']:
            component = name
            module = importlib.import_module(name)
            checks[name] = getattr(module,'__version__','installed')
        component = 'whisper_weights'
        checks['whisper_weights'] = (tools.whisper_model_root/(tools.whisper_model+'.pt')).is_file()
        if not checks['whisper_weights']: raise RuntimeError('configured Whisper weights missing')
        component = 'chat_provider_configuration'
        model = ProviderConfig.from_environment(required=True)
        component = 'embedding_provider_configuration'
        embedding = embedding_provider_config_from_environment(required=True)
        checks['chat'] = {'transport':model.transport,'model':model.model,'credential_present':bool(model.api_key),'provider_pin':list(model.provider_only)}
        checks['embedding'] = {'model':embedding.model,'credential_present':bool(embedding.api_key)}
        checks['profile'] = 'production'
        return checks
    except Exception as exc:
        raise PreflightError(component,type(exc).__name__) from None
    finally:
        if infra is not None: infra.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['check','worker-ready','api','worker','frontend','preview','alembic-current','alembic-check'])
    parser.add_argument('--settings',type=Path,default=ROOT/'work/product-r2/settings.json')
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding='utf-8-sig'))
    env = apply_environment(settings)
    if args.action == 'worker-ready':
        from dovideo.infrastructure.celery_tasks import create_worker_app
        from dovideo.infrastructure.celery_transport import CeleryTransportSettings, RabbitMQTopology
        transport = CeleryTransportSettings.from_environment(require_production=True)
        app = create_worker_app(transport)
        registered = app.control.inspect(timeout=2).registered() or {}
        ready = any(name.startswith('videomind-local@') and transport.task_name in tasks for name,tasks in registered.items())
        ready = ready and RabbitMQTopology(transport).queue_stats()['mainConsumers'] >= 1
        print(json.dumps({'registered_local_worker':ready,'queue':transport.queue}))
        app.close()
        return 0 if ready else 1
    if args.action == 'check':
        try:
            print(json.dumps(asyncio.run(check(settings)),indent=2))
            return 0
        except Exception as exc:
            # Do not serialize arbitrary connector exceptions containing URLs/secrets.
            print(json.dumps({'status':'FAILED','component':getattr(exc,'component','configuration'),'category':getattr(exc,'category',type(exc).__name__),'missing_module':getattr(exc,'name',None),'action':'check','guidance':'Inspect the named component using QUICK_START; no schema changed. Provider network availability is checked separately by controlled live validation.'}))
            return 1
    if args.action == 'api':
        import uvicorn
        from dovideo.presentation.api import create_app
        uvicorn.run(create_app(),host='127.0.0.1',port=settings.get('api_port',8000),access_log=False)
        return 0
    if args.action == 'worker':
        from dovideo.infrastructure.celery_transport import CeleryTransportSettings
        queue = CeleryTransportSettings.from_environment(require_production=True).queue
        command = [sys.executable,'-m','celery','-A','dovideo.infrastructure.celery_worker:celery_app','worker','--loglevel=WARNING','--pool=solo','--concurrency=1','-Q',queue,'--hostname=videomind-local@%h']
        cwd = ROOT
    elif args.action in {'frontend','preview'}:
        node = shutil.which('node')
        if not node: raise RuntimeError('Node executable missing')
        command = [node,str(ROOT/'client/node_modules/vite/bin/vite.js')]
        if args.action=='preview': command.append('preview')
        command += ['--host','127.0.0.1','--port',str(settings.get('frontend_port',5175)),'--strictPort']
        cwd = ROOT/'client'
    else:
        command = [sys.executable,'-m','alembic','current' if args.action=='alembic-current' else 'check']
        cwd = ROOT
    return subprocess.call(command,cwd=cwd,env=env)


if __name__=='__main__':
    raise SystemExit(main())

import asyncio
import contextlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from gpu_monitor.collector import collect, parse_snapshot

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get('GPU_MONITOR_CONFIG', ROOT / '.local' / 'config.json'))


class Server(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    name: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    identity_file: str = Field(default='', max_length=1024)

    @field_validator('target')
    @classmethod
    def valid_target(cls, value):
        if not re.fullmatch(r'(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.:-]*', value):
            raise ValueError('请输入 SSH 别名或 user@hostname，不要包含空格或命令参数')
        return value


class Settings(BaseModel):
    interval_seconds: int = Field(default=5, ge=5, le=60)
    servers: list[Server] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [s.id for s in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError('服务器 ID 不能重复')
        return self


class Monitor:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.settings = Settings.model_validate_json(path.read_text('utf-8')) if path.exists() else Settings()
        self.states = {}
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.revision = 0
        self.demo = parse_snapshot((ROOT / 'examples' / 'nvidia-smi.txt').read_text('utf-8'))

    def save(self, settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(settings.model_dump_json(indent=2), encoding='utf-8')
        if os.name != 'nt':
            temporary.chmod(0o600)
        temporary.replace(self.path)
        previous = {s.id: s for s in self.settings.servers}
        retained = {s.id for s in settings.servers if previous.get(s.id) == s}
        self.states = {k: v for k, v in self.states.items() if k in retained}
        self.settings = settings
        self.revision += 1
        self.wake.set()

    async def poll(self):
        async with self.lock:
            revision = self.revision
            async def one(server):
                try:
                    data = await asyncio.to_thread(collect, server)
                    if revision != self.revision:
                        return
                    now = time.time()
                    self.states[server.id] = {**data, 'updated_at': now, 'error': None}
                except Exception as exc:
                    if revision != self.revision:
                        return
                    previous = self.states.get(server.id, {'gpus': [], 'updated_at': None})
                    self.states[server.id] = {**previous, 'error': str(exc)}
            await asyncio.gather(*(one(server) for server in self.settings.servers))

    async def run(self):
        while True:
            self.wake.clear()
            await self.poll()
            try:
                await asyncio.wait_for(self.wake.wait(), self.settings.interval_seconds)
            except asyncio.TimeoutError:
                pass

    def snapshot(self):
        now = time.time()
        demo = not self.settings.servers
        servers = self.settings.servers if not demo else [Server(id='demo', name='示例 GPU 服务器', target='example-gpu-server')]
        result = []
        for server in servers:
            state = self.demo if demo else self.states.get(server.id, {'gpus': [], 'updated_at': None})
            updated = state.get('updated_at')
            stale = not demo and (updated is None or now - updated > max(20, self.settings.interval_seconds * 3) or bool(state.get('error')))
            gpus = []
            for gpu in state['gpus']:
                status = 'offline' if stale else ('unknown' if gpu['utilization'] is None else ('zero' if gpu['utilization'] == 0 else 'running'))
                gpus.append({**gpu, 'status': status})
            result.append({'id': server.id, 'name': server.name, 'target': server.target,
                           'driver': state.get('driver'), 'cuda': state.get('cuda'),
                           'updated_at': updated, 'error': state.get('error'), 'stale': stale, 'gpus': gpus})
        return {'demo': demo, 'snapshot_label': '虚构示例 · 静态快照' if demo else None,
                'now': now, 'settings': self.settings.model_dump(), 'servers': result}


@asynccontextmanager
async def lifespan(app):
    app.state.monitor = Monitor()
    task = asyncio.create_task(app.state.monitor.run())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title='GPU-manager', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])


@app.middleware('http')
async def local_requests(request: Request, call_next):
    from urllib.parse import urlsplit
    if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '仅允许本机页面修改配置'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return response


@app.get('/api/state')
async def state(request: Request):
    return request.app.state.monitor.snapshot()


@app.put('/api/settings')
async def settings(value: Settings, request: Request):
    try:
        request.app.state.monitor.save(value)
    except OSError as exc:
        raise HTTPException(500, '无法保存本地配置，请检查目录写入权限') from exc
    return {'ok': True}


@app.post('/api/refresh')
async def refresh(request: Request):
    monitor = request.app.state.monitor
    if monitor.lock.locked():
        return {'ok': True, 'collecting': True}
    monitor.wake.set()
    return {'ok': True, 'collecting': True}


@app.get('/api/servers/{server_id}/gpus/{gpu_uuid}/processes')
async def processes(server_id: str, gpu_uuid: str, request: Request):
    monitor = request.app.state.monitor
    server = next((s for s in monitor.settings.servers if s.id == server_id), None)
    if not server:
        raise HTTPException(404, '服务器连接不存在')
    try:
        data = await asyncio.to_thread(collect, server, True)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    gpu = next((g for g in data['gpus'] if g['uuid'] == gpu_uuid), None)
    if not gpu:
        raise HTTPException(404, '服务器未返回此显卡，请刷新首页')
    return {'processes': gpu['processes'], 'updated_at': time.time()}


@app.get('/')
async def index():
    return FileResponse(ROOT / 'gpu_monitor' / 'static' / 'index.html')


app.mount('/static', StaticFiles(directory=ROOT / 'gpu_monitor' / 'static'), name='static')

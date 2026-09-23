"""Lifecycle inside the relay process. Windows owns crash restart; no child relay."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from .model_evidence import home_key
from .observer_control import atomic_write
from .observer_state import ProcessLock, read_json
from .version import PROXY_VERSION


class ManagedProxy:
    def __init__(self, manager):
        self.manager = manager
        self.instance = uuid.uuid4().hex
        self.control = manager.directory / ('proxy-control-' + self.instance + '.json')
        self.started = time.monotonic()

    def allowed(self):
        state = self.manager.state()
        return bool(state.get('enabled') or state.get('pending') or
                    state.get('phase') in ('starting', 'prepared', 'validated'))

    def publish(self, health, phase):
        value = dict(home=home_key(self.manager.home), url=self.manager.url,
                     version=PROXY_VERSION, pid=os.getpid(), worker_pid=os.getpid(),
                     worker_version=PROXY_VERSION, instance=self.instance, lifecycle='managed',
                     phase=phase, updated_at=time.time(), control_file=str(self.control))
        try:
            atomic_write(self.manager.runtime_path, json.dumps(value).encode())
        except OSError:
            pass  # Status storage cannot interrupt a relay.

    def tick(self, health):
        try:
            state = self.manager.state()
            configured = self.manager.config()[1].get('openai_base_url') == self.manager.url
        except (ValueError, OSError):
            self.publish(health, 'configuration_error')
            return
        # Only an explicit off/configuration action or idle preparation expiry drains.
        if (not state.get('enabled') and state.get('phase') in ('off', 'faulted', 'failed')
                or state.get('enabled') and not configured
                or not state.get('enabled') and time.monotonic() - self.started > 120):
            health['draining'] = True
        # Observation is optional: a broken store must not take down the transport.
        if health.get('storage_failure_streak', 0) >= 3:
            health['observation_enabled'] = False
        self.publish(health, 'draining' if health['draining'] else 'active' if configured else 'ready')

    async def serve(self, store, endpoint, context, port):
        from aiohttp import web
        from .model_proxy import create_app
        # Same lifetime lock as the legacy supervisor permits safe update/migration.
        with ProcessLock(self.manager.directory / 'proxy-supervisor.lock'):
            if not self.allowed():
                return
            atomic_write(self.control, json.dumps({'action':'run','id':self.instance}).encode())
            stop = asyncio.Event()
            app = create_app(store, self.manager.home, endpoint, ssl_context=context,
                             control_file=self.control, control_id=self.instance,
                             stop_event=stop, managed=self)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, '127.0.0.1', port).start()
                await stop.wait()
            finally:
                await runner.cleanup()
                self.publish({}, 'stopped')

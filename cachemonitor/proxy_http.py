"""Reusable HTTP/2 connections with cancellation isolated to one request.

HTTPX/httpcore does not emit RST_STREAM when a response is abandoned. Leasing
one client per concurrent request lets us close that request's connection on
cancellation without terminating unrelated requests. Completed requests reuse
their connection; nothing is replayed by this pool.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar
import asyncio
import ssl

import httpx
from .proxy_websocket import POLICY, LocalCapacity


class NoCookieJar(CookieJar):
    def set_cookie(self,cookie,*args,**kwargs):pass


@dataclass
class Lease:
    client: httpx.AsyncClient
    complete: bool = False


class HTTPRelayPool:
    def __init__(self,ssl_context=None,limit=100):
        self.ssl_context=ssl_context or ssl.create_default_context()
        self.slots=asyncio.Semaphore(limit)
        self.idle=asyncio.LifoQueue()
        self.clients=set()
        self.waiting=0

    @asynccontextmanager
    async def lease(self):
        if self.waiting>=POLICY.waiters:raise LocalCapacity()
        self.waiting+=1
        try:
            try:await asyncio.wait_for(self.slots.acquire(),POLICY.capacity_seconds)
            except asyncio.TimeoutError:raise LocalCapacity() from None
        finally:self.waiting-=1
        client=None
        lease=None
        try:
            try:client=self.idle.get_nowait()
            except asyncio.QueueEmpty:
                transport=httpx.AsyncHTTPTransport(http2=True,retries=0,verify=self.ssl_context,
                    limits=httpx.Limits(max_connections=1,max_keepalive_connections=1))
                client=httpx.AsyncClient(transport=transport,trust_env=False,cookies=NoCookieJar(),
                                          timeout=httpx.Timeout(None,connect=30))
                self.clients.add(client)
            lease=Lease(client)
            yield lease
        finally:
            try:
                if client is not None:
                    if lease is not None and lease.complete:self.idle.put_nowait(client)
                    else:
                        await client.aclose()
                        self.clients.discard(client)
            finally:self.slots.release()

    async def close(self):
        await asyncio.gather(*(client.aclose() for client in self.clients))
        self.clients.clear()

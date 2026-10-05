"""Bounded release metadata requests shared by automatic and explicit checks."""
from __future__ import annotations

import json
import math
import random
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
import sys
from urllib.error import HTTPError

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from .app_update import HEADERS, RELEASE_URL, validated_release, version_parts
from .version import VERSION

INTERVAL = 6*60*60
MAX_BODY = 2_000_000


def production_updates_allowed(*, isolated=False):
    """QA/source launches must not contact the release service."""
    if isolated or not getattr(sys,'frozen',False):return False
    from .installation import installed, valid_paths
    from .observer_state import read_json
    install=installed()
    if not valid_paths(install):return False
    receipt=read_json(Path(install['InstallRoot'])/'installation.json')
    return not receipt.get('isolated') and Path(sys.executable).resolve()==Path(install['AppPath']).resolve()


class UpdateMonitor(QObject):
    available = Signal(object)
    manual_finished = Signal(object)

    def __init__(self,settings,parent=None,*,clock=time.time,jitter=random.uniform):
        super().__init__(parent)
        self.settings=settings;self.clock=clock;self.jitter=jitter
        self.enabled=settings.value('updates/automatic',True,type=bool)
        self.running=False;self.manual=False;self.reply=None;self.network=None
        self.cache=None;self.etag='';self.body=bytearray();self.error=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.due)
        self.deadline=QTimer(self);self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self.timed_out)
        try:
            cached=json.loads(settings.value('updates/cache','{}'))
            self.cache=validated_release(cached['release'])
            etag=cached.get('etag','')
            if isinstance(etag,str) and len(etag)<1024 and '\r' not in etag and '\n' not in etag:self.etag=etag
        except (ValueError,TypeError,KeyError,RecursionError):pass

    def timestamp(self,key):
        try:
            value=float(self.settings.value('updates/'+key,0))
            return value if math.isfinite(value) and value>=0 else 0
        except (TypeError,ValueError):return 0

    def start(self):
        self.running=True
        self.publish()
        self.schedule(startup=True)

    def publish(self):
        release=self.cache
        self.available.emit(release if release and version_parts(release['tag_name'])>version_parts(VERSION) else None)

    def set_enabled(self,enabled):
        self.enabled=bool(enabled);self.settings.setValue('updates/automatic',self.enabled)
        self.timer.stop()
        if not self.enabled and not self.manual:self.cancel()
        if self.enabled:
            self.publish();self.schedule(startup=True)

    def schedule(self,*,startup=False):
        self.timer.stop()
        if not self.running or not self.enabled:return
        now=self.clock();due=max(self.timestamp('nextCheck'),self.timestamp('retryAfter'))
        if startup and due<=now:
            due=now+self.jitter(15,45)
            self.settings.setValue('updates/nextCheck',due);self.settings.sync()
        self.timer.start(min(2_147_483_647,max(1,math.ceil((due-now)*1000))))

    def due(self):
        if not self.running or not self.enabled:return
        if self.clock()<max(self.timestamp('nextCheck'),self.timestamp('retryAfter')):
            self.schedule();return
        self.request()

    def check_manually(self):
        self.manual=True
        self.request()

    def request(self):
        if self.reply is not None:return
        if self.clock()<self.timestamp('retryAfter'):
            if self.manual:
                self.manual=False
                self.manual_finished.emit(HTTPError(RELEASE_URL,429,'Rate limited',{},None))
            self.schedule();return
        # Reserve the slot before I/O, including failed/interrupted attempts.
        self.settings.setValue('updates/nextCheck',self.clock()+INTERVAL+self.jitter(0,600))
        self.settings.sync();self.schedule()
        self.error=None;self.body=bytearray()
        self.network=QNetworkAccessManager(self)
        request=QNetworkRequest(QUrl(RELEASE_URL))
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,QNetworkRequest.ManualRedirectPolicy)
        for key,value in HEADERS.items():request.setRawHeader(key.encode(),value.encode())
        if self.etag and self.cache:request.setRawHeader(b'If-None-Match',self.etag.encode())
        self.reply=self.network.get(request)
        self.reply.setReadBufferSize(MAX_BODY+1)
        self.reply.readyRead.connect(self.read)
        self.reply.finished.connect(self.finished)
        self.deadline.start(10_000)

    def read(self):
        self.body.extend(bytes(self.reply.readAll()))
        if len(self.body)>MAX_BODY:
            self.error=ValueError('배포 정보가 허용 크기를 초과했습니다.')
            self.reply.abort()

    def timed_out(self):
        if self.reply is not None:
            self.error=TimeoutError('Release request exceeded 10 seconds')
            self.reply.abort()

    def server_delay(self,status,headers):
        now=self.clock();until=0
        retry=headers.get('retry-after','')
        try:until=now+max(0,float(retry))
        except ValueError:
            try:until=parsedate_to_datetime(retry).timestamp()
            except (ValueError,TypeError,OverflowError):pass
        if headers.get('x-ratelimit-remaining')=='0':
            try:until=max(until,float(headers.get('x-ratelimit-reset','0')))
            except ValueError:pass
        if status in (403,429):until=max(until,now+60)
        if math.isfinite(until) and until>now:
            self.settings.setValue('updates/retryAfter',until)

    def finished(self):
        reply=self.reply
        if reply is None:return
        # readyRead normally consumed it; include any final bytes without recursive abort.
        self.body.extend(bytes(reply.readAll()))
        status=reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        headers={bytes(k).decode('latin1').lower():bytes(v).decode('latin1') for k,v in reply.rawHeaderPairs()}
        self.server_delay(status,headers)
        result=None
        try:
            if self.error:raise self.error
            if len(self.body)>MAX_BODY:raise ValueError('배포 정보가 허용 크기를 초과했습니다.')
            if status==304 and self.cache:result=self.cache
            elif status!=200:
                if status:raise HTTPError(RELEASE_URL,status,'Release request failed',headers,None)
                raise ConnectionError(reply.errorString())
            else:
                if reply.error()!=QNetworkReply.NoError:raise ConnectionError(reply.errorString())
                result=validated_release(json.loads(self.body))
                etag=headers.get('etag','')
                self.etag=etag if len(etag)<1024 and '\r' not in etag and '\n' not in etag else ''
            self.cache=result
            self.settings.setValue('updates/cache',json.dumps(dict(release=result,etag=self.etag)))
            self.settings.setValue('updates/lastSuccess',self.clock())
        except (ValueError,TypeError,KeyError,OSError,RecursionError) as exc:result=exc
        manual=self.manual;self.manual=False
        self.cleanup()
        self.settings.sync();self.schedule()
        if not isinstance(result,Exception):self.publish()
        if manual:self.manual_finished.emit(result)

    def cleanup(self):
        self.deadline.stop()
        reply,self.reply=self.reply,None
        network,self.network=self.network,None
        if reply:reply.deleteLater()
        if network:network.deleteLater()
        self.body=bytearray()

    def cancel(self):
        if self.reply is not None:
            self.reply.finished.disconnect(self.finished)
            self.reply.readyRead.disconnect(self.read)
            self.reply.abort()
            self.cleanup()

    def stop(self):
        self.running=False;self.manual=False;self.timer.stop();self.cancel()

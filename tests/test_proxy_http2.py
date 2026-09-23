import asyncio
from contextlib import asynccontextmanager, suppress
import datetime
import gzip
import ipaddress
import json
import ssl
import pytest

from aiohttp import ClientSession
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import h2.config
import h2.connection
import h2.events

from cachemonitor.model_proxy import create_app
from cachemonitor.model_evidence import EvidenceStore
from test_model_proxy import server, run_proxy_test


def tls_contexts(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'loopback proxy test')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False)
          .sign(key,hashes.SHA256()))
    certfile=tmp_path/'cert.pem';keyfile=tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    upstream=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);upstream.load_cert_chain(certfile,keyfile)
    upstream.set_alpn_protocols(['h2'])
    client=ssl.create_default_context(cafile=str(certfile))
    return upstream,client


@asynccontextmanager
async def origin(tmp_path,handler):
    server_tls,client_tls=tls_contexts(tmp_path)
    state={'connections':0,'requests':0,'protocols':[],'closed':asyncio.Queue(),'errors':[]}
    active=set()
    async def connected(reader,writer):
        task=asyncio.current_task();active.add(task);children=[]
        state['connections']+=1;connection_id=state['connections']
        state['protocols'].append(writer.get_extra_info('ssl_object').selected_alpn_protocol())
        protocol=h2.connection.H2Connection(h2.config.H2Configuration(client_side=False,header_encoding='utf-8'))
        protocol.initiate_connection();writer.write(protocol.data_to_send())
        requests={}
        async def respond(stream_id):
            headers,body=requests[stream_id]
            async def send(extra,payload,end_stream=True):
                protocol.send_headers(stream_id,[(':status','200'),*extra])
                protocol.send_data(stream_id,payload,end_stream=end_stream)
                writer.write(protocol.data_to_send());await writer.drain()
            try:await handler(connection_id,headers,bytes(body),send)
            except Exception as exc:
                state['errors'].append(repr(exc));writer.close()
        try:
            while data:=await reader.read(65536):
                for event in protocol.receive_data(data):
                    if isinstance(event,h2.events.RequestReceived):
                        requests[event.stream_id]=(dict(event.headers),bytearray());state['requests']+=1
                    elif isinstance(event,h2.events.DataReceived):
                        requests[event.stream_id][1].extend(event.data)
                        protocol.acknowledge_received_data(event.flow_controlled_length,event.stream_id)
                    elif isinstance(event,h2.events.StreamEnded):children.append(asyncio.create_task(respond(event.stream_id)))
                writer.write(protocol.data_to_send());await writer.drain()
        except (ConnectionResetError,BrokenPipeError):pass
        finally:
            state['closed'].put_nowait(connection_id)
            for child in children:child.cancel()
            await asyncio.gather(*children,return_exceptions=True)
            writer.close()
            with suppress(OSError):await writer.wait_closed()
            active.discard(task)
    listener=await asyncio.start_server(connected,'127.0.0.1',0,ssl=server_tls)
    port=listener.sockets[0].getsockname()[1]
    try:yield f'https://127.0.0.1:{port}',client_tls,state
    finally:
        listener.close();await listener.wait_closed()
        for task in list(active):task.cancel()
        await asyncio.gather(*active,return_exceptions=True)
        assert not state['errors'],state['errors']


def test_real_h2_tls_negotiation_reuse_and_compressed_observation(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');traces=[];seen=[]
        response=gzip.compress(b'data: {"type":"response.completed","response":{"id":"h2","model":"m","service_tier":"default"}}\n\n')
        async def handle(connection,headers,body,send):
            assert headers[':path']=='/base/responses?x=%2f&x=a+b'
            assert headers['authorization']=='Bearer PRIVATE'
            assert json.loads(gzip.decompress(body))['model']=='m'
            seen.append(connection)
            await send([('content-encoding','gzip'),('content-type','application/octet-stream')],response)
        try:
            async with origin(tmp_path,handle) as (url,tls,state), server(create_app(store,tmp_path,url+'/base',ssl_context=tls,diagnostics=traces.append)) as proxy:
                from yarl import URL
                async with ClientSession(auto_decompress=False) as client:
                    for _ in range(2):
                        body=gzip.compress(b'{"model":"m","service_tier":"priority"}')
                        async with asyncio.timeout(5):
                            async with client.post(URL(proxy+'/responses?x=%2f&x=a+b',encoded=True),data=body,
                                                   headers={'Authorization':'Bearer PRIVATE','Content-Encoding':'gzip'}) as result:
                                assert result.status==200 and await result.read()==response
                assert state['protocols']==['h2'] and state['requests']==2 and seen==[1,1]
                assert all(item['upstream_http']=='HTTP/2' for item in traces)
                assert store.db.execute("select count(*) from model_observations where status='completed' and requested_model='m' and response_model='m'").fetchone()[0]==2
                assert store.db.execute("select requested_service_tier,response_service_tier from model_observations where status='completed'").fetchall()==[('priority','default')]*2
        finally:store.close()
    run_proxy_test(run())


@pytest.mark.parametrize('after_headers',[False,True])
def test_h2_client_cancel_closes_only_its_upstream_connection(tmp_path,after_headers):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');started=asyncio.Event();other_started=asyncio.Event();release=asyncio.Event()
        connections={}
        async def handle(connection,headers,body,send):
            path=headers[':path'];connections[path]=connection
            if path=='/responses':
                if after_headers:await send([('content-type','text/event-stream')],b': waiting\n\n',end_stream=False)
                started.set();await asyncio.Event().wait()
            else:
                other_started.set();await release.wait();await send([],b'other request completed')
        try:
            async with origin(tmp_path,handle) as (url,tls,state), server(create_app(store,tmp_path,url,ssl_context=tls)) as proxy, ClientSession() as client:
                reader,writer=await asyncio.open_connection('127.0.0.1',int(proxy.rsplit(':',1)[1]))
                other=None
                try:
                    writer.write(b'POST /responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: 13\r\n\r\n{"model":"m"}')
                    await writer.drain();await asyncio.wait_for(started.wait(),3)
                    if after_headers:await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
                    other=asyncio.create_task(client.get(proxy+'/other'))
                    await asyncio.wait_for(other_started.wait(),3)
                    writer.close();await writer.wait_closed()
                    assert await asyncio.wait_for(state['closed'].get(),1)==connections['/responses']
                    release.set()
                    result=await asyncio.wait_for(other,3)
                    assert await result.read()==b'other request completed'
                    health=await (await client.get(proxy+'/health')).json()
                    assert health['client_disconnects']==1 and health['relay_errors']==0
                finally:
                    release.set();writer.close()
                    with suppress(OSError):await writer.wait_closed()
                    if other is not None:
                        other.cancel();await asyncio.gather(other,return_exceptions=True)
        finally:release.set();store.close()
    run_proxy_test(run())

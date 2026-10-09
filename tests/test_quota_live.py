import base64
import hashlib
import json
import time

import httpx
import pytest

from cachemonitor.quota_live import AccountClient


def jwt(claims):
    payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'e30.'+payload+'.fixture'


def auth_file(home, *, expired=False, identity='fixture@example.invalid'):
    value={'auth_mode':'chatgpt','tokens':{'account_id':'fixture-account',
           'access_token':jwt({'exp':time.time()+(-60 if expired else 3600)}),
           'id_token':jwt({'email':identity}),'refresh_token':'never-use-refresh-secret'}}
    path=home/'auth.json';path.write_text(json.dumps(value),encoding='utf-8')
    return path


def usage():
    return dict(plan_type='pro',rate_limit=dict(primary_window=dict(used_percent=12,
                limit_window_seconds=604800,reset_at=time.time()+600),secondary_window=None),
                additional_rate_limits=[dict(metered_feature='separate',normal_model_slug='separate-model')],
                credits=dict(balance='62500.125',has_credits=True,unlimited=False),
                rate_limit_reset_credits=dict(available_count=3,credits=[dict(id='one',
                    title='Full reset',status='available',reset_type='codexRateLimits',
                    granted_at=1700000000,expires_at=1900000000)]))


@pytest.mark.parametrize('status',[200,401,403,302])
def test_usage_read_never_refreshes_auth_or_follows_redirects(tmp_path,monkeypatch,status):
    import subprocess
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('Quota reads must not launch an auth owner'))
    AccountClient._quota_cache.clear()
    path=auth_file(tmp_path);before=path.read_bytes();requests=[]
    def respond(request):
        requests.append((request.method,str(request.url)))
        assert request.headers['chatgpt-account-id']=='fixture-account'
        assert b'never-use-refresh-secret' not in request.content
        value=usage() if request.url.path.endswith('/usage') else usage()['rate_limit_reset_credits']
        return httpx.Response(status,json=value if status==200 else {'error':'never-use-refresh-secret'},
                              headers={'location':'https://example.invalid/collect'})
    client=AccountClient(tmp_path,transport=httpx.MockTransport(respond))
    try:
        if status==200:
            result=client.fetch()
            assert result['account']==hashlib.sha256(b'fixture@example.invalid').hexdigest()
            assert result['windows']['weekly']['used_percent']==12
            assert result['unlimited_windows']==['five_hour']
            assert result['separate_models']==['separate-model']
            assert result['credits']['balance']=='62500.125'
            assert result['reset_credits']['available_count']==3
            assert result['reset_credits']['credits'][0]['expires_at']==1900000000
            assert client.fetch()==result  # Reuses the shared observation without changing its timestamp.
        else:
            with pytest.raises(RuntimeError) as error:client.fetch()
            assert 'never-use-refresh-secret' not in str(error.value)
    finally:client.close()
    expected=[('GET',AccountClient.USAGE_URL)]
    if status==200:expected.append(('GET',AccountClient.RESET_DETAILS_URL))
    assert requests==expected
    assert path.read_bytes()==before
    assert list(tmp_path.iterdir())==[path]


@pytest.mark.parametrize('missing',[False,True])
def test_expired_or_unavailable_credentials_wait_without_network(tmp_path,missing):
    path=None if missing else auth_file(tmp_path,expired=True)
    before=path.read_bytes() if path else None
    client=AccountClient(tmp_path,transport=httpx.MockTransport(lambda r:pytest.fail('Must not renew credentials')))
    with pytest.raises(RuntimeError):client.fetch()
    assert client.client is None
    assert (path.read_bytes() if path else None)==before


def test_account_change_during_usage_read_does_not_publish_wrong_account(tmp_path):
    AccountClient._quota_cache.clear();path=auth_file(tmp_path)
    def respond(request):
        auth_file(tmp_path,identity='other@example.invalid')
        return httpx.Response(200,json=usage())
    client=AccountClient(tmp_path,transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError,match='로그인 변경'):client.fetch()
    assert not AccountClient._quota_cache
    assert json.loads(path.read_bytes())['tokens']['refresh_token']=='never-use-refresh-secret'


def test_missing_window_is_unknown_and_not_unlimited(tmp_path):
    AccountClient._quota_cache.clear();auth_file(tmp_path)
    raw=usage();del raw['rate_limit']['secondary_window']
    client=AccountClient(tmp_path,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw)))
    try:assert client.fetch().get('unlimited_windows',[])==[]
    finally:client.close()


def test_reset_details_dates_and_count_survive_read_only_transport(tmp_path):
    AccountClient._quota_cache.clear();auth_file(tmp_path)
    def respond(request):
        raw=usage()
        if request.url.path.endswith('/usage'):
            raw['rate_limit_reset_credits']={'available_count':3}
        else:
            raw={'available_count':1,'credits':[dict(id='one',status='available',
                 reset_type='codex_rate_limits',granted_at='2026-06-17T00:00:00Z',
                 expires_at='2026-07-17T00:00:00Z')]}
        return httpx.Response(200,json=raw)
    client=AccountClient(tmp_path,transport=httpx.MockTransport(respond))
    try:
        result=client.fetch()['reset_credits']
        assert result['available_count']==3
        assert result['credits'][0]['reset_type']=='codexRateLimits'
        assert result['credits'][0]['expires_at']==1784246400
    finally:client.close()


def test_keyring_selection_never_uses_a_stale_file_login(tmp_path):
    auth_file(tmp_path)
    (tmp_path/'config.toml').write_text('cli_auth_credentials_store="keyring"')
    client=AccountClient(tmp_path,transport=httpx.MockTransport(lambda r:pytest.fail('Stale login used')))
    with pytest.raises(RuntimeError,match='저장 방식'):client.fetch()


def test_shared_cache_separates_workspaces_for_the_same_user(tmp_path):
    AccountClient._quota_cache.clear();path=auth_file(tmp_path);accounts=[]
    def respond(request):
        value=usage()
        if request.url.path.endswith('/usage'):
            accounts.append(request.headers['chatgpt-account-id'])
            value['rate_limit']['primary_window']['used_percent']=10*len(accounts)
        return httpx.Response(200,json=value)
    client=AccountClient(tmp_path,transport=httpx.MockTransport(respond))
    try:
        first=client.fetch()
        auth=json.loads(path.read_bytes());auth['tokens']['account_id']='another-workspace'
        path.write_text(json.dumps(auth))
        second=client.fetch()
        assert accounts==['fixture-account','another-workspace']
        assert first['windows']['weekly']['used_percent']==10
        assert second['windows']['weekly']['used_percent']==20
        assert first['account']==second['account']  # Preserve existing historical identity.
    finally:client.close()

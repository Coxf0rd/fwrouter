from __future__ import annotations

import json
import pytest
from fwrouter_api.adapters.provider_base import ProviderError, RequestBudget
from fwrouter_api.adapters import stealthsurf_protocols as profiles
from fwrouter_api.services import provider_managed as service
from fwrouter_api.db import provider_managed as store
from test_provider_managed_integration import _operation_setup, CONNECTION_URL

TROJAN = "trojan://fixture-password@vpn.example.test:443?security=tls&sni=example.test#fixture"


def setup(monkeypatch):
    conn, binding, fake = _operation_setup(monkeypatch)
    fake.supported_protocols = tuple(p.key for p in profiles.PROFILES)
    store.record_config(conn, 'source-a', binding['binding_revision'], {
        'server_id': 900, 'location_id': 6, 'protocol': 'hysteria2'})
    store.record_applied(conn, 'source-a', revision=binding['binding_revision'], member_id=900,
                         protocol='hysteria2', applied_at=100)
    monkeypatch.setattr('fwrouter_api.services.events.write_audit_event', lambda **kw: None)
    fake.mutation_result = {'id':1234,'server_id':900,'location_id':6,'protocol':'hysteria2','connection_url':CONNECTION_URL}
    target = {'id':1234,'server_id':901,'location_id':6,'protocol':'trojan','connection_url':TROJAN}
    def change(*args, **kwargs):
        fake.calls.append(('change_protocol',*args))
        fake.mutation_result = target
        return target
    fake.change_protocol = change
    return conn,binding,fake,target


def test_wire_mapping_is_at_adapter_boundary():
    for p in profiles.PROFILES:
        assert profiles.wire_protocol(p.key) == p.wire_id
        assert profiles.normalize_config({'protocol':p.wire_id,'connection_url':'private'}) == {
            'protocol':p.key,'connection_url':'private'}
    assert profiles.wire_protocol('wireguard') == 'wg'
    assert profiles.wire_protocol('amneziawg2') == 'amnezia-wg-2'
    with pytest.raises(ProviderError): profiles.wire_protocol('wg')
    with pytest.raises(ProviderError): profiles.normalize_config({'protocol':'unknown'})


def test_protocol_change_persists_intent_and_uses_common_pipeline(monkeypatch):
    conn,binding,fake,target=setup(monkeypatch)
    calls=[]
    def refresh(bound, config, **kwargs):
        calls.append((bound,config,kwargs))
        assert bound['protocol']=='trojan'
        assert service._normalized_refresh(bound, config).servers[0].raw['type']=='trojan'
        store.record_applied(conn,'source-a',revision=bound['binding_revision'],member_id=901,protocol='trojan')
        return {'ok':True,'runtime_verified':True,'last_good_retained':False}
    monkeypatch.setattr(service,'_refresh_with_material',refresh)
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',expected_revision=binding['binding_revision'],_adapter=fake)
    assert result['ok'] and result['runtime_verified'] and result['outcome']=='verified'
    assert [x[0] for x in fake.calls]==['get_configs','change_protocol']
    current=store.get_binding(conn,'source-a')
    assert current['protocol']==current['observed_protocol']==current['applied_protocol']=='trojan'
    assert current['applied_revision']==current['binding_revision']==binding['binding_revision']+1
    assert len(calls)==1 and calls[0][1] is target
    assert 'fixture-password' not in json.dumps(result)
    assert 'fixture-password' not in conn.execute('select safe_json from provider_evidence order by observed_at desc').fetchone()[0]


@pytest.mark.parametrize('outcome',['partial','failed','unconfirmed'])
def test_protocol_apply_failure_preserves_last_good(monkeypatch,outcome):
    conn,binding,fake,_=setup(monkeypatch)
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:{'ok':False,'runtime_verified':False,'outcome':outcome,'last_good_retained':True})
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert not result['ok'] and result['outcome']==outcome and result['last_good_retained']
    current=store.get_binding(conn,'source-a')
    assert current['protocol']==current['observed_protocol']=='trojan'
    assert current['applied_member_id']=='900' and current['applied_protocol']=='hysteria2'
    assert current['applied_revision']==binding['binding_revision']


def test_protocol_timeout_is_not_replayed_and_retains_old_observation(monkeypatch):
    conn,binding,fake,_=setup(monkeypatch)
    def timeout(*a,**kw):
        fake.calls.append(('change_protocol',))
        raise ProviderError('TIMEOUT',retryable=True)
    fake.change_protocol=timeout
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['outcome']=='unconfirmed' and result['last_good_retained']
    assert [x[0] for x in fake.calls]==['get_configs','change_protocol']
    current=store.get_binding(conn,'source-a')
    assert current['protocol']=='trojan' and current['observed_protocol']=='hysteria2'
    assert current['applied_protocol']=='hysteria2' and current['applied_revision']==binding['binding_revision']


def test_documented_incomplete_mutation_requires_one_authoritative_get(monkeypatch):
    _,_,fake,target=setup(monkeypatch)
    original=fake.change_protocol
    def change(*a,**kw):
        original(*a,**kw)
        return {'server_id':901,'connection_url':TROJAN}
    fake.change_protocol=change
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:{'ok':True,'runtime_verified':True})
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['ok']
    assert [x[0] for x in fake.calls]==['get_configs','change_protocol','get_configs']


def test_stale_post_mutation_config_is_unconfirmed(monkeypatch):
    conn,_,fake,_=setup(monkeypatch)
    def change(*a,**kw):
        fake.calls.append(('change_protocol',))
        return {'server_id':901,'connection_url':TROJAN}
    fake.change_protocol=change
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:pytest.fail('Must not apply stale material'))
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['outcome']=='unconfirmed' and result['error_code']=='PROVIDER_PROTOCOL_MISMATCH'
    current=store.get_binding(conn,'source-a');assert current['protocol']=='trojan' and current['applied_protocol']=='hysteria2'


def test_unsupported_protocol_rejected_before_http_or_intent(monkeypatch):
    conn,binding,fake,_=setup(monkeypatch)
    result=service.execute_provider_operation('source-a','protocol',protocol='tuic',_adapter=fake)
    assert result['error_code']=='PROVIDER_PROTOCOL_UNSUPPORTED' and fake.calls==[]
    assert store.get_binding(conn,'source-a')['binding_revision']==binding['binding_revision']


def test_extended_protocol_settings_reject_before_mutation(monkeypatch):
    conn,binding,fake,_=setup(monkeypatch)
    fake.mutation_result['is_extended_settings_enabled']=True
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['error_code']=='PROVIDER_PROTOCOL_EXTENDED_SETTINGS_UNSUPPORTED'
    assert [x[0] for x in fake.calls]==['get_configs']
    assert store.get_binding(conn,'source-a')['binding_revision']==binding['binding_revision']


def test_invalid_mutation_material_cannot_fallback_to_valid_uri(monkeypatch):
    conn,_,fake,target=setup(monkeypatch)
    target['xray_config']='not a supported config: fixture-secret'
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:pytest.fail('Must not apply invalid extended material'))
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['outcome']=='unconfirmed' and result['last_good_retained']
    assert store.get_binding(conn,'source-a')['applied_protocol']=='hysteria2'
    assert 'fixture-secret' not in json.dumps(result)


def test_unexpected_apply_exception_is_secret_safe_and_unconfirmed(monkeypatch):
    conn,_,fake,_=setup(monkeypatch)
    def broken(*a, **kw):
        raise RuntimeError('private-configuration-and-credential')
    monkeypatch.setattr(service,'_refresh_with_material',broken)
    result=service.execute_provider_operation('source-a','protocol',protocol='trojan',_adapter=fake)
    assert result['outcome']=='unconfirmed' and result['last_good_retained']
    assert result['error_code']=='PROVIDER_OPERATION_FAILED'
    assert 'private-configuration' not in json.dumps(result)
    assert store.get_binding(conn,'source-a')['applied_protocol']=='hysteria2'


@pytest.mark.parametrize('profile',profiles.PROFILES,ids=lambda p:p.wire_id)
def test_actual_client_maps_protocol_at_http_boundary(profile):
    import httpx
    from fwrouter_api.adapters.stealthsurf import StealthSurfClient
    calls=[]
    def handler(request):
        calls.append(request)
        data=[{'id':1234,'protocol':profile.wire_id}] if request.method=='GET' else {'protocol':profile.wire_id}
        return httpx.Response(200,json={'status':True,'statusCode':200,'data':data})
    client=StealthSurfClient('fixture-key-'+profile.key,transport=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert client.get_configs(1234,budget=RequestBudget(1,2))[0]['protocol']==profile.key
        assert client.change_protocol(1234,6,profile.key,budget=RequestBudget(1,2))['protocol']==profile.key
        assert json.loads(calls[-1].content)['protocol']==profile.wire_id
        assert len(calls)==2
    finally:
        client.close()


def test_standard_structured_material_can_change_but_unknown_extended_state_cannot():
    material=json.dumps({'outbounds':[{'protocol':'trojan','settings':{'servers':[{'address':'vpn.example.test','port':443,'password':'fixture'}]},'streamSettings':{'security':'tls','network':'tcp'}}]})
    current={'protocol':'trojan','xray_config':material,'is_extended_settings_enabled':False}
    profiles.validate_change_preflight('hysteria2',current)
    current.pop('is_extended_settings_enabled')
    with pytest.raises(ProviderError,match='PROVIDER_PROTOCOL_EXTENDED_SETTINGS_UNSUPPORTED'):
        profiles.validate_change_preflight('hysteria2',current)


@pytest.mark.parametrize('verified',[True,False])
def test_same_protocol_noop_requires_fresh_local_readback_without_provider_http(monkeypatch,verified):
    conn,binding,fake,_=setup(monkeypatch)
    monkeypatch.setattr(service,'verify_provider_handoff',lambda:{'ok':verified,'error_code':'PROVIDER_MEMBER_READBACK_UNCONFIRMED'})
    result=service.execute_provider_operation('source-a','protocol',protocol='hysteria2',_adapter=fake)
    assert result['ok'] is verified
    assert bool(result.get('runtime_verified')) is verified
    assert result['outcome']==('noop' if verified else 'failed')
    assert store.get_binding(conn,'source-a')['last_outcome']==result['outcome']
    assert fake.calls==[]
    assert store.get_binding(conn,'source-a')['binding_revision']==binding['binding_revision']


def test_provider_vless_unknown_uri_option_rejects_without_echoing_material():
    with pytest.raises(ProviderError,match='PROVIDER_PROTOCOL_VALIDATION_FAILED'):
        profiles.parse_material('vless',{'connection_url':'vless://id@node.example:443?security=tls&unsupported=secret-option'})


def test_provider_json_cannot_publish_a_subset_with_unsupported_outbounds():
    material=json.dumps({'outbounds':[{'protocol':'trojan','settings':{'servers':[{'address':'edge.example','port':443,'password':'fixture'}]},'streamSettings':{'security':'tls'}},
                                    {'protocol':'unsupported-tunnel','settings':{'secret':'private'}}]})
    with pytest.raises(ProviderError):
        profiles.parse_material('trojan',{'xray_config':material})

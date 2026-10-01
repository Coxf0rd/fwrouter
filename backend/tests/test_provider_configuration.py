from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fwrouter_api.db import provider_managed as store
from fwrouter_api.services import provider_managed as service
from fwrouter_api.services.provider_adapters import provider_adapter
from fwrouter_api.routes.subscription import ProviderConfigurationRequest, provider_configuration_endpoint


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    store.ensure_schema(conn)
    conn.execute('CREATE TABLE server_preferences (server_id TEXT PRIMARY KEY,vpn_auto INTEGER,vpn_auto_priority INTEGER)')
    @contextmanager
    def session():
        yield conn
    monkeypatch.setattr(service, 'db_session', session)
    monkeypatch.setattr('fwrouter_api.db.connection.db_session', session)
    monkeypatch.setattr('fwrouter_api.adapters.xray_common.xray_writer_guard', lambda: nullcontext())
    monkeypatch.setattr('fwrouter_api.services.subscription._subscription_url_for_source_ref', lambda _: 'https://ordinary.example.test/private-source-token')
    monkeypatch.setattr('fwrouter_api.services.events.write_audit_event', lambda **kw: None)
    return conn


def test_toggle_is_explicit_and_has_no_url_recognition_or_provider_io(db, monkeypatch):
    monkeypatch.setattr(service, 'provider_adapter', lambda *a, **kw: pytest.fail('intent cannot poll provider'))
    dto = service.save_provider_configuration('ordinary', enabled=True)
    assert dto['enabled'] and not dto['configured'] and dto['resource_id'] is None
    assert store.list_bindings(db)[0]['source_ref'] == 'ordinary'
    dto = service.save_provider_configuration('ordinary', enabled=False)
    assert not dto['enabled']
    assert service.fetch_provider_subscription('ordinary') is None


def test_write_only_key_and_replacement_are_scoped(db):
    first = service.save_provider_configuration('a', enabled=True, api_key='secret-a', resource_id=11)
    second = service.save_provider_configuration('b', enabled=True, api_key='secret-b', resource_id=22)
    assert first['configured'] and second['configured']
    assert first['resource_id'] == 11 and second['resource_id'] == 22
    assert 'secret-' not in json.dumps([first,second])
    assert 'api_key' not in repr(ProviderConfigurationRequest(api_key='secret-a'))
    assert 'api_key' not in ProviderConfigurationRequest(api_key='secret-a').model_dump()
    service.save_provider_configuration('a', api_key='replacement-a')
    assert store.get_credential(db, 'a') == 'replacement-a'
    assert store.get_credential(db, 'b') == 'secret-b'
    assert store.get_binding(db, 'a')['resource_id'] == ''
    clients=[]
    factory=lambda key, **kw: clients.append((key,kw)) or SimpleNamespace()
    provider_adapter('stealthsurf', source_ref='a', client_factory=factory)
    provider_adapter('stealthsurf', source_ref='b', client_factory=factory)
    assert [key for key,_ in clients] == ['replacement-a','secret-b']


@pytest.mark.parametrize('key', [{'secret-value': 1}, 123, '', ['secret-value']])
def test_invalid_credential_is_not_reflected_in_response(db, key):
    ref='src:'+'a'*64
    response=provider_configuration_endpoint(ref,ProviderConfigurationRequest(enabled=True,api_key=key))
    assert not response.ok
    assert 'secret-value' not in response.model_dump_json()
    assert response.error['code']=='PROVIDER_CREDENTIAL_INVALID'


class Configs:
    def __init__(self, rows): self.rows=rows; self.calls=0
    def get_configs(self, **kwargs):
        self.calls+=1
        assert kwargs['budget'].max_requests==1
        return self.rows


@pytest.mark.parametrize('ids,selected', [([11],11), ([11,22],None), ([],None)])
def test_one_config_auto_select_and_multiple_configs_require_choice(db, ids, selected):
    service.save_provider_configuration('a',enabled=True,api_key='never-return-key')
    adapter=Configs([{'id':i,'name':f'Account config {i}','connection_url':'hysteria2://private-material'} for i in ids])
    result=service.discover_provider_configs('a',_adapter=adapter)
    assert result['selected_resource_id']==selected
    assert [row['resource_id'] for row in result['configs']]==ids
    assert 'private-material' not in json.dumps(result)
    assert adapter.calls==1
    if len(ids)>1:
        dto=service.save_provider_configuration('a',resource_id=22)
        assert dto['resource_id']==22


def test_disabled_discovery_never_calls_provider(db):
    service.save_provider_configuration('a',enabled=False,api_key='test')
    adapter=Configs([])
    with pytest.raises(service.ProviderError,match='PROVIDER_DISABLED'):
        service.discover_provider_configs('a',_adapter=adapter)
    assert adapter.calls==0


def test_member_runtime_identity_is_account_and_source_scoped(db):
    a=service.save_provider_configuration('a',enabled=True,api_key='key-a',resource_id=11)
    b=service.save_provider_configuration('b',enabled=True,api_key='key-b',resource_id=22)
    assert service.provider_runtime_member_id(a,1456,'hysteria2') != service.provider_runtime_member_id(b,1456,'hysteria2')


@pytest.mark.parametrize('verified',[True,False])
def test_enable_selects_scoped_candidate_and_always_requests_effective_apply(db, monkeypatch, verified):
    from test_provider_managed_integration import FakeAdapter
    service.save_provider_configuration('a',enabled=True,api_key='key-a',resource_id=1234)
    service.save_provider_configuration('b',enabled=True,api_key='key-b',resource_id=1234)
    adapter=FakeAdapter()
    calls=[]
    def refresh(binding, config, **kwargs):
        calls.append((binding['source_ref'],config['server_id'],kwargs))
        return {'ok':verified,'runtime_verified':verified,'last_good_retained':not verified}
    monkeypatch.setattr(service,'_refresh_with_material',refresh)
    result=service.execute_provider_operation('a','enable',_adapter=adapter)
    assert calls==[('a',901,{'select_logical':True})]
    assert result['ok'] is verified
    assert result['runtime_verified'] is verified
    assert store.get_binding(db,'a')['enabled']==1
    assert store.get_binding(db,'b')['current_member_id'] is None
    if not verified:
        assert result['outcome']=='failed' and result['last_good_retained']


def test_enable_omitted_current_with_negative_preference_retains_last_good(db,monkeypatch):
    from test_provider_managed_integration import FakeAdapter
    service.save_provider_configuration('a',enabled=True,api_key='test',resource_id=1234)
    binding=store.get_binding(db,'a')
    store.record_config(db,'a',binding['binding_revision'],{'server_id':901,'location_id':6,'protocol':'hysteria2'})
    store.update_member_preference(db,'a',901,6,'hysteria2',auto_enabled=False,expected_binding_revision=binding['binding_revision'])
    adapter=FakeAdapter()
    adapter.discover=lambda *args, **kwargs: []
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:pytest.fail('No candidate must not apply'))
    result=service.execute_provider_operation('a','enable',_adapter=adapter)
    assert not result['ok'] and result['last_good_retained']
    assert result['error_code']=='PROVIDER_CANDIDATE_UNAVAILABLE'
    assert store.get_binding(db,'a')['enabled']==1


def test_configuration_audit_contains_only_safe_configuration(db,monkeypatch):
    events=[]
    monkeypatch.setattr('fwrouter_api.services.events.write_audit_event',lambda **kw:events.append({k:v for k,v in kw.items() if k!='connection'}))
    service.save_provider_configuration('a',enabled=True,api_key='never-audit-me',resource_id=1234)
    assert len(events)==1
    assert 'never-audit-me' not in json.dumps(events)
    assert events[0]['new_value']['configured'] is True


def test_key_replacement_invalidates_observation_but_preserves_last_good_application(db):
    dto=service.save_provider_configuration('a',enabled=True,api_key='old',resource_id=1234)
    store.record_config(db,'a',dto['binding_revision'],{'server_id':901,'location_id':6,'protocol':'hysteria2'})
    store.record_applied(db,'a',revision=dto['binding_revision'],member_id=901,protocol='hysteria2',applied_at=100)
    service.save_provider_configuration('a',api_key='new')
    binding=store.get_binding(db,'a')
    assert binding['current_member_id'] is None
    assert binding['applied_member_id']=='901' and binding['applied_at']==100
    assert binding['applied_revision']!=binding['binding_revision']


@pytest.mark.parametrize('verified',[True,False])
def test_enable_imports_current_without_implicit_switch_and_retains_partial_apply(db,monkeypatch,verified):
    from test_provider_managed_integration import FakeAdapter
    dto=service.save_provider_configuration('a',enabled=True,api_key='key',resource_id=1234)
    store.record_discovery(db,'a',dto['binding_revision'],6,'hysteria2',[{'server_id':901,'available_slots':2},{'server_id':902,'available_slots':2}])
    store.update_member_preference(db,'a',902,6,'hysteria2',priority=5,expected_binding_revision=dto['binding_revision'])
    store.record_applied(db,'a',revision=dto['binding_revision'],member_id=900,protocol='hysteria2',applied_at=100)
    adapter=FakeAdapter()
    adapter.discover=lambda *args,**kw:[{'server_id':901,'available_slots':2},{'server_id':902,'available_slots':2}]
    switches=[]
    def switch(config, location, member, protocol, **kw):
        switches.append((config,location,member,protocol))
        return dict(adapter.mutation_result,server_id=member)
    adapter.switch_member=switch
    monkeypatch.setattr(service,'_refresh_with_material',lambda *args,**kw:{'ok':verified,'runtime_verified':verified,'last_good_retained':not verified,'outcome':'success' if verified else 'partial'})
    result=service.execute_provider_operation('a','enable',_adapter=adapter)
    assert switches==[]
    assert result['actual_member_id']=='901' and result['requested_member_id']=='901'
    assert result['ok'] is verified
    if not verified:
        assert result['outcome']=='partial' and result['last_good_retained']
        assert store.get_binding(db,'a')['applied_member_id']=='900'


def test_enable_exact_effective_readback_cannot_accept_old_active_server(db,monkeypatch):
    dto=service.save_provider_configuration('a',enabled=True,api_key='test',resource_id=1234)
    binding=store.get_binding(db,'a')
    monkeypatch.setattr('fwrouter_api.services.logical_topology.get_logical_runtime_name',lambda _: 'provider-logical-runtime')
    monkeypatch.setattr('fwrouter_api.services.runtime_adapters.active_runtime_adapter',lambda _: {})
    runtime=SimpleNamespace(get_logical_group_state=lambda _:{'effective_member_runtime_identity':'new-member'},get_active_server_id=lambda:'old-runtime-server')
    monkeypatch.setattr('fwrouter_api.services.runtime_adapters.runtime_adapter_operations',lambda _:runtime)
    with service.material_handoff(binding,{'server_id':901,'protocol':'hysteria2'},select_logical=True):
        result=service.verify_provider_handoff()
    assert not result['ok'] and result['error_code']=='PROVIDER_EFFECTIVE_TARGET_UNCONFIRMED'


def test_api_configuration_and_projection_never_return_key_or_create_secret_jobs(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.db.connection import db_session,initialize_database
    from fwrouter_api.main import create_app
    from fwrouter_api.services.subscription import _source_id
    monkeypatch.setenv('FWROUTER_STATE_DIR',str(tmp_path/'state'))
    get_settings.cache_clear();initialize_database()
    url='https://ordinary.example.test/subscription'
    ref=_source_id(url)
    with db_session() as connection:
        connection.execute("INSERT INTO subscription_state(id,url,status,metadata_json) VALUES(1,?,'success',?)",(url,json.dumps({'subscriptions':{'items':[{'url':url,'enabled':True}]}})))
    client=TestClient(create_app(enable_startup_tasks=False))
    response=client.post(f'/api/v2/subscription/sources/{ref}/provider/configuration',json={'enabled':True,'api_key':'fixture-private-key','resource_id':11})
    assert response.status_code==200 and response.json()['ok']
    assert response.json()['data']['binding']['configured'] is True
    projection=client.get('/api/v2/subscription')
    assert 'fixture-private-key' not in response.text+projection.text
    assert 'api_key' not in response.text+projection.text
    with db_session() as connection:
        events=[row[0] for row in connection.execute('SELECT details_json FROM operational_logs')]
        assert 'fixture-private-key' not in repr(events)
        assert connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
        assert store.get_credential(connection,ref)=='fixture-private-key'


def test_env_bootstrap_migrates_only_existing_bound_resources(db,monkeypatch):
    from fwrouter_api.db.migrations import _migrate_22_to_23
    from pydantic import SecretStr
    monkeypatch.setattr('fwrouter_api.core.config.get_settings',lambda:SimpleNamespace(stealthsurf_api_key=SecretStr('bootstrap-fixture-key'),stealthsurf_config_id=11))
    _migrate_22_to_23(db)
    assert store.list_bindings(db)==[]
    store.save_binding(db,'a','stealthsurf',11,'logical-a','hysteria2',True)
    store.save_binding(db,'b','stealthsurf',22,'logical-b','hysteria2',True)
    _migrate_22_to_23(db)
    assert store.get_credential(db,'a')=='bootstrap-fixture-key'
    assert store.get_credential(db,'b') is None
    store.set_credential(db,'a','user-replacement')
    _migrate_22_to_23(db)
    assert store.get_credential(db,'a')=='user-replacement'


def test_settings_projection_and_locale_reads_do_not_call_provider(db,monkeypatch):
    monkeypatch.setattr(service,'provider_adapter',lambda *a,**kw:pytest.fail('Projection must be local'))
    monkeypatch.setattr('fwrouter_api.services.subscription.get_subscription_state',lambda:{'metadata':{'subscriptions':{'items':[{'url':'https://ordinary.example.test/one'},{'url':'https://other.example.test/two'}]}}})
    monkeypatch.setattr('fwrouter_api.services.provider_recovery.emergency_override',lambda:None)
    service.save_provider_configuration('a',enabled=True,api_key='fixture-only',resource_id=1)
    service.save_provider_configuration('b',enabled=True,api_key='fixture-other',resource_id=2)
    for _ in range(3):
        projection=service.provider_projection()
        assert len(projection['bindings'])==4
        assert 'fixture-only' not in json.dumps(projection)


def test_enable_current_omitted_from_discovery_still_requires_verified_local_apply(db,monkeypatch):
    from test_provider_managed_integration import FakeAdapter
    service.save_provider_configuration('a',enabled=True,api_key='key',resource_id=1234)
    adapter=FakeAdapter()
    adapter.discover=lambda *args,**kw:[{'server_id':902,'available_slots':2}]
    calls=[]
    monkeypatch.setattr(service,'_refresh_with_material',lambda binding,config,**kw:calls.append((config['server_id'],kw)) or {'ok':True,'runtime_verified':True})
    result=service.execute_provider_operation('a','enable',_adapter=adapter)
    assert result['ok'] and calls==[(901,{'select_logical':True})]
    assert not any(call[0]=='switch' for call in adapter.calls)


def test_enable_current_material_does_not_require_old_discovery_revision(db,monkeypatch):
    from test_provider_managed_integration import FakeAdapter
    dto=service.save_provider_configuration('a',enabled=True,api_key='old',resource_id=1234)
    store.record_config(db,'a',dto['binding_revision'],{'server_id':901,'location_id':6,'protocol':'hysteria2'})
    service.save_provider_configuration('a',api_key='replacement',resource_id=1234)
    adapter=FakeAdapter();adapter.discover=lambda *a,**kw:[]
    monkeypatch.setattr(service,'_refresh_with_material',lambda *a,**kw:{'ok':True,'runtime_verified':True})
    result=service.execute_provider_operation('a','enable',_adapter=adapter)
    assert result['ok'] and result['actual_member_id']=='901'


def test_config_discovery_cannot_echo_write_only_key_in_label(db):
    service.save_provider_configuration('a',enabled=True,api_key='private-test-key')
    result=service.discover_provider_configs('a',_adapter=Configs([{'id':11,'name':'prefix private-test-key suffix'}]))
    assert 'private-test-key' not in json.dumps(result)
    assert result['configs'][0]['label']=='11'

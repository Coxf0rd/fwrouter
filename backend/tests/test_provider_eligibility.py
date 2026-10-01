from __future__ import annotations

import json
import pytest
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.db import provider_managed as store
from fwrouter_api.services.server_inventory import list_servers
from fwrouter_api.services.server_global_selection import _validate_user_selectable_server, _validate_global_fixed_server
from fwrouter_api.services.selector import _load_selector_candidates
from fwrouter_api.services.server_preferences import update_server_preferences
from fwrouter_api.services.mihomo_config_proxies import _load_vpn_auto_proxy_names
from fwrouter_api.services.xray_subscription_service import _vpn_auto_servers_for_xray_subscription


@pytest.fixture
def inventory():
    initialize_database()
    with db_session() as c:
        for sid in ('old', 'ordinary', 'managed', 'internal'):
            raw={'type':'vless','server':'edge.example','port':443,'uuid':'00000000-0000-4000-8000-000000000241','tls':True,'reality-opts':{'public-key':'fixture-public-key'},'name':sid}
            c.execute("INSERT INTO servers(server_id,server_name,inventory_state,raw_json) VALUES(?,?,'active',?)",(sid,sid,json.dumps(raw)))
            c.execute('INSERT INTO server_preferences(server_id,vpn_auto,global_list) VALUES(?,1,1)',(sid,))
        for sid in ('old','managed'):
            c.execute('INSERT INTO subscription_server_memberships(source_id,server_id,source_url,entry_identity_hash) VALUES(?,?,?,?)',('source-a',sid,'fixture',sid))
        store.save_binding(c,'source-a','stealthsurf',123,'managed','hysteria2',True)
        c.execute("INSERT INTO logical_server_topology(logical_server_id,topology_kind,selection_policy) VALUES('managed','logical_multi','fallback')")
        c.execute("INSERT INTO logical_server_members(logical_server_id,member_id,member_runtime_name,member_config_json,transport_fingerprint,member_order) VALUES('managed','internal','internal','{}','fixture',0)")


def test_enabled_source_masks_ordinary_entries_and_internal_members(inventory):
    rows={r['server_id']:r for r in list_servers()}
    assert rows['old']['provider_managed_legacy'] and not rows['old']['selectable'] and not rows['old']['auto_eligible']
    assert rows['old']['preferences']['vpn_auto']  # persistent preferences are preserved
    assert rows['internal']['provider_internal_member'] and not rows['internal']['selectable']
    assert rows['managed']['selectable'] and rows['ordinary']['selectable']
    assert {r['server_id'] for r in _load_selector_candidates()}=={'managed','ordinary'}
    assert set(_load_vpn_auto_proxy_names())=={'managed','ordinary'}
    assert {r['server_id'] for r in _vpn_auto_servers_for_xray_subscription()}=={'managed','ordinary','virtual:xray:vpn-auto'}
    assert {r['server_id'] for r in list_servers(inventory_state='active',global_list=True)}=={'managed','ordinary'}


def test_user_and_admin_validation_reject_phantom_targets(inventory):
    for validate in (_validate_user_selectable_server,_validate_global_fixed_server):
        assert validate('old')['error_code']=='SERVER_PROVIDER_MANAGED'
        assert validate('internal')['error_code']=='SERVER_INTERNAL_MEMBER'
        assert validate('managed')['ok'] and validate('ordinary')['ok']
    assert not update_server_preferences('old',vpn_auto=True,reconcile_mihomo=False)['ok']


def test_admin_can_include_missing_legacy_without_reactivating_it(inventory):
    with db_session() as c:
        c.execute("UPDATE servers SET inventory_state='missing' WHERE server_id='old'")
    assert 'old' not in {r['server_id'] for r in list_servers(inventory_state='active')}
    row=next(r for r in list_servers(inventory_state='active',include_provider_legacy=True) if r['server_id']=='old')
    assert row['inventory_state']=='missing' and row['provider_managed_legacy'] and not row['selectable']


def test_active_shared_ordinary_owner_preserves_eligibility(inventory):
    with db_session() as c:
        c.execute("INSERT INTO subscription_server_memberships(source_id,server_id,source_url,entry_identity_hash) VALUES('source-b','old','fixture','shared')")
    row=next(r for r in list_servers() if r['server_id']=='old')
    assert not row['provider_managed_legacy'] and row['selectable'] and row['auto_eligible']
    assert _validate_user_selectable_server('old')['ok']


def test_disable_restores_ordinary_preferences_without_rewriting_them(inventory):
    with db_session() as c:
        c.execute("UPDATE provider_bindings SET enabled=0 WHERE source_ref='source-a'")
    old=next(r for r in list_servers() if r['server_id']=='old')
    assert old['selectable'] and old['auto_eligible'] and old['preferences']['vpn_auto']


def test_manual_only_with_no_visible_list_is_not_a_user_target(inventory):
    with db_session() as c:
        c.execute("UPDATE server_preferences SET vpn_auto_priority=-1,global_list=0 WHERE server_id='ordinary'")
    row=next(r for r in list_servers() if r['server_id']=='ordinary')
    assert not row['selectable'] and not row['auto_eligible']
    assert not _validate_user_selectable_server('ordinary')['ok']
    assert _validate_global_fixed_server('ordinary')['ok']

from fwrouter_api.routes import subscription as routes
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.subscription_refresh_job import SUBSCRIPTION_REFRESH_LOCK_KEY

SOURCE = 'src:' + 'a' * 64


class Manager:
    def __init__(self, conflict=None):
        self.conflict = conflict
        self.created = None
        self.handlers = {}

    def register_handler(self, name, handler):
        self.handlers[name] = handler

    def create(self, name, **kwargs):
        self.created = (name, kwargs)
        if self.conflict:
            raise JobLockConflictError(kwargs['lock_key'], self.conflict)
        return {'job_id': 'exclusive-job', 'job_type': name, 'status': 'queued', 'input': kwargs['input_data']}

    def start_job(self, job_id):
        return None


def test_exclusive_api_reuses_subscription_lock_and_safe_job_input(monkeypatch):
    manager = Manager()
    monkeypatch.setattr(routes, 'get_default_job_manager', lambda: manager)
    response = routes.set_subscription_vpn_auto_exclusive_endpoint(
        SOURCE, routes.SubscriptionVpnAutoExclusiveRequest(enabled=True))
    assert response.ok and response.data['accepted']
    operation, kwargs = manager.created
    assert operation in manager.handlers
    assert kwargs['lock_key'] == SUBSCRIPTION_REFRESH_LOCK_KEY
    assert kwargs['input_data'] == {'source_ref': SOURCE, 'enabled': True}


def test_exclusive_api_deduplicates_same_intent_and_rejects_conflicting_work(monkeypatch):
    operation = 'subscription_vpn_auto_exclusive'
    active = {'job_id': 'active', 'job_type': operation, 'input': {'source_ref': SOURCE, 'enabled': True}}
    manager = Manager(active)
    monkeypatch.setattr(routes, 'get_default_job_manager', lambda: manager)
    response = routes.set_subscription_vpn_auto_exclusive_endpoint(
        SOURCE, routes.SubscriptionVpnAutoExclusiveRequest(enabled=True))
    assert response.ok and response.data['already_running'] and not response.data['accepted']
    response = routes.set_subscription_vpn_auto_exclusive_endpoint(
        SOURCE, routes.SubscriptionVpnAutoExclusiveRequest(enabled=False))
    assert not response.ok
    assert response.error['code'] == 'SUBSCRIPTION_OPERATION_IN_PROGRESS'


def test_exclusive_api_rejects_invalid_reference_before_creating_job(monkeypatch):
    monkeypatch.setattr(routes, 'get_default_job_manager', lambda: (_ for _ in ()).throw(AssertionError('No job')))
    response = routes.set_subscription_vpn_auto_exclusive_endpoint(
        'not-a-source', routes.SubscriptionVpnAutoExclusiveRequest(enabled=True))
    assert not response.ok and response.error['code'] == 'SUBSCRIPTION_SOURCE_REF_INVALID'

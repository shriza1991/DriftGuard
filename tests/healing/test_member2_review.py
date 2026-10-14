"""Review regressions using Member 1's real ASGI API, not invented errors."""
import asyncio
import json
import time
import httpx
import pytest
from langchain_core.messages import AIMessage
from mock_api.app import app as mock_app
from mock_api.config import settings as mock_settings
from orchestrator.client.mock_client import MockBusinessApiClient, MockAdminApiClient, redact_secrets
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.events import EventBroadcaster
from orchestrator.models import ExecutionContext
from orchestrator.healing.recovery import RecoveryController, RecoveryStatus
from orchestrator.agents.healing import HealingAgent
from orchestrator.agents.baseline import BaselineAgent
from orchestrator.audit.store import SQLiteAuditStore


@pytest.mark.parametrize('scenario', ['rename','type_change','nesting','required_header','combined'])
async def test_actual_mock_api_recovery_and_contract_reuse(scenario):
    transport = httpx.ASGITransport(app=mock_app)
    admin = MockAdminApiClient(base_url='http://mock', transport=transport)
    client = MockBusinessApiClient(base_url='http://mock', transport=transport,
        credential_resolver=lambda ref: mock_settings.assignment_bearer_token if ref == 'secops_auth' else None)
    try:
        await admin.reset()
        await admin.set_mutation(scenario, evidence_level='explicit_hint')
        registry = VersionedToolRegistry.create_default()
        executor = ToolExecutor(registry=registry, client=client)
        original = {'ticket_id':81,'user_id':'123'}
        failed = await executor.execute('assign_ticket', original, ExecutionContext(run_id='review',tool_call_id='assign',idempotency_key='review-1'))
        controller = RecoveryController(registry, executor, event_broadcaster=EventBroadcaster(),
            approved_header_credentials={'Authorization':'secops_auth'})
        recovered = await controller.recover_failure(failed.observation, original, failed.operation_identity, 'review', deadline=2)
        assert recovered.recovered, recovered.termination_reason
        assert original == {'ticket_id':81,'user_id':'123'}
        active = registry.get('assign_ticket')
        if scenario == 'rename':
            args = {'ticket_id':81,'assignee_id':'123'}
            assert 'user_id' not in active.argument_schema.properties
            assert 'assignee_id' in active.argument_schema.required
        elif scenario in ('nesting','combined'):
            args = {'ticket_id':81,'target':{'id':123 if scenario=='combined' else '123'}}
            assert 'target' in active.argument_schema.required
        elif scenario == 'type_change':
            args = {'ticket_id':81,'user_id':123}
        else:
            args = original
        second = await executor.execute('assign_ticket', args, ExecutionContext(run_id='review2', idempotency_key='review-2'))
        assert second.is_success, second.failure
        ticket = await executor.execute('get_ticket', {'ticket_id':81})
        assert ticket.outcome['assignee_id']=='123'
        assert len(ticket.outcome['history'])==2
    finally:
        await admin.reset()
        await admin.aclose()
        await client.aclose()


class Model:
    def __init__(self, messages):self.messages=list(messages)
    def bind_tools(self, tools):self.tools=tools;return self
    async def ainvoke(self, messages):return self.messages.pop(0)


async def test_healing_never_accepts_wrong_assignee():
    def handler(req):
        if req.url.path=='/users':return httpx.Response(200,json=[{'id':'123','name':'Maya','email':'m@test'}])
        return httpx.Response(200,json={'id':81,'status':'assigned','assignee_id':'999','history':[]})
    client=MockBusinessApiClient(transport=httpx.MockTransport(handler))
    model=Model([AIMessage(content='',tool_calls=[{'name':'find_user','args':{'name':'Maya'},'id':'find'}]), AIMessage(content='done')])
    result=await HealingAgent(client=client,model=model).execute_workflow('review-wrong')
    assert result['status']=='failed'
    await client.aclose()


async def test_real_baseline_with_audit_does_not_crash_on_usage(tmp_path):
    transport=httpx.ASGITransport(app=mock_app)
    admin=MockAdminApiClient(base_url='http://mock',transport=transport);await admin.reset()
    client=MockBusinessApiClient(base_url='http://mock',transport=transport)
    store=SQLiteAuditStore(str(tmp_path/'audit.sqlite3'));await store.initialize()
    model=Model([AIMessage(content='',tool_calls=[{'name':'find_user','args':{'name':'Maya'},'id':'find'}],usage_metadata={'input_tokens':10,'output_tokens':2,'total_tokens':12}),AIMessage(content='done')])
    result=await BaselineAgent(client=client,model=model,audit_store=store).execute_workflow('review-baseline')
    assert result['termination_reason']=='verification_failed'
    assert len(await store.get_run_model_usages('review-baseline'))==2
    await admin.aclose();await client.aclose()


async def test_healing_model_deadline_is_enforced():
    class Slow(Model):
        async def ainvoke(self,messages):await asyncio.sleep(.5);return AIMessage(content='done')
    client=MockBusinessApiClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={})))
    start=time.perf_counter()
    result=await HealingAgent(client=client,model=Slow([]),timeout_seconds=.01).execute_workflow('review-timeout')
    assert time.perf_counter()-start<.2
    assert result['status']=='failed'
    assert result['termination_reason']=='deadline_exceeded'
    await client.aclose()


async def test_event_delivery_is_durable_and_usage_survives(tmp_path):
    store=SQLiteAuditStore(str(tmp_path/'events.sqlite3'));await store.initialize()
    await store.record_run_start('durable','healing','task')
    broadcaster=EventBroadcaster(store);sub=broadcaster.register_subscriber('durable')
    broadcaster.publish_event('durable','run_started','healing',data={'diagnosis_tokens':12,'message':'Bearer abc_secret_token'})
    assert sub.queue.empty()
    await broadcaster.flush('durable')
    event=await sub.queue.get()
    persisted=await store.get_run_events('durable')
    assert persisted[0].sequence==event.sequence
    assert persisted[0].data['diagnosis_tokens']==12
    assert 'abc_secret_token' not in json.dumps(persisted[0].data)


async def test_recovery_verifier_is_inside_deadline():
    from tests.healing.test_recovery import _create_assign_observation
    registry=VersionedToolRegistry.create_default()
    client=MockBusinessApiClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'ticket_id':81,'assignee_id':'123','status':'assigned'})))
    async def slow(**kw):await asyncio.sleep(.5);return {'verified':True}
    ctrl=RecoveryController(registry,ToolExecutor(registry,client),verifier=slow,event_broadcaster=EventBroadcaster())
    obs=_create_assign_observation();start=time.perf_counter()
    result=await ctrl.recover_failure(obs,obs.original_arguments,obs.operation_identity,'deadline',deadline=.01)
    assert result.status==RecoveryStatus.TIMEOUT
    assert time.perf_counter()-start<.2
    assert registry.get('assign_ticket').version=='1.0.0'
    assert not any(c.status=='staged' for c in registry.list_staged())
    await client.aclose()


@pytest.mark.parametrize('agent_type',[BaselineAgent,HealingAgent])
async def test_unresolved_or_wrong_intent_never_writes(agent_type):
    calls=[]
    def handler(req):
        calls.append(req.method)
        return httpx.Response(200,json={'id':81,'status':'assigned','assignee_id':'999','history':[]})
    client=MockBusinessApiClient(transport=httpx.MockTransport(handler))
    model=Model([AIMessage(content='',tool_calls=[{'name':'assign_ticket','args':{'ticket_id':81,'user_id':'999'},'id':'wrong'}]),AIMessage(content='done')])
    result=await agent_type(client=client,model=model).execute_workflow('intent-'+agent_type.__name__)
    assert 'POST' not in calls
    assert result['status']=='failed'
    await client.aclose()


async def test_public_mutation_healing_audit_and_replay(tmp_path,monkeypatch):
    from orchestrator.app import app
    from orchestrator.execution.events import event_broadcaster
    from orchestrator.execution.workflow import workflow_manager
    import orchestrator.agents.healing as module
    old_state={k:getattr(app.state,k) for k in ('mock_client','admin_client','audit_store','registry')}
    old_store=event_broadcaster._audit_store
    old_manager_store=workflow_manager.audit_store
    transport=httpx.ASGITransport(app=mock_app)
    business=MockBusinessApiClient(base_url='http://mock',transport=transport,
        credential_resolver=lambda ref:mock_settings.assignment_bearer_token if ref=='secops_auth' else None)
    admin=MockAdminApiClient(base_url='http://mock',transport=transport)
    store=SQLiteAuditStore(str(tmp_path/'public.sqlite3'));await store.initialize()
    app.state.mock_client=business;app.state.admin_client=admin;app.state.audit_store=store
    app.state.registry=VersionedToolRegistry.create_default()
    workflow_manager.reset();workflow_manager.set_audit_store(store)
    event_broadcaster.set_audit_store(store)
    model=Model([
        AIMessage(content='',tool_calls=[{'name':'find_user','args':{'name':'Maya'},'id':'find'}],usage_metadata={'input_tokens':10,'output_tokens':2,'total_tokens':12}),
        AIMessage(content='',tool_calls=[{'name':'assign_ticket','args':{'ticket_id':81,'user_id':'123'},'id':'assign'}]),
        AIMessage(content='done')])
    monkeypatch.setattr(module,'create_chat_model',lambda:model)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://orch') as c:
            assert (await c.post('/demo/reset')).status_code==200
            mutation=await c.post('/demo/mutation',json={'scenario':'combined','evidence_level':'explicit_hint'})
            assert mutation.status_code==200,mutation.text
            run=await c.post('/runs',json={'mode':'healing','wait':True})
            assert run.status_code==200,run.text
            record=run.json();assert record['status']=='completed',record
            run_id=record['run_id']
            persisted=await store.get_run(run_id);assert persisted.status=='completed'
            repairs=await store.get_run_repair_attempts(run_id);assert len(repairs)==1 and repairs[0].registry_committed
            events=await store.get_run_events(run_id)
            assert [e.sequence for e in events]==list(range(1,len(events)+1))
            assert all(e.event_type != 'pre_write_audit_authorized' for e in events)
            replay=await c.get(f'/runs/{run_id}/events',headers={'Last-Event-ID':'2'})
            ids=[int(line[4:]) for line in replay.text.splitlines() if line.startswith('id: ')]
            assert ids==list(range(3,len(events)+1)), replay.text
            assert (await c.get(f'/runs/{run_id}/events?cursor=1&last_event_id=2')).status_code==400
            assert (await c.get('/patches')).json()['status']=='available'
    finally:
        await event_broadcaster.flush()
        event_broadcaster.set_audit_store(old_store);workflow_manager.set_audit_store(old_manager_store)
        for k,v in old_state.items():setattr(app.state,k,v)
        await admin.reset();await business.aclose();await admin.aclose()


def test_correspondence_is_directional_not_keyword_cooccurrence():
    from orchestrator.healing.validator import _is_supported_natural_language_rename
    assert not _is_supported_natural_language_rename('assignee_id was renamed to user_id','user_id','assignee_id')
    assert not _is_supported_natural_language_rename('user_id and assignee_id exist. unrelated was renamed to other.','user_id','assignee_id')
    assert _is_supported_natural_language_rename('user_id was renamed to assignee_id','user_id','assignee_id')

import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import requests
from app import create_app
from services.worker import process


class FakeStore:
    """External dependencies replaced; shared state survives Flask app recreation."""
    def __init__(self):
        self.accounts = {'a': dict(id='a', business_name='A', system_prompt='Business A'),
                         'b': dict(id='b', business_name='B', system_prompt='Business B')}
        self.events = {}
        self.conversations = {}
        self.messages = {}
    def account(self, aid): return self.accounts.get(aid)
    def instagram_account(self, iid): return self.account({'ig-a': 'a', 'ig-a-scoped': 'a', 'ig-b': 'b'}.get(iid))
    def ready(self): pass
    def setup(self, data): return dict(id='new', **data)
    def enqueue(self, aid, sender, content, external, channel, occurred_at=None):
        cid = aid + ':' + sender
        eid = cid + ':' + channel + ':' + external
        self.conversations.setdefault(cid, dict(id=cid, account_id=aid, sender_id=sender, human_required=False))
        self.messages.setdefault(cid, [])
        self.events.setdefault(eid, dict(id=eid, conversation_id=cid, content=content,
            channel=channel, status='pending', attempts=1, response=None,
            created_at=occurred_at or datetime.now(timezone.utc).isoformat()))
        return self.events[eid]
    def event(self, eid): return self.events.get(eid)
    def conversation(self, cid): return self.conversations[cid]
    def context(self, cid): return list(self.messages[cid])
    def handoff(self, cid, required): self.conversations[cid]['human_required'] = required
    def update_sender_profile(self, cid, profile): self.conversations[cid].update(profile)
    def handoffs(self):
        return [c for c in self.conversations.values() if c['human_required']]
    def save_response(self, event, text): event['response'] = text
    def finish(self, event, status, error_code=None):
        event['status'] = status
        if status == 'done':
            self.messages[event['conversation_id']].append(dict(role='assistant', content=event['response']))
    def start(self, event):
        self.messages[event['conversation_id']].append(dict(role='user', content=event['content']))


class LLM:
    def __init__(self): self.messages = self; self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text='Bonjour')])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ADMIN_API_KEY='local-test', META_APP_SECRET='secret', META_VERIFY_TOKEN='verify')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = FakeStore()
        self.client = create_app(self.store).test_client()
        self.auth = {'Authorization': 'Bearer local-test'}
    def signed(self, payload):
        raw = json.dumps(payload).encode()
        sig = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhook', data=raw, content_type='application/json', headers={'X-Hub-Signature-256': sig})
    def payload(self, echo=False):
        return {'object': 'instagram', 'entry': [{'id': 'ig-a', 'messaging': [{
            'sender': {'id': 'user'}, 'recipient': {'id': 'ig-a'}, 'timestamp': int(datetime.now(timezone.utc).timestamp()*1000),
            'message': {'mid': 'mid1', 'text': 'Bonjour', 'is_echo': echo}}]}]}
    def test_admin_required(self):
        self.assertEqual(self.client.post('/api/setup', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/simulate', json={}).status_code, 401)
    def test_validation(self):
        self.assertEqual(self.client.post('/api/simulate', json=[], headers=self.auth).status_code, 400)
        for message in ['', 'x'*2001, None]:
            self.assertEqual(self.client.post('/api/simulate', json={'account_id':'a','sender_id':'u','message':message}, headers=self.auth).status_code,400)
    def test_challenge(self):
        result = self.client.get('/webhook?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=123')
        self.assertEqual(result.data, b'123')
        self.assertEqual(self.client.get('/webhook?hub.verify_token=wrong').status_code,403)
    def test_public_legal_pages(self):
        privacy = self.client.get('/privacy')
        deletion = self.client.get('/data-deletion')
        self.assertEqual(privacy.status_code, 200)
        self.assertEqual(deletion.status_code, 200)
        self.assertIn(b'alterys.agency@gmail.com', privacy.data)
        self.assertIn(b'30 jours', deletion.data)
    def test_signature_and_echo(self):
        self.assertEqual(self.client.post('/webhook',json=self.payload()).status_code,403)
        self.assertEqual(self.signed(self.payload(True)).status_code,200)
        self.assertEqual(len(self.store.events),0)
    def test_duplicate(self):
        self.signed(self.payload()); self.signed(self.payload())
        self.assertEqual(len(self.store.events),1)
    def test_scoped_instagram_id_routes_to_same_business(self):
        payload=self.payload(); payload['entry'][0]['id']='ig-a-scoped'; payload['entry'][0]['messaging'][0]['recipient']['id']='ig-a-scoped'
        self.assertEqual(self.signed(payload).status_code,200)
        self.assertEqual(len(self.store.events),1)
    def test_recipient_mismatch(self):
        payload=self.payload(); payload['entry'][0]['id']='ig-b'
        self.assertEqual(self.signed(payload).status_code,400)
    def test_memory_and_business_isolation(self):
        llm=LLM()
        e=self.store.enqueue('a','user','first','1','simulation');self.store.start(e);process(self.store,e,llm)
        # A fresh app references the durable dependency, not any Flask RAM history.
        create_app(self.store)
        e=self.store.enqueue('a','user','second','2','simulation');self.store.start(e);process(self.store,e,llm)
        self.assertEqual([m['content'] for m in llm.calls[-1]['messages']],['first','Bonjour','second'])
        e=self.store.enqueue('b','user','different','3','simulation');self.store.start(e);process(self.store,e,llm)
        self.assertEqual(llm.calls[-1]['messages'],[dict(role='user',content='different')])
        self.assertTrue(llm.calls[-1]['system'].startswith('Business B'))
    def test_handoff(self):
        e=self.store.enqueue('a','u','Je veux parler à quelqu’un','1','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(e['status'],'done');self.assertEqual(llm.calls,[])
        self.assertTrue(self.store.conversation(e['conversation_id'])['human_required'])
        self.assertIn('prise en charge humaine', e['response'])
    def test_confirmation_after_handoff_offer(self):
        e=self.store.enqueue('a','u','Projet précis','1','simulation');self.store.start(e)
        self.store.messages[e['conversation_id']].append(
            dict(role='assistant', content='Souhaitez-vous que je transmette votre demande ?'))
        e=self.store.enqueue('a','u','Oui','2','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(e['status'],'done');self.assertEqual(llm.calls,[])
        self.assertTrue(self.store.conversation(e['conversation_id'])['human_required'])
    def test_handoff_queue_requires_admin(self):
        self.store.conversations['a:u'] = dict(id='a:u', account_id='a', sender_id='u', human_required=True)
        self.assertEqual(self.client.get('/api/handoffs').status_code, 401)
        result = self.client.get('/api/handoffs', headers=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['items']), 1)
    def test_instagram_profile_is_saved(self):
        e=self.store.enqueue('a','ig-user','Bonjour','1','instagram');self.store.start(e)
        profile=lambda account,sender: dict(sender_username='client_test',sender_name='Client Test')
        process(self.store,e,LLM(),lambda *args: None,profile)
        conversation=self.store.conversation(e['conversation_id'])
        self.assertEqual(conversation['sender_username'],'client_test')
        self.assertEqual(conversation['sender_name'],'Client Test')
    def test_send_timeout_no_resend(self):
        e=self.store.enqueue('a','u','Bonjour','1','instagram');self.store.start(e)
        def send(*args): raise requests.Timeout()
        process(self.store,e,LLM(),send)
        self.assertEqual(e['status'],'delivery_unknown')
        self.assertTrue(self.store.conversation(e['conversation_id'])['human_required'])
    def test_retry_reuses_response(self):
        e=self.store.enqueue('a','u','Bonjour','1','simulation');e['response']='Saved'
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(llm.calls,[]);self.assertEqual(e['status'],'done')
    def test_dependency_failure(self):
        with patch.object(self.store,'ready',side_effect=RuntimeError('private token')):
            result=self.client.get('/health/ready')
        self.assertEqual(result.status_code,503)
        self.assertNotIn(b'private token',result.data)
    def test_old_dm_not_sent(self):
        e=self.store.enqueue('a','u','Bonjour','1','instagram', '2020-01-01T00:00:00+00:00')
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(e['status'],'expired');self.assertEqual(llm.calls,[])
    def test_upstream_value_error_redacted(self):
        with patch.object(self.store,'ready',side_effect=ValueError('private token')):
            result=self.client.get('/health/ready')
        self.assertEqual(result.status_code,503);self.assertNotIn(b'private token',result.data)


if __name__ == '__main__': unittest.main()

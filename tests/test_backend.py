import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import requests
from app import create_app
from services.worker import process, process_appointment
from services.agent_policy import (contact_consent_reply, critical_reply,
                                   extract_lead_data, greeting_reply, guard_assistant_reply,
                                   lead_summary, normalize_reply)


class FakeStore:
    """External dependencies replaced; shared state survives Flask app recreation."""
    def __init__(self):
        self.accounts = {'a': dict(id='a', business_name='A', system_prompt='Business A'),
                         'b': dict(id='b', business_name='B', system_prompt='Business B')}
        self.events = {}
        self.conversations = {}
        self.messages = {}
        self.appointment_rows = {}
    def account(self, aid): return self.accounts.get(aid)
    def instagram_account(self, iid): return self.account({'ig-a': 'a', 'ig-a-scoped': 'a', 'ig-b': 'b'}.get(iid))
    def ready(self): pass
    def setup(self, data): return dict(id='new', **data)
    def enqueue(self, aid, sender, content, external, channel, occurred_at=None):
        cid = aid + ':' + sender
        eid = cid + ':' + channel + ':' + external
        self.conversations.setdefault(cid, dict(id=cid, account_id=aid, sender_id=sender,
                                                human_required=False, lead_data={}))
        self.messages.setdefault(cid, [])
        self.events.setdefault(eid, dict(id=eid, conversation_id=cid, content=content,
            channel=channel, status='pending', attempts=1, response=None,
            created_at=occurred_at or datetime.now(timezone.utc).isoformat()))
        return self.events[eid]
    def event(self, eid): return self.events.get(eid)
    def conversation(self, cid): return self.conversations[cid]
    def context(self, cid): return list(self.messages[cid])
    def handoff(self, cid, required, lead_data=None, reason=None):
        self.conversations[cid]['human_required'] = required
        if required:
            self.conversations[cid]['lead_data'] = lead_data or {}
            self.conversations[cid]['handoff_reason'] = reason
        else:
            self.conversations[cid].update(lead_data={}, handoff_reason=None,
                                           handoff_requested_at=None)
    def update_sender_profile(self, cid, profile): self.conversations[cid].update(profile)
    def handoffs(self):
        return [c for c in self.conversations.values() if c['human_required']]
    def conversation_detail(self, cid):
        if cid not in self.conversations: raise IndexError(cid)
        return {'conversation': self.conversations[cid], 'messages': self.messages[cid]}
    def match_conversation(self, account_id, conversation_id=None,
                           instagram_username=None, attendee_email=None):
        rows = [row for row in self.conversations.values() if row['account_id'] == account_id]
        if conversation_id:
            matches = [row for row in rows if row['id'] == conversation_id]
            return (matches[0], 'conversation_id') if len(matches) == 1 else (None, None)
        username = (instagram_username or '').lstrip('@').casefold()
        matches = [row for row in rows
                   if str(row.get('sender_username', '')).casefold() == username] if username else []
        if len(matches) == 1: return matches[0], 'instagram_username'
        email = (attendee_email or '').casefold()
        matches = [row for row in rows if str((row.get('lead_data') or {})
                   .get('contact_email', '')).casefold() == email] if email else []
        return ((matches[0], 'lead_email') if len(matches) == 1 else (None, None))
    def upsert_appointment(self, values):
        key = values['account_id'] + ':' + values['external_id']
        current = self.appointment_rows.get(key, {})
        row = dict(current, id=current.get('id', key), **values)
        self.appointment_rows[key] = row
        return row
    def appointments(self, status=None):
        rows = list(self.appointment_rows.values())
        return [row for row in rows if not status or row['status'] == status]
    def appointment(self, appointment_id):
        return next((row for row in self.appointment_rows.values()
                     if row['id'] == appointment_id), None)
    def appointment_detail(self, appointment_id):
        row = self.appointment(appointment_id)
        if not row: raise IndexError(appointment_id)
        return {'appointment': row, 'conversation': None, 'messages': []}
    def link_appointment(self, appointment_id, conversation_id, method='manual'):
        row = self.appointment(appointment_id)
        if not row: raise IndexError(appointment_id)
        conversation, _ = self.match_conversation(row['account_id'], conversation_id)
        if not conversation: raise ValueError('Conversation does not belong to appointment account')
        row.update(conversation_id=conversation_id, match_method=method, status='pending')
        return row
    def appointment_context(self, appointment):
        conversation = self.conversations.get(appointment.get('conversation_id'))
        return (self.account(appointment['account_id']), conversation,
                list(self.messages.get(appointment.get('conversation_id'), [])))
    def finish_appointment(self, appointment, status, error_code=None, brief=None, brief_text=None):
        appointment.update(status=status, error_code=error_code, brief=brief, brief_text=brief_text)
    def save_notion_sync(self, appointment_id, page_id=None, error=None):
        row = self.appointment(appointment_id)
        row.update(notion_page_id=page_id, notion_sync_error=error)
    def save_response(self, event, text): event['response'] = text
    def finish(self, event, status, error_code=None):
        event['status'] = status
        if status == 'done':
            self.messages[event['conversation_id']].append(dict(role='assistant', content=event['response']))
    def start(self, event):
        self.messages[event['conversation_id']].append(dict(role='user', content=event['content']))


class LLM:
    def __init__(self, text='Bonjour'): self.messages = self; self.calls = []; self.text = text
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=self.text)])


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ADMIN_API_KEY='local-test', META_APP_SECRET='secret',
                              META_VERIFY_TOKEN='verify', CALENDAR_INGEST_KEY='calendar-test')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = FakeStore()
        self.client = create_app(self.store).test_client()
        self.auth = {'Authorization': 'Bearer local-test'}
        self.calendar_auth = {'Authorization': 'Bearer calendar-test'}
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
        self.assertEqual(self.client.get('/api/appointments').status_code, 401)
        self.assertEqual(self.client.post('/api/calendar/events', json={}).status_code, 401)
    def test_calendar_event_matches_instagram_username(self):
        self.store.conversations['a:ig-user'] = dict(
            id='a:ig-user', account_id='a', sender_id='ig-user',
            sender_username='thiziri_hml', human_required=False, lead_data={})
        self.store.messages['a:ig-user'] = []
        payload = dict(account_id='a', external_id='google-1', status='confirmed',
                       title='Appel découverte Alterys',
                       start_at='2026-10-25T14:00:00+02:00',
                       end_at='2026-10-25T14:20:00+02:00',
                       instagram_username='@thiziri_hml', attendee_email='client@example.com')
        result = self.client.post('/api/calendar/events', json=payload,
                                  headers=self.calendar_auth)
        self.assertEqual(result.status_code, 202)
        self.assertTrue(result.json['matched'])
        row = self.store.appointment_rows['a:google-1']
        self.assertEqual(row['conversation_id'], 'a:ig-user')
        self.assertEqual(row['match_method'], 'instagram_username')
    def test_calendar_event_never_guesses_ambiguous_email(self):
        for sender in ('one', 'two'):
            cid = 'a:' + sender
            self.store.conversations[cid] = dict(
                id=cid, account_id='a', sender_id=sender, human_required=False,
                lead_data={'contact_email': 'same@example.com'})
            self.store.messages[cid] = []
        payload = dict(account_id='a', external_id='google-2', status='confirmed',
                       title='Appel découverte Alterys',
                       start_at='2026-10-25T14:00:00Z', end_at='2026-10-25T14:20:00Z',
                       attendee_email='same@example.com')
        result = self.client.post('/api/calendar/events', json=payload,
                                  headers=self.calendar_auth)
        self.assertEqual(result.status_code, 202)
        self.assertFalse(result.json['matched'])
    def test_calendar_rejects_invalid_range(self):
        payload = dict(account_id='a', external_id='bad', title='Appel',
                       start_at='2026-10-25T15:00:00Z', end_at='2026-10-25T14:00:00Z')
        self.assertEqual(self.client.post('/api/calendar/events', json=payload,
                         headers=self.calendar_auth).status_code, 400)
    def test_home_exposes_deployed_revision(self):
        with patch.dict(os.environ, RENDER_GIT_COMMIT='1234567890'):
            result = self.client.get('/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['version'], '1234567')
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
        self.assertIn('file de suivi', e['response'])
    def test_confirmation_after_handoff_offer(self):
        e=self.store.enqueue('a','u','Projet précis','1','simulation');self.store.start(e)
        self.store.messages[e['conversation_id']].append(
            dict(role='assistant', content='Souhaitez-vous que je transmette votre demande ?'))
        e=self.store.enqueue('a','u','Oui','2','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(e['status'],'done');self.assertEqual(llm.calls,[])
        self.assertTrue(self.store.conversation(e['conversation_id'])['human_required'])
        self.assertEqual(self.store.conversation(e['conversation_id'])['handoff_reason'], 'client_confirmed')
    def test_contact_capture_requests_consent_without_false_handoff(self):
        cid = 'a:u'
        self.store.conversations[cid] = dict(id=cid, account_id='a', sender_id='u',
                                             human_required=False, lead_data={})
        self.store.messages[cid] = [
            dict(role='user', content='Je reçois 50 DM par semaine et j’utilise Stripe.'),
            dict(role='assistant', content='Quel est le meilleur moyen de vous recontacter, email ou DM Instagram ?'),
        ]
        e=self.store.enqueue('a','u','DM ou test@example.com','1','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(llm.calls, [])
        self.assertFalse(self.store.conversation(cid)['human_required'])
        self.assertIn('Souhaitez-vous que j’enregistre', e['response'])
        self.assertIn('50 DM par semaine', e['response'])
        self.assertNotIn('test@example.com', e['response'])
    def test_natural_contact_question_triggers_deterministic_consent(self):
        history = [
            dict(role='user', content='Je reçois 50 DM par semaine.'),
            dict(role='assistant', content=(
                "Quel moyen de contact préférez-vous pour qu'une personne de l'équipe revienne "
                "vers vous (email, Instagram) ?")),
            dict(role='user', content='DM Instagram ou test@example.com'),
        ]
        response = contact_consent_reply(history)
        self.assertIn('Souhaitez-vous que j’enregistre', response)
        self.assertNotIn("l'équipe", response)
    def test_consent_after_structured_summary_creates_actionable_handoff(self):
        cid = 'a:u'
        self.store.conversations[cid] = dict(id=cid, account_id='a', sender_id='u',
                                             human_required=False, lead_data={})
        self.store.messages[cid] = [
            dict(role='user', content='50 DM par semaine, Stripe, objectif sous 1 semaine, offre 500 euros.'),
            dict(role='assistant', content="Souhaitez-vous que j’enregistre maintenant cette demande dans la file de suivi de l’agence ?"),
        ]
        e=self.store.enqueue('a','u','Oui merci','1','simulation');self.store.start(e)
        process(self.store,e,LLM())
        conversation = self.store.conversation(cid)
        self.assertTrue(conversation['human_required'])
        self.assertEqual(conversation['lead_data']['dm_volume'], '50 par semaine')
        self.assertIn('stripe', conversation['lead_data']['tools'])
        self.assertEqual(conversation['handoff_reason'], 'client_confirmed')
    def test_informal_confirmation_after_consent_creates_handoff(self):
        cid = 'a:u'
        self.store.conversations[cid] = dict(id=cid, account_id='a', sender_id='u',
                                             human_required=False, lead_data={})
        self.store.messages[cid] = [
            dict(role='user', content='Je reçois 50 DM par semaine.'),
            dict(role='assistant', content="Souhaitez-vous que j’enregistre maintenant cette demande dans la file de suivi de l’agence ?"),
        ]
        e=self.store.enqueue('a','u','Bah oui jsp c quoi les options','1','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(llm.calls, [])
        self.assertTrue(self.store.conversation(cid)['human_required'])
        self.assertIn('file de suivi', e['response'])
    def test_generated_false_handoff_claim_requests_missing_contact(self):
        e=self.store.enqueue('a','u','Voici mon projet','1','simulation');self.store.start(e)
        llm=LLM("Une personne de l'équipe va étudier votre demande et vous recontacter.")
        process(self.store,e,llm)
        self.assertFalse(self.store.conversation(e['conversation_id'])['human_required'])
        self.assertIn('quel moyen de contact', e['response'])
        self.assertNotIn('va étudier', e['response'])
    def test_generated_false_handoff_claim_with_contact_requests_consent(self):
        e=self.store.enqueue('a','u','Contactez-moi par DM Instagram','1','simulation');self.store.start(e)
        llm=LLM("Une personne de l'équipe va étudier votre demande et vous recontacter.")
        process(self.store,e,llm)
        self.assertFalse(self.store.conversation(e['conversation_id'])['human_required'])
        self.assertIn('Souhaitez-vous que j’enregistre', e['response'])
        self.assertNotIn('va étudier', e['response'])
    def test_generated_future_handoff_variants_are_blocked(self):
        history = [dict(role='user', content='Contactez-moi par DM Instagram')]
        for unsafe in [
            "Une personne de l'équipe reviendra vers vous.",
            "Je l'enregistre pour qu'une personne de l'équipe l'étudie.",
            "Votre demande est enregistrée dans la file de suivi.",
        ]:
            response = guard_assistant_reply(history, unsafe)
            self.assertIn('Souhaitez-vous que j’enregistre', response)
            self.assertNotEqual(response, unsafe)
    def test_handoff_queue_requires_admin(self):
        self.store.conversations['a:u'] = dict(id='a:u', account_id='a', sender_id='u', human_required=True)
        self.assertEqual(self.client.get('/api/handoffs').status_code, 401)
        result = self.client.get('/api/handoffs', headers=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['items']), 1)
    def test_conversation_detail_requires_admin(self):
        self.store.conversations['a:u'] = dict(id='a:u', account_id='a', sender_id='u', human_required=True)
        self.store.messages['a:u'] = [dict(role='user', content='Bonjour')]
        self.assertEqual(self.client.get('/api/conversations/a:u').status_code, 401)
        result = self.client.get('/api/conversations/a:u', headers=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['messages'][0]['content'], 'Bonjour')
    def test_password_question_has_guaranteed_safe_answer(self):
        e=self.store.enqueue('a','u',"Dois-je vous donner mon mot de passe Instagram ?",'1','simulation')
        self.store.start(e); llm=LLM(); process(self.store,e,llm)
        self.assertEqual(llm.calls, [])
        self.assertIn('jamais votre mot de passe', e['response'])
        self.assertIn('autorisation officielle de Meta', e['response'])
        self.assertIn('combien de DM', e['response'])
    def test_first_greeting_is_short_and_deterministic(self):
        history = [dict(role='user', content='Bonjour')]
        self.assertEqual(greeting_reply(history), 'Bonjour ! Comment puis-je vous aider ?')
        e=self.store.enqueue('a','u','Bonjour','1','simulation');self.store.start(e)
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(llm.calls, [])
        self.assertEqual(e['response'], 'Bonjour ! Comment puis-je vous aider ?')
    def test_multipart_critical_question_answers_every_part(self):
        message = "Quels sont vos tarifs, comment fonctionne l'automatisation et faut-il donner mes accès Insta ?"
        response = critical_reply([dict(role='user', content=message)])
        self.assertIn('mot de passe', response)
        self.assertIn('Les DM arrivent', response)
        self.assertIn('Le tarif dépend', response)
    def test_lead_extraction_uses_explicit_facts(self):
        history = [dict(role='user', content=(
            'Je reçois 50 DM par semaine. Mon offre coûte 500 euros, Stripe est utilisé, '
            'je veux prévenir avant le lien de paiement et être prêt sous 1 semaine. '
            'Contact par DM ou test@example.com.'))]
        data = extract_lead_data(history)
        self.assertEqual(data['dm_volume'], '50 par semaine')
        self.assertEqual(data['prospect_prices'], ['500 euros'])
        self.assertEqual(data['tools'], ['stripe'])
        self.assertTrue(data['manual_approval_before_payment_link'])
        self.assertEqual(data['desired_deadline'], 'sous 1 semaine')
        self.assertEqual(data['contact_email'], 'test@example.com')
        self.assertTrue(data['instagram_contact_ok'])
        reverse = extract_lead_data([dict(role='user', content=
            'Envoyer un lien Stripe seulement après ma validation.')])
        self.assertTrue(reverse['manual_approval_before_payment_link'])
    def test_short_answers_are_extracted_from_question_context(self):
        history = [
            dict(role='user', content='Je crée des sites web HTML.'),
            dict(role='assistant', content='Quel volume de DM recevez-vous par semaine ?'),
            dict(role='user', content='50'),
            dict(role='assistant', content='Quel délai souhaitez-vous pour la mise en place ?'),
            dict(role='user', content='1 semaine pas plus'),
            dict(role='assistant', content='Quel moyen de contact préférez-vous ?'),
            dict(role='user', content='DM insta'),
            dict(role='assistant', content='La mise à jour est-elle incluse ?'),
            dict(role='user', content='500 euros et il y a 1 mise à jour incluse'),
        ]
        data = extract_lead_data(history)
        self.assertEqual(data['dm_volume'], '50 par semaine')
        self.assertEqual(data['desired_deadline'], '1 semaine')
        self.assertTrue(data['instagram_contact_ok'])
        self.assertEqual(data['business_activity'], 'création de sites web HTML')
        self.assertEqual(data['included_updates'], 1)
        self.assertIn('volume : 50 DM par semaine', lead_summary(history))
    def test_instagram_topic_is_not_mistaken_for_contact_preference(self):
        data = extract_lead_data([dict(role='user', content=
            'Je veux automatiser mes DM Instagram et je reçois 50 DM par semaine.')])
        self.assertNotIn('instagram_contact_ok', data)
    def test_truthfulness_guard_leaves_safe_reply_unchanged(self):
        history = [dict(role='user', content='Bonjour')]
        self.assertEqual(guard_assistant_reply(history, 'Bonjour, comment puis-je vous aider ?'),
                         'Bonjour, comment puis-je vous aider ?')
    def test_critical_reply_does_not_repeat_known_volume(self):
        history = [
            dict(role='user', content='Je reçois environ 80 DM par semaine.'),
            dict(role='assistant', content='Quelles questions reviennent souvent ?'),
            dict(role='user', content='Comment ça marche et faut-il donner mon mot de passe ?'),
        ]
        response = critical_reply(history)
        self.assertNotIn('combien de DM', response)
        self.assertIn('Pour quelle période', response)
    def test_reply_normalization(self):
        response = normalize_reply('Bonjour   test\n\n\nSuite', limit=20)
        self.assertLessEqual(len(response), 21)
        self.assertNotIn('   ', response)
        self.assertEqual(normalize_reply('Phrase incomplète ici', truncated=True), 'Phrase incomplète…')
    def test_instagram_profile_is_saved(self):
        e=self.store.enqueue('a','ig-user','Bonjour','1','instagram');self.store.start(e)
        profile=lambda account,sender: dict(sender_username='client_test',sender_name='Client Test')
        process(self.store,e,LLM(),lambda *args: None,profile)
        conversation=self.store.conversation(e['conversation_id'])
        self.assertEqual(conversation['sender_username'],'client_test')
        self.assertEqual(conversation['sender_name'],'Client Test')
    def test_pre_call_brief_uses_real_instagram_profile_and_explicit_facts(self):
        cid = 'a:ig-user'
        self.store.conversations[cid] = dict(
            id=cid, account_id='a', sender_id='ig-user', sender_username='client_test',
            sender_name='Client Test', human_required=False, lead_data={})
        self.store.messages[cid] = [
            dict(role='user', content='Je crée des sites web et je reçois 50 DM par semaine.'),
            dict(role='assistant', content='Quel est votre principal problème ?'),
            dict(role='user', content='Je perds du temps à répondre aux mêmes questions.'),
        ]
        appointment = dict(
            id='appointment-1', account_id='a', conversation_id=cid,
            external_id='google-1', status='processing', title='Appel découverte Alterys',
            start_at='2026-10-25T14:00:00+02:00', end_at='2026-10-25T14:20:00+02:00',
            timezone='Europe/Paris', attendee_name='Client Test',
            attendee_email='client@example.com', meeting_url='https://meet.google.com/test')
        self.store.appointment_rows['a:google-1'] = appointment
        answer = json.dumps({
            'summary': 'Le prospect veut réduire le temps passé sur les questions répétitives.',
            'main_need': 'Automatiser les réponses répétitives.',
            'pain_points': ['Temps perdu sur les réponses répétitives'],
            'objections': [], 'missing_information': ['Budget non précisé'],
            'recommended_agenda': ['Valider les questions récurrentes'],
            'suggested_questions': ['Quelles questions reviennent le plus ?'],
        })
        brief = process_appointment(self.store, appointment, LLM(answer), notion_sync=False)
        self.assertEqual(appointment['status'], 'ready')
        self.assertEqual(brief['prospect']['instagram_username'], 'client_test')
        self.assertEqual(brief['known_facts']['dm_volume'], '50 par semaine')
        self.assertIn('@client_test', appointment['brief_text'])
        self.assertNotIn('budget de', appointment['brief_text'].casefold())
    def test_unmatched_appointment_is_flagged_without_model_call(self):
        appointment = dict(
            id='appointment-2', account_id='a', conversation_id=None,
            external_id='google-2', status='processing', title='Appel découverte Alterys',
            start_at='2026-10-25T14:00:00Z', end_at='2026-10-25T14:20:00Z',
            attendee_email='unknown@example.com')
        self.store.appointment_rows['a:google-2'] = appointment
        llm = LLM()
        brief = process_appointment(self.store, appointment, llm, notion_sync=False)
        self.assertEqual(llm.calls, [])
        self.assertEqual(appointment['status'], 'unmatched')
        self.assertIn('aucune conversation Instagram certaine', brief['analysis']['summary'])
    def test_booking_form_need_is_preserved_for_unmatched_appointment(self):
        appointment = dict(
            id='appointment-booking-answer', account_id='a', conversation_id=None,
            external_id='google-booking-answer', status='processing',
            title='Appel découverte Alterys',
            start_at='2026-10-25T14:00:00Z', end_at='2026-10-25T14:30:00Z',
            event_payload={'description': (
                'Votre @ Instagram: client_test\n'
                'Décrivez votre activité et votre besoin principal: '
                'Je vends des formations et je veux automatiser les FAQ.\n'
                'Autre champ: valeur')})
        self.store.appointment_rows['a:google-booking-answer'] = appointment
        brief = process_appointment(self.store, appointment, LLM(), notion_sync=False)
        self.assertEqual(appointment['status'], 'unmatched')
        self.assertEqual(
            brief['analysis']['main_need'],
            'Je vends des formations et je veux automatiser les FAQ.')
        self.assertEqual(
            brief['known_facts']['booking_need'],
            'Je vends des formations et je veux automatiser les FAQ.')
    def test_structured_booking_answer_takes_precedence_over_html_description(self):
        appointment = dict(
            id='appointment-structured-answer', account_id='a', conversation_id=None,
            external_id='google-structured-answer', status='processing',
            title='Appel découverte Alterys',
            start_at='2026-10-25T14:00:00Z', end_at='2026-10-25T14:30:00Z',
            event_payload={
                'description': '<b>Texte HTML non fiable</b>',
                'booking_answers': {'need': 'Automatiser les demandes de devis.'},
            })
        self.store.appointment_rows['a:google-structured-answer'] = appointment
        brief = process_appointment(self.store, appointment, LLM(), notion_sync=False)
        self.assertEqual(brief['analysis']['main_need'], 'Automatiser les demandes de devis.')
    def test_notion_failure_does_not_destroy_ready_brief(self):
        cid = 'a:ig-user'
        self.store.conversations[cid] = dict(
            id=cid, account_id='a', sender_id='ig-user', sender_username='client_test',
            sender_name='Client Test', human_required=False, lead_data={})
        self.store.messages[cid] = [dict(role='user', content='Je reçois 20 DM par semaine.')]
        appointment = dict(
            id='appointment-3', account_id='a', conversation_id=cid,
            external_id='google-3', status='processing', title='Appel découverte Alterys',
            start_at='2026-10-25T14:00:00Z', end_at='2026-10-25T14:20:00Z')
        self.store.appointment_rows['a:google-3'] = appointment
        answer = json.dumps({key: ('Résumé' if key in ('summary','main_need') else [])
                             for key in ('summary','main_need','pain_points','objections',
                                         'missing_information','recommended_agenda','suggested_questions')})
        def broken_sync(*args): raise requests.ConnectionError('secret body')
        process_appointment(self.store, appointment, LLM(answer), notion_sync=broken_sync)
        self.assertEqual(appointment['status'], 'ready')
        self.assertEqual(appointment['notion_sync_error'], 'ConnectionError')
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
    def test_readiness_checks_worker_heartbeat_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / 'worker-heartbeat'
            with patch.dict(os.environ, AGENT_WORKER_HEARTBEAT=str(heartbeat)):
                self.assertEqual(self.client.get('/health/ready').status_code, 503)
                heartbeat.touch()
                self.assertEqual(self.client.get('/health/ready').status_code, 200)
                old = datetime.now(timezone.utc).timestamp() - 181
                os.utime(heartbeat, (old, old))
                self.assertEqual(self.client.get('/health/ready').status_code, 503)
    def test_old_dm_not_sent(self):
        e=self.store.enqueue('a','u','Bonjour','1','instagram', '2020-01-01T00:00:00+00:00')
        llm=LLM();process(self.store,e,llm)
        self.assertEqual(e['status'],'expired');self.assertEqual(llm.calls,[])
    def test_upstream_value_error_redacted(self):
        with patch.object(self.store,'ready',side_effect=ValueError('private token')):
            result=self.client.get('/health/ready')
        self.assertEqual(result.status_code,503);self.assertNotIn(b'private token',result.data)


if __name__ == '__main__': unittest.main()

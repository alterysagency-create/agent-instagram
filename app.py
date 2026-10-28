"""Flask backend: durable Supabase memory and verified Meta webhook."""
import hashlib
import hmac
import json
import logging
import os
from functools import wraps
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
import requests
from services.notion_sync import configured as notion_configured, sync_appointment
from services.store import Store

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

class InputError(ValueError):
    pass

def create_app(store=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 262144
    store = store or Store()
    app.extensions['store'] = store
    def admin(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            token = os.getenv('ADMIN_API_KEY', '')
            if not token or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token):
                return jsonify(error='Unauthorized'), 401
            return fn(*args, **kwargs)
        return wrapped
    def calendar_ingest(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            token = os.getenv('CALENDAR_INGEST_KEY', '')
            supplied = request.headers.get('Authorization', '')
            if not token or not hmac.compare_digest(supplied, 'Bearer ' + token):
                return jsonify(error='Unauthorized'), 401
            return fn(*args, **kwargs)
        return wrapped
    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise InputError('Expected JSON object')
        return data
    def field(data, name, limit=2000):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise InputError('Invalid ' + name)
        return value.strip()
    def optional_field(data, name, limit=2000):
        value = data.get(name)
        if value is None or value == '':
            return None
        if not isinstance(value, str) or len(value) > limit:
            raise InputError('Invalid ' + name)
        return value.strip()
    def iso_datetime(data, name, required=True):
        value = optional_field(data, name, 100)
        if value is None and not required:
            return None
        if value is None:
            raise InputError('Invalid ' + name)
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise InputError('Invalid ' + name)
        if parsed.tzinfo is None:
            raise InputError(name + ' must include timezone')
        return parsed.isoformat()
    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.name), exc.code
        if isinstance(exc, InputError):
            return jsonify(error=str(exc)), 400
        app.logger.error('request_failed type=%s', type(exc).__name__)
        return jsonify(error='Service temporarily unavailable'), 503
    @app.get('/')
    def home():
        version = os.getenv('RENDER_GIT_COMMIT', 'local')[:7]
        return jsonify(status='ok', service='AGENT-IA', version=version)
    @app.get('/privacy')
    def privacy():
        return '''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Politique de confidentialite - AGENT-IA</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#202124}h1,h2{line-height:1.25}small{color:#5f6368}</style></head>
<body><h1>Politique de confidentialite - AGENT-IA</h1><small>Derniere mise a jour : 20 octobre 2026</small>
<p>AGENT-IA aide Alterys Agency a repondre aux messages directs recus sur son compte Instagram professionnel et a preparer les rendez-vous reserves avec l'agence.</p>
<h2>Donnees traitees</h2><p>Le service traite l'identifiant Instagram de l'expediteur, le contenu et l'heure des messages, ainsi que les reponses produites. Lors d'une reservation, il traite aussi le nom, l'adresse e-mail, le creneau choisi et les reponses fournies dans le formulaire. Il ne demande ni mot de passe Instagram ni donnee de paiement.</p>
<h2>Utilisation</h2><p>Ces donnees servent uniquement a conserver le contexte de la conversation, generer une reponse et assurer le suivi technique du service.</p>
<h2>Prestataires et conservation</h2><p>Les conversations et fiches sont stockees dans Supabase. Le contenu necessaire aux reponses et aux fiches de preparation est traite par Anthropic. L'application est hebergee par Render, les rendez-vous sont geres par Google Calendar et les fiches de suivi peuvent etre copiees dans Notion. Les donnees sont conservees pendant la duree necessaire au service et peuvent etre supprimees sur demande.</p>
<h2>Vos droits et suppression</h2><p>Pour demander l'acces, la rectification ou la suppression de vos donnees, ecrivez a <a href="mailto:alterys.agency@gmail.com">alterys.agency@gmail.com</a> en indiquant votre identifiant Instagram. Les demandes de suppression sont traitees dans les 30 jours.</p>
<h2>Contact</h2><p>Alterys Agency - <a href="mailto:alterys.agency@gmail.com">alterys.agency@gmail.com</a></p></body></html>'''
    @app.get('/data-deletion')
    def data_deletion():
        return '''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Suppression des donnees - AGENT-IA</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#202124}h1{line-height:1.25}</style></head>
<body><h1>Suppression des donnees AGENT-IA</h1><p>Pour demander la suppression de vos messages, de votre identifiant Instagram ou des informations liees a un rendez-vous, ecrivez a <a href="mailto:alterys.agency@gmail.com">alterys.agency@gmail.com</a> depuis une adresse permettant de confirmer votre demande et indiquez votre identifiant Instagram ou l'adresse e-mail de reservation.</p><p>Nous confirmerons la demande et supprimerons les donnees concernees sous 30 jours.</p></body></html>'''
    @app.get('/health/ready')
    def ready():
        store.ready()
        heartbeat = os.getenv('AGENT_WORKER_HEARTBEAT')
        if heartbeat:
            try:
                age = datetime.now(timezone.utc).timestamp() - Path(heartbeat).stat().st_mtime
            except OSError as exc:
                raise RuntimeError('Worker heartbeat unavailable') from exc
            # A model call can legitimately take more than 30 seconds. This
            # threshold detects a stuck consumer without killing active work.
            if age > 180:
                raise RuntimeError('Worker heartbeat stale')
        return jsonify(status='ready')
    @app.post('/api/setup')
    @admin
    def setup():
        data = body()
        account = {'business_name': field(data, 'business_name', 200), 'system_prompt': field(data, 'system_prompt', 12000)}
        for name in ['email', 'instagram_handle', 'instagram_id', 'instagram_scoped_id', 'instagram_token_env']:
            if data.get(name):
                account[name] = field(data, name, 200)
        account['plan'] = field(data, 'plan', 50) if data.get('plan') else 'starter'
        result = store.setup(account)
        return jsonify(account_id=result['id'], status='success'), 201
    @app.post('/api/simulate')
    @admin
    def simulate():
        data = body()
        account_id = field(data, 'account_id', 200)
        if not store.account(account_id):
            return jsonify(error='Unknown business'), 404
        event = store.enqueue(account_id, field(data, 'sender_id', 200), field(data, 'message'), field(data, 'event_id', 200) if data.get('event_id') else str(uuid4()), 'simulation')
        return jsonify(event_id=event['id'], status=event['status']), 202
    @app.get('/api/events/<event_id>')
    @admin
    def status(event_id):
        result = store.event(event_id)
        if not result:
            return jsonify(error='Unknown event'), 404
        return jsonify({key: result.get(key) for key in ['id', 'status', 'response', 'attempts', 'error_code', 'conversation_id']})
    @app.post('/api/conversations/<conversation_id>/handoff')
    @admin
    def handoff(conversation_id):
        data = body()
        if not isinstance(data.get('human_required'), bool):
            raise InputError('human_required must be boolean')
        store.handoff(conversation_id, data['human_required'])
        return jsonify(status='success')
    @app.get('/api/handoffs')
    @admin
    def handoffs():
        return jsonify(items=store.handoffs())
    @app.get('/api/conversations/<conversation_id>')
    @admin
    def conversation_detail(conversation_id):
        try:
            return jsonify(store.conversation_detail(conversation_id))
        except (IndexError, KeyError):
            return jsonify(error='Unknown conversation'), 404
    @app.post('/api/calendar/events')
    @calendar_ingest
    def calendar_event():
        data = body()
        account_id = field(data, 'account_id', 200)
        if not store.account(account_id):
            return jsonify(error='Unknown business'), 404
        payload = data.get('event_payload') or {}
        if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 50000:
            raise InputError('Invalid event_payload')
        start_at = iso_datetime(data, 'start_at')
        end_at = iso_datetime(data, 'end_at')
        if datetime.fromisoformat(end_at) <= datetime.fromisoformat(start_at):
            raise InputError('end_at must be after start_at')
        external_status = optional_field(data, 'status', 30) or 'confirmed'
        if external_status not in ('confirmed', 'tentative', 'cancelled'):
            raise InputError('Invalid status')
        conversation, match_method = store.match_conversation(
            account_id,
            conversation_id=optional_field(data, 'conversation_id', 200),
            instagram_username=optional_field(data, 'instagram_username', 200),
            attendee_email=optional_field(data, 'attendee_email', 300),
        )
        values = {
            'account_id': account_id,
            'conversation_id': conversation.get('id') if conversation else None,
            'provider': 'google',
            'external_id': field(data, 'external_id', 500),
            'status': 'cancelled' if external_status == 'cancelled' else 'pending',
            'title': field(data, 'title', 500),
            'start_at': start_at, 'end_at': end_at,
            'timezone': optional_field(data, 'timezone', 100),
            'attendee_name': optional_field(data, 'attendee_name', 300),
            'attendee_email': optional_field(data, 'attendee_email', 300),
            'instagram_username': optional_field(data, 'instagram_username', 200),
            'match_method': match_method,
            'meeting_url': optional_field(data, 'meeting_url', 2000),
            'event_url': optional_field(data, 'event_url', 2000),
            'event_payload': payload,
            'source_updated_at': iso_datetime(data, 'source_updated_at', required=False),
        }
        result = store.upsert_appointment(values)
        # Cancelled events do not enter the brief-generation queue. Mirror the
        # cancellation immediately to an existing Notion page so it cannot
        # remain incorrectly marked as ready.
        if (result.get('status') == 'cancelled' and result.get('notion_page_id')
                and result.get('brief') and notion_configured()):
            try:
                page_id = sync_appointment(
                    result, result['brief'], result.get('brief_text') or '')
                store.save_notion_sync(result['id'], page_id=page_id)
            except (requests.RequestException, ValueError, KeyError) as exc:
                error_code = getattr(exc, 'safe_code', type(exc).__name__)
                app.logger.warning('notion_cancellation_sync_failed code=%s', error_code)
                store.save_notion_sync(result['id'], error=error_code)
        return jsonify(appointment_id=result['id'], status=result['status'],
                       matched=bool(result.get('conversation_id'))), 202
    @app.get('/api/appointments')
    @admin
    def appointments():
        status_filter = request.args.get('status')
        allowed = {'pending','processing','retry','ready','unmatched','cancelled','failed'}
        if status_filter and status_filter not in allowed:
            raise InputError('Invalid status')
        return jsonify(items=store.appointments(status_filter))
    @app.get('/api/appointments/<appointment_id>')
    @admin
    def appointment_detail(appointment_id):
        try:
            return jsonify(store.appointment_detail(appointment_id))
        except (IndexError, KeyError):
            return jsonify(error='Unknown appointment'), 404
    @app.post('/api/appointments/<appointment_id>/link')
    @admin
    def appointment_link(appointment_id):
        data = body()
        try:
            result = store.link_appointment(
                appointment_id, field(data, 'conversation_id', 200))
        except IndexError:
            return jsonify(error='Unknown appointment'), 404
        except ValueError as exc:
            raise InputError(str(exc))
        return jsonify(appointment_id=result['id'], status=result['status'])
    @app.get('/webhook')
    def verify():
        token = os.getenv('META_VERIFY_TOKEN', '')
        if token and request.args.get('hub.mode') == 'subscribe' and hmac.compare_digest(request.args.get('hub.verify_token', ''), token):
            return request.args.get('hub.challenge', ''), 200, {'Content-Type': 'text/plain'}
        return jsonify(error='Verification failed'), 403
    @app.post('/webhook')
    def webhook():
        secret = os.getenv('META_APP_SECRET', '')
        expected = 'sha256=' + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(request.headers.get('X-Hub-Signature-256', ''), expected):
            return jsonify(error='Invalid signature'), 403
        data = body()
        if data.get('object') != 'instagram':
            return jsonify(status='ignored')
        for entry in data.get('entry', []):
            for item in entry.get('messaging', []):
                message = item.get('message', {})
                if message.get('is_echo') or message.get('is_deleted') or not message.get('text'):
                    continue
                recipient = str(item.get('recipient', {}).get('id', ''))
                if recipient != str(entry.get('id', '')):
                    raise InputError('Recipient mismatch')
                account = store.instagram_account(recipient)
                if not account:
                    app.logger.warning('webhook_unknown_business')
                    continue
                timestamp = item.get('timestamp')
                if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or timestamp <= 0:
                    raise InputError('Invalid message timestamp')
                try:
                    occurred = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    raise InputError('Invalid message timestamp')
                if (occurred - datetime.now(timezone.utc)).total_seconds() > 60:
                    raise InputError('Future message timestamp')
                store.enqueue(account['id'], field(item.get('sender', {}), 'id', 200), field(message, 'text'), field(message, 'mid', 200), 'instagram', occurred.isoformat())
        return jsonify(status='accepted')
    return app

app = create_app()
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '10000')), debug=False)

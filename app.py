"""Flask backend: durable Supabase memory and verified Meta webhook."""
import hashlib
import hmac
import logging
import os
from functools import wraps
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
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
        return jsonify(status='ok', service='AGENT-IA')
    @app.get('/health/ready')
    def ready():
        store.ready()
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

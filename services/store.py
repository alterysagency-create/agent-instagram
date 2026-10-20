import os
from supabase import create_client


class Store:
    def __init__(self):
        self.db = None

    def client(self):
        if self.db is None:
            self.db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
        return self.db

    def table(self, name):
        return self.client().schema('agent_ia').table(name)

    def rpc(self, name, params):
        return self.client().schema('agent_ia').rpc(name, params).execute().data

    def ready(self):
        self.table('events').select('id').limit(1).execute()

    def setup(self, account):
        return self.table('accounts').insert(account).execute().data[0]

    def account(self, account_id):
        rows = self.table('accounts').select('*').eq('id', account_id).execute().data
        return rows[0] if rows else None

    def instagram_account(self, instagram_id):
        rows = self.table('accounts').select('*').eq('instagram_id', instagram_id).execute().data
        return rows[0] if rows else None

    def enqueue(self, account_id, sender_id, content, external_id, channel, occurred_at=None):
        return self.rpc('enqueue', dict(p_account=str(account_id), p_sender=sender_id,
                                       p_content=content, p_external=external_id, p_channel=channel,
                                       p_occurred=occurred_at))[0]

    def event(self, event_id):
        rows = self.table('events').select('*').eq('id', event_id).execute().data
        return rows[0] if rows else None

    def claim(self):
        rows = self.rpc('claim', {})
        return rows[0] if rows else None

    def context(self, conversation_id):
        rows = self.table('messages').select('role,content').eq('conversation_id', conversation_id).order('created_at', desc=True).order('id', desc=True).limit(30).execute().data
        history = list(reversed(rows))
        while history and history[0]['role'] != 'user':
            history.pop(0)
        return history

    def conversation(self, conversation_id):
        return self.table('conversations').select('*').eq('id', conversation_id).execute().data[0]

    def handoff(self, conversation_id, required):
        return self.table('conversations').update({'human_required': required}).eq('id', conversation_id).execute()

    def save_response(self, event, text):
        self.rpc('save_response', dict(p_event=event['id'], p_response=text))

    def finish(self, event, status, error_code=None):
        self.rpc('finish', dict(p_event=event['id'], p_status=status, p_error=error_code))

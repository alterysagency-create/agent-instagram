import os
from datetime import datetime, timezone
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
        escaped = str(instagram_id).replace('\\', '\\\\').replace(',', '\\,')
        rows = (self.table('accounts').select('*')
                .or_(f'instagram_id.eq.{escaped},instagram_scoped_id.eq.{escaped}')
                .execute().data)
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

    def handoff(self, conversation_id, required, lead_data=None, reason=None):
        values = {'human_required': required}
        if required:
            values['handoff_requested_at'] = datetime.now(timezone.utc).isoformat()
            if lead_data is not None:
                values['lead_data'] = lead_data
            if reason:
                values['handoff_reason'] = reason
        else:
            values.update(lead_data={}, handoff_reason=None, handoff_requested_at=None)
        return self.table('conversations').update(values).eq('id', conversation_id).execute()

    def update_sender_profile(self, conversation_id, profile):
        values = {key: profile.get(key) for key in
                  ['sender_username', 'sender_name', 'sender_profile_pic'] if profile.get(key)}
        if values:
            self.table('conversations').update(values).eq('id', conversation_id).execute()

    def handoffs(self):
        conversations = (self.table('conversations')
                         .select('id,account_id,sender_id,sender_username,sender_name,sender_profile_pic,created_at,human_required,lead_data,handoff_reason,handoff_requested_at')
                         .eq('human_required', True).order('created_at', desc=True).execute().data)
        for conversation in conversations:
            latest = (self.table('messages').select('content,created_at')
                      .eq('conversation_id', conversation['id']).eq('role', 'user')
                      .order('created_at', desc=True).order('id', desc=True).limit(1).execute().data)
            conversation['latest_message'] = latest[0]['content'] if latest else None
            conversation['latest_message_at'] = latest[0]['created_at'] if latest else None
        return conversations

    def conversation_detail(self, conversation_id):
        conversation = self.conversation(conversation_id)
        messages = (self.table('messages').select('role,content,created_at')
                    .eq('conversation_id', conversation_id)
                    .order('created_at').order('id').execute().data)
        return {'conversation': conversation, 'messages': messages}

    def save_response(self, event, text):
        self.rpc('save_response', dict(p_event=event['id'], p_response=text))

    def finish(self, event, status, error_code=None):
        self.rpc('finish', dict(p_event=event['id'], p_status=status, p_error=error_code))

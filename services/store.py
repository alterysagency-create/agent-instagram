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

    def match_conversation(self, account_id, conversation_id=None,
                           instagram_username=None, attendee_email=None):
        """Return one certain match and its method; never guess between rows."""
        if conversation_id:
            rows = (self.table('conversations').select('*').eq('id', conversation_id)
                    .eq('account_id', account_id).limit(2).execute().data)
            return (rows[0], 'conversation_id') if len(rows) == 1 else (None, None)

        username = (instagram_username or '').strip().lstrip('@')
        if username:
            rows = (self.table('conversations').select('*').eq('account_id', account_id)
                    .ilike('sender_username', username).limit(2).execute().data)
            if len(rows) == 1:
                return rows[0], 'instagram_username'

        email = (attendee_email or '').strip().casefold()
        if email:
            rows = (self.table('conversations')
                    .select('id,account_id,sender_id,sender_username,sender_name,lead_data')
                    .eq('account_id', account_id).limit(500).execute().data)
            matches = [row for row in rows
                       if str((row.get('lead_data') or {}).get('contact_email', '')).casefold() == email]
            if len(matches) == 1:
                return matches[0], 'lead_email'
        return None, None

    def upsert_appointment(self, values):
        """Idempotently store one external calendar event."""
        query = (self.table('appointments').select('*')
                 .eq('account_id', values['account_id'])
                 .eq('provider', values['provider'])
                 .eq('external_id', values['external_id']).limit(1))
        rows = query.execute().data
        if not rows:
            return self.table('appointments').insert(values).execute().data[0]
        current = rows[0]
        incoming_updated = values.get('source_updated_at')
        if (incoming_updated and current.get('source_updated_at')
                and incoming_updated <= current['source_updated_at']):
            return current
        changed = dict(values)
        # A later Calendar notification can omit a custom booking answer.
        # Never discard a previously certain conversation link because of that.
        if current.get('conversation_id') and not changed.get('conversation_id'):
            changed['conversation_id'] = current['conversation_id']
            changed['match_method'] = current.get('match_method')
        changed['status'] = 'cancelled' if values.get('status') == 'cancelled' else 'pending'
        changed.update(error_code=None, available_at=datetime.now(timezone.utc).isoformat(),
                       updated_at=datetime.now(timezone.utc).isoformat())
        return (self.table('appointments').update(changed).eq('id', current['id'])
                .execute().data[0])

    def appointments(self, status=None):
        query = self.table('appointments').select('*').order('start_at', desc=True)
        if status:
            query = query.eq('status', status)
        return query.limit(200).execute().data

    def appointment(self, appointment_id):
        rows = self.table('appointments').select('*').eq('id', appointment_id).limit(1).execute().data
        return rows[0] if rows else None

    def appointment_detail(self, appointment_id):
        appointment = self.appointment(appointment_id)
        if not appointment:
            raise IndexError(appointment_id)
        result = {'appointment': appointment, 'conversation': None, 'messages': []}
        if appointment.get('conversation_id'):
            detail = self.conversation_detail(appointment['conversation_id'])
            result.update(detail)
        return result

    def claim_appointment(self):
        rows = self.rpc('claim_appointment', {})
        return rows[0] if rows else None

    def finish_appointment(self, appointment, status, error_code=None,
                           brief=None, brief_text=None):
        self.rpc('finish_appointment', dict(p_id=appointment['id'], p_status=status,
                                            p_error=error_code, p_brief=brief,
                                            p_text=brief_text))

    def link_appointment(self, appointment_id, conversation_id, method='manual'):
        appointment = self.appointment(appointment_id)
        if not appointment:
            raise IndexError(appointment_id)
        conversation, _ = self.match_conversation(
            appointment['account_id'], conversation_id=conversation_id)
        if not conversation:
            raise ValueError('Conversation does not belong to appointment account')
        values = {'conversation_id': conversation_id, 'match_method': method,
                  'status': 'pending', 'attempts': 0, 'error_code': None,
                  'available_at': datetime.now(timezone.utc).isoformat(),
                  'updated_at': datetime.now(timezone.utc).isoformat()}
        return (self.table('appointments').update(values).eq('id', appointment_id)
                .execute().data[0])

    def appointment_context(self, appointment):
        account = self.account(appointment['account_id'])
        conversation = None
        messages = []
        if appointment.get('conversation_id'):
            conversation = self.conversation(appointment['conversation_id'])
            messages = (self.table('messages').select('role,content,created_at')
                        .eq('conversation_id', conversation['id'])
                        .order('created_at').order('id').execute().data)
        return account, conversation, messages

    def save_notion_sync(self, appointment_id, page_id=None, error=None):
        values = {'notion_sync_error': error,
                  'notion_synced_at': datetime.now(timezone.utc).isoformat() if not error else None}
        if page_id:
            values['notion_page_id'] = page_id
        self.table('appointments').update(values).eq('id', appointment_id).execute()

    def save_response(self, event, text):
        self.rpc('save_response', dict(p_event=event['id'], p_response=text))

    def finish(self, event, status, error_code=None):
        self.rpc('finish', dict(p_event=event['id'], p_status=status, p_error=error_code))

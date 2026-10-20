"""Send simulated DMs through the same durable backend pipeline."""
import argparse
import os
import time
from uuid import uuid4
import requests
from dotenv import load_dotenv


def simulate(account_id, sender_id, message, base_url='http://127.0.0.1:10000'):
    load_dotenv()
    headers = {'Authorization': 'Bearer ' + os.environ['ADMIN_API_KEY']}
    result = requests.post(base_url + '/api/simulate', headers=headers, json={
        'account_id': account_id, 'sender_id': sender_id, 'message': message,
        'event_id': str(uuid4())}, timeout=15)
    result.raise_for_status()
    event_id = result.json()['event_id']
    for _ in range(90):
        response = requests.get(base_url + '/api/events/' + event_id, headers=headers, timeout=15)
        response.raise_for_status()
        event = response.json()
        if event['status'] in ['done', 'handoff', 'failed', 'expired', 'delivery_unknown']:
            print(event)
            return event
        time.sleep(2)
    raise TimeoutError('Event remains queued. Check that the worker is running.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('account_id')
    parser.add_argument('message')
    parser.add_argument('--sender', default='demo-user')
    parser.add_argument('--url', default='http://127.0.0.1:10000')
    args = parser.parse_args()
    simulate(args.account_id, args.sender, args.message, args.url)

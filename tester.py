"""Create a clearly marked demo account and exercise durable DM simulation."""
import os
import requests
from dotenv import load_dotenv
from simuler_dm import simulate

if __name__ == '__main__':
    load_dotenv()
    base = os.getenv('API_BASE_URL', 'http://127.0.0.1:10000')
    response = requests.post(base + '/api/setup', headers={
        'Authorization': 'Bearer ' + os.environ['ADMIN_API_KEY']}, json={
        'business_name': 'Portfolio Demo (fictional)',
        'system_prompt': 'Compte fictif de démonstration. Horaires de démonstration: 11h–20h. Ne donne aucun prix inventé.'}, timeout=15)
    response.raise_for_status()
    account = response.json()['account_id']
    print('Demo account:', account)
    simulate(account, 'demo-user', 'Je m’appelle Marie. Quels sont vos horaires ?', base)
    simulate(account, 'demo-user', 'Comment je m’appelle ?', base)

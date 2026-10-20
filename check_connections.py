"""Read-only diagnostics. Never prints URLs, credentials, upstream bodies or DMs."""
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

ENV_KEYS = [
    'ANTHROPIC_API_KEY',
    'SUPABASE_URL',
    'SUPABASE_KEY',
    'ADMIN_API_KEY',
    'META_APP_SECRET',
    'META_VERIFY_TOKEN',
    'META_API_VERSION',
    'INSTAGRAM_TOKEN_ALTERYS',
]


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    failed = False
    for key in ENV_KEYS:
        configured = bool(os.getenv(key, '').strip())
        print(key + ': ' + ('configured' if configured else 'missing'))

    if '--anthropic' in sys.argv:
        from anthropic import Anthropic
        try:
            Anthropic(timeout=15, max_retries=0).messages.create(
                model=os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-5'), max_tokens=8,
                messages=[{'role': 'user', 'content': 'Réponds OK'}])
            print('Anthropic: OK')
        except Exception as exc:
            print('Anthropic:', type(exc).__name__, 'status:', getattr(exc, 'status_code', None))
            failed = True

    host = urlparse(os.getenv('SUPABASE_URL', '')).hostname
    try:
        if not host:
            raise TypeError('Missing host')
        socket.getaddrinfo(host, 443)
        print('Supabase DNS: OK')
    except (OSError, TypeError):
        print('Supabase DNS: FAILED')
        failed = True
    else:
        from services.store import Store
        try:
            Store().ready()
            print('Supabase agent_ia schema: OK')
        except Exception as exc:
            print('Supabase schema check:', type(exc).__name__)
            failed = True

    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

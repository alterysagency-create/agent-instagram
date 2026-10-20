"""Explicit paid integration checks. Creates marked demo data; never sends Instagram DMs."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4
from dotenv import load_dotenv
from services.store import Store
from services.worker import process

load_dotenv(Path(__file__).with_name('.env'))


def worker_once(expected):
    s = Store()
    event = s.claim()
    if not event or event['id'] != expected:
        if event:
            s.finish(event, 'retry', 'ValidationQueueConflict')
        raise RuntimeError('Validation requires an idle queue')
    try:
        process(s, event)
    except Exception as exc:
        s.finish(event, 'retry', type(exc).__name__)
        raise RuntimeError(type(exc).__name__) from None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker-once':
        worker_once(sys.argv[2])
        return
    s = Store()
    s.ready()
    pending = s.table('events').select('id').in_('status', ['pending','retry','processing','sending']).limit(1).execute().data
    if pending:
        raise RuntimeError('Validation requires an idle queue')
    run = str(uuid4())[:8]
    sender = 'portfolio-validation-' + run
    code = 'LUNE-' + run
    a = s.setup({'business_name': 'Validation A ' + run,
                 'system_prompt': 'Compte fictif de validation. Retenir les informations du client dans cette discussion. Répondre brièvement.'})
    b = s.setup({'business_name': 'Validation B ' + run,
                 'system_prompt': 'Compte fictif distinct. Ne jamais inventer des informations client inconnues.'})

    def turn(account, text):
        event = s.enqueue(account['id'], sender, text, str(uuid4()), 'simulation')
        result = subprocess.run([sys.executable, __file__, '--worker-once', event['id']],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if result.returncode:
            raise RuntimeError('Worker integration check failed')
        row = Store().event(event['id'])
        assert row['status'] == 'done', 'Event did not complete'
        return row

    first = turn(a, 'Mon code dossier est ' + code + '. Retiens-le.')
    second = turn(a, 'Quel est mon code dossier ? Réponds uniquement avec le code.')
    assert code.casefold() in second['response'].casefold(), 'Memory recall failed'
    print('Persistent memory across new processes: PASS')
    third = turn(b, 'Connais-tu mon code dossier ?')
    history = Store().context(third['conversation_id'])
    assert code not in str(history), 'Business context leaked'
    assert third['conversation_id'] != second['conversation_id']
    print('Same sender, different businesses: PASS')
    original = s.event(first['id'])
    duplicate = s.enqueue(a['id'], sender, 'Duplicate test', original['external_id'], 'simulation')
    assert duplicate['id'] == first['id'] and duplicate['status'] == 'done'
    assert s.table('messages').select('id', count='exact').eq('conversation_id',first['conversation_id']).execute().count == 4
    print('Completed duplicate does not enqueue another reply: PASS')
    print('Demo data retained under Validation A/B ' + run)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Validation failed:', type(exc).__name__, str(exc))
        raise SystemExit(1)

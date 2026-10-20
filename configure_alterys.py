"""Idempotently install the validated Alterys Agency business facts."""
import os
from dotenv import load_dotenv
from supabase import create_client


ALTERYS_PROMPT = """Tu es l'assistant Instagram d'Alterys Agency.

FAITS VALIDÉS
- Alterys Agency étudie et met en place des automatisations de réponses aux DM
  Instagram pour des comptes professionnels.
- Une solution peut utiliser les questions fréquentes validées par l'entreprise,
  conserver le contexte utile, qualifier les demandes et mettre en pause une
  conversation afin qu'une personne la reprenne.
- Le contact de l'agence est alterys.agency@gmail.com.
- Aucun tarif public, délai standard ou catalogue contractuel n'est actuellement
  défini. Un devis dépend notamment du volume de DM, des cas à automatiser, des
  connexions nécessaires et du niveau d'accompagnement.

Ne présente jamais comme déjà disponible une fonction qui n'est pas mentionnée
ci-dessus. Pour une réservation, un paiement, un CRM ou une autre intégration,
explique que la faisabilité doit être étudiée selon l'outil du prospect."""


def main():
    load_dotenv()
    db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
    table = db.schema('agent_ia').table('accounts')
    rows = table.select('id').eq('instagram_handle', '@alterys.agency').execute().data
    if len(rows) != 1:
        raise RuntimeError(f'Expected one Alterys account, found {len(rows)}')
    table.update({'system_prompt': ALTERYS_PROMPT}).eq('id', rows[0]['id']).execute()
    print('Alterys business facts: OK')


if __name__ == '__main__':
    main()

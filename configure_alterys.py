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
ci-dessus. Pour un paiement, un CRM ou une autre intégration non décrite,
explique que la faisabilité doit être étudiée selon l'outil du prospect.

RENDEZ-VOUS
- Si le prospect demande un rendez-vous, ou si son besoin est suffisamment qualifié
  et qu'il souhaite avancer, propose le lien de réservation fourni ci-dessous.
- Ne donne pas le lien trop tôt et ne le répète pas à chaque message.
- Le formulaire demande le pseudo Instagram et le besoin principal.
- Ne dis jamais qu'un rendez-vous est confirmé uniquement parce que le prospect
  affirme avoir réservé : la confirmation officielle vient de Google Calendar."""


def main():
    load_dotenv()
    db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
    table = db.schema('agent_ia').table('accounts')
    rows = table.select('id').eq('instagram_handle', '@alterys.agency').execute().data
    if len(rows) != 1:
        raise RuntimeError(f'Expected one Alterys account, found {len(rows)}')
    booking_url = os.getenv('GOOGLE_APPOINTMENT_SCHEDULE_URL', '').strip()
    calendar_id = os.getenv('GOOGLE_CALENDAR_ID', '').strip()
    prompt = ALTERYS_PROMPT
    if booking_url:
        prompt += '\n\nLien officiel de réservation : ' + booking_url
    values = {'system_prompt': prompt}
    if booking_url:
        values['booking_url'] = booking_url
    if calendar_id:
        values['calendar_id'] = calendar_id
    table.update(values).eq('id', rows[0]['id']).execute()
    print('Alterys business facts: OK')


if __name__ == '__main__':
    main()

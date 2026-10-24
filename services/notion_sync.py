"""Optional server-side sync of generated call briefs to the Notion meetings database."""
import os
from datetime import datetime, timezone
import requests


def configured():
    return bool(os.getenv('NOTION_API_TOKEN') and os.getenv('NOTION_MEETINGS_DATA_SOURCE_ID'))


def _text(value, limit=1900):
    value = str(value or '').strip()
    return [{'type': 'text', 'text': {'content': value[:limit]}}] if value else []


def _join(values):
    return '\n'.join('• ' + str(value) for value in (values or []))


def _paragraph_blocks(value):
    """Keep the complete brief while respecting Notion's rich-text size limit."""
    value = str(value or '').strip()
    return [
        {'object': 'block', 'type': 'paragraph',
         'paragraph': {'rich_text': _text(value[index:index + 1900], 1900)}}
        for index in range(0, len(value), 1900)
    ]


def _properties(appointment, brief, status):
    prospect = brief['prospect']
    analysis = brief['analysis']
    title = prospect.get('instagram_username')
    if not title or title == 'Non précisé':
        title = prospect.get('attendee_name') or 'Rendez-vous à identifier'
    title = f"Appel — {str(title).lstrip('@')}"
    properties = {
        'Rendez-vous': {'title': _text(title, 250)},
        'Statut': {'status': {'name': {
            'ready': 'Prêt', 'unmatched': 'À identifier',
            'cancelled': 'Annulé', 'processing': 'À préparer',
        }.get(status, 'À préparer')}},
        'Date et heure': {'date': {'start': appointment['start_at'], 'end': appointment['end_at']}},
        'Instagram': {'rich_text': _text(prospect.get('instagram_username'))},
        'Entreprise': {'rich_text': _text(prospect.get('business_activity'))},
        'Email': {'email': None if prospect.get('attendee_email') == 'Non précisé'
                  else prospect.get('attendee_email')},
        'Lien Google Meet': {'url': appointment.get('meeting_url')},
        'Google Event ID': {'rich_text': _text(appointment.get('external_id'))},
        'Résumé IA': {'rich_text': _text(analysis.get('summary'))},
        'Besoin principal': {'rich_text': _text(analysis.get('main_need'))},
        'Problèmes repérés': {'rich_text': _text(_join(analysis.get('pain_points')))},
        'Objections': {'rich_text': _text(_join(analysis.get('objections')))},
        'Informations manquantes': {'rich_text': _text(_join(analysis.get('missing_information')))},
        'Plan d’appel conseillé': {'rich_text': _text(_join(analysis.get('recommended_agenda')))},
        'Prochaine étape': {'rich_text': _text(
            'Identifier la conversation Instagram' if status == 'unmatched'
            else 'Lire la fiche puis préparer les questions de l’appel')},
        'Responsable': {'select': {'name': 'Ensemble'}},
        'Dernière génération': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
    }
    return properties


def sync_appointment(appointment, brief, brief_text):
    """Create or update one Notion page and return its page id."""
    if not configured():
        return None
    token = os.environ['NOTION_API_TOKEN']
    data_source = os.environ['NOTION_MEETINGS_DATA_SOURCE_ID']
    headers = {
        'Authorization': 'Bearer ' + token,
        'Notion-Version': os.getenv('NOTION_VERSION', '2026-03-11'),
        'Content-Type': 'application/json',
    }
    payload = {'properties': _properties(appointment, brief, appointment['status'])}
    page_id = appointment.get('notion_page_id')
    if page_id:
        response = requests.patch('https://api.notion.com/v1/pages/' + page_id,
                                  headers=headers, json=payload, timeout=(5, 20))
    else:
        payload['parent'] = {'type': 'data_source_id', 'data_source_id': data_source}
        payload['children'] = [
            {'object': 'block', 'type': 'callout', 'callout': {
                'icon': {'type': 'emoji', 'emoji': '🤖'},
                'rich_text': _text('Fiche générée automatiquement à partir du rendez-vous et des faits explicitement donnés en DM.')
            }},
            {'object': 'block', 'type': 'heading_2',
             'heading_2': {'rich_text': _text('Fiche complète')}},
        ] + _paragraph_blocks(brief_text)
        response = requests.post('https://api.notion.com/v1/pages', headers=headers,
                                 json=payload, timeout=(5, 20))
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result.get('id'):
        raise ValueError('Notion page acknowledgement missing')
    return result['id']

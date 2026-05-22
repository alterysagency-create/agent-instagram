import os
from dotenv import load_dotenv
from anthropic import Anthropic
from supabase import create_client
from flask import Flask, request, jsonify
from datetime import datetime

load_dotenv()

app = Flask(__name__)
client = Anthropic()
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)

historique = {}

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "✅ Agent Instagram DM marche !"})

@app.route('/api/setup', methods=['POST'])
def setup():
    data = request.json
    result = supabase.table("accounts").insert({
        "business_name": data.get('business_name'),
        "email": data.get('email'),
        "instagram_handle": data.get('instagram_handle'),
        "system_prompt": data.get('system_prompt'),
        "plan": data.get('plan', 'starter')
    }).execute()
    account_id = result.data[0]['id']
    return jsonify({
        "account_id": account_id,
        "status": "success",
        "message": "Agent créé !"
    }), 201

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    account_id = data.get('account_id')
    sender_id = data.get('sender_id')
    message = data.get('message')

    account = supabase.table("accounts").select("*").eq("id", account_id).execute()
    if not account.data:
        return jsonify({"error": "Client non trouvé"}), 404

    account = account.data[0]
    system_prompt = account['system_prompt']

    cle = f"{account_id}_{sender_id}"
    if cle not in historique:
        historique[cle] = []

    historique[cle].append({"role": "user", "content": message})

    reponse = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        system=system_prompt,
        messages=historique[cle]
    )

    agent_reponse = reponse.content[0].text
    historique[cle].append({"role": "assistant", "content": agent_reponse})

    mots_cles = ['commander', 'acheter', 'combien', 'prix', 'payer', 'promo']
    lead = 'hot' if any(m in message.lower() for m in mots_cles) else 'warm'

    supabase.table("conversations").insert({
        "account_id": account_id,
        "sender_id": sender_id,
        "user_message": message,
        "agent_response": agent_reponse,
        "lead_quality": lead
    }).execute()

    return jsonify({
        "response": agent_reponse,
        "lead_quality": lead,
        "business": account['business_name']
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
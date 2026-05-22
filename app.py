import os
import os
from dotenv import load_dotenv
load_dotenv()
from dotenv import load_dotenv
from anthropic import Anthropic
from supabase import create_client
from flask import Flask, request, jsonify
import uuid
from datetime import datetime

load_dotenv()

app = Flask(__name__)
client = Anthropic()
supabase = create_client(
    os.environ.get("SUPABASE_URL"),
    os.environ.get("SUPABASE_KEY")
)

# Historique en mémoire
historique = {}

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "✅ Agent Instagram DM marche !"})

# ================================
# CRÉER UN COMPTE CLIENT
# ================================
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

    print(f"✅ Nouveau client créé : {data.get('business_name')}")

    return jsonify({
        "account_id": account_id,
        "status": "success",
        "message": "Agent créé !"
    }), 201

# ================================
# RECEVOIR UN DM
# ================================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    account_id = data.get('account_id')
    sender_id = data.get('sender_id')
    message = data.get('message')

    # Chercher le client dans Supabase
    account = supabase.table("accounts").select("*").eq("id", account_id).execute()

    if not account.data:
        return jsonify({"error": "Client non trouvé"}), 404

    account = account.data[0]
    system_prompt = account['system_prompt']

    print(f"\n📩 DM reçu pour : {account['business_name']}")
    print(f"De : @{sender_id}")
    print(f"Message : {message}")

    # Historique
    cle = f"{account_id}_{sender_id}"
    if cle not in historique:
        historique[cle] = []

    historique[cle].append({
        "role": "user",
        "content": message
    })

    # Appeler Claude
    reponse = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        system=system_prompt,
        messages=historique[cle]
    )

    agent_reponse = reponse.content[0].text

    historique[cle].append({
        "role": "assistant",
        "content": agent_reponse
    })

    # Détecter le lead
    mots_cles = ['commander', 'acheter', 'combien', 'prix', 'payer', 'promo']
    lead = 'hot' if any(m in message.lower() for m in mots_cles) else 'warm'

    # Sauvegarder dans Supabase
    supabase.table("conversations").insert({
        "account_id": account_id,
        "sender_id": sender_id,
        "user_message": message,
        "agent_response": agent_reponse,
        "lead_quality": lead
    }).execute()

    print(f"✅ Réponse : {agent_reponse}")
    print(f"📊 Lead : {lead}")

    return jsonify({
        "response": agent_reponse,
        "lead_quality": lead,
        "business": account['business_name']
    })

if __name__ == '__main__':
    print("🚀 Serveur démarré sur http://localhost:5000")
    app.run(debug=True, port=5000)
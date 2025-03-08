from datetime import datetime, timedelta
import requests
from models import AmazonOAuthTokens, db

# Amazon OAuth Variables (ensure these are imported or defined here)
LWA_APP_ID = 'your_lwa_app_id'
LWA_CLIENT_SECRET = 'your_lwa_client_secret'
TOKEN_URL = 'your_token_url'

def refresh_access_token(selling_partner_id):
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return None

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token_entry.refresh_token,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET
    }

    response = requests.post(TOKEN_URL, data=payload)
    data = response.json()

    if "access_token" in data:
        token_entry.access_token = data["access_token"]
        token_entry.expires_at = datetime.utcnow() + timedelta(seconds=data["expires_in"])
        db.session.commit()
        return data["access_token"]

    print("❌ Failed to refresh token:", data)
    return None
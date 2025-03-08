from datetime import datetime
from models import AmazonOAuthTokens
from auth import refresh_access_token

def get_stored_tokens(selling_partner_id):
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return None

    if token_entry.expires_at and datetime.utcnow() >= token_entry.expires_at:
        print(f"🔄 Token expired for {selling_partner_id}, refreshing...")
        return refresh_access_token(selling_partner_id)

    return token_entry.access_token


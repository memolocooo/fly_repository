from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

db = SQLAlchemy()

# AMAZON OAUTH TOKENS
class AmazonOAuthTokens(db.Model):
    __tablename__ = "amazon_oauth_tokens"

    selling_partner_id = db.Column(db.String(255), primary_key=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=False)
    expires_in = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    
    def __init__(self, selling_partner_id, access_token, refresh_token, expires_in):
        self.selling_partner_id = selling_partner_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in
        self.created_at = datetime.utcnow()
        self.expires_at = self.created_at + timedelta(seconds=expires_in)


# AMAZON ORDERS
class AmazonOrders(db.Model):
    __tablename__ = 'amazon_orders'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    order_id = db.Column(db.String, unique=True, nullable=False)
    amazon_order_id = db.Column(db.String, unique=True, nullable=False)  # ✅ Ensure this is required
    marketplace_id = db.Column(db.String, nullable=True)
    selling_partner_id = db.Column(db.String, db.ForeignKey("amazon_oauth_tokens.selling_partner_id"), nullable=False)
    number_of_items_shipped = db.Column(db.Integer, nullable=True)
    order_status = db.Column(db.String, nullable=False)
    total_amount = db.Column(db.Numeric, nullable=True)
    currency = db.Column(db.String, nullable=True)
    purchase_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "order_id": self.order_id,
            "amazon_order_id": self.amazon_order_id,  # ✅ Ensure this field is included
            "selling_partner_id": self.selling_partner_id,
            "marketplace_id": self.marketplace_id,
            "number_of_items_shipped": self.number_of_items_shipped,
            "order_status": self.order_status,
            "total_amount": float(self.total_amount) if self.total_amount else None,
            "currency": self.currency,
            "purchase_date": self.purchase_date.strftime('%Y-%m-%d %H:%M:%S') if self.purchase_date else None,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }



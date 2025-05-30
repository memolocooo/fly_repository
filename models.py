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
    amazon_fees = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    shipping_price = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    asin = db.Column(db.String(20), nullable=True)
    seller_sku = db.Column(db.String(50), nullable=True)
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    postal_code = db.Column(db.String(20))
    country_code = db.Column(db.String(10))



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
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "amazon_fees": self.amazon_fees,
            "shipping_price": self.shipping_price,
            "asin": self.asin,
            "seller_sku": self.seller_sku,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "country_code": self.country_code


        }


class AmazonSettlementData(db.Model):
    __tablename__ = 'amazon_settlement_data'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    selling_partner_id = db.Column(db.String, db.ForeignKey("amazon_oauth_tokens.selling_partner_id"), nullable=False)
    settlement_id = db.Column(db.String, nullable=False)
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    order_id = db.Column(db.String, nullable=True)
    type = db.Column(db.String, nullable=True)
    amount = db.Column(db.Numeric(10, 2), nullable=True)
    amazon_fee = db.Column(db.Numeric(10, 2), nullable=True)
    shipping_fee = db.Column(db.Numeric(10, 2), nullable=True)
    total_amount = db.Column(db.Numeric(10, 2), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "selling_partner_id": self.selling_partner_id,
            "settlement_id": self.settlement_id,
            "date_time": self.date_time.strftime('%Y-%m-%d %H:%M:%S'),
            "order_id": self.order_id,
            "type": self.type,
            "amount": float(self.amount) if self.amount else None,
            "amazon_fee": float(self.amazon_fee) if self.amazon_fee else None,
            "shipping_fee": float(self.shipping_fee) if self.shipping_fee else None,
            "total_amount": float(self.total_amount) if self.total_amount else None,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    

class AmazonOrderItem(db.Model):
    __tablename__ = 'amazon_order_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    selling_partner_id = db.Column(db.String, db.ForeignKey("amazon_oauth_tokens.selling_partner_id"), nullable=False)
    amazon_order_id = db.Column(db.String, db.ForeignKey("amazon_orders.amazon_order_id"), nullable=False)
    order_item_id = db.Column(db.String, unique=True, nullable=False)
    asin = db.Column(db.String(20), nullable=True)
    title = db.Column(db.Text, nullable=True)
    seller_sku = db.Column(db.String(50), nullable=True)
    condition = db.Column(db.String(20), nullable=True)
    is_gift = db.Column(db.Boolean, default=False)
    quantity_ordered = db.Column(db.Integer, nullable=True)
    quantity_shipped = db.Column(db.Integer, nullable=True)
    item_price = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    item_tax = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    shipping_price = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    shipping_tax = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cogs = db.Column(db.Numeric(10, 2), nullable=True, default=0)
    purchase_date = db.Column(db.DateTime, nullable=True)
    image_url = db.Column(db.Text)
    marketplace = db.Column(db.String(50), nullable=True)
    safety_stock = db.Column(db.Integer, nullable=True)



    def to_dict(self):
        return {
            "id": self.id,
            "selling_partner_id": self.selling_partner_id,
            "amazon_order_id": self.amazon_order_id,
            "order_item_id": self.order_item_id,
            "asin": self.asin,
            "title": self.title,
            "seller_sku": self.seller_sku,
            "condition": self.condition,
            "is_gift": self.is_gift,
            "quantity_ordered": self.quantity_ordered,
            "quantity_shipped": self.quantity_shipped,
            "item_price": float(self.item_price) if self.item_price else None,
            "item_tax": float(self.item_tax) if self.item_tax else None,
            "shipping_price": float(self.shipping_price) if self.shipping_price else None,
            "shipping_tax": float(self.shipping_tax) if self.shipping_tax else None,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "cogs": self.cogs,
            "purchase_date" : self.purchase_date.strftime('%Y-%m-%d %H:%M:%S') if self.purchase_date else None,
            "image_url": self.image_url,
            "marketplace": self.marketplace,
            "safety_stock": self.safety_stock
        }



class AmazonFinancialShipmentEvent(db.Model):
    __tablename__ = 'amazon_financial_shipment_events'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    selling_partner_id = db.Column(db.String, db.ForeignKey("amazon_oauth_tokens.selling_partner_id"), nullable=False)
    group_id = db.Column(db.String, nullable=False)
    order_id = db.Column(db.String, nullable=True)
    marketplace = db.Column(db.String, nullable=True)
    posted_date = db.Column(db.DateTime, nullable=True)
    sku = db.Column(db.String, nullable=True)
    quantity = db.Column(db.Integer, nullable=True)
    principal = db.Column(db.Numeric(10, 2), nullable=True)
    tax = db.Column(db.Numeric(10, 2), nullable=True)
    shipping = db.Column(db.Numeric(10, 2), nullable=True)
    fba_fee = db.Column(db.Numeric(10, 2), nullable=True)
    commission = db.Column(db.Numeric(10, 2), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    service_fee = db.Column(db.Numeric)  # NEW
    ads_fee = db.Column(db.Numeric)      # NEW
    storage_fee = db.Column(db.Numeric(10, 2), nullable=True)
    refund_fee = db.Column(db.Numeric(10, 2), nullable=True)  # NEW

    def to_dict(self):
        return {
            "id": self.id,
            "selling_partner_id": self.selling_partner_id,
            "group_id": self.group_id,
            "order_id": self.order_id,
            "marketplace": self.marketplace,
            "posted_date": self.posted_date.strftime('%Y-%m-%d %H:%M:%S') if self.posted_date else None,
            "sku": self.sku,
            "quantity": self.quantity,
            "principal": float(self.principal) if self.principal else None,
            "tax": float(self.tax) if self.tax else None,
            "shipping": float(self.shipping) if self.shipping else None,
            "fba_fee": float(self.fba_fee) if self.fba_fee else None,
            "commission": float(self.commission) if self.commission else None,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "service_fee": float(self.service_fee) if self.service_fee else None,  # NEW}
            "ads_fee": float(self.ads_fee) if self.ads_fee else None,  # NEW
            "storage_fee": float(self.storage_fee) if self.storage_fee else None,
            "refund_fee": float(self.refund_fee) if self.refund_fee else None  # NEW
        }



class AmazonInventoryItem(db.Model):
    __tablename__ = 'amazon_inventory_items'

    id = db.Column(db.Integer, primary_key=True)
    selling_partner_id = db.Column(db.String, db.ForeignKey("amazon_oauth_tokens.selling_partner_id"), nullable=False)
    asin = db.Column(db.String, nullable=False)
    fnsku = db.Column(db.String, nullable=True)
    sku = db.Column(db.String, nullable=True)
    product_name = db.Column(db.String, nullable=True)
    condition = db.Column(db.String, nullable=True)
    fulfillment_center_id = db.Column(db.String, nullable=True)
    detailed_disposition = db.Column(db.String, nullable=True)
    inventory_country = db.Column(db.String, nullable=True)
    inventory_status = db.Column(db.String, nullable=True)
    quantity_available = db.Column(db.Integer, nullable=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    price = db.Column(db.String, nullable=True)  # for "price"
    inventory_status = db.Column(db.String, nullable=True)  # for "status"
    image_url = db.Column(db.String, nullable=True)  # for "image_url"
    


    def to_dict(self):
        return {
            "asin": self.asin,
            "fnsku": self.fnsku,
            "sku": self.sku,
            "product_name": self.product_name,
            "condition": self.condition,
            "fulfillment_center_id": self.fulfillment_center_id,
            "detailed_disposition": self.detailed_disposition,
            "inventory_country": self.inventory_country,
            "inventory_status": self.inventory_status,
            "quantity_available": self.quantity_available,
            "last_updated": self.last_updated.strftime('%Y-%m-%d %H:%M:%S'),
            "price": float(self.price) if self.price else None,
            "inventory_status": self.inventory_status,  # for "status"
            "image_url": f"https://ws-na.amazon-adsystem.com/widgets/q?_encoding=UTF8&ASIN={self.asin}&Format=_SL75_&ID=AsinImage" if self.asin else None
        }



class MarketplaceParticipation(db.Model):
    __tablename__ = "marketplace_participations"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    selling_partner_id = db.Column(db.String(255), nullable=False)
    marketplace_id = db.Column(db.String(255), nullable=False)
    country_code = db.Column(db.String(10), nullable=True)
    is_participating = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<MarketplaceParticipation {self.marketplace_id} ({self.country_code})>"





class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    selling_partner_id = db.Column(db.String(255), unique=True, nullable=False)
    access_token = db.Column(db.String(2048), nullable=False)
    refresh_token = db.Column(db.String(2048), nullable=False)
    region = db.Column(db.String(10), nullable=False, default="na")  # na, eu, fe
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # Relationship: One client ➔ Many marketplaces
    marketplaces = db.relationship('MarketplaceParticipation', backref='client', lazy=True)

    def __repr__(self):
        return f"<Client {self.selling_partner_id}>"
    




class AmazonReturnStats(db.Model):
    __tablename__ = 'amazon_return_stats'

    id = db.Column(db.Integer, primary_key=True)
    selling_partner_id = db.Column(db.String, nullable=False)
    marketplace = db.Column(db.String, nullable=True)
    month = db.Column(db.String, nullable=False)  # Format: YYYY-MM
    total_orders = db.Column(db.Integer, nullable=False)
    total_returns = db.Column(db.Integer, nullable=False)
    return_rate = db.Column(db.Float, nullable=False)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)




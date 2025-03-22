import os
import uuid
import requests
import redis
import logging
import json
import time
from flask import Flask, session, redirect, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from datetime import timedelta, datetime  # Instead of `import datetime`
from flask_cors import CORS
from models import db, AmazonOAuthTokens, AmazonOrders, AmazonSettlementData, AmazonOrderItem  # Use the correct class name
import psycopg2
from psycopg2.extras import execute_values
from amazon_api import fetch_orders_from_amazon, fetch_financial_events, fetch_shipping_data, fetch_fees_data, fetch_amazon_fees, fetch_order_items


# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
redis_client = redis.StrictRedis(host="localhost", port=6379, decode_responses=True)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@0.0.0.0:5432/dbname")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL")
redis_client = redis.StrictRedis.from_url(REDIS_URL, decode_responses=True)

# Initialize database
db.init_app(app)
migrate = Migrate(app, db)

# Amazon OAuth Variables
LWA_APP_ID = os.getenv("LWA_APP_ID")
LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
AUTH_URL = os.getenv("AUTH_URL")
TOKEN_URL = os.getenv("TOKEN_URL")
SP_API_BASE_URL = os.getenv("SP_API_BASE_URL")
APP_ID = os.getenv("APP_ID")

# Ensure database connection
try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.close()
    print("✅ Database connection successful!")
except Exception as e:
    print(f"❌ Database connection failed: {e}")

one_year_ago = datetime.utcnow() - timedelta(days=365)

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

def exchange_auth_code_for_tokens(auth_code):
    """Exchanges auth code for access & refresh tokens from Amazon SP-API."""
    payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET,
        "redirect_uri": os.getenv("REDIRECT_URI"),
    }
    
    response = requests.post(TOKEN_URL, data=payload)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Error fetching tokens: {response.text}")
        return None

def get_stored_tokens(selling_partner_id):
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return None

    if token_entry.expires_at and datetime.utcnow() >= token_entry.expires_at:
        print(f"🔄 Token expired for {selling_partner_id}, refreshing...")
        return refresh_access_token(selling_partner_id)

    return token_entry.access_token

def save_oauth_tokens(selling_partner_id, access_token, refresh_token, expires_in):
    try:
        print(f"🔄 Attempting to save OAuth Tokens for {selling_partner_id}")  # Debug log
        
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Ensure table exists (Replace 'amazon_tokens' with 'amazon_oauth_tokens' if incorrect)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS amazon_oauth_tokens (
            id SERIAL PRIMARY KEY,
            selling_partner_id TEXT UNIQUE,
            access_token TEXT,
            refresh_token TEXT,
            expires_in INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
        """)

        # Calculate token expiration time
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Insert or update the token
        cur.execute("""
        INSERT INTO amazon_oauth_tokens (selling_partner_id, access_token, refresh_token, expires_in, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (selling_partner_id) DO UPDATE 
        SET access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            expires_in = EXCLUDED.expires_in,
            expires_at = EXCLUDED.expires_at
        """, (selling_partner_id, access_token, refresh_token, expires_in, expires_at))

        # Commit the transaction
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Tokens saved successfully for {selling_partner_id}")

    except psycopg2.Error as e:
        print(f"❌ Database Error while saving tokens: {e}")
        if conn:
            conn.rollback()  # Ensure rollback on failure

    finally:
        if conn:
            conn.close()



def store_orders_in_db(selling_partner_id, orders):
    """Save only necessary Amazon order fields into PostgreSQL, including fees and shipping price."""
    for order in orders:
        order_id = order.get("AmazonOrderId")
        marketplace_id = order.get("MarketplaceId")
        amazon_order_id = order.get("AmazonOrderId")
        number_of_items_shipped = order.get("NumberOfItemsShipped", 0)
        order_status = order.get("OrderStatus", "UNKNOWN")
        total_amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        currency = order.get("OrderTotal", {}).get("CurrencyCode")
        purchase_date = order.get("PurchaseDate")

        # ✅ Extract Amazon fees and shipping price
        amazon_fees = float(order.get("AmazonFees", {}).get("Amount", 0) or 0)
        shipping_price = float(order.get("ShippingPrice", {}).get("Amount", 0) or 0)

        # ✅ Check if the order already exists
        existing_order = AmazonOrders.query.filter_by(order_id=order_id).first()

        if not existing_order:  # ✅ Avoid duplicate entries
            new_order = AmazonOrders(
                order_id=order_id,
                amazon_order_id=amazon_order_id,
                marketplace_id=marketplace_id,
                selling_partner_id=selling_partner_id,
                number_of_items_shipped=number_of_items_shipped,
                order_status=order_status,
                total_amount=total_amount,
                currency=currency,
                purchase_date=datetime.strptime(purchase_date, "%Y-%m-%dT%H:%M:%SZ"),
                amazon_fees=amazon_fees,  # ✅ Store Amazon fees
                shipping_price=shipping_price,  # ✅ Store shipping price
                created_at=datetime.utcnow()
            )

            db.session.add(new_order)

    db.session.commit()
    print(f"✅ {len(orders)} orders saved to database with Amazon fees & shipping price.")




@app.route('/start-oauth')
def start_oauth():
    """Redirects user to Amazon for OAuth authentication."""
    state = str(uuid.uuid4())
    session["oauth_state"] = state

    oauth_url = (
        f"{AUTH_URL}/apps/authorize/consent"
        f"?application_id={APP_ID}"
        f"&state={state}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&version=beta"
    )

    print(f"🔗 OAuth Redirect URL: {oauth_url}")
    return redirect(oauth_url)


@app.route('/callback')
def callback():
    auth_code = request.args.get("spapi_oauth_code")
    selling_partner_id = request.args.get("selling_partner_id")

    if not auth_code or not selling_partner_id:
        return jsonify({"error": "Missing auth_code or selling_partner_id"}), 400

    print(f"🚀 Received auth_code: {auth_code}")
    print(f"🔍 Received selling_partner_id: {selling_partner_id}")

    # Exchange auth code for tokens
    token_payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET
    }

    response = requests.post(TOKEN_URL, data=token_payload)
    token_data = response.json()

    if "access_token" in token_data and "refresh_token" in token_data:
        access_token = token_data["access_token"]
        refresh_token = token_data["refresh_token"]
        expires_in = token_data["expires_in"]

        # ✅ Save the tokens
        save_oauth_tokens(selling_partner_id, access_token, refresh_token, expires_in)

        # ✅ Fetch orders
        print(f"📦 Fetching orders for {selling_partner_id}...")
        created_after = (datetime.utcnow() - timedelta(days=365)).isoformat()
        orders = fetch_orders_from_amazon(selling_partner_id, access_token, created_after)

        if orders:
            print(f"✅ {len(orders)} orders retrieved. Saving now...")
            store_orders_in_db(selling_partner_id, orders)

            # ✅ For each order, fetch and store order items
            for order in orders:
                amazon_order_id = order.get("AmazonOrderId")
                order_items = fetch_order_items(access_token, amazon_order_id)

                if order_items:
                    print(f"📦 Saving {len(order_items)} items for order {amazon_order_id}")
                    try:
                        store_order_items_in_db(
                            selling_partner_id=selling_partner_id,
                            order_data={
                                "amazon_order_id": amazon_order_id,
                                "order_items": order_items
                            }
                        )
                    except Exception as e:
                        print(f"❌ Error saving order items for {amazon_order_id}: {e}")
        else:
            print("⚠️ No orders returned from Amazon.")

        # ✅ Redirect to your dashboard
        return redirect("https://guillermos-amazing-site-b0c75a.webflow.io/dashboard")

    return jsonify({"error": "Failed to obtain tokens", "details": token_data}), 400





@app.route("/get-orders", methods=["GET"])
def get_orders():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # ✅ Step 1: Check Redis Cache First (Optimize Performance)
    cache_key = f"orders:{selling_partner_id}"
    cached_orders = redis_client.get(cache_key)
    if cached_orders:
        return jsonify(json.loads(cached_orders))

    # ✅ Step 2: Fetch Orders from Database First
    one_year_ago = datetime.utcnow() - timedelta(days=365)
    orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.purchase_date >= one_year_ago
    ).all()

    if orders:
        orders_data = [order.to_dict() for order in orders]
        redis_client.setex(cache_key, 900, json.dumps(orders_data))
        return jsonify(orders_data)

    # ✅ Step 3: If No Orders in DB, Fetch from Amazon API
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No valid access token found"}), 400

    created_after = one_year_ago.isoformat()
    fetched_orders = fetch_orders_from_amazon(selling_partner_id, access_token, created_after)

    if not fetched_orders:
        return jsonify({"error": "No orders found in Amazon API"}), 404

    # ✅ Step 4: Save Orders to Database
    store_orders_in_db(selling_partner_id, fetched_orders)

    # ✅ Step 5: Return the Newly Fetched Orders
    orders_data = [order.to_dict() for order in fetched_orders]
    redis_client.setex(cache_key, 900, json.dumps(orders_data))  # Cache results
    return jsonify(orders_data), 200




@app.route("/api/orders", methods=["GET"])
def get_amazon_orders():
    orders = AmazonOrders.query.all()
    orders_data = [
        {
            "marketplace_id": order.marketplace_id,
            "total_amount": float(order.total_amount) if order.total_amount else 0,
            "order_status": order.order_status,
            "purchase_date": order.purchase_date.strftime('%Y-%m-%d') if order.purchase_date else None,
        }
        for order in orders
    ]
    return jsonify(orders_data)







@app.route("/")
def home():
    return "Flask is running on Fly.io!"



# ✅ Ensure logs go to stdout for Fly.io
logging.basicConfig(level=logging.INFO)

@app.before_request
def log_request():
    """Log incoming HTTP requests."""
    logging.info(f"📥 Request: {request.method} {request.url}")



@app.after_request
def log_response(response):
    """Log outgoing HTTP responses."""
    logging.info(f"📤 Response: {response.status_code} {request.url}")
    return response



@app.route("/fetch-financial-events", methods=["GET"])
def fetch_financial_events_endpoint():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        logging.error("❌ Missing selling_partner_id")
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        logging.error("❌ No valid access token found")
        return jsonify({"error": "No valid access token found"}), 401

    posted_after = (datetime.utcnow() - timedelta(days=365)).isoformat()

    try:
        financial_events = fetch_financial_events(selling_partner_id, access_token, posted_after)

        if financial_events is None:
            logging.error("❌ Financial events response is None")
            return jsonify({"error": "Failed to fetch financial events"}), 500

        logging.info(f"📊 API Response: {json.dumps(financial_events, indent=2)[:500]}")  # Log the first 500 chars of response
        return jsonify(financial_events), 200

    except Exception as e:
        logging.error(f"❌ Exception occurred: {str(e)}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500



@app.route("/fetch-shipping-data", methods=["GET"])
def fetch_shipping_data_endpoint():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        logging.error("❌ Missing selling_partner_id")
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        logging.error("❌ No valid access token found")
        return jsonify({"error": "No valid access token found"}), 401

    try:
        shipping_data = fetch_shipping_data(selling_partner_id, access_token)

        if shipping_data is None:
            logging.error("❌ Shipping data response is None")
            return jsonify({"error": "Failed to fetch shipping data"}), 500

        logging.info(f"📦 API Response: {json.dumps(shipping_data, indent=2)[:500]}")  # Log first 500 chars
        return jsonify(shipping_data), 200

    except Exception as e:
        logging.error(f"❌ Exception occurred: {str(e)}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500






@app.route('/api/fetch_fees', methods=['GET'])
def get_fees_data():
    selling_partner_id = request.args.get('selling_partner_id')

    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No valid access token found"}), 401

    data = fetch_fees_data(selling_partner_id, access_token)

    if data is not None:
        return jsonify(data), 200
    else:
        # Attempt to refresh the token and retry
        access_token = refresh_access_token(selling_partner_id)
        if access_token:
            data = fetch_fees_data(selling_partner_id, access_token)
            if data is not None:
                return jsonify(data), 200

        return jsonify({"error": "Failed to fetch fees data"}), 500


@app.route("/fetch-amazon-fees", methods=["GET"])
def fetch_amazon_fees_endpoint():
    """Fetch Amazon fees including shipping fees for a product SKU."""
    selling_partner_id = request.args.get("selling_partner_id")
    sku = request.args.get("sku")
    marketplace_id = request.args.get("marketplace_id", "A1AM78C64UM0Y8")  # Default to MX marketplace

    if not selling_partner_id or not sku:
        return jsonify({"error": "Missing selling_partner_id or SKU"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No valid access token found"}), 401

    try:
        fees_data = fetch_amazon_fees(selling_partner_id, access_token, marketplace_id, sku)

        if fees_data is None:
            logging.error("❌ Amazon fees response is None")
            return jsonify({"error": "Failed to fetch Amazon fees"}), 500

        return jsonify(fees_data), 200

    except Exception as e:
        logging.error(f"❌ Exception occurred: {str(e)}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500




def store_order_items_in_db(selling_partner_id, order_data):
    """
    Save only necessary Amazon order item fields into PostgreSQL, linked to selling_partner_id.
    Avoids duplicates based on order_item_id.
    """
    amazon_order_id = order_data.get("amazon_order_id")
    order_items = order_data.get("order_items", [])

    for item in order_items:
        order_item_id = item.get("OrderItemId")

        # ✅ Check if the item already exists
        existing_item = AmazonOrderItem.query.filter_by(order_item_id=order_item_id).first()

        if not existing_item:
            new_item = AmazonOrderItem(
                selling_partner_id=selling_partner_id,
                amazon_order_id=amazon_order_id,
                order_item_id=order_item_id,
                asin=item.get("ASIN"),
                title=item.get("Title"),
                seller_sku=item.get("SellerSKU"),
                condition=item.get("Condition"),
                is_gift=item.get("IsGift", "false").lower() == "true",
                quantity_ordered=int(item.get("QuantityOrdered", 0)),
                quantity_shipped=int(item.get("QuantityShipped", 0)),
                item_price=float(item.get("ItemPrice", 0) or 0),
                item_tax=float(item.get("ItemTax", 0) or 0),
                shipping_price=float(item.get("ShippingPrice", 0) or 0),
                shipping_tax=float(item.get("ShippingTax", 0) or 0),
                created_at=datetime.utcnow()
            )

            db.session.add(new_item)

    db.session.commit()
    print(f"✅ {len(order_items)} order items saved to database.")


@app.route("/get-order-items", methods=["GET"])
def get_order_items():
    """Fetches order items for a given Amazon order ID."""
    
    amazon_order_id = request.args.get("amazon_order_id")
    selling_partner_id = request.args.get("selling_partner_id")

    if not amazon_order_id or not selling_partner_id:
        return jsonify({"error": "Missing required parameters (amazon_order_id, selling_partner_id)"}), 400

    # ✅ Get or refresh access token
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No valid access token found"}), 401

    # ✅ Fetch order items
    order_items = fetch_order_items(access_token, amazon_order_id)

    if order_items is None:
        return jsonify({"error": "Failed to retrieve order items"}), 500

    return jsonify({"amazon_order_id": amazon_order_id, "order_items": order_items})






if __name__ == "__main__":
    print("🚀 Starting Flask server...")
    app.run(host="0.0.0.0", port=8080, debug=False)






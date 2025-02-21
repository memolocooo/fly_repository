import os
import uuid
import requests
import redis
import json
from flask import Flask, session, redirect, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from datetime import timedelta, datetime  # Instead of `import datetime`
from flask_cors import CORS
from models import db, AmazonOAuthTokens, AmazonOrders  # Use the correct class name
import psycopg2
from psycopg2.extras import execute_values
from amazon_api import fetch_orders_from_amazon  # Adjust module name if needed


# Load environment variables
load_dotenv()




# Initialize Flask app
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
redis_client = redis.StrictRedis(host="localhost", port=6379, decode_responses=True)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")
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
    """Save only necessary Amazon order fields into PostgreSQL."""
    for order in orders:
        order_id = order.get("AmazonOrderId")  # ✅ Amazon's order ID
        marketplace_id = order.get("MarketplaceId")
        amazon_order_id = order.get("AmazonOrderId")  # ✅ Ensure this field is stored
        number_of_items_shipped = order.get("NumberOfItemsShipped", 0)
        order_status = order.get("OrderStatus", "UNKNOWN")
        total_amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        currency = order.get("OrderTotal", {}).get("CurrencyCode")
        purchase_date = order.get("PurchaseDate")

        # Check if the order already exists
        existing_order = AmazonOrders.query.filter_by(order_id=order_id).first()

        if not existing_order:  # ✅ Avoid duplicate entries
            new_order = AmazonOrders(
                order_id=order_id,
                amazon_order_id=amazon_order_id,  # ✅ Ensure this is stored
                marketplace_id=marketplace_id,
                selling_partner_id=selling_partner_id,
                number_of_items_shipped=number_of_items_shipped,
                order_status=order_status,
                total_amount=total_amount,
                currency=currency,
                purchase_date=datetime.strptime(purchase_date, "%Y-%m-%dT%H:%M:%SZ"),
                created_at=datetime.utcnow()
            )

            db.session.add(new_order)

    db.session.commit()
    print(f"✅ {len(orders)} orders saved to database.")






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
    """Handles Amazon OAuth callback and stores access tokens."""
    auth_code = request.args.get("spapi_oauth_code")
    selling_partner_id = request.args.get("selling_partner_id")
    if not auth_code or not selling_partner_id:
        return jsonify({"error": "Missing auth_code or selling_partner_id"}), 400
    print(f"🚀 Received auth_code: {auth_code}")
    print(f"🔍 Received selling_partner_id: {selling_partner_id}")
    # Exchange auth code for access & refresh tokens
    token_payload = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": LWA_APP_ID,
        "client_secret": LWA_CLIENT_SECRET
    }
    token_response = requests.post(TOKEN_URL, data=token_payload)
    token_data = token_response.json()
    if "access_token" in token_data and "refresh_token" in token_data:
        save_oauth_tokens(
            selling_partner_id,
            token_data["access_token"],
            token_data["refresh_token"],
            token_data["expires_in"]
        )
        return redirect(f"https://guillermos-amazing-site-b0c75a.webflow.io/dashboard")
    return jsonify({"error": "Failed to obtain tokens", "details": token_data}), 400




@app.route("/get-orders", methods=["GET"])
def get_orders():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400
    # ✅ Step 1: Check Redis Cache First (To Reduce Database Load)
    cache_key = f"orders:{selling_partner_id}"
    cached_orders = redis_client.get(cache_key)
    if cached_orders:
        return jsonify(json.loads(cached_orders))  # Return cached orders
    # ✅ Step 2: Ensure Seller Token Exists
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()
    if not token_entry:
        return jsonify({"error": "No OAuth token found for seller"}), 404
    # ✅ Step 3: Handle Date Filtering Properly
    try:
        start_date = datetime.strptime(request.args.get("start_date", (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")), "%Y-%m-%d")
        end_date = datetime.strptime(request.args.get("end_date", datetime.utcnow().strftime("%Y-%m-%d")), "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
    # ✅ Step 4: Query Database for Orders (Filtering by Date)
    orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.purchase_date >= start_date,
        AmazonOrders.purchase_date <= end_date
    ).order_by(AmazonOrders.purchase_date.desc()).all()
    if not orders:
        return jsonify({"message": "No orders found"}), 404
    # ✅ Step 5: Convert Orders to JSON Format
    orders_data = [{
        "order_id": order.order_id,
        "total_amount": order.total_amount,
        "currency": order.currency,
        "order_status": order.order_status,
        "purchase_date": order.purchase_date.strftime("%Y-%m-%d"),
        "marketplace_id": order.marketplace_id,
        "number_of_items_shipped": order.number_of_items_shipped
    } for order in orders]
    # ✅ Step 6: Store in Redis for Caching (Expire in 15 Minutes)
    redis_client.setex(cache_key, 900, json.dumps(orders_data))
    return jsonify(orders_data)






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
    return "Flask App Running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)


selling_partner_id = "A3IW67JB0KIPK8"

with app.app_context():
    token_entry = AmazonOAuthTokens.query.filter_by(selling_partner_id=selling_partner_id).first()

    if not token_entry:
        print("❌ No OAuth token found for seller!")
    else:
        access_token = token_entry.access_token
        created_after = (datetime.utcnow() - timedelta(days=365)).isoformat()

        print(f"🔍 Fetching orders for seller {selling_partner_id} since {created_after}")

        orders = fetch_orders_from_amazon(selling_partner_id, access_token, created_after)

        if not orders:
            print("❌ No orders returned from Amazon API!")
        else:
            print(f"✅ Amazon returned {len(orders)} orders!")
            print(orders[:2])  # Show the first two orders for debugging
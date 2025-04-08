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
from models import db, AmazonOAuthTokens, AmazonOrders, AmazonSettlementData,AmazonOrderItem  # Use the correct class name
import psycopg2
from psycopg2.extras import execute_values
from amazon_api import fetch_orders_from_amazon, fetch_fba_fees_report, fetch_order_items, request_sales_and_fees_report, download_fba_fee_report, sync_fba_fees_last_year  # Adjust module name if needed

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
    """Save Amazon order fields into PostgreSQL, including fees, shipping price, asin, and sku."""

    saved_count = 0

    for order in orders:
        # ✅ Skip FBM (MFN) orders, only keep FBA (AFN)
        if order.get("FulfillmentChannel") != "AFN":
            continue

        order_id = order.get("AmazonOrderId")
        marketplace_id = order.get("MarketplaceId")
        amazon_order_id = order.get("AmazonOrderId")
        number_of_items_shipped = order.get("NumberOfItemsShipped", 0)
        order_status = order.get("OrderStatus", "UNKNOWN")
        total_amount = float(order.get("OrderTotal", {}).get("Amount", 0) or 0)
        currency = order.get("OrderTotal", {}).get("CurrencyCode")
        purchase_date = order.get("PurchaseDate")

        amazon_fees = float(order.get("AmazonFees", {}).get("Amount", 0) or 0)
        shipping_price = float(order.get("ShippingPrice", {}).get("Amount", 0) or 0)

        # ✅ Default ASIN and SKU values
        asin = None
        seller_sku = None

        # ✅ Try to get ASIN and SKU from DB if the items exist
        first_item = AmazonOrderItem.query.filter_by(amazon_order_id=amazon_order_id).first()
        if first_item:
            asin = first_item.asin
            seller_sku = first_item.seller_sku

        # ✅ Check for duplicates
        existing_order = AmazonOrders.query.filter_by(order_id=order_id).first()

        if not existing_order:
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
                amazon_fees=amazon_fees,
                shipping_price=shipping_price,
                asin=asin,
                seller_sku=seller_sku,
                created_at=datetime.utcnow()
            )

            db.session.add(new_order)
            saved_count += 1

    db.session.commit()
    print(f"✅ {saved_count} FBA orders saved to database with Amazon fees, shipping, ASIN & SKU.")



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
    f"&scope=advertising::campaign_management"
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



def store_order_items_in_db(selling_partner_id, order_data):
    """Save order items using SQLAlchemy ORM."""

    amazon_order_id = order_data.get("amazon_order_id")
    order_items = order_data.get("order_items", [])

    if not amazon_order_id or not order_items:
        print("⚠️ No order items to save.")
        return

    # ✅ Fetch the purchase_date from the AmazonOrders table
    order_record = AmazonOrders.query.filter_by(amazon_order_id=amazon_order_id).first()
    purchase_date = order_record.purchase_date if order_record else None

    try:
        for item in order_items:
            order_item_id = item.get("OrderItemId")

            existing_item = AmazonOrderItem.query.filter_by(order_item_id=order_item_id).first()
            if existing_item:
                print(f"🔁 Skipping duplicate item: {order_item_id}")
                continue

            new_item = AmazonOrderItem(
                selling_partner_id=selling_partner_id,
                amazon_order_id=amazon_order_id,
                order_item_id=order_item_id,
                asin=item.get("ASIN"),
                title=item.get("Title"),
                seller_sku=item.get("SellerSKU"),
                condition=item.get("Condition", "Unknown"),
                is_gift=item.get("IsGift", "false") == "true",
                quantity_ordered=int(item.get("QuantityOrdered", 0)),
                quantity_shipped=int(item.get("QuantityShipped", 0)),
                item_price=float(item.get("ItemPrice", 0) or 0),
                item_tax=float(item.get("ItemTax", 0) or 0),
                shipping_price=float(item.get("ShippingPrice", 0) or 0),
                shipping_tax=float(item.get("ShippingTax", 0) or 0),
                purchase_date=purchase_date,  # ✅ This line was missing
                created_at=datetime.utcnow()
            )

            db.session.add(new_item)

        db.session.commit()
        print(f"✅ Saved {len(order_items)} item(s) for order {amazon_order_id}")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error saving order items to DB: {e}")





@app.route("/api/update-cogs", methods=["POST"])
def update_cogs():
    data = request.json
    seller_sku = data.get("seller_sku")
    cogs = data.get("cogs")

    if not seller_sku or cogs is None:
        return jsonify({"error": "Missing seller_sku or cogs"}), 400

    items = AmazonOrderItem.query.filter_by(seller_sku=seller_sku).all()

    if not items:
        return jsonify({"error": f"No items found for seller_sku: {seller_sku}"}), 404

    for item in items:
        item.cogs = cogs

    db.session.commit()
    return jsonify({"message": f"✅ COGS updated for seller_sku {seller_sku}"}), 200






@app.route("/get-all-order-items", methods=["GET"])
def get_all_order_items():
    """Fetch and store order items for all orders in the last year."""
    from datetime import datetime, timedelta

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    print(f"🔍 Fetching order items for all orders of {selling_partner_id}...")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    # ✅ Get all orders in the last year for this selling_partner_id
    one_year_ago = datetime.utcnow() - timedelta(days=365)
    recent_orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.purchase_date >= one_year_ago
    ).all()

    if not recent_orders:
        return jsonify({"message": "No orders found for the last year"}), 200

    saved_count = 0
    for order in recent_orders:
        amazon_order_id = order.amazon_order_id
        print(f"📦 Fetching items for order {amazon_order_id}")

        try:
            order_items = fetch_order_items(access_token, amazon_order_id)
            if not order_items:
                print(f"⚠️ No items found for {amazon_order_id}")
                continue

            store_order_items_in_db(
                selling_partner_id=selling_partner_id,
                order_data={
                    "amazon_order_id": amazon_order_id,
                    "order_items": order_items
                }
            )
            saved_count += len(order_items)
        except Exception as e:
            print(f"❌ Failed to process {amazon_order_id}: {e}")
            continue

    return jsonify({
        "status": "✅ Complete",
        "orders_processed": len(recent_orders),
        "order_items_saved": saved_count
    })




@app.route("/api/order-items", methods=["GET"])
def get_order_items_for_dashboard():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # Get all items for the seller
    items = AmazonOrderItem.query.filter_by(selling_partner_id=selling_partner_id).all()

    seen_asins = set()
    unique_items = []

    def compute_margin(item):
        cogs = float(item.cogs or 0)
        fees = float(item.item_tax or 0) + float(item.shipping_tax or 0)
        revenue = float(item.item_price or 0) + float(item.shipping_price or 0)
        margin = revenue - (cogs + fees)
        return round(margin, 2)

    for item in items:
        if item.asin not in seen_asins:
            seen_asins.add(item.asin)
            unique_items.append({
                "order_item_id": item.order_item_id,
                "title": item.title,
                "seller_sku": item.seller_sku,
                "asin": item.asin,
                "item_price": float(item.item_price or 0),
                "shipping_price": float(item.shipping_price or 0),
                "amazon_fees": float(item.item_tax or 0) + float(item.shipping_tax or 0),
                "cogs": float(item.cogs or 0),
                "margin": compute_margin(item)
            })

    return jsonify(unique_items)




@app.route("/api/dashboard-data", methods=["GET"])
def get_dashboard_data():
    selling_partner_id = request.args.get("selling_partner_id")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    orders_query = AmazonOrders.query.filter_by(selling_partner_id=selling_partner_id)

    # Apply date filters
    if start_date:
        try:
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
            orders_query = orders_query.filter(AmazonOrders.purchase_date >= start_date_obj)
        except ValueError:
            return jsonify({"error": "Invalid start_date format"}), 400

    if end_date:
        try:
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
            orders_query = orders_query.filter(AmazonOrders.purchase_date <= end_date_obj)
        except ValueError:
            return jsonify({"error": "Invalid end_date format"}), 400

    orders = orders_query.all()

    # Aggregate shipping from related order items
    shipping_map = {}
    for order in orders:
        shipping_price = sum(
            float(item.shipping_price or 0)
            for item in AmazonOrderItem.query.filter_by(amazon_order_id=order.amazon_order_id).all()
        )
        shipping_map[order.amazon_order_id] = shipping_price

    # Financial calculations
    order_total_sales = sum(float(order.total_amount or 0) for order in orders)
    shipping_total = sum(shipping_map.values())
    total_sales = order_total_sales + shipping_total
    total_fees = sum(float(order.amazon_fees or 0) for order in orders)
    total_cogs = sum(
        float(item.cogs or 0)
        for order in orders
        for item in AmazonOrderItem.query.filter_by(amazon_order_id=order.amazon_order_id).all()
    )
    revenue = total_sales
    roi = ((revenue - total_cogs - total_fees) / (total_cogs + total_fees + 1e-5)) * 100

    return jsonify({
        "cards": {
            "total_sales": round(total_sales, 2),
            "revenue": round(revenue, 2),
            "amazon_fees": round(total_fees, 2),
            "roi": round(roi, 2)
        },
        "orders": [
            {
                **order.to_dict(),
                "shipping_price": round(shipping_map.get(order.amazon_order_id, 0), 2)
            }
            for order in orders
        ]
    })



@app.route("/api/fba-fees-report", methods=["GET"])
def trigger_fba_fees_report():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "Unable to retrieve access token"}), 401

    from amazon_api import sync_fba_fees_last_year
    file_path = sync_fba_fees_last_year(selling_partner_id, access_token)

    if not file_path:
        return jsonify({"error": "Failed to fetch report"}), 500

    return jsonify({"message": "✅ FBA Fees Report downloaded", "file_path": file_path})

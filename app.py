import os
import pandas as pd  # Add this at the top if you haven't
import csv
import uuid
import requests
from io import StringIO
import redis
import logging
import glob
import json
import time
from flask import Flask, session, redirect, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from datetime import timedelta, datetime  # Instead of `import datetime`
from flask_cors import CORS
from models import db, AmazonOAuthTokens, AmazonOrders, AmazonSettlementData,AmazonOrderItem, AmazonFinancialShipmentEvent, AmazonInventoryItem  # Use the correct class name
import psycopg2
from psycopg2.extras import execute_values
from amazon_api import fetch_orders_from_amazon,  fetch_order_items, sync_settlement_report, fetch_and_preview_latest_settlement_report, get_presigned_settlement_url, save_settlement_report_to_v2_table, request_all_listings_report, download_listings_report, list_financial_event_groups, fetch_order_address, request_unsuppressed_inventory_report,download_inventory_report_file, save_unsuppressed_inventory_to_db, get_report_document_id   #Adjust module name if needed

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

        # ✅ Extract shipping address
        shipping_address = order.get("ShippingAddress", {})
        city = shipping_address.get("City")
        state = shipping_address.get("StateOrRegion")
        postal_code = shipping_address.get("PostalCode")
        country_code = shipping_address.get("CountryCode")

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
                created_at=datetime.utcnow(),
                city=city,
                state=state,
                postal_code=postal_code,
                country_code=country_code

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
    f"&scope=sellingpartnerapi::notifications"
    f"&version=beta"
)



    print(f"🔗 OAuth Redirect URL: {oauth_url}")
    return redirect(oauth_url)


@app.route("/callback")
def callback():
    from models import db, Client, MarketplaceParticipation
    from amazon_api import fetch_marketplaces_for_seller
    import requests

    try:
        auth_code = request.args.get("spapi_oauth_code")
        selling_partner_id = request.args.get("selling_partner_id")
        state = request.args.get("state")  # Optional for CSRF protection

        print(f"🚀 Received auth_code: {auth_code}")
        print(f"🔍 Received selling_partner_id: {selling_partner_id}")

        if not auth_code or not selling_partner_id:
            return jsonify({"error": "Missing required parameters."}), 400

        # Step 1: Exchange auth_code for access_token
        token_data = exchange_auth_code_for_tokens(auth_code)
        if not token_data:
            return jsonify({"error": "Failed to exchange authorization code."}), 500

        # Step 2: Save tokens to AmazonOAuthTokens
        save_oauth_tokens(
            selling_partner_id,
            token_data["access_token"],
            token_data["refresh_token"],
            token_data["expires_in"]
        )
        print(f"✅ Tokens saved successfully for {selling_partner_id}")

        # Step 3: Create or find Client
        client = Client.query.filter_by(selling_partner_id=selling_partner_id).first()
        if not client:
            client = Client(
                selling_partner_id=selling_partner_id,
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                region="na"  # Adjust if needed
            )
            db.session.add(client)
            db.session.commit()
            print(f"✅ New Client created for {selling_partner_id}")
        else:
            print(f"✅ Existing Client found for {selling_partner_id}")

        # Step 4: Fetch and Save Marketplaces
        try:
            marketplaces = fetch_marketplaces_for_seller(token_data["access_token"])

            if marketplaces:
                for marketplace in marketplaces:
                    existing = MarketplaceParticipation.query.filter_by(
                        client_id=client.id,
                        selling_partner_id=selling_partner_id,
                        marketplace_id=marketplace["marketplace_id"]
                    ).first()

                    if not existing:
                        new_marketplace = MarketplaceParticipation(
                            client_id=client.id,
                            selling_partner_id=selling_partner_id,
                            marketplace_id=marketplace["marketplace_id"],
                            country_code=marketplace["country_code"],
                            is_participating=True
                        )
                        db.session.add(new_marketplace)

                db.session.commit()
                print(f"✅ Saved {len(marketplaces)} marketplaces for {selling_partner_id}")

            else:
                print(f"⚠️ No marketplaces returned for {selling_partner_id}")

        except Exception as e:
            print(f"❌ Error fetching marketplaces: {str(e)}")

        # Step 5: Optionally trigger /api/start-data-fetch
        try:
            requests.post(
                "https://render-webflow-restless-river-7629.fly.dev/api/start-data-fetch",
                json={"selling_partner_id": selling_partner_id},
                timeout=5
            )
            print("✅ Data fetching triggered successfully!")
        except Exception as e:
            print(f"⚠️ Failed to trigger start-data-fetch: {e}")

        # 🚀 Step 6: Redirect to dashboard WITH selling_partner_id
        return redirect(f"https://guillermos-amazing-site-b0c75a.webflow.io/dashboard?selling_partner_id={selling_partner_id}")

    except Exception as e:
        print(f"❌ Error in callback: {str(e)}")
        return jsonify({"error": "Internal server error."}), 500



@app.route('/api/start-data-fetch', methods=["POST"])
def start_data_fetch():
    from amazon_api import (
        fetch_orders_from_amazon,
        fetch_order_items,
        save_shipment_events_by_group,
        list_financial_event_groups,
        get_marketplace_id_for_seller
    )
    from models import db, AmazonOrders, AmazonOrderItem, AmazonFinancialShipmentEvent
    from app import store_order_items_in_db  # ⬅️ Make sure this matches where it's defined

    data = request.get_json()
    selling_partner_id = data.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    try:
        print(f"🚀 Starting full data fetch for {selling_partner_id}...")

        marketplace_id = get_marketplace_id_for_seller(selling_partner_id)
        if not marketplace_id:
            return jsonify({"error": "No marketplace_id found"}), 400

        one_year_ago = (datetime.utcnow() - timedelta(days=365)).isoformat()

        # 1️⃣ Fetch and store orders
        orders = fetch_orders_from_amazon(selling_partner_id, access_token, one_year_ago)
        if orders:
            for order in orders:
                amazon_order_id = order["AmazonOrderId"]
                existing = AmazonOrders.query.filter_by(amazon_order_id=amazon_order_id).first()
                if not existing:
                    db.session.add(AmazonOrders(
                        selling_partner_id=selling_partner_id,
                        order_id=amazon_order_id,
                        amazon_order_id=amazon_order_id,
                        purchase_date=order.get("PurchaseDate"),
                        order_status=order.get("OrderStatus"),
                        marketplace_id=order.get("MarketplaceId"),
                        number_of_items_shipped=order.get("NumberOfItemsShipped", 0),
                        total_amount=order.get("OrderTotal", {}).get("Amount"),
                        currency=order.get("OrderTotal", {}).get("CurrencyCode"),
                        city=order.get("ShippingAddress", {}).get("City"),
                        state=order.get("ShippingAddress", {}).get("StateOrRegion"),
                        postal_code=order.get("ShippingAddress", {}).get("PostalCode"),
                        country_code=order.get("ShippingAddress", {}).get("CountryCode")
                    ))
            db.session.commit()
            print(f"✅ Stored {len(orders)} orders.")
        else:
            print("⚠️ No orders found.")

        # 2️⃣ Fetch order items for those orders
        one_year_ago_dt = datetime.utcnow() - timedelta(days=365)
        recent_orders = AmazonOrders.query.filter(
            AmazonOrders.selling_partner_id == selling_partner_id,
            AmazonOrders.purchase_date >= one_year_ago_dt
        ).all()

        item_count = 0
        for order in recent_orders:
            amazon_order_id = order.amazon_order_id
            print(f"📦 Fetching items for order {amazon_order_id}")
            try:
                order_items = fetch_order_items(access_token, amazon_order_id)
                if order_items:
                    store_order_items_in_db(selling_partner_id, {
                        "amazon_order_id": amazon_order_id,
                        "order_items": order_items
                    })
                    item_count += len(order_items)
                else:
                    print(f"⚠️ No items returned for {amazon_order_id}")
            except Exception as e:
                print(f"❌ Failed to fetch items for {amazon_order_id}: {e}")
                continue

        print(f"✅ Saved {item_count} order items.")

        # 3️⃣ Fetch shipment events
        financial_groups = list_financial_event_groups(access_token)
        if financial_groups:
            for group in financial_groups.get("payload", {}).get("FinancialEventGroupList", []):
                group_id = group.get("FinancialEventGroupId")
                if group_id:
                    save_shipment_events_by_group(access_token, selling_partner_id, group_id)

        print(f"✅ Finished full data sync for {selling_partner_id}")
        return jsonify({"message": "✅ Data fetching and saving completed."})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error during start-data-fetch: {e}")
        return jsonify({"error": "Failed to fetch data."}), 500





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
                "margin": compute_margin(item),
                "image_url": item.image_url or f"https://render-webflow-restless-river-7629.fly.dev/images/{item.asin}.jpg"
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

    # 🚚 Shipping by order
    shipping_map = {}
    for order in orders:
        shipping_price = sum(
            float(item.shipping_price or 0)
            for item in AmazonOrderItem.query.filter_by(amazon_order_id=order.amazon_order_id).all()
        )
        shipping_map[order.amazon_order_id] = shipping_price

    # 💰 Card metrics
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

    # 🔗 JOIN shipment events + order items
    joined_data = db.session.query(
        AmazonFinancialShipmentEvent,
        AmazonOrderItem.asin,
        AmazonOrderItem.title,
        AmazonOrderItem.cogs
    ).outerjoin(
        AmazonOrderItem,
        (AmazonFinancialShipmentEvent.order_id == AmazonOrderItem.amazon_order_id) &
        (AmazonFinancialShipmentEvent.sku == AmazonOrderItem.seller_sku)
    ).filter(
        AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id,
        AmazonFinancialShipmentEvent.posted_date.isnot(None)
    ).all()

    # 📦 Format joined results
    shipment_events = []
    for event, asin, title, cogs in joined_data:
        data = event.to_dict()
        data["asin"] = asin
        data["title"] = title
        data["cogs"] = float(cogs) if cogs is not None else None
        shipment_events.append(data)

    # 🚀 Final response
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
        ],
        "shipment_events": shipment_events
    })






@app.route("/api/settlement-report")
def run_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)

    if not access_token:
        return jsonify({"message": "❌ No access token found. Please check your credentials."}), 401

    file_path = sync_settlement_report(selling_partner_id, access_token)
    if file_path:
        try:
            with open(file_path, "r") as f:
                preview = f.read(2000)  # Preview first 2000 characters
            return jsonify({
                "message": "✅ Report downloaded",
                "preview": preview,
                "file_path": file_path
            })
        except Exception as e:
            return jsonify({"error": f"Failed to read file: {str(e)}"}), 500
    else:
        return jsonify({"message": "❌ Failed to download settlement report"}), 500



@app.route("/api/access-token")
def get_access_token_for_partner():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found or token is invalid"}), 404

    return jsonify({"access_token": access_token})





@app.route("/api/preview-settlement-report")
def preview_settlement():
    url = request.args.get("url")
    if not url:
        return {"message": "❌ Missing pre-signed URL"}

    from amazon_api import preview_settlement_report_from_url
    preview = preview_settlement_report_from_url(url)

    if preview is None:
        return {"message": "❌ Could not preview report"}

    return preview.to_json(orient="records")




@app.route("/api/settlement-preview")
def preview_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    document_id = request.args.get("document_id")

    if not selling_partner_id or not document_id:
        return {"message": "❌ Missing parameters"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 400

    preview = fetch_and_preview_latest_settlement_report(access_token, document_id)
    if preview:
        return jsonify(preview)
    else:
        return {"message": "❌ Could not preview report"}, 500



@app.route("/api/settlement-save", methods=["POST"])
def save_settlement_report():
    selling_partner_id = request.args.get("selling_partner_id")
    document_id = request.args.get("document_id")

    if not selling_partner_id or not document_id:
        return {"message": "❌ Missing parameters"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 400

    url = get_presigned_settlement_url(access_token, document_id)
    if not url:
        return {"message": "❌ Failed to fetch document URL"}, 500

    response = requests.get(url)
    if response.status_code != 200:
        return {"message": "❌ Failed to download settlement report"}, 500

    save_settlement_report_to_v2_table(response.content.decode("utf-8"), selling_partner_id)
    return {"message": "✅ Settlement report saved"}




@app.route("/api/request-listings-report", methods=["GET"])
def trigger_all_listings_report():
    from amazon_api import request_all_listings_report

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return {"message": "❌ Missing selling_partner_id"}, 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"message": "❌ No access token found"}, 401

    report = request_all_listings_report(access_token)
    if report:
        return jsonify(report)
    else:
        return {"message": "❌ Failed to request listings report"}, 500
    


@app.route("/api/download-listings-report", methods=["GET"])
def download_listings_report_csv():
    """
    Downloads the Merchant Listings All Data report (using the reportId) and sends
    it as a downloadable CSV file.
    """
    selling_partner_id = request.args.get("selling_partner_id")
    report_id = request.args.get("report_id")
    
    if not selling_partner_id or not report_id:
        return jsonify({"error": "Missing selling_partner_id or report_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 400

    from amazon_api import download_listings_report
    file_path = download_listings_report(report_id, access_token)
    if not file_path:
        return jsonify({"error": "Failed to download listings report"}), 500

    return send_file(file_path, as_attachment=True)



@app.route("/api/simulate-fba-fee", methods=["GET"])
def simulate_fba_fulfillment_fee():
    from amazon_api import get_dimensions_by_asin

    selling_partner_id = request.args.get("selling_partner_id")
    asin = request.args.get("asin")

    if not selling_partner_id or not asin:
        return jsonify({"error": "Missing asin or selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "Access token not found"}), 401

    # Step 1: Get product dimensions and weight
    dimensions = get_dimensions_by_asin(access_token, asin)
    if not dimensions:
        return jsonify({"error": "Could not fetch dimensions from Amazon"}), 500

    length = dimensions.get("length_cm")
    width = dimensions.get("width_cm")
    height = dimensions.get("height_cm")
    weight_kg = dimensions.get("weight_kg")

    if not all([length, width, height, weight_kg]):
        return jsonify({
            "error": "Missing one or more values for length, width, height, or weight",
            "dimensions": dimensions
        }), 400

    # Step 2: Estimate dimensional weight
    dim_weight = (length * width * height) / 5000
    billable_weight = max(weight_kg, dim_weight)

    # Step 3: Estimate FBA fulfillment fee (MX rough tiers)
    if billable_weight <= 0.2:
        fba_fee = 57
        tier = "Small Standard"
    elif billable_weight <= 0.5:
        fba_fee = 72
        tier = "Large Standard"
    elif billable_weight <= 1.0:
        fba_fee = 90
        tier = "Standard Heavy"
    else:
        fba_fee = 90 + ((billable_weight - 1.0) * 20)
        tier = "Oversize or Heavy"

    return jsonify({
        "asin": asin,
        "tier": tier,
        "fba_fee_mxn": round(fba_fee, 2),
        "billable_weight_kg": round(billable_weight, 3),
        "actual_weight_kg": round(weight_kg, 3),
        "dim_weight_kg": round(dim_weight, 3),
        "dimensions_cm": {
            "length": length,
            "width": width,
            "height": height
        }
    })



@app.route("/api/financial-events/group", methods=["GET"])
def financial_events_by_group():
    selling_partner_id = request.args.get("selling_partner_id")
    group_id = request.args.get("group_id")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_group
    events = get_financial_events_by_group(access_token, group_id)
    return jsonify(events or {"error": "Failed to retrieve events"})



@app.route("/download/financial-group/<group_id>", methods=["GET"])
def download_financial_group(group_id):
    file_path = f"financial_event_reports/group_{group_id}.json"
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        return {"error": "File not found"}, 404



# Endpoint: Financial Events by Order
@app.route("/api/financial-events/order", methods=["GET"])
def financial_events_by_order():
    selling_partner_id = request.args.get("selling_partner_id")
    order_id = request.args.get("order_id")

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_order
    events = get_financial_events_by_order(access_token, order_id)
    return jsonify(events or {"error": "Failed to retrieve events"})


# Endpoint: Financial Events by Date Range
@app.route("/api/financial-events/date-range", methods=["GET"])
def financial_events_by_date_range():
    selling_partner_id = request.args.get("selling_partner_id")
    start_date = request.args.get("start_date")  # Example: 2024-12-01T00:00:00Z
    end_date = request.args.get("end_date")      # Example: 2024-12-31T23:59:59Z

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import get_financial_events_by_date_range
    events = get_financial_events_by_date_range(access_token, start_date, end_date)
    return jsonify(events or {"error": "Failed to retrieve events"})



@app.route("/api/financial-events/groups", methods=["GET"])
def list_event_groups():
    selling_partner_id = request.args.get("selling_partner_id")
    start_date = request.args.get("start_date")  # Optional: 2024-12-01T00:00:00Z
    end_date = request.args.get("end_date")      # Optional: 2024-12-31T23:59:59Z

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "No access token"}, 401

    from amazon_api import list_financial_event_groups
    groups = list_financial_event_groups(access_token, start_date, end_date)
    return jsonify(groups or {"error": "Failed to fetch event groups"})




@app.route("/api/fetch-and-save-fba-events", methods=["GET"])
def fetch_and_save_fba_events():
    from amazon_api import list_financial_event_groups, save_shipment_events_by_group

    selling_partner_id = request.args.get("selling_partner_id")
    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return {"error": "Access token not found"}, 401

    # Obtener los grupos de los últimos 365 días
    start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    group_data = list_financial_event_groups(access_token, start_date, end_date)
    group_list = group_data.get("payload", {}).get("FinancialEventGroupList", [])

    saved_groups = 0

    for group in group_list:
        group_id = group.get("FinancialEventGroupId")
        if group_id:
            save_shipment_events_by_group(access_token, selling_partner_id, group_id)
            saved_groups += 1

    return {"message": f"✅ Guardado exitoso de eventos de envío para {saved_groups} grupos"}




from sqlalchemy.orm import aliased
from models import AmazonFinancialShipmentEvent, AmazonOrderItem

@app.route("/api/joined-financial-events", methods=["GET"])
def get_joined_financial_data():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    # Perform the join
    results = (
        db.session.query(
            AmazonFinancialShipmentEvent,
            AmazonOrderItem.asin,
            AmazonOrderItem.title,
            AmazonOrderItem.cogs
        )
        .outerjoin(
            AmazonOrderItem,
            (AmazonFinancialShipmentEvent.order_id == AmazonOrderItem.amazon_order_id) &
            (AmazonFinancialShipmentEvent.sku == AmazonOrderItem.seller_sku)
        )
        .filter(AmazonFinancialShipmentEvent.selling_partner_id == selling_partner_id)
        .all()
    )

    # Format results
    joined_data = []
    for event, asin, title, cogs in results:
        row = event.to_dict()
        row["asin"] = asin
        row["title"] = title
        row["cogs"] = float(cogs) if cogs else None
        joined_data.append(row)

    return jsonify(joined_data)



@app.route("/api/inventory-report", methods=["GET"])
def inventory_report():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    access_token = get_stored_tokens(selling_partner_id)
    if not access_token:
        return jsonify({"error": "No access token found"}), 401

    # Step 1: Request the Inventory Report
    report_id = request_unsuppressed_inventory_report(access_token, selling_partner_id)
    if not report_id:
        return jsonify({"error": "Failed to request inventory report"}), 500

    # Step 2: Poll until report is DONE and get documentId
    document_id = get_report_document_id(report_id, access_token)
    if not document_id:
        return jsonify({"error": "Failed to retrieve documentId for report"}), 500

    # Step 3: Download the Inventory Report File
    file_path = download_inventory_report_file(document_id, access_token)
    if not file_path:
        return jsonify({"error": "Failed to download inventory report file"}), 500

    # Step 4: Save Inventory Data to Database
    save_unsuppressed_inventory_to_db(file_path, selling_partner_id)

    # Step 5: Respond
    return jsonify({"message": "✅ Inventory data saved", "file_path": file_path})




@app.route("/update-order-addresses", methods=["POST"])
def update_order_addresses():
    selling_partner_id = request.json.get("selling_partner_id")
    access_token = get_stored_tokens(selling_partner_id)

    orders = AmazonOrders.query.filter(
        AmazonOrders.selling_partner_id == selling_partner_id,
        AmazonOrders.state == None  # Only update orders with missing state
    ).all()

    updated = 0
    for order in orders:
        address = fetch_order_address(access_token, order.order_id)
        if address:
            order.state = address.get("StateOrRegion")
            order.city = address.get("City")
            order.postal_code = address.get("PostalCode")
            order.country_code = address.get("CountryCode")
            updated += 1

    db.session.commit()
    return jsonify({"updated": updated})


@app.route("/api/order-locations", methods=["GET"])
def get_order_locations():
    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    orders = AmazonOrders.query.filter_by(selling_partner_id=selling_partner_id).all()

    location_counts = {}
    for order in orders:
        state = order.state or "Unknown"
        location_counts[state] = location_counts.get(state, 0) + 1

    return jsonify([
        {"location": state, "order_count": count}
        for state, count in location_counts.items()
    ])



import requests
from flask import Response

@app.route("/images/<asin>.jpg")
def proxy_amazon_image(asin):
    url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.jpg"
    try:
        r = requests.get(url, stream=True)
        if r.status_code == 200:
            return Response(r.content, mimetype="image/jpeg")
        else:
            return "Image not found", 404
    except Exception as e:
        return f"Error fetching image: {str(e)}", 500




@app.route("/api/fba-inventory-save", methods=["GET"])
def save_fba_unsuppressed_inventory():
    from amazon_api import sync_unsuppressed_inventory_report

    selling_partner_id = request.args.get("selling_partner_id")
    if not selling_partner_id:
        return jsonify({"error": "Missing selling_partner_id"}), 400

    try:
        file_path = sync_unsuppressed_inventory_report(selling_partner_id)
        if file_path:
            return jsonify({"message": "✅ Inventory saved", "file_path": file_path})
        else:
            return jsonify({"error": "❌ Failed to save inventory"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

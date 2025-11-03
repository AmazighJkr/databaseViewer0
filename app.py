import time
import pymysql
import socketio
import decimal
import configparser
import os
import urllib3
import threading

# Load backend configuration
CONFIG_FILE = 'backend_config.ini'
config = configparser.ConfigParser()
if os.path.exists(CONFIG_FILE):
    config.read(CONFIG_FILE)
else:
    # Create a default config file
    config['STORE'] = {
        'store_code': 'store1',
        'auth_code': 'denzer'
    }
    config['DATABASE'] = {
        'host': 'localhost',
        'user': 'root',
        'password': '',
        'user_database': 'user',
        'business_database': 'stokyo'
    }
    config['API'] = {
        'url': 'https://databaseviewer0.onrender.com'
    }
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    print(f"Created default {CONFIG_FILE}. Please edit it and restart.")
    raise SystemExit(1)

API_URL = config.get('API', 'url')
STORE_CODE = config.get('STORE', 'store_code')
AUTH_CODE = config.get('STORE', 'auth_code')

# Connect to local MySQL database
DB_CONFIG_USER = {
    'host': config.get('DATABASE', 'host'),
    'user': config.get('DATABASE', 'user'),
    'password': config.get('DATABASE', 'password'),
    'database': config.get('DATABASE', 'user_database'),
}
DB_CONFIG_STOCKIO = {
    'host': config.get('DATABASE', 'host'),
    'user': config.get('DATABASE', 'user'),
    'password': config.get('DATABASE', 'password'),
    'database': config.get('DATABASE', 'business_database'),
}

def get_db_connection():
    return pymysql.connect(**DB_CONFIG_USER)

def get_stockio_connection():
    return pymysql.connect(**DB_CONFIG_STOCKIO)

# Silence insecure HTTPS warning (we intentionally disable SSL verification for this controlled setup)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Increase Engine.IO request timeout and prefer websocket transport to avoid polling timeouts
sio = socketio.Client(
    ssl_verify=False,
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=1,
    reconnection_delay_max=30,
    request_timeout=30
)
# --- OFFLINE SNAPSHOT BACKEND ---
@sio.on('get_snapshot_table')
def on_get_snapshot_table(data):
    client_sid = data.get('client_sid')
    table = data.get('table')
    offset = int(data.get('offset', 0))
    limit = int(data.get('limit', 1000))
    if not table:
        sio.emit('snapshot_table_data', {'client_sid': client_sid, 'table': table or '', 'error': 'MISSING_TABLE'})
        return
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Count total rows once when offset == 0
            total = None
            if offset == 0:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                total = cursor.fetchone()[0]
            # Stream a chunk
            cursor.execute(f"SELECT * FROM `{table}` LIMIT %s OFFSET %s", (limit, offset))
            rows = cursor.fetchall()
            # Column names for mapping
            cols = [d[0] for d in cursor.description] if cursor.description else []
        conn.close()
        # Build JSON-safe chunk
        def to_json_row(row):
            obj = {}
            for i, col in enumerate(cols):
                val = row[i] if i < len(row) else None
                if isinstance(val, (time.struct_time,)):
                    try:
                        obj[col] = str(val)
                    except Exception:
                        obj[col] = None
                elif isinstance(val, (decimal.Decimal,)):
                    obj[col] = float(val)
                else:
                    obj[col] = val
            return obj
        payload = {
            'client_sid': client_sid,
            'table': table,
            'offset': offset,
            'limit': limit,
            'total': total,
            'rows': [to_json_row(r) for r in rows],
            'error': None
        }
        sio.emit('snapshot_table_data', payload)
    except Exception as e:
        sio.emit('snapshot_table_data', {'client_sid': client_sid, 'table': table, 'offset': offset, 'limit': limit, 'total': None, 'rows': [], 'error': str(e)})

@sio.event
def connect():
    print('Connected to API')
    # Register this store with the API
    sio.emit('register_store', {'store_code': STORE_CODE, 'auth_code': AUTH_CODE})
    # Start heartbeat to keep connection alive and update activity
    start_heartbeat()

@sio.on('register_store_response')
def on_register_store_response(data):
    if data.get('success'):
        print(f"Store registered successfully: {data['store_code']}")
    else:
        print(f"Failed to register store: {data.get('error')}")

heartbeat_thread = None
heartbeat_running = False

def start_heartbeat():
    """Start a background thread that sends heartbeat every 30 seconds"""
    global heartbeat_thread, heartbeat_running
    if heartbeat_running:
        return
    heartbeat_running = True
    
    def heartbeat_loop():
        while heartbeat_running and sio.connected:
            try:
                time.sleep(30)  # Send heartbeat every 30 seconds
                if heartbeat_running and sio.connected:
                    # Send a simple heartbeat event to update activity
                    sio.emit('heartbeat', {'store_code': STORE_CODE})
                    print(f"[HEARTBEAT] Sent heartbeat for {STORE_CODE}")
            except Exception as e:
                print(f"[HEARTBEAT] Error: {e}")
                break
        print("[HEARTBEAT] Heartbeat thread stopped")
    
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    print("[HEARTBEAT] Heartbeat thread started")

def stop_heartbeat():
    """Stop the heartbeat thread"""
    global heartbeat_running
    heartbeat_running = False

@sio.event
def disconnect():
    print('Disconnected from API')
    stop_heartbeat()
    # The client has auto-reconnect enabled; no action needed here

# Handle login requests from API
@sio.on('login_request')
def on_login_request(data):
    client_sid = data.get('client_sid')
    username = data.get('username')
    password = data.get('password')
    result = {'client_sid': client_sid, 'success': False, 'error': 'Invalid credentials', 'user_info': None}
    if not username or not password:
        result['error'] = 'Missing username or password'
        sio.emit('login_response', result)
        return
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Fetch all permission fields needed for BUTTONS_ACCESS
            sql = """
                SELECT iduser, np, nom, prenom, ventecompt, listeproduit, clients, survolrecept, survolvente, caisse, fournisseurs
                FROM user
                WHERE np=%s AND password=%s LIMIT 1
            """
            cursor.execute(sql, (username, password))
            user = cursor.fetchone()
        conn.close()
        if user:
            result['success'] = True
            result['error'] = None
            # Return user info and access rights for all mapped fields
            result['user_info'] = {
                'iduser': user[0],
                'np': user[1],
                'nom': user[2],
                'prenom': user[3],
                'ventecompt': user[4],
                'listeproduit': user[5],
                'clients': user[6],
                'survolrecept': user[7],
                'survolvente': user[8],
                'caisse': user[9],
                'fournisseurs': user[10],
            }
        else:
            result['error'] = 'Invalid username or password'
    except Exception as e:
        result['error'] = str(e)
    sio.emit('login_response', result)

# --- STOCK HANDLERS ---
@sio.on('get_products')
def on_get_products(data):
    # Returns list of products with code, name, and total quantity from nomenc.qg
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            sql = '''SELECT p.nop, p.nom, COALESCE(s.qg, 0) as qte
                     FROM prod p
                     LEFT JOIN nomenc s ON p.nop = s.nop
                     GROUP BY p.nop, p.nom, s.qg'''
            cursor.execute(sql)
            products = cursor.fetchall()
        conn.close()
        def to_str(val):
            return str(val) if val is not None else ''
        sio.emit('products_data', {'products': [
            {'np': to_str(row[0]), 'produits': to_str(row[1]), 'qte': to_str(row[2])} for row in products
        ]})
    except Exception as e:
        sio.emit('products_data', {'error': str(e)})

@sio.on('get_product_details')
def on_get_product_details(data):
    # Returns all stock entries for a selected product, with total quantity from nomenc.qg
    np = data.get('np')
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Get all stoc entries for the product - include num field
            sql = '''SELECT num, nop, pacha, q, pv, pgros, psgros FROM stoc WHERE nop = %s'''
            cursor.execute(sql, (np,))
            details = cursor.fetchall()
            # Get total quantity from nomenc.qg
            sql_qg = '''SELECT COALESCE(qg, 0) FROM nomenc WHERE nop = %s'''
            cursor.execute(sql_qg, (np,))
            qg_row = cursor.fetchone()
            total_qte = qg_row[0] if qg_row else 0
        conn.close()
        def to_str(val, is_price=False):
            if val is None:
                return ''
            try:
                f = float(val)
                return f"{f:.2f}" if is_price else str(val)
            except Exception:
                return str(val)
        sio.emit('product_details_data', {
            'details': [
                {'num': to_str(row[0]), 'np': to_str(row[1]), 'pachat': to_str(row[2], True), 'qte': to_str(row[3]), 'pv1': to_str(row[4], True), 'pv2': to_str(row[5], True), 'pv3': to_str(row[6], True)} for row in details
            ],
            'total_qte': to_str(total_qte)
        })
    except Exception as e:
        sio.emit('product_details_data', {'error': str(e)})

# --- BARCODE HANDLER ---
@sio.on('get_product_by_barcode')
def on_get_product_by_barcode(data):
    barcode = data.get('barcode')
    try:
        if not barcode:
            sio.emit('product_by_barcode_data', {'success': False, 'error': 'MISSING_BARCODE'})
            return
        # Normalize input
        raw_barcode = str(barcode)
        barcode = raw_barcode.strip()
        print(f"[BARCODE] Received barcode='{raw_barcode}' -> normalized='{barcode}'")
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # DEBUG: Show exact matches
            try:
                cursor.execute("SELECT nop, cbar, num FROM stoc WHERE cbar = %s ORDER BY num DESC LIMIT 5", (barcode,))
                rows_dbg = cursor.fetchall()
                print(f"[BARCODE] Exact cbar matches (top 5): {rows_dbg}")
            except Exception as e_dbg:
                print(f"[BARCODE] DEBUG exact query error: {e_dbg}")

            # DEBUG: Show matches ignoring leading zeros
            try:
                cursor.execute("SELECT nop, cbar, num FROM stoc WHERE TRIM(LEADING '0' FROM cbar) = TRIM(LEADING '0' FROM %s) ORDER BY num DESC LIMIT 5", (barcode,))
                rows_zero = cursor.fetchall()
                print(f"[BARCODE] Trim-leading-zeros matches (top 5): {rows_zero}")
            except Exception as e_dbg:
                print(f"[BARCODE] DEBUG trim-leading-zeros query error: {e_dbg}")

            # DEBUG: Show matches ignoring spaces/dashes
            try:
                cursor.execute("SELECT nop, cbar, num FROM stoc WHERE REPLACE(REPLACE(cbar,'-',''),' ', '') = REPLACE(REPLACE(%s,'-',''),' ', '') ORDER BY num DESC LIMIT 5", (barcode,))
                rows_strip = cursor.fetchall()
                print(f"[BARCODE] Strip spaces/dashes matches (top 5): {rows_strip}")
            except Exception as e_dbg:
                print(f"[BARCODE] DEBUG strip query error: {e_dbg}")

            # 1) Resolve nop by cbar with filtering rule: ignore products with nop < 16
            # Try exact, then ignoring leading zeros, spacing, dashes, and numeric equality
            sql_candidates = (
                "SELECT nop, num FROM stoc "
                "WHERE cbar = %s "
                "   OR TRIM(LEADING '0' FROM cbar) = TRIM(LEADING '0' FROM %s) "
                "   OR (REPLACE(cbar,' ', '') = REPLACE(%s,' ', '')) "
                "   OR (REPLACE(REPLACE(cbar,'-',''),' ', '') = REPLACE(REPLACE(%s,'-',''),' ', '')) "
                "   OR (cbar REGEXP '^[0-9]+' AND %s REGEXP '^[0-9]+' AND CAST(cbar AS UNSIGNED) = CAST(%s AS UNSIGNED)) "
                "ORDER BY num DESC LIMIT 20"
            )
            cursor.execute(sql_candidates, (barcode, barcode, barcode, barcode, barcode, barcode))
            candidate_rows = cursor.fetchall() or []
            print(f"[BARCODE] Candidate nops ordered by latest num (top 20): {candidate_rows}")
            chosen_nop = None
            for cand in candidate_rows:
                try:
                    cand_nop_int = int(cand[0])
                except Exception:
                    continue
                if cand_nop_int >= 16:
                    chosen_nop = cand_nop_int
                    break
            if chosen_nop is None:
                conn.close()
                print(f"[BARCODE] No candidate with nop >= 16 for '{barcode}'. Ignoring lower nops.")
                sio.emit('product_by_barcode_data', {'success': False, 'error': 'NOT_FOUND'})
                return
            nop = chosen_nop

            # 2) Compose product fields
            # Name from prod
            sql_name = 'SELECT nom FROM prod WHERE nop = %s LIMIT 1'
            cursor.execute(sql_name, (nop,))
            name_row = cursor.fetchone()
            name = name_row[0] if name_row else ''
            print(f"[BARCODE] name lookup: nop={nop} -> nom='{name}'")

            # Total quantity from nomenc.qg
            sql_qg = 'SELECT COALESCE(qg,0) FROM nomenc WHERE nop = %s'
            cursor.execute(sql_qg, (nop,))
            qg_row = cursor.fetchone()
            qte_total = qg_row[0] if qg_row else 0
            print(f"[BARCODE] qg lookup: nop={nop} -> qg={qte_total}")

            # No need to read supplier or price data for barcode search; keep it lightweight
        conn.close()

        def to_str(val, is_price=False):
            if val is None:
                return ''
            try:
                f = float(val)
                return f"{f:.2f}" if is_price else str(val)
            except Exception:
                return str(val)

        # For barcode search, we only need the product name to drive the phone-side filter
        print(f"[BARCODE] FOUND nop={nop}, name='{name}' -> sending name only")
        sio.emit('product_by_barcode_data', {'success': True, 'name': str(name)})
    except Exception as e:
        print(f"[BARCODE] ERROR for '{barcode}': {e}")
        sio.emit('product_by_barcode_data', {'success': False, 'error': str(e)})

@sio.on('get_clients')
def on_get_clients(data):
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Use the correct column names based on the actual database schema
            sql = '''SELECT idclient, np, adr, tel1, tel2, credit, email FROM clients'''
            cursor.execute(sql)
            clients = cursor.fetchall()
        conn.close()
        def to_str(val):
            return str(val) if val is not None else ''
        
        if clients:
            sio.emit('clients_data', {'clients': [
                {
                    'id': to_str(row[0]),
                    'nom': to_str(row[1]),  # np column contains the name
                    'adr': to_str(row[2]),
                    'tel1': to_str(row[3]),
                    'tel2': to_str(row[4]),
                    'credit': to_str(row[5]),
                    'email': to_str(row[6])
                } for row in clients
            ]})
        else:
            sio.emit('clients_data', {'clients': []})
    except Exception as e:
        sio.emit('clients_data', {'error': str(e)})

@sio.on('get_usernames_request')
def on_get_usernames_request(data):
    client_sid = data.get('client_sid')
    if not client_sid:
        print("[BACKEND] get_usernames_request missing client_sid")
        return
    
    # Single-list response: only 'users' is returned; client derives usernames
    result = {'client_sid': client_sid, 'users': [], 'error': None}
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Return usernames and full profiles with permissions so mobile can cache offline
            sql = (
                "SELECT iduser, np, nom, prenom, password, ventecompt, listeproduit, clients, "
                "survolrecept, survolvente, caisse, fournisseurs FROM user ORDER BY np"
            )
            cursor.execute(sql)
            rows = cursor.fetchall()
        conn.close()
        
        usernames = []
        users = []
        processed_count = 0
        skipped_count = 0
        
        for row in rows:
            try:
                username = str(row[1]) if row[1] is not None else ''
                if username and username.strip():
                    username = username.strip()
                    usernames.append(username)
                    
                    # Create full user profile
                    # Send a single, normalized record per user with stable field names
                    user_profile = {
                        'username': username,  # explicit username field
                        'iduser': int(row[0]) if row[0] is not None else 0,
                        'np': username,  # keep legacy np for compatibility
                        'nom': '' if row[2] is None else str(row[2]),
                        'prenom': '' if row[3] is None else str(row[3]),
                        'password': '' if row[4] is None else str(row[4]),
                        'ventecompt': int(row[5] or 0),
                        'listeproduit': int(row[6] or 0),
                        'clients': int(row[7] or 0),
                        'survolrecept': int(row[8] or 0),
                        'survolvente': int(row[9] or 0),
                        'caisse': int(row[10] or 0),
                        'fournisseurs': int(row[11] or 0)
                    }
                    users.append(user_profile)
                    processed_count += 1
                    
                    # Debug log for each user processed
                    print(f"[BACKEND] Processed user: {username} (ID: {user_profile['iduser']}, Permissions: listeproduit={user_profile['listeproduit']}, clients={user_profile['clients']})")
                else:
                    skipped_count += 1
                    print(f"[BACKEND] Skipped user with empty username: {row}")
            except Exception as e:
                skipped_count += 1
                print(f"[BACKEND] Error processing user row {row}: {e}")
                continue
        
        result['users'] = users
        
        print(f"[BACKEND] users count={len(users)}, processed={processed_count}, skipped={skipped_count}")
        
        if users:
            print(f"[BACKEND] First user profile: {users[0]}")
            if len(users) > 1:
                print(f"[BACKEND] Second user profile: {users[1]}")
            print(f"[BACKEND] Last user profile: {users[-1]}")
        
        # Debug: Show the complete result structure
        print(f"[BACKEND] Complete result structure: {result}")
        
    except Exception as e:
        error_msg = f"Database error: {str(e)}"
        result['error'] = error_msg
        print(f"[BACKEND] ERROR in get_usernames_request: {error_msg}")
    
    # Send a single response with everything - use the event name the Android app expects
    # But send it through the API relay, not directly to client
    sio.emit('usernames_list_response', result)
    print(f"[BACKEND] Emitted usernames_list_response to API relay with {len(usernames)} usernames and {len(users)} user profiles")

# --- SALES HANDLERS ---
@sio.on('get_sales')
def on_get_sales(data):
    # Get sales data with filters
    date_from = data.get('date_from')
    date_to = data.get('date_to')
    vendeur = data.get('vendeur')
    client = data.get('client')
    
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Build the query with correct cross-database join
            sql = '''SELECT v.nov, v.datv, c.np as client_name, u.np as vendeur_name, v.verse
                     FROM vente v
                     LEFT JOIN clients c ON v.idclient = c.idclient
                     LEFT JOIN user.user u ON v.emp = u.iduser'''
            conditions = []
            params = []
            if date_from:
                conditions.append("v.datv >= %s")
                params.append(date_from)
            if date_to:
                conditions.append("v.datv <= %s")
                params.append(date_to)
            if vendeur:
                conditions.append("u.np LIKE %s")
                params.append(f"%{vendeur}%")
            if client:
                conditions.append("c.np LIKE %s")
                params.append(f"%{client}%")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY v.datv DESC"
            cursor.execute(sql, params)
            sales = cursor.fetchall()
        conn.close()
        def to_str(val):
            return str(val) if val is not None else ''
        if sales:
            sales_data = [
                {
                    'id': to_str(row[0]),  # nov
                    'date': to_str(row[1]),  # datv
                    'client': to_str(row[2]),  # client_name
                    'vendeur': to_str(row[3]),  # vendeur_name
                    'montant': to_str(row[4])  # verse
                } for row in sales
            ]
            sio.emit('sales_data', {'sales': sales_data})
        else:
            sio.emit('sales_data', {'sales': []})
    except Exception as e:
        sio.emit('sales_data', {'error': str(e)})

@sio.on('get_sale_details')
def on_get_sale_details(data):
    # Get sale details for a specific sale
    sale_id = data.get('sale_id')
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Select product name, quantity, and product price (ppa)
            sql = '''SELECT p.nom as produit, vx.qt, vx.ppa
                     FROM ventex vx
                     LEFT JOIN prod p ON vx.nop = p.nop
                     WHERE vx.nov = %s'''
            cursor.execute(sql, (sale_id,))
            details = cursor.fetchall()
        conn.close()
        def to_str(val, is_price=False):
            if val is None:
                return ''
            try:
                f = float(val)
                return f"{f:.2f}" if is_price else str(val)
            except Exception:
                return str(val)
        if details:
            sio.emit('sale_details_data', {'details': [
                {
                    'produit': to_str(row[0]),  # produit name
                    'qtt': to_str(row[1]),      # qt
                    'pv': to_str(row[2], True)  # ppa (product price)
                } for row in details
            ]})
        else:
            sio.emit('sale_details_data', {'details': []})
    except Exception as e:
        sio.emit('sale_details_data', {'error': str(e)})

@sio.on('get_vendeurs')
def on_get_vendeurs(data):
    # Get list of vendeurs (users)
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # First, let's check what tables exist
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()
            
            # Check if user table exists and has data
            cursor.execute("SELECT COUNT(*) FROM user")
            user_count = cursor.fetchone()[0]
            
            sql = "SELECT np FROM user ORDER BY np"
            cursor.execute(sql)
            rows = cursor.fetchall()
        conn.close()
        
        vendeurs = [row[0] for row in rows if row[0]]
        sio.emit('vendeurs_data', {'vendeurs': vendeurs})
    except Exception as e:
        sio.emit('vendeurs_data', {'error': str(e)})

@sio.on('get_clients_list')
def on_get_clients_list(data):
    # Get list of clients for dropdown
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # First, let's check what tables exist
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()
            
            # Check if clients table exists and has data
            cursor.execute("SELECT COUNT(*) FROM clients")
            clients_count = cursor.fetchone()[0]
            
            sql = "SELECT np FROM clients ORDER BY np"
            cursor.execute(sql)
            rows = cursor.fetchall()
        conn.close()
        
        clients = [row[0] for row in rows if row[0]]
        sio.emit('clients_list_data', {'clients': clients})
    except Exception as e:
        sio.emit('clients_list_data', {'error': str(e)})

@sio.on('get_treasury')
def on_get_treasury(data):
    """
    Receives: {'date_from': 'YYYY-MM-DD', 'date_to': 'YYYY-MM-DD', 'client_sid': ...}
    Responds: {'client_sid': ..., 'success': True/False, 'error': ..., 'data': {...}}
    """
    client_sid = data.get('client_sid')
    date_from = data.get('date_from')
    date_to = data.get('date_to')
    print(f"[DEBUG] get_treasury: date_from={date_from}, date_to={date_to}")
    result = {'client_sid': client_sid, 'success': False, 'error': None, 'data': {}}
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Build query to sum all relevant columns in rj for the date range
            sql = '''SELECT 
                COALESCE(SUM(mesv),0), COALESCE(SUM(mesm),0),
                COALESCE(SUM(mhsv),0), COALESCE(SUM(mhsm),0),
                COALESCE(SUM(remisag),0), COALESCE(SUM(caisse),0),
                COALESCE(SUM(vers),0), COALESCE(SUM(dette),0),
                COALESCE(SUM(shp),0), COALESCE(SUM(trem),0),
                COALESCE(SUM(benefice),0), COALESCE(SUM(deps),0),
                COALESCE(SUM(perim),0), COALESCE(SUM(pfourn),0)
                FROM rj WHERE date >= %s AND date <= %s'''
            print(f"[DEBUG] get_treasury SQL: {sql}")
            cursor.execute(sql, (date_from, date_to))
            row = cursor.fetchone()
            print(f"[DEBUG] get_treasury row: {row}")
        conn.close()
        # Map results
        (mesv, mesm, mhsv, mhsm, remisag, caisse, vers, dette, shp, trem, benefice, deps, perim, pfourn) = row
        # Calculated fields
        total_ventes_ca = (mesv or 0) + (mhsv or 0)
        total_ventes_marge = (mesm or 0) + (mhsm or 0)
        total_depenses = (deps or 0) + (perim or 0)
        remise_marge = -(remisag or 0)
        benefice_net = (benefice or 0) - total_depenses
        # Prepare data dict
        def to_str(val):
            if val is None:
                return '0.00'
            try:
                return f"{float(val):.2f}"
            except Exception:
                return str(val)
        treasury = {
            'mesv': to_str(mesv),
            'mesm': to_str(mesm),
            'mhsv': to_str(mhsv),
            'mhsm': to_str(mhsm),
            'total_ventes_ca': to_str(total_ventes_ca),
            'total_ventes_marge': to_str(total_ventes_marge),
            'remisag': to_str(remisag),
            'remise_marge': to_str(remise_marge),
            'caisse': to_str(caisse),
            'vers': to_str(vers),
            'dette': to_str(dette),
            'shp': to_str(shp),
            'trem': to_str(trem),
            'benefice': to_str(benefice),
            'deps': to_str(deps),
            'perim': to_str(perim),
            'total_depenses': to_str(total_depenses),
            'pfourn': to_str(pfourn),
            'benefice_net': to_str(benefice_net),
        }
        print(f"[DEBUG] get_treasury treasury dict: {treasury}")
        result['success'] = True
        result['data'] = treasury
    except Exception as e:
        result['error'] = str(e)
    sio.emit('treasury_data', result)

# --- FOURNISSEURS HANDLER ---
@sio.on('get_fournisseurs')
def on_get_fournisseurs(data):
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            sql = "SELECT nfourn, fourn FROM fourn ORDER BY fourn"
            cursor.execute(sql)
            rows = cursor.fetchall()
        conn.close()
        fournisseurs = [{'nfourn': str(row[0]), 'fourn': str(row[1])} for row in rows]
        sio.emit('fournisseurs_data', {'fournisseurs': fournisseurs})
    except Exception as e:
        sio.emit('fournisseurs_data', {'error': str(e)})

# --- FACTURES ACHAT HANDLER ---
@sio.on('get_factures_achat')
def on_get_factures_achat(data):
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            sql = '''SELECT f.nof, f.datf, fr.fourn, f.tht, f.ttva, f.trem, f.netttc
                     FROM fact f
                     LEFT JOIN fourn fr ON f.nfourn = fr.nfourn
                     ORDER BY f.datf DESC LIMIT 100'''
            cursor.execute(sql)
            rows = cursor.fetchall()
        conn.close()
        factures = [
            {
                'nof': str(row[0]),
                'datf': str(row[1]),
                'fournisseur': str(row[2]),
                'tht': str(row[3]),
                'ttva': str(row[4]),
                'trem': str(row[5]),
                'netttc': str(row[6])
            } for row in rows
        ]
        sio.emit('factures_achat_data', {'factures': factures})
    except Exception as e:
        sio.emit('factures_achat_data', {'error': str(e)})

# --- FACTURE ACHAT DETAILS HANDLER ---
@sio.on('get_facture_achat_details')
def on_get_facture_achat_details(data):
    nof = data.get('facture_id')
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Get facture main info
            sql = '''SELECT f.datf, fr.fourn, f.tht, f.ttva, f.trem, f.netttc
                     FROM fact f
                     LEFT JOIN fourn fr ON f.nfourn = fr.nfourn
                     WHERE f.nof = %s LIMIT 1'''
            cursor.execute(sql, (nof,))
            row = cursor.fetchone()
            if not row:
                sio.emit('facture_achat_details_data', {'details': {}})
                return
            details = {
                'datf': str(row[0]),
                'fournisseur': str(row[1]),
                'tht': str(row[2]),
                'ttva': str(row[3]),
                'trem': str(row[4]),
                'netttc': str(row[5]),
                'products': []
            }
            # Get products for this facture
            sql_prod = '''SELECT p.nom, fx.qt, fx.pacha, fx.pvente
                          FROM factx fx
                          LEFT JOIN prod p ON fx.nop = p.nop
                          WHERE fx.nof = %s'''
            cursor.execute(sql_prod, (nof,))
            prod_rows = cursor.fetchall()
            def fmt_price(val):
                try:
                    return f"{float(val):.2f}"
                except Exception:
                    return str(val) if val is not None else "0.00"

            details['products'] = [
                {
                    'produit': str(prod[0]),
                    'qte': str(prod[1]),
                    'pacha': fmt_price(prod[2]),
                    'pv': fmt_price(prod[3])
                } for prod in prod_rows
            ]
        conn.close()
        sio.emit('facture_achat_details_data', {'details': details})
    except Exception as e:
        sio.emit('facture_achat_details_data', {'error': str(e)})

@sio.on('get_factures_vente')
def on_get_factures_vente(data):
    print(f"Windows Backend: get_factures_vente called with data: {data}")
    # Get sales invoices with filters
    date_from = data.get('date_from')
    date_to = data.get('date_to')
    client_id = data.get('client_id')
    
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Build the query with correct table joins
            sql = '''SELECT f.idfactv, f.date, c.np as client_name, f.tht, f.ttva, f.tremise, f.tttc
                     FROM factv f
                     LEFT JOIN clients c ON f.idclient = c.idclient'''
            conditions = []
            params = []
            if date_from:
                conditions.append("f.date >= %s")
                params.append(date_from)
            if date_to:
                conditions.append("f.date <= %s")
                params.append(date_to)
            if client_id:
                conditions.append("f.idclient = %s")
                params.append(client_id)
            
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            
            sql += " ORDER BY f.date DESC"
            print(f"Windows Backend: Executing SQL: {sql} with params: {params}")
            cursor.execute(sql, params)
            factures = cursor.fetchall()
            print(f"Windows Backend: Found {len(factures)} factures")
        conn.close()
        
        def to_str(val):
            return str(val) if val is not None else ''
        
        response_data = {'factures': [
            {
                'idfactv': to_str(row[0]),
                'date': to_str(row[1]),
                'client_name': to_str(row[2]),
                'tht': to_str(row[3]),
                'ttva': to_str(row[4]),
                'tremise': to_str(row[5]),
                'tttc': to_str(row[6])
            } for row in factures
        ]}
        print(f"Windows Backend: Sending response: {response_data}")
        sio.emit('factures_vente_data', response_data)
    except Exception as e:
        print(f"Windows Backend: Error in get_factures_vente: {e}")
        sio.emit('factures_vente_data', {'error': str(e)})

@sio.on('get_facture_vente_details')
def on_get_facture_vente_details(data):
    print(f"Windows Backend: get_facture_vente_details called with data: {data}")
    facture_id = data.get('facture_id')
    
    if not facture_id:
        print("Windows Backend: No facture_id provided")
        sio.emit('facture_vente_details_data', {'error': 'No facture_id provided'})
        return
    
    try:
        conn = get_stockio_connection()
        with conn.cursor() as cursor:
            # Query facture details from factvx table
            sql = '''SELECT fx.idfactvx, fx.idfactv, fx.nop, fx.produit, fx.q, fx.prix, fx.total, fx.remise
                     FROM factvx fx
                     WHERE fx.idfactv = %s'''
            print(f"Windows Backend: Executing SQL: {sql} with facture_id: {facture_id}")
            cursor.execute(sql, (facture_id,))
            details = cursor.fetchall()
            print(f"Windows Backend: Found {len(details)} detail rows")
            
            # Debug: Print first few rows to see the data structure
            if details:
                print(f"Windows Backend: First detail row: {details[0]}")
        conn.close()
        
        def to_str(val):
            return str(val) if val is not None else ''
        
        response_data = {'details': [
            {
                'idfactvx': to_str(row[0]),
                'idfactv': to_str(row[1]),
                'nop': to_str(row[2]),
                'produit': to_str(row[3]),
                'qte': to_str(row[4]),
                'prix': to_str(row[5]),
                'total': to_str(row[6]),
                'remise': to_str(row[7])
            } for row in details
        ]}
        print(f"Windows Backend: Sending details response: {response_data}")
        sio.emit('facture_vente_details_data', response_data)
    except Exception as e:
        print(f"Windows Backend: Error in get_facture_vente_details: {e}")
        import traceback
        traceback.print_exc()
        sio.emit('facture_vente_details_data', {'error': str(e)})

if __name__ == '__main__':
    # Outer loop for infinite retries with exponential backoff when API is offline
    backoff = 2
    while True:
        try:
            print(f"Connecting to API at {API_URL} using long-polling ...")
            sio.connect(API_URL, transports=['polling'], wait_timeout=60, socketio_path='/socket.io')
            backoff = 2  # reset after successful connect
            sio.wait()   # block here; internal reconnect keeps trying after disconnect
        except KeyboardInterrupt:
            print('Shutting down by user request')
            try:
                sio.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            print(f"[BACKEND] Connection failed: {e}. Retrying in {backoff}s ...")
            try:
                sio.disconnect()
            except Exception:
                pass
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                print('Shutting down by user request')
                break
            backoff = min(backoff * 2, 60)
            continue

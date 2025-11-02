import pymysql
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import gevent
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
# Tighter, mobile-friendly Engine.IO settings: longer ping timeout, reasonable ping interval
socketio = SocketIO(
    app,
    async_mode='gevent',
    cors_allowed_origins='*',
    ping_interval=25,
    ping_timeout=90
)

# Store active store and client sessions
store_sessions = {}  # store_code: sid
client_sessions = {}  # sid: store_code
pending_logins = {}  # client_sid: (store_code, username, password)
# For stock requests
# Change pending_requests to allow multiple requests per client
# pending_requests: {client_sid: [request_dict, ...]}
pending_requests = {}  # client_sid: [ {'type': ..., ...}, ... ]
# --- OFFLINE SNAPSHOT RELAY ---
@socketio.on('get_snapshot_table')
def handle_get_snapshot_table(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    table = (data or {}).get('table')
    if not store_code or not table:
        emit('snapshot_table_data', {'error': 'Missing store or table', 'table': table or ''})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('snapshot_table_data', {'error': 'Store backend not connected', 'table': table})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'snapshot', 'store_code': store_code, 'table': table})
    payload = {
        'client_sid': client_sid,
        'table': table,
        'offset': int((data or {}).get('offset', 0)),
        'limit': int((data or {}).get('limit', 1000)),
    }
    socketio.emit('get_snapshot_table', payload, room=store_sid)

@socketio.on('snapshot_table_data')
def handle_snapshot_table_data(data):
    table = (data or {}).get('table')
    client_sid = (data or {}).get('client_sid')
    if not client_sid:
        # Fallback: deliver to first snapshot requester
        for csid, reqs in list(pending_requests.items()):
            for i, req in enumerate(reqs):
                if req.get('type') == 'snapshot' and req.get('table') == table:
                    socketio.emit('snapshot_table_data', data, room=csid)
                    del pending_requests[csid][i]
                    if not pending_requests[csid]:
                        del pending_requests[csid]
                    return
        return
    # Route directly to requesting client
    socketio.emit('snapshot_table_data', data, room=client_sid)


# --- DB connection helper ---
def get_api_db_connection():
    # For API store authentication, connect to the 'stores' database
    return pymysql.connect(host='db4free.net', user='vmmachine03', password='vmmachine03', database='vmmachine03')

@app.route('/')
def index():
    return jsonify({'message': 'API is running'})

@app.route('/api/store_status/<store_code>')
def get_store_status(store_code):
    """REST endpoint to check store status (ONLINE or OFFLINE)"""
    try:
        conn = get_api_db_connection()
        with conn.cursor() as cursor:
            # Check if store exists and get its status
            sql = "SELECT status FROM stores WHERE store_code=%s LIMIT 1"
            cursor.execute(sql, (store_code,))
            row = cursor.fetchone()
        conn.close()
        if row:
            status = row[0] if row[0] else 'OFFLINE'  # Default to OFFLINE if NULL
            return jsonify({'store_code': store_code, 'status': status})
        else:
            return jsonify({'store_code': store_code, 'status': 'OFFLINE', 'error': 'Store not found'}), 404
    except Exception as e:
        return jsonify({'store_code': store_code, 'status': 'OFFLINE', 'error': str(e)}), 500

@socketio.on('register_store')
def handle_register_store(data):
    store_code = data.get('store_code')
    auth_code = data.get('auth_code')
    if not store_code or not auth_code:
        emit('register_store_response', {'success': False, 'error': 'Missing store code or auth code'})
        return
    # Check credentials in the API database
    try:
        conn = get_api_db_connection()
        with conn.cursor() as cursor:
            sql = "SELECT id FROM stores WHERE store_code=%s AND auth_code=%s LIMIT 1"
            cursor.execute(sql, (store_code, auth_code))
            row = cursor.fetchone()
        conn.close()
        if not row:
            emit('register_store_response', {'success': False, 'error': 'Invalid store code or auth code'})
            return
    except Exception as e:
        emit('register_store_response', {'success': False, 'error': f'Database error: {e}'})
        return
    # Always update to latest sid so reconnect rebinds correctly
    store_sessions[store_code] = request.sid
    join_room(store_code)
    # Initialize activity tracking (use current time)
    store_last_activity[store_code] = time.time()
    # Update store status to ONLINE in database
    try:
        conn = get_api_db_connection()
        with conn.cursor() as cursor:
            sql = "UPDATE stores SET status='ONLINE' WHERE store_code=%s"
            cursor.execute(sql, (store_code,))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error updating store status to ONLINE: {e}")
    emit('register_store_response', {'success': True, 'store_code': store_code})
    print(f"Store registered: {store_code}, sid: {request.sid}, activity initialized at {store_last_activity.get(store_code, 'unknown')}")
    # Notify all clients bound to this store that backend is online
    socketio.emit('store_online', {'store_code': store_code}, room=store_code)

@socketio.on('register_client')
def handle_register_client(data):
    store_code = data.get('store_code')
    if not store_code:
        emit('register_client_response', {'success': False, 'error': 'Missing store code'})
        return
    # Allow client to register even if backend currently offline; it will receive errors per-request
    try:
        conn = get_api_db_connection()
        with conn.cursor() as cursor:
            sql = "SELECT name FROM stores WHERE store_code=%s LIMIT 1"
            cursor.execute(sql, (store_code,))
            row = cursor.fetchone()
        conn.close()
        store_name = row[0] if row else ""
    except Exception as e:
        store_name = ""
    # Then, when emitting register_client_response or login_result:
    client_sessions[request.sid] = store_code
    join_room(store_code)
    emit('register_client_response', {'success': True, 'store_code': store_code, 'store_name': store_name})
    print(f"Client registered for store: {store_code}, sid: {request.sid}")
    # Immediately inform client about backend availability
    if store_code not in store_sessions:
        socketio.emit('store_offline', {'store_code': store_code}, room=request.sid)

# --- Secure login relay ---
@socketio.on('login')
def handle_login(data):
    store_code = data.get('store_code')
    username = data.get('username')
    password = data.get('password')
    client_sid = request.sid
    if not store_code or not username or not password:
        emit('login_result', {'success': False, 'error': 'Missing fields'})
        return
    # Find the Windows backend for this store
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('login_result', {'success': False, 'error': 'Store backend not connected'})
        return
    # Save pending login to match response
    pending_logins[client_sid] = (store_code, username, password)
    # Forward login request to Windows backend
    socketio.emit('login_request', {
        'client_sid': client_sid,
        'username': username,
        'password': password
    }, room=store_sid)
    print(f"Forwarded login for user '{username}' to store '{store_code}' backend.")

@socketio.on('login_response')
def handle_login_response(data):
    print(f"API: login_response received with data: {data}")
    # Update activity for the store that sent this response
    sid = request.sid
    for store_code, store_sid in store_sessions.items():
        if store_sid == sid:
            update_store_activity(store_code)
            break
    client_sid = data.get('client_sid')
    success = data.get('success')
    error = data.get('error')
    user_info = data.get('user_info')
    if not client_sid:
        print("API: No client_sid in login_response")
        return
    # Get store_code from pending_logins
    store_code = None
    if client_sid in pending_logins:
        store_code = pending_logins[client_sid][0]
    # Relay result to Android client
    # After verifying store_code and auth_code:
    try:
        store_name = ""
        if store_code:
            conn = get_api_db_connection()
            with conn.cursor() as cursor:
                sql = "SELECT name FROM stores WHERE store_code=%s LIMIT 1"
                cursor.execute(sql, (store_code,))
                row = cursor.fetchone()
            conn.close()
            store_name = row[0] if row else ""
    except Exception as e:
        store_name = ""
    print(f"API: Emitting login_result to client {client_sid}: success={success}, error={error}")
    socketio.emit('login_result', {
        'success': success,
        'error': error,
        'user_info': user_info,
        'store_name': store_name
    }, room=client_sid)
    print(f"API: Relayed login result to client {client_sid}: {success}, {error}")
    # Remove from pending
    if client_sid in pending_logins:
        del pending_logins[client_sid]

# --- STOCK RELAY HANDLERS ---
@socketio.on('get_products')
def handle_get_products(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('products_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('products_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'products', 'store_code': store_code})
    print(f'API: pending_requests after get_products: {pending_requests}')
    socketio.emit('get_products', data, room=store_sid)

# --- BARCODE RELAY HANDLERS ---
@socketio.on('get_product_by_barcode')
def handle_get_product_by_barcode(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('product_by_barcode_data', {'success': False, 'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('product_by_barcode_data', {'success': False, 'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'barcode', 'store_code': store_code})
    print(f'API: pending_requests after get_product_by_barcode: {pending_requests}')
    socketio.emit('get_product_by_barcode', data, room=store_sid)

@socketio.on('product_by_barcode_data')
def handle_product_by_barcode_data(data):
    print(f'API: product_by_barcode_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'barcode':
                print(f'API: relaying product_by_barcode_data to client_sid={client_sid}')
                # Payload may contain only {'success': True, 'name': '...'} now
                socketio.emit('product_by_barcode_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('products_data')
def handle_products_data(data):
    # Update activity for the store that sent this response
    sid = request.sid
    for store_code, store_sid in store_sessions.items():
        if store_sid == sid:
            update_store_activity(store_code)
            break
    print(f'API: products_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'products':
                print(f'API: relaying products_data to client_sid={client_sid}')
                socketio.emit('products_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_product_details')
def handle_get_product_details(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    np = data.get('np')
    if not store_code or not np:
        emit('product_details_data', {'error': 'Missing store or product code'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('product_details_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'details', 'store_code': store_code, 'np': np})
    print(f'API: pending_requests after get_product_details: {pending_requests}')
    socketio.emit('get_product_details', {'np': np}, room=store_sid)

@socketio.on('product_details_data')
def handle_product_details_data(data):
    # Update activity for the store that sent this response
    sid = request.sid
    for store_code, store_sid in store_sessions.items():
        if store_sid == sid:
            update_store_activity(store_code)
            break
    print(f'API: product_details_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'details':
                print(f'API: relaying product_details_data to client_sid={client_sid}')
                socketio.emit('product_details_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_clients')
def handle_get_clients(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('clients_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('clients_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'clients', 'store_code': store_code})
    print(f'API: pending_requests after get_clients: {pending_requests}')
    socketio.emit('get_clients', data, room=store_sid)

@socketio.on('clients_data')
def handle_clients_data(data):
    print(f'API: clients_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'clients':
                print(f'API: relaying clients_data to client_sid={client_sid}')
                socketio.emit('clients_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_sales')
def handle_get_sales(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('sales_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('sales_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'sales', 'store_code': store_code})
    print(f'API: pending_requests after get_sales: {pending_requests}')
    socketio.emit('get_sales', data, room=store_sid)

@socketio.on('sales_data')
def handle_sales_data(data):
    print(f'API: sales_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'sales':
                print(f'API: relaying sales_data to client_sid={client_sid}')
                socketio.emit('sales_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_sale_details')
def handle_get_sale_details(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    sale_id = data.get('sale_id')
    if not store_code or not sale_id:
        emit('sale_details_data', {'error': 'Missing store or sale id'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('sale_details_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'sale_details', 'store_code': store_code, 'sale_id': sale_id})
    print(f'API: pending_requests after get_sale_details: {pending_requests}')
    socketio.emit('get_sale_details', {'sale_id': sale_id}, room=store_sid)

@socketio.on('sale_details_data')
def handle_sale_details_data(data):
    print(f'API: sale_details_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'sale_details':
                print(f'API: relaying sale_details_data to client_sid={client_sid}')
                socketio.emit('sale_details_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_vendeurs')
def handle_get_vendeurs(data):
    client_sid = request.sid
    print(f'API: get_vendeurs from client_sid={client_sid}')
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('vendeurs_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('vendeurs_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'vendeurs', 'store_code': store_code})
    print(f'API: pending_requests after get_vendeurs: {pending_requests}')
    socketio.emit('get_vendeurs', data, room=store_sid)

@socketio.on('vendeurs_data')
def handle_vendeurs_data(data):
    print(f'API: vendeurs_data received, pending_requests={pending_requests}')
    # Relay vendeurs data to the client who requested it
    for client_sid, reqs in list(pending_requests.items()):
        for i, req in enumerate(reqs):
            if req['type'] == 'vendeurs':
                print(f'API: relaying vendeurs_data to client_sid={client_sid}')
                socketio.emit('vendeurs_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_clients_list')
def handle_get_clients_list(data):
    client_sid = request.sid
    print(f'API: get_clients_list from client_sid={client_sid}')
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('clients_list_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('clients_list_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'clients_list', 'store_code': store_code})
    print(f'API: pending_requests after get_clients_list: {pending_requests}')
    socketio.emit('get_clients_list', data, room=store_sid)

@socketio.on('clients_list_data')
def handle_clients_list_data(data):
    print(f'API: clients_list_data received, pending_requests={pending_requests}')
    # Relay clients list data to the client who requested it
    for client_sid, reqs in list(pending_requests.items()):
        for i, req in enumerate(reqs):
            if req['type'] == 'clients_list':
                print(f'API: relaying clients_list_data to client_sid={client_sid}')
                socketio.emit('clients_list_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_usernames')
def handle_get_usernames(data):
    store_code = data.get('store_code')
    client_sid = request.sid
    if not store_code:
        emit('usernames_list', {'usernames': [], 'error': 'Missing store code'})
        return
    # Validate that the store backend is connected (store previously registered with auth_code)
    if store_code not in store_sessions:
        emit('usernames_list', {'usernames': [], 'error': 'Store backend not connected'})
        return
    # Relay to Windows backend - let it handle the response directly
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('usernames_list', {'usernames': [], 'error': 'Store backend not connected'})
        return
    
    # Send request to backend - backend will respond directly to client
    socketio.emit('get_usernames_request', {'client_sid': client_sid}, room=store_sid)
    print(f"[API] Relayed get_usernames_request to backend for store {store_code}, client {client_sid}")
    print(f"[API] Backend will respond directly to client {client_sid}")

@socketio.on('usernames_list_response')
def handle_usernames_list_response(data):
    # Update activity for the store that sent this response
    sid = request.sid
    for store_code, store_sid in store_sessions.items():
        if store_sid == sid:
            update_store_activity(store_code)
            break
    client_sid = data.get('client_sid')
    users = data.get('users', [])
    error = data.get('error')
    
    if not client_sid:
        print(f"[API] usernames_list_response missing client_sid, data: {data}")
        return
    
    if error:
        print(f"[API] Backend error for client {client_sid}: {error}")
        socketio.emit('usernames_list', {'usernames': [], 'users': [], 'error': error}, room=client_sid)
        return
    
    # Derive usernames on the fly for UI convenience
    usernames = []
    if isinstance(users, list):
        for u in users:
            try:
                name = u.get('username') or u.get('np') or u.get('nom') or ''
                if name:
                    usernames.append(name)
            except Exception:
                pass
    else:
        users = []
    
    response_data = {'usernames': usernames, 'users': users, 'error': None}
    socketio.emit('usernames_list', response_data, room=client_sid)
    print(f"[API] Relayed usernames_list to client {client_sid} (users: {len(users)}, usernames: {len(usernames)})")

# Backend now sends usernames_list_response which we relay as usernames_list

@socketio.on('get_treasury')
def handle_get_treasury(data):
    # data: {'date_from': ..., 'date_to': ...}
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code or store_code not in store_sessions:
        emit('treasury_data', {'success': False, 'error': 'Store not connected', 'data': {}, 'client_sid': client_sid})
        return
    backend_sid = store_sessions[store_code]
    # Track pending request
    req = {'type': 'get_treasury', 'client_sid': client_sid}
    if client_sid not in pending_requests or not isinstance(pending_requests[client_sid], list):
        pending_requests[client_sid] = []
    pending_requests[client_sid].append(req)
    # Relay to backend
    socketio.emit('get_treasury', {
        'date_from': data.get('date_from'),
        'date_to': data.get('date_to'),
        'client_sid': client_sid
    }, room=backend_sid)

@socketio.on('treasury_data')
def handle_treasury_data(data):
    client_sid = data.get('client_sid')
    if not client_sid:
        return
    # Find and remove the matching pending request
    reqs = pending_requests.get(client_sid, [])
    if isinstance(reqs, list):
        for i, req in enumerate(reqs):
            if req.get('type') == 'get_treasury':
                del reqs[i]
                break
        if not reqs:
            pending_requests.pop(client_sid, None)
        else:
            pending_requests[client_sid] = reqs
    # Relay result to client
    socketio.emit('treasury_data', data, room=client_sid)

# --- FOURNISSEURS RELAY ---
@socketio.on('get_fournisseurs')
def handle_get_fournisseurs(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('fournisseurs_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('fournisseurs_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    pending_requests[client_sid].append({'type': 'fournisseurs', 'store_code': store_code})
    socketio.emit('get_fournisseurs', data, room=store_sid)

@socketio.on('fournisseurs_data')
def handle_fournisseurs_data(data):
    for client_sid, reqs in list(pending_requests.items()):
        for i, req in enumerate(reqs):
            if req['type'] == 'fournisseurs':
                socketio.emit('fournisseurs_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

# --- FACTURES ACHAT RELAY ---
@socketio.on('get_factures_achat')
def handle_get_factures_achat(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('factures_achat_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('factures_achat_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    pending_requests[client_sid].append({'type': 'factures_achat', 'store_code': store_code})
    socketio.emit('get_factures_achat', data, room=store_sid)

@socketio.on('factures_achat_data')
def handle_factures_achat_data(data):
    for client_sid, reqs in list(pending_requests.items()):
        for i, req in enumerate(reqs):
            if req['type'] == 'factures_achat':
                socketio.emit('factures_achat_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

# --- FACTURE ACHAT DETAILS RELAY ---
@socketio.on('get_facture_achat_details')
def handle_get_facture_achat_details(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        emit('facture_achat_details_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('facture_achat_details_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    pending_requests[client_sid].append({'type': 'facture_achat_details', 'store_code': store_code})
    socketio.emit('get_facture_achat_details', data, room=store_sid)

@socketio.on('facture_achat_details_data')
def handle_facture_achat_details_data(data):
    for client_sid, reqs in list(pending_requests.items()):
        for i, req in enumerate(reqs):
            if req['type'] == 'facture_achat_details':
                socketio.emit('facture_achat_details_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_factures_vente')
def handle_get_factures_vente(data):
    print(f"API Backend: get_factures_vente called with data: {data}")
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    if not store_code:
        print("API Backend: No store code found")
        emit('factures_vente_data', {'error': 'Not registered for a store'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        print("API Backend: Store backend not connected")
        emit('factures_vente_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'factures_vente', 'store_code': store_code})
    print(f'API Backend: pending_requests after get_factures_vente: {pending_requests}')
    print(f"API Backend: Emitting to store_sid: {store_sid}")
    socketio.emit('get_factures_vente', data, room=store_sid)

@socketio.on('factures_vente_data')
def handle_factures_vente_data(data):
    print(f'API Backend: factures_vente_data received: {data}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'factures_vente':
                print(f'API Backend: relaying factures_vente_data to client_sid={client_sid}')
                socketio.emit('factures_vente_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('get_facture_vente_details')
def handle_get_facture_vente_details(data):
    client_sid = request.sid
    store_code = client_sessions.get(client_sid)
    facture_id = data.get('facture_id')
    if not store_code or not facture_id:
        emit('facture_vente_details_data', {'error': 'Missing store or facture ID'})
        return
    store_sid = store_sessions.get(store_code)
    if not store_sid:
        emit('facture_vente_details_data', {'error': 'Store backend not connected'})
        return
    if client_sid not in pending_requests:
        pending_requests[client_sid] = []
    if isinstance(pending_requests[client_sid], dict):
        pending_requests[client_sid] = [pending_requests[client_sid]]
    pending_requests[client_sid].append({'type': 'facture_vente_details', 'store_code': store_code, 'facture_id': facture_id})
    print(f'API: pending_requests after get_facture_vente_details: {pending_requests}')
    socketio.emit('get_facture_vente_details', {'facture_id': facture_id}, room=store_sid)

@socketio.on('facture_vente_details_data')
def handle_facture_vente_details_data(data):
    print(f'API: facture_vente_details_data received, pending_requests={pending_requests}')
    for client_sid, reqs in list(pending_requests.items()):
        if not isinstance(reqs, list):
            print(f"WARNING: pending_requests[{client_sid}] is not a list, skipping: {reqs}")
            continue
        for i, req in enumerate(reqs):
            if req['type'] == 'facture_vente_details':
                print(f'API: relaying facture_vente_details_data to client_sid={client_sid}')
                socketio.emit('facture_vente_details_data', data, room=client_sid)
                del pending_requests[client_sid][i]
                if not pending_requests[client_sid]:
                    del pending_requests[client_sid]
                return

@socketio.on('disconnect')
def handle_disconnect():
    # Remove from sessions
    sid = request.sid
    for store_code, store_sid in list(store_sessions.items()):
        if store_sid == sid:
            # Keep store mapping around briefly to allow quick reconnects without flapping
            try:
                del store_sessions[store_code]
                # Remove activity tracking
                if store_code in store_last_activity:
                    del store_last_activity[store_code]
                # Update store status to OFFLINE in database
                try:
                    conn = get_api_db_connection()
                    with conn.cursor() as cursor:
                        sql = "UPDATE stores SET status='OFFLINE' WHERE store_code=%s"
                        cursor.execute(sql, (store_code,))
                        conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"Error updating store status to OFFLINE: {e}")
            except Exception as e:
                print(f"Error removing store session for {store_code}: {e}")
            print(f"Store disconnected: {store_code}")
    if sid in client_sessions:
        print(f"Client disconnected from store: {client_sessions[sid]}")
        del client_sessions[sid]
    # Clean up pending logins
    if sid in pending_logins:
        del pending_logins[sid]
    # Clean up pending stock requests
    if sid in pending_requests:
        del pending_requests[sid]

@socketio.on('test_event')
def handle_test_event(data):
    print('Received test_event:', data)
    emit('test_response', {'message': 'Test event received', 'data': data})

@socketio.on('heartbeat')
def handle_heartbeat(data):
    """Handle heartbeat from backend to update activity tracking"""
    # First try to get store_code from data
    store_code = data.get('store_code') if data else None
    
    # If not in data, find it from session ID (like other handlers do)
    if not store_code:
        sid = request.sid
        for code, store_sid in store_sessions.items():
            if store_sid == sid:
                store_code = code
                break
    
    if store_code:
        update_store_activity(store_code)
        print(f"[HEARTBEAT] Received heartbeat from store {store_code}, updated activity")
    else:
        print(f"[HEARTBEAT] WARNING: Received heartbeat but could not identify store (sid={request.sid})")

# Track last activity time for each store (any event from backend)
store_last_activity = {}  # store_code: timestamp

def update_store_activity(store_code):
    """Update last activity timestamp for a store"""
    if store_code:
        old_time = store_last_activity.get(store_code, 0)
        new_time = time.time()
        store_last_activity[store_code] = new_time
        if old_time > 0:
            elapsed = new_time - old_time
            if elapsed > 60:  # Only log if it's been a while (to reduce log spam)
                print(f"[ACTIVITY] Updated activity for {store_code} (was {elapsed:.1f}s since last update)")

def check_store_connections():
    """Periodically check if store sessions are still alive and update database status"""
    while True:
        gevent.sleep(10)  # Check every 10 seconds (less frequent since we check connection state)
        try:
            current_time = time.time()
            stores_to_check = list(store_sessions.keys())
            
            for store_code in stores_to_check:
                store_sid = store_sessions.get(store_code)
                if store_sid:
                    try:
                        # Check if the socket session exists in Socket.IO
                        namespace = socketio.server.namespace_handlers.get('/')
                        if namespace:
                            # Try to get the socket - if it doesn't exist, connection is dead
                            socket_exists = False
                            try:
                                
                                socket_exists = True  # Assume exists for now, check timeout instead
                            except:
                                socket_exists = False
                    except Exception as e:
                        print(f"Error checking socket state for {store_code}: {e}")
                        socket_exists = False
                    
                    # Check activity timeout as fallback
                    # ping_interval=25s, ping_timeout=90s
                    # Use timeout of 120 seconds (ping_interval + ping_timeout + buffer)
                    # This ensures we don't mark idle but connected stores as offline
                    last_activity = store_last_activity.get(store_code, 0)
                    if last_activity == 0:
                        # Store just registered, initialize with current time
                        store_last_activity[store_code] = current_time
                        continue
                    
                    time_since_activity = current_time - last_activity
                    
                    # Use 90 seconds timeout to account for:
                    # - Heartbeat every 5 seconds (so ~18 heartbeats per timeout period)
                    # - Network delays and temporary issues
                    # - Allows for multiple missed heartbeats while still detecting dead backends quickly
                    if time_since_activity > 90:
                        print(f"Store {store_code} has no activity in {time_since_activity:.1f} seconds (timeout=90s), marking as offline")
                        print(f"  Last activity was at: {time.time() - last_activity:.1f} seconds ago")
                        print(f"  Current time: {current_time}")
                        print(f"  Store sessions: {list(store_sessions.keys())}")
                        print(f"  Store activity times: {store_last_activity}")
                        # Remove from sessions
                        if store_code in store_sessions:
                            del store_sessions[store_code]
                        if store_code in store_last_activity:
                            del store_last_activity[store_code]
                        # Update database status to OFFLINE
                        try:
                            conn = get_api_db_connection()
                            with conn.cursor() as cursor:
                                sql = "UPDATE stores SET status='OFFLINE' WHERE store_code=%s"
                                cursor.execute(sql, (store_code,))
                                conn.commit()
                            conn.close()
                            print(f"Updated store {store_code} status to OFFLINE (timeout)")
                        except Exception as db_error:
                            print(f"Error updating store status to OFFLINE: {db_error}")
        except Exception as e:
            print(f"Error in store connection check: {e}")

# Start background task to check store connections
gevent.spawn(check_store_connections)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000) 

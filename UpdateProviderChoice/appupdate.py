import sqlite3
import requests
import json
from flask import Flask, request, jsonify, session, send_from_directory
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.')
app.secret_key = 'wifi_secret_key_dynamic_provider'

# --- KONFIGURASI PAYMENT GATEWAY (AutoGoPay) ---
AUTOGOPAY_API_KEY = "agp_0a5754ac03e58a0f5d06cda048a2fb8cd469dded723582512234a6661db682cd"
AUTOGOPAY_BASE_URL = "https://v1-gateway.autogopay.site"

HEADERS_PG = {
    "Authorization": f"Bearer {AUTOGOPAY_API_KEY}",
    "Content-Type": "application/json"
}

ADMIN_WA = "082292615651"

# Konfigurasi Paket Tetap (Durasi)
PACKAGES = {
    "6 Jam": {"minutes": 360, "default_price": 3000},
    "12 Jam": {"minutes": 720, "default_price": 10000},
    "24 Jam": {"minutes": 1440, "default_price": 20000},
    "7 Hari": {"minutes": 10080, "default_price": 50000},
    "1 Bulan": {"minutes": 43200, "default_price": 100000}
}

def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        whatsapp TEXT UNIQUE NOT NULL,
        role TEXT DEFAULT 'member',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Tabel Provider Dinamis
    c.execute('''CREATE TABLE IF NOT EXISTS providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        provider_name TEXT NOT NULL,
        duration_label TEXT NOT NULL, 
        duration_minutes INTEGER NOT NULL,
        price INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        voucher_code TEXT NOT NULL,
        provider_name TEXT NOT NULL,
        duration_label TEXT NOT NULL,
        price INTEGER NOT NULL,
        bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    c.execute("SELECT * FROM users WHERE whatsapp = ?", (ADMIN_WA,))
    if not c.fetchone():
        admin_pass = generate_password_hash("gK7pQ2zX9")
        c.execute("INSERT INTO users (username, password_hash, whatsapp, role) VALUES (?, ?, ?, ?)",
                  ("Admin", admin_pass, ADMIN_WA, "admin"))

    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

# --- AUTH ROUTES ---

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    whatsapp = data.get('whatsapp')
    
    if not all([username, password, whatsapp]):
        return jsonify({'success': False, 'message': 'Data tidak lengkap'}), 400
        
    conn = get_db()
    try:
        pwd_hash = generate_password_hash(password)
        conn.execute("INSERT INTO users (username, password_hash, whatsapp) VALUES (?, ?, ?)",
                     (username, pwd_hash, whatsapp))
        conn.commit()
        return jsonify({'success': True, 'message': 'Registrasi berhasil.'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Username/WA sudah ada'}), 409
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    
    if user and check_password_hash(user['password_hash'], password):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        return jsonify({
            'success': True, 
            'role': user['role'],
            'username': user['username']
        })
    else:
        return jsonify({'success': False, 'message': 'Login gagal'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/me', methods=['GET'])
def me():
    if 'user_id' in session:
        return jsonify({
            'logged_in': True,
            'username': session['username'],
            'role': session['role']
        })
    return jsonify({'logged_in': False}), 401

# --- ADMIN PROVIDER MANAGEMENT ---

@app.route('/api/admin/providers', methods=['GET'])
def get_providers_admin():
    if session.get('role') != 'admin': return jsonify([]), 403
    conn = get_db()
    rows = conn.execute("SELECT * FROM providers ORDER BY name ASC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/admin/providers/add', methods=['POST'])
def add_provider():
    if session.get('role') != 'admin': return jsonify({'success': False}), 403
    data = request.json
    name = data.get('name')
    if not name: return jsonify({'success': False, 'message': 'Nama provider wajib diisi'}), 400
    
    try:
        conn = get_db()
        conn.execute("INSERT INTO providers (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Provider sudah ada'}), 409

@app.route('/api/admin/providers/delete', methods=['POST'])
def delete_provider():
    if session.get('role') != 'admin': return jsonify({'success': False}), 403
    data = request.json
    name = data.get('name')
    
    conn = get_db()
    # Cek apakah masih ada stok voucher untuk provider ini
    count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE provider_name = ?", (name,)).fetchone()[0]
    if count > 0:
        conn.close()
        return jsonify({'success': False, 'message': 'Tidak bisa menghapus provider yang masih memiliki stok voucher'}), 400
        
    conn.execute("DELETE FROM providers WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# --- ADMIN VOUCHER & STATS ---

@app.route('/api/admin/add-voucher', methods=['POST'])
def add_voucher():
    if session.get('role') != 'admin': return jsonify({'success': False}), 403
    
    data = request.json
    code = data.get('code')
    provider = data.get('provider')
    label = data.get('label')
    minutes = int(data.get('minutes'))
    price = int(data.get('price'))
    
    if not provider:
        return jsonify({'success': False, 'message': 'Provider tidak valid'}), 400

    try:
        conn = get_db()
        conn.execute("INSERT INTO vouchers (code, provider_name, duration_label, duration_minutes, price) VALUES (?, ?, ?, ?, ?)",
                     (code, provider, label, minutes, price))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Kode sudah ada'}), 409

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if session.get('role') != 'admin': return jsonify([]), 403
    conn = get_db()
    
    # Ambil semua provider
    providers = conn.execute("SELECT name FROM providers ORDER BY name ASC").fetchall()
    stats = {}
    
    for p in providers:
        prov_name = p['name']
        prov_stats = {}
        for lbl in PACKAGES.keys():
            count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE provider_name = ? AND duration_label = ?", (prov_name, lbl)).fetchone()[0]
            prov_stats[lbl] = count
        stats[prov_name] = prov_stats
        
    conn.close()
    return jsonify(stats)

@app.route('/api/admin/transactions', methods=['GET'])
def admin_transactions():
    if session.get('role') != 'admin': return jsonify([]), 403
    
    conn = get_db()
    rows = conn.execute("""
        SELECT t.voucher_code, t.provider_name, t.duration_label, t.price, t.bought_at, 
               u.username, u.whatsapp
        FROM transactions t
        JOIN users u ON t.user_id = u.id
        ORDER BY t.bought_at DESC
    """).fetchall()
    conn.close()
    
    result = []
    for row in rows:
        result.append({
            'voucher_code': row['voucher_code'],
            'provider': row['provider_name'],
            'package': row['duration_label'],
            'price': row['price'],
            'bought_at': row['bought_at'],
            'username': row['username'],
            'whatsapp': row['whatsapp']
        })
    return jsonify(result)

# --- MEMBER & PAYMENT ROUTES ---

@app.route('/api/member/providers', methods=['GET'])
def get_providers_member():
    """Mengambil daftar provider yang memiliki stok"""
    if 'user_id' not in session: return jsonify([]), 401
    conn = get_db()
    
    # Join providers dengan vouchers untuk memastikan hanya provider yg punya stok muncul
    rows = conn.execute("""
        SELECT DISTINCT v.provider_name 
        FROM vouchers v
        JOIN providers p ON v.provider_name = p.name
        ORDER BY p.name ASC
    """).fetchall()
    
    available_providers = [row['provider_name'] for row in rows]
    conn.close()
    return jsonify(available_providers)

@app.route('/api/member/packages', methods=['POST'])
def get_packages_by_provider():
    """Mengambil paket yang tersedia untuk Provider tertentu"""
    if 'user_id' not in session: return jsonify({}), 401
    
    data = request.json
    provider = data.get('provider')
    
    if not provider: return jsonify({}), 400

    conn = get_db()
    packages_info = {}
    for label, config in PACKAGES.items():
        count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE provider_name = ? AND duration_label = ?", (provider, label)).fetchone()[0]
        price_row = conn.execute("SELECT price FROM vouchers WHERE provider_name = ? AND duration_label = ? LIMIT 1", (provider, label)).fetchone()
        price = price_row['price'] if price_row else config['default_price']
        packages_info[label] = {"available": count > 0, "price": price, "minutes": config['minutes']}
    conn.close()
    return jsonify(packages_info)

@app.route('/api/member/buy', methods=['POST'])
def buy_voucher():
    """Langkah 1: Generate QRIS dengan Filter Provider"""
    if 'user_id' not in session:
        return jsonify({'success': False}), 401
        
    data = request.json
    label = data.get('label')
    provider = data.get('provider')
    
    if label not in PACKAGES or not provider:
        return jsonify({'success': False}), 400
    
    conn = get_db()
    
    # Cek Stok Spesifik Provider
    count_row = conn.execute("SELECT COUNT(*) as cnt FROM vouchers WHERE provider_name = ? AND duration_label = ?", (provider, label)).fetchone()
    if count_row['cnt'] < 1:
        conn.close()
        return jsonify({'success': False, 'message': 'STOK_HABIS'}), 404

    # Ambil Voucher Spesifik Provider
    v = conn.execute("SELECT * FROM vouchers WHERE provider_name = ? AND duration_label = ? LIMIT 1", (provider, label)).fetchone()
    
    if not v:
        conn.close()
        return jsonify({'success': False, 'message': 'STOK_HABIS'}), 404
        
    price_sold = v['price']
    code_to_sell = v['code']
    voucher_id = v['id']
    conn.close()

    expiry_time = (datetime.now() + timedelta(minutes=15)).isoformat()
    ref_id = f"TRX-{session['user_id']}-{int(datetime.now().timestamp())}"

    qr_url = ""
    tx_id = ""

    # --- STRICT API CALL ---
    try:
        payload_pg = {
            "amount": price_sold,
            "description": f"Voucher {provider} {label}",
            "reference_id": ref_id
        }
        
        response = requests.post(f"{AUTOGOPAY_BASE_URL}/qris/generate", headers=HEADERS_PG, json=payload_pg, timeout=10)
        
        if response.status_code != 200:
            raise Exception(f"API Error: {response.status_code}")
            
        pg_data = response.json()
        data_content = pg_data.get('data', pg_data) 
        
        qr_url = data_content.get('qr_url') or data_content.get('qr_image') or data_content.get('checkout_url')
        qr_string = data_content.get('qr_string') or data_content.get('contents') 
        tx_id = data_content.get('transaction_id') or data_content.get('id')
        
        if qr_string and not qr_url:
            import urllib.parse
            encoded_qr = urllib.parse.quote(qr_string)
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded_qr}"
        
        if not qr_url or not tx_id:
            raise Exception("Invalid Response from Payment Gateway")

    except Exception as e:
        print(f"Payment Gateway Error: {e}")
        return jsonify({'success': False, 'message': 'Gagal menghubungkan ke Payment Gateway. Silakan coba lagi nanti.'}), 502

    # Simpan transaksi PENDING di Session
    session['pending_transaction'] = {
        'voucher_id': voucher_id,
        'voucher_code': code_to_sell,
        'provider': provider,
        'duration_label': label,
        'price': price_sold,
        'pg_transaction_id': tx_id,
        'expiry_time': expiry_time
    }

    return jsonify({
        'success': True,
        'qris_url': qr_url,
        'checkout_url': qr_url,
        'amount': price_sold,
        'expiry_time': expiry_time,
        'transaction_id': tx_id
    })

@app.route('/api/member/check-payment', methods=['POST'])
def check_payment():
    """Langkah 2: Polling Status Pembayaran"""
    if 'user_id' not in session or 'pending_transaction' not in session:
        return jsonify({'status': 'invalid'}), 400

    pending_tx = session['pending_transaction']
    pg_tx_id = pending_tx['pg_transaction_id']
    voucher_id = pending_tx['voucher_id']
    voucher_code = pending_tx['voucher_code']

    status = "pending" 
    
    try:
        payload_check = {"transaction_id": pg_tx_id}
        resp = requests.post(f"{AUTOGOPAY_BASE_URL}/qris/status", headers=HEADERS_PG, json=payload_check, timeout=5)
        resp_data = resp.json().get('data', {})
        status = resp_data.get('transaction_status', 'pending')
    except Exception as e:
        print(f"Check Status Error: {e}")
        status = "pending"

    if status == 'settlement' or status == 'success':
        conn = get_db()
        try:
            v_check = conn.execute("SELECT id FROM vouchers WHERE id = ?", (voucher_id,)).fetchone()
            
            if not v_check:
                session.pop('pending_transaction', None)
                return jsonify({'status': 'failed_stolen'})
            
            conn.execute("DELETE FROM vouchers WHERE id = ?", (voucher_id,))
            
            conn.execute("""
                INSERT INTO transactions (user_id, voucher_code, provider_name, duration_label, price) 
                VALUES (?, ?, ?, ?, ?)
            """, (
                session['user_id'],
                voucher_code,
                pending_tx['provider'],
                pending_tx['duration_label'],
                pending_tx['price']
            ))
            conn.commit()
            
            result_code = voucher_code
            result_pkg = pending_tx['duration_label']
            result_prov = pending_tx['provider']
            session.pop('pending_transaction', None)
            
            return jsonify({
                'status': 'success',
                'voucher_code': result_code,
                'package': result_pkg,
                'provider': result_prov
            })
        except Exception as e:
            conn.rollback()
            print(f"DB Error on Settlement: {e}")
            return jsonify({'status': 'error'}), 500
        finally:
            conn.close()
            
    elif status in ['expire', 'cancel', 'failed']:
        session.pop('pending_transaction', None)
        return jsonify({'status': status})
    else:
        return jsonify({'status': 'pending'})

@app.route('/api/member/history', methods=['GET'])
def member_history():
    if 'user_id' not in session: return jsonify([]), 401
    
    conn = get_db()
    rows = conn.execute("""
        SELECT voucher_code, provider_name, duration_label, price, bought_at 
        FROM transactions 
        WHERE user_id = ? 
        ORDER BY bought_at DESC
    """, (session['user_id'],)).fetchall()
    conn.close()
    
    history = []
    for row in rows:
        bought_dt = datetime.strptime(row['bought_at'], '%Y-%m-%d %H:%M:%S')
        mins = PACKAGES[row['duration_label']]['minutes']
        exp_dt = bought_dt + timedelta(minutes=mins)
        
        history.append({
            'code': row['voucher_code'],
            'provider': row['provider_name'],
            'package': row['duration_label'],
            'price': row['price'],
            'bought_at': row['bought_at'],
            'expired_at': exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify(history)

# --- TAMBAHKAN DI app.py (Setelah import dan inisialisasi app) ---
@app.route('/api/admin/vouchers', methods=['GET'])
def get_all_vouchers():
    if session.get('role') != 'admin':
        return jsonify([]), 403
        
    conn = get_db()
    rows = conn.execute("SELECT * FROM vouchers ORDER BY created_at DESC").fetchall()
    conn.close()
    
    # Konversi ke list of dict agar bisa di-serialize ke JSON
    vouchers = [dict(row) for row in rows]
    return jsonify(vouchers)

@app.route('/api/admin/vouchers/<int:voucher_id>', methods=['PUT'])
def update_voucher(voucher_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False}), 403
        
    data = request.json
    code = data.get('code')
    provider = data.get('provider_name')
    label = data.get('duration_label')
    minutes = int(data.get('duration_minutes'))
    price = int(data.get('price'))
    
    try:
        conn = get_db()
        # Cek apakah kode voucher baru sudah ada (dan bukan milik voucher ini sendiri)
        existing = conn.execute(
            "SELECT id FROM vouchers WHERE code = ? AND id != ?", 
            (code, voucher_id)
        ).fetchone()
        if existing:
            return jsonify({'success': False, 'message': 'Kode voucher sudah digunakan.'}), 409
            
        conn.execute(
            """UPDATE vouchers 
               SET code = ?, provider_name = ?, duration_label = ?, duration_minutes = ?, price = ?
               WHERE id = ?""",
            (code, provider, label, minutes, price, voucher_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error updating voucher: {e}")
        return jsonify({'success': False, 'message': 'Gagal memperbarui voucher.'}), 500

@app.route('/api/admin/vouchers/<int:voucher_id>', methods=['DELETE'])
def delete_voucher(voucher_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False}), 403
        
    try:
        conn = get_db()
        conn.execute("DELETE FROM vouchers WHERE id = ?", (voucher_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error deleting voucher: {e}")
        return jsonify({'success': False, 'message': 'Gagal menghapus voucher.'}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

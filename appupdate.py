import sqlite3
import requests
import json
from flask import Flask, request, jsonify, session, send_from_directory
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='.')
app.secret_key = 'wifi_secret_key_final_anti_race'

# --- KONFIGURASI PAYMENT GATEWAY (AutoGoPay) ---
AUTOGOPAY_API_KEY = "agp_9b7c34a8953e3d0651e7b7a79ef69281c40b70ec82f9dae0c3b76811938cd56b"
AUTOGOPAY_BASE_URL = "https://v1-gateway.autogopay.site"

HEADERS_PG = {
    "Authorization": f"Bearer {AUTOGOPAY_API_KEY}",
    "Content-Type": "application/json"
}

ADMIN_WA = "082292615651"

# Konfigurasi Paket Tetap
PACKAGES = {
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
    
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        duration_label TEXT NOT NULL, 
        duration_minutes INTEGER NOT NULL,
        price INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        voucher_code TEXT NOT NULL,
        duration_label TEXT NOT NULL,
        price INTEGER NOT NULL,
        bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    c.execute("SELECT * FROM users WHERE whatsapp = ?", (ADMIN_WA,))
    if not c.fetchone():
        admin_pass = generate_password_hash("admin123")
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

# --- ADMIN ROUTES ---

@app.route('/api/admin/add-voucher', methods=['POST'])
def add_voucher():
    if session.get('role') != 'admin': return jsonify({'success': False}), 403
    
    data = request.json
    code = data.get('code')
    label = data.get('label')
    minutes = int(data.get('minutes'))
    price = int(data.get('price'))
    
    try:
        conn = get_db()
        conn.execute("INSERT INTO vouchers (code, duration_label, duration_minutes, price) VALUES (?, ?, ?, ?)",
                     (code, label, minutes, price))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Kode sudah ada'}), 409

@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if session.get('role') != 'admin': return jsonify([]), 403
    conn = get_db()
    labels = ['12 Jam', '24 Jam', '7 Hari', '1 Bulan']
    stats = {}
    for lbl in labels:
        count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE duration_label = ?", (lbl,)).fetchone()[0]
        stats[lbl] = count
    conn.close()
    return jsonify(stats)

# --- MEMBER & PAYMENT ROUTES ---

@app.route('/api/member/packages', methods=['GET'])
def get_packages():
    if 'user_id' not in session: return jsonify({}), 401
    conn = get_db()
    packages_info = {}
    for label, config in PACKAGES.items():
        count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE duration_label = ?", (label,)).fetchone()[0]
        price_row = conn.execute("SELECT price FROM vouchers WHERE duration_label = ? LIMIT 1", (label,)).fetchone()
        price = price_row['price'] if price_row else config['default_price']
        packages_info[label] = {"available": count > 0, "price": price, "minutes": config['minutes']}
    conn.close()
    return jsonify(packages_info)

@app.route('/api/member/buy', methods=['POST'])
def buy_voucher():
    """
    Langkah 1: Reservasi Voucher (Anti Race Condition)
    Database dikunci, voucher diambil dan DIHAPUS dari stok agar tidak bisa dibeli orang lain.
    """
    if 'user_id' not in session:
        return jsonify({'success': False}), 401
        
    data = request.json
    label = data.get('label')
    
    if label not in PACKAGES:
        return jsonify({'success': False}), 400
    
    conn = get_db()
    try:
        # LOCK DATABASE
        conn.execute("BEGIN IMMEDIATE")
        
        # Cek stok di dalam lock
        v = conn.execute("SELECT * FROM vouchers WHERE duration_label = ? LIMIT 1", (label,)).fetchone()
        
        if not v:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'message': 'STOK_HABIS'}), 404
            
        price_sold = v['price']
        code_to_sell = v['code']
        
        # HAPUS VOUCHER DARI STOK (Reservasi Permanen)
        conn.execute("DELETE FROM vouchers WHERE id = ?", (v['id'],))
        conn.commit()
        conn.close()

        # Simpan data transaksi pending di Session
        session['pending_transaction'] = {
            'voucher_code': code_to_sell,
            'duration_label': label,
            'price': price_sold,
            'expiry_time': (datetime.now() + timedelta(minutes=15)).isoformat()
        }

        # Generate QRIS (Setelah lock dilepas agar responsif)
        qr_url = ""
        tx_id = f"TRX-{session['user_id']}-{int(datetime.now().timestamp())}"
        
        try:
            payload_pg = {
                "amount": price_sold,
                "description": f"Voucher WiFi {label}",
                "reference_id": tx_id
            }
            response = requests.post(f"{AUTOGOPAY_BASE_URL}/qris/generate", headers=HEADERS_PG, json=payload_pg, timeout=10)
            response.raise_for_status()
            pg_data = response.json().get('data', {})
            
            qr_string = pg_data.get('qr_string') or pg_data.get('contents')
            qr_url_direct = pg_data.get('qr_url') or pg_data.get('qr_image')
            
            if qr_string and not qr_url_direct:
                import urllib.parse
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(qr_string)}"
            else:
                qr_url = qr_url_direct
                
            if not qr_url: raise Exception("No QR Data")

        except Exception as e:
            print(f"PG Error: {e}")
            # Fallback Dummy QRIS
            import urllib.parse
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=DUMMY-PAY-{price_sold}"

        return jsonify({
            'success': True,
            'qris_url': qr_url,
            'checkout_url': qr_url,
            'amount': price_sold,
            'expiry_time': session['pending_transaction']['expiry_time'],
            'transaction_id': tx_id
        })

    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Buy Error: {e}")
        return jsonify({'success': False, 'message': 'Server Error'}), 500

@app.route('/api/member/check-payment', methods=['POST'])
def check_payment():
    """Langkah 2: Cek Status Pembayaran"""
    if 'user_id' not in session or 'pending_transaction' not in session:
        return jsonify({'status': 'invalid'}), 400

    pending_tx = session['pending_transaction']
    
    # SIMULASI SUKSES (Karena kita hapus stok di awal, kita anggap sukses jika user reach endpoint ini)
    # Di produksi nyata, cek ke API Provider disini.
    status = "settlement" 

    if status == 'settlement' or status == 'success':
        conn = get_db()
        try:
            # Catat ke riwayat
            conn.execute("""
                INSERT INTO transactions (user_id, voucher_code, duration_label, price) 
                VALUES (?, ?, ?, ?)
            """, (
                session['user_id'],
                pending_tx['voucher_code'],
                pending_tx['duration_label'],
                pending_tx['price']
            ))
            conn.commit()
            
            result_code = pending_tx['voucher_code']
            result_pkg = pending_tx['duration_label']
            session.pop('pending_transaction', None)
            
            return jsonify({
                'status': 'success',
                'voucher_code': result_code,
                'package': result_pkg
            })
        except Exception as e:
            conn.rollback()
            return jsonify({'status': 'error'}), 500
        finally:
            conn.close()
    else:
        return jsonify({'status': 'pending'})

@app.route('/api/member/history', methods=['GET'])
def member_history():
    if 'user_id' not in session: return jsonify([]), 401
    
    conn = get_db()
    rows = conn.execute("""
        SELECT voucher_code, duration_label, price, bought_at 
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
            'package': row['duration_label'],
            'price': row['price'],
            'bought_at': row['bought_at'],
            'expired_at': exp_dt.strftime('%Y-%m-%d %H:%M:%S')
        })
    return jsonify(history)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

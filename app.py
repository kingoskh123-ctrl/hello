import time
import json
import websocket
import os
import sys
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, g
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock

# ==========================================================
# BOT CONSTANT SETTINGS (R_100 | x2.1 | 1 Tick | Digit Diff Strategy)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"               
DURATION = 1                   
DURATION_UNIT = "t"            # ⬅️ المدة 1 تيك (Tick)
CONTRACT_TYPE_BASE = "DIGITDIFF" # ⬅️ نوع العقد: الفرق الرقمي (الرقم الأخير ليس 1 أو 8)

# إعدادات المضاعفة
MARTINGALE_STEPS = 5           
MAX_CONSECUTIVE_LOSSES = 6     
MARTINGALE_MULTIPLIER = 2.1    
TARGET_DIGIT = 1               # ⬅️ الرقم المستهدف للصفقة الأساسية
REVERSAL_DIGIT = 8             # ⬅️ الرقم المستهدف لصفقة المضاعفة (التعويض)

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# ==========================================================

# ==========================================================
# GLOBAL STATE AND UTILITIES
# ==========================================================
active_processes = {}
is_contract_open = {} 
PROCESS_LOCK = Lock()
TRADE_LOCK = Lock() 

DEFAULT_SESSION_STATE = {
    "api_token": "",
    "base_stake": 1.0,
    "tp_target": 10.0,
    "is_running": False,
    "current_profit": 0.0,
    "current_stake": 1.0,      # الستيك الحالي للصفقة
    "consecutive_losses": 0,
    "current_step": 0,
    "total_wins": 0,
    "total_losses": 0,
    "stop_reason": "Stopped Manually",
    "last_tick_data": None,
    "currency": "USD", 
    "account_type": "demo",
    
    "last_entry_time": 0,      # وقت آخر دخول
    "last_entry_price": 0.0,
}

# --- Persistence functions (Loading and Saving Session Data) ---
def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE): return {}
    try:
        with open(ACTIVE_SESSIONS_FILE, 'r') as f:
            content = f.read()
            return json.loads(content) if content else {}
    except: return {}

def save_session_data(email, session_data):
    all_sessions = load_persistent_sessions()
    all_sessions[email] = session_data
    with open(ACTIVE_SESSIONS_FILE, 'w') as f:
        try: json.dump(all_sessions, f, indent=4)
        except: pass

def get_session_data(email):
    all_sessions = load_persistent_sessions()
    if email in all_sessions:
        data = all_sessions[email]
        # تحديث البيانات الافتراضية إذا كانت مفقودة
        for key, default_val in DEFAULT_SESSION_STATE.items():
             if key not in data: data[key] = default_val
        return data
    return DEFAULT_SESSION_STATE.copy()

def delete_session_data(email):
    all_sessions = load_persistent_sessions()
    if email in all_sessions: del all_sessions[email]
    with open(ACTIVE_SESSIONS_FILE, 'w') as f:
        try: json.dump(all_sessions, f, indent=4)
        except: pass

def load_allowed_users():
    if not os.path.exists(USER_IDS_FILE): return set()
    try:
        with open(USER_IDS_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except: return set()
        
def stop_bot(email, clear_data=True, stop_reason="Stopped Manually"):
    global is_contract_open, active_processes
    current_data = get_session_data(email)
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
        save_session_data(email, current_data)

    with PROCESS_LOCK:
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                print(f"🛑 [INFO] Terminating Process for {email}...")
                process.terminate() 
            del active_processes[email]
    
    if email in is_contract_open: is_contract_open[email] = False
    
    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        else:
            delete_session_data(email)
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    else:
        print(f"⚠ [INFO] Process disconnected for {email}. Data kept for retry.")
# --- End of Utilities ---

# ==========================================================
# TRADING BOT CORE LOGIC
# ==========================================================

def calculate_martingale_stake(base_stake, current_step, multiplier):
    """ منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة لعدد الخطوات """
    if current_step == 0: 
        return base_stake
    
    return base_stake * (multiplier ** current_step)


def execute_single_transaction(email, token, stake, digit, contract_type, currency):
    """
    يتصل، يشتري، ينتظر البيع، ويغلق الاتصال.
    """
    global WSS_URL, SYMBOL, DURATION, DURATION_UNIT
    
    ws = None
    try:
        ws = websocket.create_connection(WSS_URL)
        
        # 1. Authorization
        auth_request = {"authorize": token}
        ws.send(json.dumps(auth_request))
        auth_response = json.loads(ws.recv())
        
        if 'error' in auth_response:
            print(f"❌ [AUTH ERROR] Failed to authorize: {auth_response['error']['message']}")
            stop_bot(email, clear_data=True, stop_reason=f"API Auth Error: {auth_response['error']['message']}")
            return 0.0
            
        # 2. Subscribe to Ticks (to get the latest price for entry)
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        # Wait for first tick response to ensure connection is ready
        while True:
            tick_response = json.loads(ws.recv())
            if tick_response.get('msg_type') == 'tick':
                print(f"✅ [TICK] Price received: {tick_response['tick']['quote']}")
                break
        
        # 3. Send Buy order 
        # 💡 تم التأكد من استخدام "digit" وتقريب الـ "stake"
        trade_request = {
            "buy": 1,
            "price": round(stake, 2),
            "parameters": {
                "amount": round(stake, 2),
                "basis": "stake",
                "contract_type": contract_type, 
                "currency": currency, 
                "duration": DURATION, 
                "duration_unit": DURATION_UNIT, 
                "symbol": SYMBOL,
                "digit": digit # ⬅️ المعامل الصحيح لاستراتيجية DIGITDIFF
            }
        }
        ws.send(json.dumps(trade_request))
        print(f"➡️ [BUY SENT] Stake: {round(stake, 2):.2f}, Digit: {digit}")

        # 4. Wait for 'buy' response and subscribe to open contract
        buy_response = json.loads(ws.recv())
        if 'error' in buy_response:
            error_msg = buy_response['error']['message']
            print(f"❌ [BUY ERROR] Failed: {error_msg}")
            
            # ⚠️ إذا فشل الشراء، يعتبر خسارة لغرض المضاعفة
            stop_bot(email, clear_data=False, stop_reason=f"API Buy Error: {error_msg}")
            return -stake # نعتبرها خسارة كاملة للرهان

        contract_id = buy_response['buy']['contract_id']
        print(f"✅ [BOUGHT] Contract ID: {contract_id}")

        # Subscribe to contract closure
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        
        profit_loss = 0.0
        
        # 5. Wait for contract settlement
        print(f"⌛ [WAITING] Waiting for contract {contract_id} to close...")
        while True:
            message = json.loads(ws.recv())
            if message.get('msg_type') == 'proposal_open_contract':
                contract = message['proposal_open_contract']
                if contract.get('is_sold') == 1:
                    profit_loss = contract.get('profit', 0.0)
                    print(f"🎉 [SOLD] Result PnL: {profit_loss:.2f}")
                    break
            
            elif 'error' in message:
                print(f"❌ [CONTRACT ERROR] {message['error']['message']}")
                
                # إذا حدث خطأ أثناء تتبع العقد، اعتبره خسارة لتجنب التعليق
                if profit_loss == 0.0:
                    profit_loss = -stake
                break
                
        return profit_loss

    except websocket.WebSocketTimeoutException:
        print(f"❌ [WS TIMEOUT] WebSocket operation timed out.")
        return 0.0 
    except Exception as e:
        print(f"❌ [CRITICAL ERROR] Error during transaction: {e}")
        return 0.0 
    finally:
        if ws:
            try: ws.close()
            except: pass


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ المنطق الأساسي للبوت: تحديد الستيك والرقم المستهدف وتنفيذ الصفقة """
    
    global MARTINGALE_STEPS, MARTINGALE_MULTIPLIER, TARGET_DIGIT, REVERSAL_DIGIT, CONTRACT_TYPE_BASE
    
    # 1. Initialize/Update Session Data
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, 
        "base_stake": stake, 
        "tp_target": tp,
        "is_running": True, 
        "current_stake": stake, 
        "stop_reason": "Running",
        "currency": currency,
        "account_type": account_type,
    })
    save_session_data(email, session_data)

    print(f"🚀 [BOT START] Running with Base Stake: {stake:.2f}, TP: {tp:.2f}. Step: {session_data['current_step']}")

    while session_data.get('is_running'):
        
        current_data = get_session_data(email)
        if not current_data.get('is_running'): break
        
        # 2. Determine Trade Parameters (Stake and Digit)
        if current_data['current_step'] == 0:
            trade_stake = current_data['base_stake']
            trade_digit = TARGET_DIGIT
            print(f"\n🧠 [NEW CYCLE] Base Stake {trade_stake:.2f}, Target Digit {trade_digit}")
        else:
            trade_stake = calculate_martingale_stake(current_data['base_stake'], current_data['current_step'], MARTINGALE_MULTIPLIER)
            trade_digit = REVERSAL_DIGIT
            print(f"\n🧠 [MARTINGALE] Step {current_data['current_step']}. New Stake {trade_stake:.2f}, Target Digit {trade_digit}")
        
        # 3. Execute Transaction
        profit_loss = execute_single_transaction(email, token, trade_stake, trade_digit, CONTRACT_TYPE_BASE, currency)
        
        # 4. Update Session Data and Apply Martingale Logic
        current_data = get_session_data(email) # إعادة تحميل البيانات للتأكد من أحدث حالة
        
        if not current_data.get('is_running'): break # قد يكون تم الإيقاف بسبب خطأ API

        current_data['current_profit'] += profit_loss
        
        # 🏆 TP Check
        if current_data['current_profit'] >= current_data['tp_target']:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason="TP Reached")
            break
            
        # ❌ Loss Condition 
        if profit_loss < 0:
            current_data['total_losses'] += 1
            current_data['consecutive_losses'] += 1
            current_data['current_step'] += 1
            
            # 🛑 SL Check (Max Steps)
            if current_data['current_step'] > MARTINGALE_STEPS: 
                save_session_data(email, current_data)
                stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Exceeded {MARTINGALE_STEPS} Martingale steps.")
                break
                
            # 🛑 SL Check (Max Consecutive Losses)
            if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
                save_session_data(email, current_data)
                stop_bot(email, clear_data=True, stop_reason=f"SL Reached: {MAX_CONSECUTIVE_LOSSES} consecutive losses.")
                break
                
            current_data['current_stake'] = trade_stake
            print(f"🔄 [LOSS] PnL: {profit_loss:.2f}. Next Step: {current_data['current_step']}")

        # ✅ Win or Draw Condition
        else:
            current_data['total_wins'] += 1 if profit_loss > 0 else 0
            current_data['current_step'] = 0 
            current_data['consecutive_losses'] = 0
            current_data['current_stake'] = current_data['base_stake']
            
            entry_result_tag = "WIN" if profit_loss > 0 else "DRAW"
            print(f"✅ [RESULT] {entry_result_tag}. PnL: {profit_loss:.2f}. Stake reset to base.")
            
        print(f"📈 [STATUS] Total PNL: {currency} {current_data['current_profit']:.2f}, Current Stake: {current_data['current_stake']:.2f}")

        save_session_data(email, current_data)
        
        # انتظار قصير قبل الصفقة التالية
        time.sleep(RECONNECT_DELAY)


    print(f"🛑 [BOT END] Bot core loop exited for {email}.")

# ==========================================================
# FLASK WEB APP SETUP AND ROUTES
# ==========================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False

# --- HTML Templates (UNCHANGED) ---

AUTH_FORM = """
<!doctype html>
<title>Login - Deriv Bot</title>
<style>
    body { font-family: Arial, sans-serif; padding: 20px; max-width: 400px; margin: auto; }
    h1 { color: #007bff; }
    input[type="email"] { width: 100%; padding: 10px; margin-top: 5px; margin-bottom: 15px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
    button { background-color: blue; color: white; padding: 10px 15px; border: none; border-radius: 5px; cursor: pointer; }
</style>
<h1>Deriv Bot Login</h1>
<p>Please enter your authorized email address:</p>
{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:red;">{{ message }}</p>
        {% endfor %}
    {% endif %}
{% endwith %}
<form method="POST" action="{{ url_for('login') }}">
    <label for="email">Email:</label><br>
    <input type="email" id="email" name="email" required><br><br>
    <button type="submit">Login</button>
</form>
"""

CONTROL_FORM = """
<!doctype html>
<title>Control Panel</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body {
        font-family: Arial, sans-serif;
        padding: 10px;
        max-width: 600px;
        margin: auto;
        direction: ltr;
        text-align: left;
    }
    h1 {
        color: #007bff;
        font-size: 1.8em;
        border-bottom: 2px solid #eee;
        padding-bottom: 10px;
    }
    .status-running {
        color: green;
        font-weight: bold;
        font-size: 1.3em;
    }
    .status-stopped {
        color: red;
        font-weight: bold;
        font-size: 1.3em;
    }
    input[type="text"], input[type="number"], select {
        width: 98%;
        padding: 10px;
        margin-top: 5px;
        margin-bottom: 10px;
        border: 1px solid #ccc;
        border-radius: 4px;
        box-sizing: border-box;
        text-align: left;
    }
    form button {
        padding: 12px 20px;
        border: none;
        border-radius: 5px;
        cursor: pointer;
        font-size: 1.1em;
        margin-top: 15px;
        width: 100%;
    }
</style>
<h1>Bot Control Panel | User: {{ email }}</h1>
<hr>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:{{ 'green' if category == 'success' else ('blue' if category == 'info' else 'red') }};">{{ message }}</p>
        {% endfor %}
        
        {% if session_data and session_data.stop_reason and session_data.stop_reason != "Running" %}
            <p style="color:red; font-weight:bold;">Last Reason: {{ session_data.stop_reason }}</p>
        {% endif %}
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set timing_logic = "Transactional Mode (Synchronous Entry)" %}
    {% set strategy = 'DIGITDIFF (R100 | Target Digit ' + target_digit|string + ' | Reversal Digit ' + reversal_digit|string + ')' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Loss: {{ max_consecutive_losses }})</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: red; color: white;">🛑 Stop Bot</button>
    </form>
{% else %}
    <p class="status-stopped">🛑 Bot is Stopped. Enter settings to start a new session.</p>
    <form method="POST" action="{{ url_for('start_bot') }}">

        <label for="account_type">Account Type:</label><br>
        <select id="account_type" name="account_type" required>
            <option value="demo" selected>Demo (USD)</option>
            <option value="live">Live (tUSDT)</option>
        </select><br>

        <label for="token">Deriv API Token:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}" {% if session_data and session_data.api_token and session_data.is_running is not none %}readonly{% endif %}><br>
        
        <label for="stake">Base Stake (USD/tUSDT):</label><br>
        <input type="number" id="stake" name="stake" value="{{ session_data.base_stake|round(2) if session_data else 0.35 }}" step="0.01" min="0.35" required><br>
        
        <label for="tp">TP Target (USD/tUSDT):</label><br>
        <input type="number" id="tp" name="tp" value="{{ session_data.tp_target|round(2) if session_data else 10.0 }}" step="0.01" required><br>
        
        <button type="submit" style="background-color: green; color: white;">🚀 Start Bot</button>
    </form>
{% endif %}
<hr>
<a href="{{ url_for('logout') }}" style="display: block; text-align: center; margin-top: 15px; font-size: 1.1em;">Logout</a>

<script>
    function autoRefresh() {
        var isRunning = {{ 'true' if session_data and session_data.is_running else 'false' }};
        
        if (isRunning) {
            setTimeout(function() {
                window.location.reload();
            }, 1000); 
        }
    }

    autoRefresh();
</script>
"""

# --- Flask Routes (UNCHANGED) ---

@app.before_request
def check_user_status():
    if request.endpoint in ('login', 'auth_page', 'logout', 'static'): return
    if 'email' in session:
        email = session['email']
        allowed_users = load_allowed_users()
        if email.lower() not in allowed_users:
            session.pop('email', None)
            flash('Your access has been revoked. Please log in again.', 'error')
            return redirect(url_for('auth_page'))

@app.route('/')
def index():
    if 'email' not in session: return redirect(url_for('auth_page'))
    email = session['email']
    session_data = get_session_data(email)

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)", "Displayed"]:
        reason = session_data["stop_reason"]
        
        if reason.startswith("SL Reached"): flash(f"🛑 STOP: Max loss reached! ({reason.split(': ')[1]})", 'error')
        elif reason == "TP Reached": flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached)", 'success')
        elif reason.startswith("API Buy Error"): flash(f"❌ API Error: {reason}. Check your token and account status.", 'error')
        
        delete_session_data(email)
        session_data = get_session_data(email)
            
    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        target_digit=TARGET_DIGIT,
        reversal_digit=REVERSAL_DIGIT,
        symbol=SYMBOL
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].lower()
        allowed_users = load_allowed_users()
        if email in allowed_users:
            session['email'] = email
            flash('Login successful.', 'success')
            return redirect(url_for('index'))
        else:
            flash('Email not authorized.', 'error')
            return redirect(url_for('auth_page'))
    return redirect(url_for('auth_page'))

@app.route('/auth')
def auth_page():
    if 'email' in session: return redirect(url_for('index'))
    return render_template_string(AUTH_FORM)

@app.route('/start', methods=['POST'])
def start_bot():
    global active_processes
    if 'email' not in session: return redirect(url_for('auth_page'))
    email = session['email']
    
    with PROCESS_LOCK:
        if email in active_processes and active_processes[email].is_alive():
            flash('Bot is already running.', 'info')
            return redirect(url_for('index'))
            
    try:
        account_type = request.form['account_type']
        currency = "USD" if account_type == 'demo' else "tUSDT"
        current_data = get_session_data(email)
        token = request.form['token'] if not current_data.get('api_token') or request.form.get('token') != current_data['api_token'] else current_data['api_token']
        stake = float(request.form['stake'])
        if stake < 0.35: raise ValueError("Stake too low")
        tp = float(request.form['tp'])
    except ValueError:
        flash("Invalid stake or TP value (Base Stake must be >= 0.35).", 'error')
        return redirect(url_for('index'))
        
    process = Process(target=bot_core_logic, args=(email, token, stake, tp, currency, account_type))
    process.daemon = True
    process.start()
    
    with PROCESS_LOCK: active_processes[email] = process
    
    flash(f'Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: DIGITDIFF (Target Digit {TARGET_DIGIT}, Reversal Digit {REVERSAL_DIGIT}) with x{MARTINGALE_MULTIPLIER} Martingale (Max {MARTINGALE_STEPS} Steps, Max {MAX_CONSECUTIVE_LOSSES} Losses)', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session: return redirect(url_for('auth_page'))
    stop_bot(session['email'], clear_data=True, stop_reason="Stopped Manually")
    flash('Bot stopped and session data cleared.', 'success')
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('email', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('auth_page'))


if __name__ == '__main__':
    all_sessions = load_persistent_sessions()
    for email in list(all_sessions.keys()):
        # لا نوقف العمليات عند التشغيل لكي لا يحدث تعليق إذا كان هناك عملية ما زالت قائمة
        pass
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

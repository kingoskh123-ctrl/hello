import time
import json
import websocket
import os
import sys
import fcntl
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, g
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock

# ==========================================================
# BOT CONSTANT SETTINGS (R_100 | x14.0 | 1 Tick | DIGITDIFF Strategy)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 1              # ⬅️ 1 تيك
DURATION_UNIT = "t"       # وحدة المدة (تيك)

# إعدادات المضاعفة
MARTINGALE_STEPS = 1              # خطوة مضاعفة واحدة فعالة
MAX_CONSECUTIVE_LOSSES = 2        # الحد الأقصى للخسائر المتتالية (يتوقف بعد الخسارة الثانية)
MARTINGALE_MULTIPLIER = 14.0      # المضاعف الجديد (x14)

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# أنواع العقود لـ DIGITDIFF
CONTRACT_TYPE_BASE = "DIGITDIFF"
CONTRACT_TYPE_MARTINGALE = "DIGITDIFF"

# ==========================================================

# ==========================================================
# GLOBAL STATE & PERSISTENCE
# ==========================================================
active_processes = {}
PROCESS_LOCK = Lock()

DEFAULT_SESSION_STATE = {
    "api_token": "",
    "base_stake": 1.0,
    "tp_target": 10.0,
    "is_running": False,
    "current_profit": 0.0,
    "current_stake_lower": 1.0,        
    "current_stake_higher": 1.0,       
    "consecutive_losses": 0,
    "current_step": 0,
    "total_wins": 0,
    "total_losses": 0,
    "stop_reason": "Stopped Manually",
    "last_entry_time": 0,
    "currency": "USD", 
    "account_type": "demo",
}

# --- Persistence functions (UNCHANGED logic) ---
def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE): return {}
    try:
        with open(ACTIVE_SESSIONS_FILE, 'r') as f:
            content = f.read()
            data = json.loads(content) if content else {}
            return data
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
    global active_processes
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
    
    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        else:
            delete_session_data(email)
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    else:
        print(f"⚠ [INFO] Process for {email} closed. Attempting immediate reconnect.")
# --- End of Persistence and Control functions ---

# ==========================================================
# TRANSACTIONAL TRADING FUNCTIONS
# ==========================================================

def calculate_martingale_stake(base_stake, current_step, multiplier):
    """منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة لعدد الخطوات"""
    if current_step == 0: 
        return base_stake
    
    return base_stake * multiplier

def wait_for_next_minute():
    """ينتظر حتى تكون الثانية المحلية هي 0 أو 1 من الدقيقة التالية."""
    now = datetime.now()
    # نحسب الثواني المتبقية حتى بداية الدقيقة
    wait_seconds = (60 - now.second) % 60
    
    # إذا كانت الثانية حالياً 0 أو 1، ننتظر 60 ثانية تقريباً للوصول إلى الدقيقة التالية
    if now.second <= 1 and now.second != 0: 
        wait_seconds = 60 + (0 - now.second)
    
    # نطرح جزء المللي ثانية لتكون عملية الانتظار أكثر دقة
    if wait_seconds > 1:
        wait_seconds -= (now.microsecond / 1000000.0)
        print(f"💤 [TIMER] Waiting {wait_seconds:.2f} seconds for the next minute start (SEC 0/1)...")
        time.sleep(max(0, wait_seconds)) # نستخدم max لتجنب الانتظار السلبي
    else:
        # إذا فاتتنا فرصة الدخول، ننتظر دقيقة كاملة لتجنب الدخول المتكرر
        print(f"💤 [TIMER] Missed entry window. Waiting 60 seconds.")
        time.sleep(60)

def apply_martingale_logic_new(email, total_profit):
    """يطبق منطق المضاعفة بناءً على نتيجة المعاملة الواحدة."""
    global MARTINGALE_MULTIPLIER, MARTINGALE_STEPS, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    current_data['current_profit'] += total_profit
    
    # Check TP
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
    
    base_stake_used = current_data['base_stake']
    
    # ❌ Loss Condition 
    if total_profit < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1 
        
        # Check SL
        if current_data['current_step'] > MARTINGALE_STEPS or current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: {current_data['consecutive_losses']} consecutive losses (Max {MAX_CONSECUTIVE_LOSSES}).")
            return
            
        # Prepare for Martingale (The next loop iteration will execute it immediately)
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake_higher'] = new_stake 
        
        print(f"🔄 [LOSS] PnL: {total_profit:.2f}. Step {current_data['current_step']}. New Stake: {round(new_stake, 2):.2f}. Retrying immediately.")

    # ✅ Win or Draw Condition 
    else: 
        current_data['total_wins'] += 1 if total_profit > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake_higher'] = base_stake_used
        
        entry_result_tag = "WIN" if total_profit > 0 else "DRAW"
        print(f"✅ [WIN] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {base_stake_used:.2f}.")

    save_session_data(email, current_data)
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Stake: {current_data['current_stake_higher']:.2f} | Next Action: {'Immediate Martingale' if current_data['current_step'] > 0 else 'Wait for SEC 0/1'}")


def execute_single_transaction(email, token, stake, digit, contract_type, currency):
    """يتصل، يشتري، ينتظر النتيجة، ثم يغلق الاتصال."""
    
    profit = 0.0
    
    # 1. Connect
    try:
        ws = websocket.create_connection(WSS_URL, timeout=10)
        print("🔗 [CONN] Connection established.")
    except Exception as e:
        print(f"❌ [CONN ERROR] Failed to connect: {e}")
        return 0.0 
    
    try:
        # 2. Authorize
        auth_request = json.dumps({"authorize": token})
        ws.send(auth_request)
        auth_response = json.loads(ws.recv())
        
        if 'error' in auth_response:
            print(f"❌ [AUTH ERROR] Failed: {auth_response['error']['message']}")
            return 0.0

        # 3. Send Buy order
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
                "digit": digit
            }
        }
        ws.send(json.dumps(trade_request))
        
        # 4. Receive Buy confirmation 
        buy_response = json.loads(ws.recv())
        if 'error' in buy_response:
            print(f"❌ [BUY ERROR] Failed: {buy_response['error']['message']}")
            profit = -stake 
            return profit
            
        contract_id = buy_response['buy']['contract_id']
        
        # 5. Subscribe to contract result
        open_contract_request = json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1})
        ws.send(open_contract_request)
        
        # 6. Wait for result (is_sold: 1)
        print(f"⏳ [TRADE] Contract {contract_id} opened. Waiting for result...")
        
        while True:
            # تعيين مهلة قصيرة لاستقبال النتيجة (3 ثواني كافية لعقد 1 تيك)
            ws.settimeout(3) 
            try:
                response = json.loads(ws.recv())
                
                if response.get('msg_type') == 'proposal_open_contract':
                    contract = response['proposal_open_contract']
                    if contract.get('is_sold') == 1:
                        profit = contract['profit']
                        print(f"✅ [RESULT] Contract sold. Profit/Loss: {profit:.2f}")
                        # نرسل طلب إلغاء الاشتراك قبل إغلاق الاتصال لبروتوكول أنظف
                        if 'subscription_id' in response:
                             ws.send(json.dumps({"forget": response['subscription_id']}))
                        break
                        
            except websocket.WebSocketTimeoutException:
                print("❌ [TIMEOUT] Result timeout reached. Assuming failure or slow connection.")
                profit = -stake 
                break
            except Exception as e:
                print(f"❌ [RESULT ERROR] Error receiving result: {e}")
                profit = -stake 
                break

    except Exception as e:
        print(f"❌ [TRANSACTION ERROR] Critical error during transaction: {e}")
        profit = -stake
        
    finally:
        # 7. Close Connection
        try:
            ws.close()
            print("🛑 [CONN] Connection closed as requested.")
        except:
            pass
            
    return profit

def bot_core_logic(email, token, stake, tp, currency, account_type):
    """منطق البوت الأساسي في الوضع المعاملاتي"""
    
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, 
        "base_stake": stake, 
        "tp_target": tp,
        "is_running": True, 
        "current_stake_lower": stake,         
        "current_stake_higher": stake,    
        "stop_reason": "Running",
        "last_entry_time": 0,
        "currency": currency,
        "account_type": account_type,
        "consecutive_losses": session_data.get('consecutive_losses', 0), # نحافظ على الخسائر المتتالية
        "current_step": session_data.get('current_step', 0)
    })
    save_session_data(email, session_data)

    while True:
        current_data = get_session_data(email)
        
        if not current_data.get('is_running'): break
        
        # 1. Base Entry Timing Control: Wait for SEC 0/1 only if current_step is 0
        if current_data['current_step'] == 0:
            wait_for_next_minute()

        # 2. Determine Trade Parameters
        current_data = get_session_data(email) 
        
        if current_data['current_step'] == 0:
            trade_stake = current_data['base_stake']
            trade_digit = 1
            trade_contract_type = CONTRACT_TYPE_BASE
            
            # نتحقق هنا مرة أخرى للتأكد من أننا لم ندخل صفقة في هذه الثانية إذا كنا في وضع الانتظار
            if int(time.time()) == current_data['last_entry_time']:
                time.sleep(1) # ننتظر ثانية واحدة لتجنب الدخول المزدوج
                continue
                
            print(f"🧠 [BASE ENTRY] Preparing trade at SEC 0/1. Stake: {trade_stake:.2f}")
        else:
            trade_stake = calculate_martingale_stake(current_data['base_stake'], current_data['current_step'], MARTINGALE_MULTIPLIER)
            trade_digit = 8
            trade_contract_type = CONTRACT_TYPE_MARTINGALE
            print(f"🔄 [MARTINGALE] Preparing trade. Step {current_data['current_step']}. Stake: {trade_stake:.2f}")

        # Update last entry time before starting the transaction
        current_data['last_entry_time'] = int(time.time())
        save_session_data(email, current_data)

        # 3. Execute Transaction
        profit_loss = execute_single_transaction(email, token, trade_stake, trade_digit, trade_contract_type, currency)
        
        # 4. Apply Martingale/Loss Logic (Handles state update, TP/SL check)
        apply_martingale_logic_new(email, profit_loss)
        
        # 5. Check if bot was stopped by TP/SL
        if get_session_data(email).get('is_running') is False: break
        
        # If Martingale trade, the loop executes immediately. If base trade (win), it waits for the next minute.
        time.sleep(0.5) # فاصل بسيط بين المعاملات

# --- (FLASK APP SETUP AND ROUTES - UNCHANGED) ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False

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
    {% set timing_logic = "Base @ Sec 0 or 1, Martingale Immediate" %}
    {% set strategy = "DIGITDIFF (Base 1 / Martingale 8) (" + symbol + " - " + duration|string + duration_unit + " - x" + martingale_multiplier|string + " Martingale, Max Steps " + martingale_steps|string + ", Max Loss " + max_consecutive_losses|string + ")" %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake_higher|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Loss: {{ max_consecutive_losses }})</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
    <p style="font-weight: bold; color: #ff5733;">Connection: Transactional (Connect, Trade, Close)</p>
    
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
        
        if reason in ["SL Reached", "TP Reached", "API Buy Error"]:
            
            if reason.startswith("SL Reached"): flash(f"🛑 STOP: Max loss reached! ({reason.split(': ')[1]})", 'error')
            elif reason == "TP Reached": flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached)", 'success')
            elif reason.startswith("API Buy Error"): flash(f"❌ API Error: {reason}. Check your token and account status.", 'error')
            
            # يجب مسح البيانات حتى لا تتكرر الرسالة عند تحديث الصفحة
            delete_session_data(email)
            session_data = get_session_data(email)
            
    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        duration=DURATION,
        duration_unit=DURATION_UNIT,
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
    
    flash(f'Bot started successfully. Mode: Transactional (Connect/Trade/Close). Strategy: DIGITDIFF (1/8) with x{MARTINGALE_MULTIPLIER} Martingale (Max {MAX_CONSECUTIVE_LOSSES} Losses)', 'success')
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
    # إيقاف أي عمليات سابقة عند بدء تشغيل الخادم
    all_sessions = load_persistent_sessions()
    for email in list(all_sessions.keys()):
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

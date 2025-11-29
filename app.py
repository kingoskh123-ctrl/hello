import time
import json
import websocket
import os
import threading
from flask import Flask, request, render_template_string, redirect, url_for, session, flash
from multiprocessing import Process
from threading import Lock

# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 10
DURATION_UNIT = "t" 
TICKS_TO_ANALYZE = 30 
MARTINGALE_STEPS = 2 
MAX_CONSECUTIVE_LOSSES = 2 
BARRIER_OFFSET = "0.1" 
FIXED_MARTINGALE_MULTIPLIER = 6.0 

MARTINGALE_MULTIPLIERS = [
    1.00,  
    FIXED_MARTINGALE_MULTIPLIER,  
    FIXED_MARTINGALE_MULTIPLIER,  
    FIXED_MARTINGALE_MULTIPLIER  
]

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"
LAST_TRADE_TIME_FILE = "last_trade_time.json"

CONTRACT_TYPE_TOUCH = "ONE_TOUCH"
CONTRACT_TYPE_NO_TOUCH = "NOT_TOUCH"

# ==========================================================

PROCESS_LOCK = Lock()
TRADE_LOCK = Lock()
LAST_TRADE_TIME = {} 

DEFAULT_SESSION_STATE = {
    "api_token": "",
    "base_stake": 1.0,
    "tp_target": 10.0,
    "is_running": False,
    "current_profit": 0.0,
    "consecutive_losses": 0,
    "total_wins": 0,
    "total_losses": 0,
    "stop_reason": "Stopped Manually",
    "currency": "USD",
    "account_type": "demo",
    "last_valid_tick_price": 0.0,
    "open_contract_ids": [], 
}

def load_last_trade_time():
    if not os.path.exists(LAST_TRADE_TIME_FILE): return {}
    try:
        with open(LAST_TRADE_TIME_FILE, 'r') as f:
            return json.load(f)
    except: return {}

def save_last_trade_time(email, timestamp):
    all_times = load_last_trade_time()
    all_times[email] = timestamp
    with open(LAST_TRADE_TIME_FILE, 'w') as f:
        json.dump(all_times, f, indent=4)

def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE): return {}
    try:
        with open(ACTIVE_SESSIONS_FILE, 'r') as f:
            content = f.read()
            data = json.loads(content) if content else {}
            for email, session_data in data.items():
                if 'open_contract_ids' not in session_data:
                    session_data['open_contract_ids'] = []
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
    
    all_times = load_last_trade_time()
    if email in all_times: del all_times[email]
    with open(LAST_TRADE_TIME_FILE, 'w') as f:
        json.dump(all_times, f, indent=4)


def load_allowed_users():
    if not os.path.exists(USER_IDS_FILE): return set()
    try:
        with open(USER_IDS_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except: return set()

def stop_bot(email, clear_data=True, stop_reason="Stopped Manually"):
    current_data = get_session_data(email)
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
        save_session_data(email, current_data)

    if clear_data and stop_reason in ["SL Reached: Max Loss Stop.", "TP Reached", "API Buy Error", "Stopped Manually"]:
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data cleared from file.")
        delete_session_data(email)
    else:
        print(f"⚠ [INFO] Bot for {email} stopped ({stop_reason}). Data kept.")

def send_trade_order(email, ws_app, stake, currency, contract_type_param, barrier_offset):
    global DURATION, DURATION_UNIT, SYMBOL

    trade_request = {
        "buy": 1,
        "price": round(stake, 2),
        "parameters": {
            "amount": round(stake, 2),
            "basis": "stake",
            "contract_type": contract_type_param, 
            "currency": currency,
            "duration": DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol": SYMBOL,
            "barrier": barrier_offset 
        }
    }
    
    try:
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        return False


def get_next_stake(base_stake, consecutive_losses):
    global MARTINGALE_MULTIPLIERS, MARTINGALE_STEPS
    
    if consecutive_losses > MARTINGALE_STEPS:
        return base_stake 

    multiplier = MARTINGALE_MULTIPLIERS[consecutive_losses]
    return base_stake * multiplier


def apply_martingale_logic(email, total_profit):
    global MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)

    if not current_data.get('is_running'): return

    current_data['current_profit'] += total_profit
    
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return

    if total_profit < 0:
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Max Loss Stop.")
            return

        next_stake = get_next_stake(current_data['base_stake'], current_data['consecutive_losses'])
        print(f"📉 [LOSS] PnL: {total_profit:.2f}. Consecutive Losses: {current_data['consecutive_losses']}. Next Stake: {next_stake:.2f}")

    else:
        current_data['total_wins'] += 1 if total_profit > 0 else 0
        current_data['consecutive_losses'] = 0
        
        next_stake = current_data['base_stake']

        entry_result_tag = "WIN" if total_profit > 0 else "DRAW"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {next_stake:.2f}.")

    current_data['open_contract_ids'] = [] 
    save_session_data(email, current_data)


def analyze_and_trade(email, ws_app, ticks_history):
    global BARRIER_OFFSET, CONTRACT_TYPE_TOUCH, TICKS_TO_ANALYZE

    current_data = get_session_data(email)
    
    if len(ticks_history) < TICKS_TO_ANALYZE:
        print(f"❌ [ENTRY FAIL] Only {len(ticks_history)} ticks available (Need {TICKS_TO_ANALYZE}). Skipping trade.")
        return False

    first_tick_price = ticks_history[0]
    last_tick_price = ticks_history[-1]
    price_difference = last_tick_price - first_tick_price 

    barrier_offset_value = BARRIER_OFFSET 

    contract_type_to_use = CONTRACT_TYPE_TOUCH
    barrier_to_use = None
    strategy_tag = None

    stake_to_use = get_next_stake(current_data['base_stake'], current_data['consecutive_losses'])
    currency_to_use = current_data['currency']

    if price_difference > 0:
        barrier_to_use = f"+{barrier_offset_value}" 
        strategy_tag = f"30 Ticks BULLISH -> ONE_TOUCH +{barrier_offset_value}"

    elif price_difference < 0:
        barrier_to_use = f"-{barrier_offset_value}" 
        strategy_tag = f"30 Ticks BEARISH -> ONE_TOUCH -{barrier_offset_value}"

    else:
        print(f"⚠ [ENTRY SKIPPED] No clear direction (Diff = 0.00000). Closing connection immediately.")
        ws_app.close() 
        return False 

    print(f"🧠 [TRADE ENTRY] Strategy: {strategy_tag} | Stake: {round(stake_to_use, 2):.2f} (Losses: {current_data['consecutive_losses']})")

    if send_trade_order(email, ws_app, stake_to_use, currency_to_use, contract_type_to_use, barrier_to_use):
        current_data['last_valid_tick_price'] = last_tick_price
        save_session_data(email, current_data)
        return True 
    
    print(f"❌ [API BUY FAILED] Closing connection.")
    ws_app.close()
    
    apply_martingale_logic(email, -stake_to_use) 
    return False


def single_trade_operation(email):
    current_data = get_session_data(email)
    if not current_data.get('is_running'): return

    if current_data.get('open_contract_ids'):
        print(f"🚫 [OP SKIP] Contract ID {current_data['open_contract_ids'][0]} is already open. Skipping analysis.")
        return

    ticks_history = None
    contract_result = None
    
    ws_app = websocket.WebSocketApp(
        WSS_URL, on_error=lambda ws, err: print(f"[WS Error {email}] {err}")
    )

    def on_open(ws):
        ws.send(json.dumps({"authorize": current_data['api_token']}))
        
    def on_message(ws, message):
        nonlocal ticks_history, contract_result
        data = json.loads(message)
        msg_type = data.get('msg_type')
        
        if not get_session_data(email).get('is_running'):
            ws.close()
            return

        if msg_type == 'authorize':
            fetch_request = {
                "ticks_history": SYMBOL,
                "end": "latest",
                "count": TICKS_TO_ANALYZE,
                "subscribe": 0 
            }
            ws.send(json.dumps(fetch_request))
            print(f"🔗 [OP] Authorized. Requesting {TICKS_TO_ANALYZE} historical ticks.")

        elif msg_type == 'history' and 'prices' in data['history']:
            ticks_history = [float(p) for p in data['history']['prices']]
            
            if not analyze_and_trade(email, ws, ticks_history):
                ws.close() 

        elif msg_type == 'buy':
            contract_id = data['buy']['contract_id']
            current_data_update = get_session_data(email)
            current_data_update['open_contract_ids'].append(contract_id)
            save_session_data(email, current_data_update)

            ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))

        elif msg_type == 'proposal_open_contract':
            contract = data['proposal_open_contract']
            if contract.get('is_sold') == 1:
                contract_result = contract['profit'] 
                if 'subscription_id' in data: ws.send(json.dumps({"forget": data['subscription_id']}))
                ws.close() 

        elif 'error' in data:
            error_message = data['error'].get('message', 'Unknown Error')
            print(f"❌❌ [API ERROR] {error_message}. Closing connection.")
            
            if data['error'].get('code') in ['InvalidContractParameters', 'Unauthorized', 'BuyNotAllowed']:
                current_stake = get_next_stake(current_data['base_stake'], current_data['consecutive_losses'])
                contract_result = -current_stake
            
            ws.close()
            
    ws_app.on_open = on_open
    ws_app.on_message = on_message
    
    wst = threading.Thread(target=lambda: ws_app.run_forever(), daemon=True)
    wst.start()
    
    wst.join(timeout=40) 
    
    if contract_result is not None:
        apply_martingale_logic(email, contract_result)
        
    elif get_session_data(email).get('is_running'):
        print(f"⚠ [OP - TIMEOUT] Operation completed with timeout. Contract status unknown.")


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
    .contract-id {
        font-weight: bold;
        color: darkorange;
        word-break: break-all;
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
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set strategy = ticks_to_analyze|string + "-Tick Historical Touch Trade (Barrier " + barrier_offset + " | Duration " + duration|string + " Ticks) | Martingale: " + (fixed_martingale_multiplier)|string + "x (Max Loss " + (max_consecutive_losses)|string + ")" %}
    {% set next_trade_in = 60 - (current_time % 60) %}
    
    <p class="status-running">✅ Bot is Running! (Next **Preparation** starts in {{ next_trade_in }} seconds)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: purple;">Last Known Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>

    {% if session_data.open_contract_ids %}
        <p style="color:red; font-weight: bold;">⚠️ صفقة مفتوحة: يرجى الانتظار حتى يتم إغلاقها.</p>
        {% for contract_id in session_data.open_contract_ids %}
            <p>ID العقد: <span class="contract-id">{{ contract_id }}</span></p>
        {% endfor %}
    {% else %}
        <p style="color:blue; font-weight: bold;">لا توجد صفقات مفتوحة حالياً. البوت جاهز **لبدء التحضير** في الثانية 0.</p>
    {% endif %}


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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)"]:
        reason = session_data["stop_reason"]
        if reason.startswith("SL Reached"):
            flash(f"🛑 تم الوصول لحد وقف الخسارة! (البوت توقف بعد {MAX_CONSECUTIVE_LOSSES} خسائر متتالية).", 'error')
        elif reason == "TP Reached":
            flash(f"✅ تم الوصول لهدف الربح ({session_data['tp_target']} {session_data.get('currency', 'USD')}) بنجاح!", 'success')
        elif reason.startswith("API Buy Error"):
            flash(f"❌ خطأ في API: {reason}. توقف البوت بسبب خطأ.", 'error')

        delete_session_data(email) 
        return redirect(url_for('index'))
    
    current_time = int(time.time())
    
    if session_data.get('is_running'):
        current_second = current_time % 60
        ALLOWED_ENTRY_SECONDS = [0]
        
        should_run = current_second in ALLOWED_ENTRY_SECONDS
        
        last_trade_time = load_last_trade_time().get(email, 0)
        
        if should_run and (current_time - last_trade_time) > 59: 
            print(f"⏰ [TRIGGER] Allowed second ({current_second}). Starting historical fetch and trade operation for {email}.")
            
            trade_process = Process(target=single_trade_operation, args=(email,))
            trade_process.start()
            
            save_last_trade_time(email, current_time)

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        duration=DURATION,
        barrier_offset=BARRIER_OFFSET,
        fixed_martingale_multiplier=FIXED_MARTINGALE_MULTIPLIER, 
        symbol=SYMBOL,
        ticks_to_analyze=TICKS_TO_ANALYZE,
        current_time=current_time
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
    if 'email' not in session: return redirect(url_for('auth_page'))
    email = session['email']

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
    
    current_data.update({
        "api_token": token,
        "base_stake": stake,
        "tp_target": tp,
        "is_running": True,
        "stop_reason": "Running",
        "currency": currency,
        "account_type": account_type,
    })
    save_session_data(email, current_data)
    save_last_trade_time(email, 0) 

    strategy_desc = f"Martingale (x{FIXED_MARTINGALE_MULTIPLIER} multiplier, Max Loss {MAX_CONSECUTIVE_LOSSES}) with {TICKS_TO_ANALYZE}-Tick Direction"
    flash(f'Bot started successfully. Preparation starts at second 0. Strategy: {strategy_desc}', 'success')
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
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

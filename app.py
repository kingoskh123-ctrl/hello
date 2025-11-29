import time
import json
import websocket
import os
from flask import Flask, request, render_template_string, redirect, url_for, session, flash
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock

# ==========================================================
# BOT CONSTANT SETTINGS
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 1               
DURATION_UNIT = "t"        
MARTINGALE_STEPS = 1       
MAX_CONSECUTIVE_LOSSES = 2 
RECONNECT_DELAY = 1        # 💡 NEW/REINTRODUCED: 1 second delay between reconnect attempts
TRADE_COOLDOWN_SECONDS = 2 
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# Strategy: DIGITDIFF with fixed barriers
BASE_CONTRACT_TYPE = "DIGITDIFF" 
BASE_BARRIER = 1             
MARTINGALE_BARRIER = 8       
MARTINGALE_MULTIPLIER = 14.0 
# ==========================================================

# ==========================================================
# GLOBAL STATE (Shared between processes via File/Lock)
# ==========================================================
active_processes = {}
active_ws = {}
PROCESS_LOCK = Lock()

DEFAULT_SESSION_STATE = {
    "api_token": "",
    "base_stake": 1.0,
    "tp_target": 10.0,
    "is_running": False,
    "current_profit": 0.0,
    "current_stake": 1.0,
    "consecutive_losses": 0,
    "current_step": 0,
    "total_wins": 0,
    "total_losses": 0,
    "stop_reason": "Stopped Manually",
    "last_entry_time": 0,
    "last_entry_price": 0.0,
    "last_tick_data": None,
    "currency": "USD", 
    "account_type": "demo",
    "open_contract_id": None,           
    "open_price": 0.0,           
    "open_time": 0,              
    "last_action_type": BASE_CONTRACT_TYPE, 
    "last_valid_tick_price": 0.0,
    "last_trade_barrier": BASE_BARRIER, 
    "last_trade_closed_time": 0,        
}
# ==========================================================

# ==========================================================
# PERSISTENT DATA MANAGEMENT
# ==========================================================
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
    """
    Stops the bot process/WS only if it's a permanent stop (SL/TP/Manual/Error).
    """
    global active_processes, active_ws
    current_data = get_session_data(email)
    
    # 1. Update state 
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
        current_data["open_contract_id"] = None
        save_session_data(email, current_data)

    # 2. Terminate/Close WS
    with PROCESS_LOCK:
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                print(f"🛑 [INFO] Forcing WS close and termination for {email}...")
                
                if email in active_ws and active_ws[email]:
                    try:
                        active_ws[email].close()
                    except:
                        pass
                
                if email in active_ws: del active_ws[email]
                
                # Terminate the process as this is a permanent stop
                process.terminate()
                process.join()
                del active_processes[email]

    if email in active_ws: del active_ws[email]
    
    # 3. Final data cleanup
    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        else: 
            delete_session_data(email)
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")

# ==========================================================
# TRADING BOT FUNCTIONS
# ==========================================================

def calculate_martingale_stake(base_stake, current_stake, current_step):
    """ Martingale staking logic (Last losing stake x 14) """
    if current_step == 0:
        return base_stake
    if current_step <= MARTINGALE_STEPS:
        return current_stake * MARTINGALE_MULTIPLIER
    return base_stake

def send_trade_order(email, stake, currency, contract_type_param, barrier_value=None):
    global active_ws, DURATION, DURATION_UNIT, SYMBOL
    
    if email not in active_ws or active_ws[email] is None: return
    ws_app = active_ws[email]
    
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
            "symbol": SYMBOL
        }
    }
    
    if barrier_value is not None:
        trade_request["parameters"]["barrier"] = str(barrier_value) 
    
    try:
        ws_app.send(json.dumps(trade_request))
        print(f"💰 [TRADE] Sent {contract_type_param} with Barrier: {barrier_value} | Stake: {round(stake, 2):.2f} {currency}")
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass

def check_pnl_limits(email, profit_loss, last_action_type, ws_app):
    """ Logic to update state, check limits, and prepare for the next trade in the same connection """

    current_data = get_session_data(email)
    current_data["open_contract_id"] = None # Contract is sold, clear ID
    current_data["last_trade_closed_time"] = int(time.time()) # Update cooldown timestamp
    
    if not current_data.get('is_running'): 
        save_session_data(email, current_data)
        return

    last_stake = current_data['current_stake']
    current_data['current_profit'] += profit_loss
    
    if profit_loss > 0:
        # Win Case
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        current_data['last_action_type'] = last_action_type
        current_data['last_trade_barrier'] = BASE_BARRIER # Next is base barrier
        save_session_data(email, current_data)
        
        print(f"✅ [WIN] Sold contract. Resetting to base stake. Waiting for next time entry...")
        
    else:
        # Loss Case (Engage Martingale)
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES or current_data['current_step'] > MARTINGALE_STEPS:
            stop_bot(email, clear_data=True, stop_reason="SL Reached")
            return
        
        # Calculate new stake
        new_stake = calculate_martingale_stake(
            current_data['base_stake'],
            last_stake,
            current_data['current_step']
        )
        
        current_data['current_stake'] = new_stake
        current_data['last_action_type'] = last_action_type
        current_data['last_trade_barrier'] = MARTINGALE_BARRIER # Next is martingale barrier
        save_session_data(email, current_data)
        
        print(f"💸 [MARTINGALE] Lost. Next stake calculated: {new_stake:.2f}. Waiting for next time entry...")

    # Check TP after win/loss
    if current_data['current_profit'] >= current_data['tp_target']:
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
    
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Strategy: {BASE_CONTRACT_TYPE}")


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Main bot logic - runs in an infinite loop to handle auto-reconnect """
    
    global active_ws, BASE_CONTRACT_TYPE, RECONNECT_DELAY
    
    # Initialize the session data for the running process
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, 
        "base_stake": stake, 
        "tp_target": tp,
        "is_running": True, 
        "current_stake": stake,
        "stop_reason": "Running",
        "last_entry_time": 0,
        "last_trade_closed_time": 0,
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "currency": currency,
        "account_type": account_type,
        "open_contract_id": None, 
        "open_price": 0.0,           
        "open_time": 0,              
        "last_action_type": BASE_CONTRACT_TYPE, 
        "last_valid_tick_price": 0.0,
        "last_trade_barrier": BASE_BARRIER, 
    })
    save_session_data(email, session_data)

    def on_open_wrapper(ws_app):
        current_data = get_session_data(email) 
        ws_app.send(json.dumps({"authorize": current_data['api_token']}))
        ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        
        if current_data.get('open_contract_id'):
            print(f"🔗 [RECONNECT] Resubscribing to Contract ID: {current_data['open_contract_id']}")
            ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": current_data['open_contract_id'], "subscribe": 1}))
        
        running_data = get_session_data(email)
        running_data['is_running'] = True
        running_data['stop_reason'] = "Running"
        save_session_data(email, running_data)
        print(f"✅ [PROCESS] Connection established for {email}.")


    def on_message_wrapper(ws_app, message):
        data = json.loads(message)
        msg_type = data.get('msg_type')
        
        current_data = get_session_data(email)
        if not current_data.get('is_running'):
            ws_app.close()
            return
            
        if msg_type == 'tick':
            current_tick_timestamp = int(data['tick']['epoch'])
            current_price = float(data['tick']['quote'])
            
            # Update state with the latest tick data
            current_data['last_tick_data'] = { "price": current_price, "timestamp": current_tick_timestamp }
            current_data['last_valid_tick_price'] = current_price
            save_session_data(email, current_data)
            
            # 1. Check if contract is OPEN
            if current_data.get('open_contract_id') is not None: 
                return
            
            # 2. Check Trade Cooldown
            now_epoch = int(time.time())
            if now_epoch < current_data.get('last_trade_closed_time', 0) + TRADE_COOLDOWN_SECONDS:
                return 

            # 3. Check for same entry time (Tick prevention)
            if current_data['last_entry_time'] == current_tick_timestamp: 
                return
            
            # 4. Check Entry Condition (Time or Martingale Retry)
            now_utc = datetime.now(timezone.utc)
            current_second = now_utc.second 
            is_base_entry_time = current_second == 0 or current_second == 30

            is_martingale_retry = current_data['current_step'] > 0
            
            if not (is_martingale_retry or (is_base_entry_time and current_data['current_step'] == 0)):
                return 

            # 5. Determine Stake/Barrier
            if is_martingale_retry:
                barrier_value = MARTINGALE_BARRIER
                action_type_to_use = BASE_CONTRACT_TYPE
                print(f"💡 [MARTINGALE] Immediate Retry. Using barrier: {barrier_value}")
            else:
                barrier_value = BASE_BARRIER
                action_type_to_use = BASE_CONTRACT_TYPE
                print(f"🎯 [BASE ENTRY] Time entry condition met (:{current_second}). Barrier set: {barrier_value}")
            
            current_data['last_trade_barrier'] = barrier_value
            save_session_data(email, current_data)

            stake_to_use = current_data['current_stake']
            currency_to_use = current_data['currency']
            
            # 6. Send Trade Order
            send_trade_order(
                email, 
                stake_to_use, 
                currency_to_use, 
                action_type_to_use, 
                barrier_value
            )
            
            current_data['last_entry_time'] = current_tick_timestamp
            current_data['last_entry_price'] = current_price
            current_data['last_action_type'] = action_type_to_use 
            save_session_data(email, current_data)
            
            entry_mode = "Instant Martingale" if is_martingale_retry else "Time-based Base"
            print(f"✅ [ENTRY @ {entry_mode}] Entered {action_type_to_use} with Target Digit: {barrier_value} on price: {current_price}")
            return 

        elif msg_type == 'buy':
            contract_id = data['buy']['contract_id']
            action_type = BASE_CONTRACT_TYPE
            
            current_data['last_action_type'] = action_type
            current_data['open_contract_id'] = contract_id 
            save_session_data(email, current_data)
            
            # Subscribe to the contract status
            ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        
        elif 'error' in data:
            error_code = data['error'].get('code', 'N/A')
            error_message = data['error'].get('message', 'Unknown Error')
            print(f"❌❌ [API ERROR] Code: {error_code}, Message: {error_message}")
            
            if current_data.get('is_running'):
                stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: {error_code} - {error_message}")

        elif msg_type == 'proposal_open_contract':
            contract = data['proposal_open_contract']
            if contract.get('is_sold') == 1:
                last_action_type = get_session_data(email).get('last_action_type', BASE_CONTRACT_TYPE) 
                
                check_pnl_limits(email, contract['profit'], last_action_type, ws_app)
                
                if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

    def on_close_wrapper(ws_app, code, msg):
        print(f"⚠ [PROCESS] WS closed for {email}. Reason: {code}/{msg}")

    # 💡 The Infinite Auto-Reconnect Loop
    while True:
        current_data = get_session_data(email)
        if not current_data.get('is_running'):
            print(f"🛑 [PROCESS] Bot stopped manually or by SL/TP. Exiting loop for {email}.")
            break
            
        print(f"🔗 [PROCESS] Attempting to connect for {email}...")

        try:
            ws = websocket.WebSocketApp(
                WSS_URL, 
                on_open=on_open_wrapper, 
                on_message=on_message_wrapper,
                on_error=lambda ws, err: print(f"[WS Error {email}] {err}"),
                on_close=on_close_wrapper
            )
            active_ws[email] = ws
            
            # This blocks until connection closes (due to error or manual close)
            ws.run_forever(ping_interval=20, ping_timeout=10)
            
        except Exception as e:
            print(f"❌ [ERROR] WebSocket failed for {email}: {e}")
        
        # If run_forever exits (connection lost/closed), wait 1 second and try again
        print(f"💤 [PROCESS] Connection lost. Retrying in {RECONNECT_DELAY} second...")
        time.sleep(RECONNECT_DELAY)
        
    print(f"🛑 [PROCESS] Bot process loop ended for {email}.")


# ==========================================================
# FLASK APP SETUP AND ROUTES (Minor adjustment to remove dead code)
# ==========================================================

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
        direction: ltr;
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
    .contract-id {
        font-size: 0.9em;
        color: #888;
        word-break: break-all;
    }
</style>
<h1>Bot Control Panel | User: {{ email }}</h1>
<hr>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:{{ 'green' if category == 'success' else ('blue' if category == 'info' else 'red') }};">{{ message }}</p>
        {% endfor %}
        
        {% if session_data and session_data.stop_reason and session_data.stop_reason not in ["Running", "Displayed"] %}
            <p style="color:red; font-weight:bold;">Last Stop Reason: {{ session_data.stop_reason }}</p>
        {% endif %}
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set strategy = base_contract_type + " (" + base_barrier|string + " & " + martingale_barrier|string + " | Instant Martingale x" + martingale_multiplier|string + ")" %}
    
    <p class="status-running">✅ Bot is Running! (Always Connected)</p>
    <p style="color:blue;">💡 Cooldown Time: {{ trade_cooldown_seconds }} seconds after sale.</p>
    <p style="color:red;">💡 Auto-Reconnect Delay: {{ reconnect_delay }} second.</p>
    
    {% if session_data.open_contract_id %}
    <p style="color: #007bff; font-weight: bold;">Open Contract: Yes</p>
    <p class="contract-id">Contract ID: {{ session_data.open_contract_id }}</p>
    {% else %}
    <p style="color: green; font-weight: bold;">Open Contract: No</p>
    {% endif %}

    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Consecutive Losses: {{ max_consecutive_losses }})</p>
    <p>Stats: {{ session_data.total_wins }} Wins | {{ session_data.total_losses }} Losses</p>
    <p style="font-weight: bold; color: green;">Last Entry Price: {{ session_data.last_entry_price|round(5) }}</p>
    <p style="font-weight: bold; color: purple;">Last Tick Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
    {% if session_data.last_trade_barrier is not none %}
        <p style="font-weight: bold; color: blue;">Target Barrier Used: {{ session_data.last_trade_barrier }}</p>
    {% endif %}
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
            <option value="live">Real (tUSDT)</option>
        </select><br>

        <label for="token">Deriv API Token:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}"><br>
        
        <label for="stake">Base Stake (USD/tUSDT):</label><br>
        <input type="number" id="stake" name="stake" value="{{ session_data.base_stake|round(2) if session_data else 0.35 }}" step="0.01" min="0.35" required><br>
        
        <label for="tp">Take Profit Target (USD/tUSDT):</label><br>
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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Displayed"]:
        reason = session_data["stop_reason"]
        
        if reason == "SL Reached": 
            flash(f"🛑 STOP: Max loss limit ({MAX_CONSECUTIVE_LOSSES} consecutive losses or exceeding {MARTINGALE_STEPS} martingale step) reached! (SL Reached)", 'error')
        elif reason == "TP Reached": 
            flash(f"✅ GOAL: Take Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached)", 'success')
        elif reason.startswith("API Buy Error") or reason.startswith("WS Crash"): 
            flash(f"❌ Error: {reason}. Please check connection and restart the bot.", 'error')
            
        session_data['stop_reason'] = "Displayed"
        save_session_data(email, session_data)
        delete_session_data(email)

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        base_contract_type=BASE_CONTRACT_TYPE, 
        base_barrier=BASE_BARRIER,
        martingale_barrier=MARTINGALE_BARRIER,
        martingale_multiplier=MARTINGALE_MULTIPLIER,
        duration=DURATION,
        trade_cooldown_seconds=TRADE_COOLDOWN_SECONDS,
        reconnect_delay=RECONNECT_DELAY # Pass the new delay to the template
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
    global active_processes, BASE_CONTRACT_TYPE, MARTINGALE_MULTIPLIER
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
        tp = float(request.form['tp'])
    except ValueError:
        flash("Invalid stake or TP value.", 'error')
        return redirect(url_for('index'))
        
    process = Process(target=bot_core_logic, args=(email, token, stake, tp, currency, account_type))
    process.daemon = True
    process.start()
    
    with PROCESS_LOCK: active_processes[email] = process
    
    flash(f'Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: {BASE_CONTRACT_TYPE} 1 & 8 (x{MARTINGALE_MULTIPLIER} Persistent Connection)', 'success')
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
    if not os.path.exists(ACTIVE_SESSIONS_FILE):
        with open(ACTIVE_SESSIONS_FILE, 'w') as f:
            f.write('{}')
            
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

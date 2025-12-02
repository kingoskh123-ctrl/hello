import time
import json
import websocket
import os
from flask import Flask, request, render_template_string, redirect, url_for, session, flash
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock
from collections import deque

# ==========================================================
# BOT CONSTANT SETTINGS
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 5               # Contract duration is 5 Ticks
DURATION_UNIT = "t"        
MARTINGALE_STEPS = 1        
MAX_CONSECUTIVE_LOSSES = 2  
RECONNECT_DELAY = 1        
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# 💡 MIXED CONTRACTS SETUP (Barriers are dynamic based on trend)
BASE_CONTRACT_TYPE_1 = "NOTOUCH" 
BASE_CONTRACT_TYPE_2 = "ONETOUCH" 
TOTAL_CONTRACTS_PER_TRADE = 2    
MARTINGALE_MULTIPLIER = 19.0     
ENTRY_SECOND = 0                   
TREND_TICKS_COUNT = 5            # New: Number of ticks to analyze for trend
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
    
    "open_contract_ids": [],    
    "open_prices": [],          
    "open_time": 0,              
    
    "closed_contracts_pnl": 0.0, 
    "closed_contracts_count": 0, 
    
    "last_valid_tick_price": 0.0,
    "last_trade_closed_time": 0,        
    
    "tick_history": deque(maxlen=TREND_TICKS_COUNT), # 💡 Updated maxlen
    "is_in_trade": False 
}
# ... (rest of persistence functions remain the same)
# ==========================================================

# ==========================================================
# PERSISTENT DATA MANAGEMENT (Keeping unchanged for brevity, assuming standard setup)
# ... (load_persistent_sessions, save_session_data, get_session_data, delete_session_data, load_allowed_users, stop_bot)
# Note: The stop_bot logic from the previous answer which handles SL/TP and data deletion is assumed to be included here.
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
    session_data['tick_history'] = list(session_data['tick_history'])
    all_sessions[email] = session_data
    with open(ACTIVE_SESSIONS_FILE, 'w') as f:
        try: json.dump(all_sessions, f, indent=4)
        except: pass
    session_data['tick_history'] = deque(session_data['tick_history'], maxlen=TREND_TICKS_COUNT)


def get_session_data(email):
    all_sessions = load_persistent_sessions()
    if email in all_sessions:
        data = all_sessions[email]
        for key, default_val in DEFAULT_SESSION_STATE.items():
            if key not in data: data[key] = default_val
        data['tick_history'] = deque(data['tick_history'], maxlen=TREND_TICKS_COUNT)
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
    global active_processes, active_ws
    current_data = get_session_data(email)
    
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
        current_data["open_contract_ids"] = []
        current_data["closed_contracts_count"] = 0
        current_data["closed_contracts_pnl"] = 0.0
        current_data["is_in_trade"] = False
        save_session_data(email, current_data) 

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
                
                process.terminate()
                process.join()
                del active_processes[email]

    if email in active_ws: del active_ws[email]
    
    if clear_data and stop_reason not in ["SL Reached", "TP Reached", "API Buy Error"]:
        delete_session_data(email)
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    elif stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept temporarily for display.") 
# ==========================================================

# ==========================================================
# TRADING BOT FUNCTIONS
# ==========================================================

def analyze_trend(tick_history):
    """
    Analyzes trend based on the difference between the oldest and newest tick prices.
    Returns 'UPTREND', 'DOWNTREND', or None if insufficient data.
    """
    if len(tick_history) < TREND_TICKS_COUNT:
        return None
    
    # Get the oldest tick price (index 0) and the newest tick price (index -1)
    # The deque stores ticks oldest to newest.
    oldest_price = tick_history[0]
    newest_price = tick_history[-1]
    
    if newest_price > oldest_price:
        return 'UPTREND'
    elif newest_price < oldest_price:
        return 'DOWNTREND'
    else:
        # Price hasn't moved or insufficient movement
        return None 

def calculate_martingale_stake(base_stake, current_stake, current_step):
    if current_step == 0:
        return base_stake
    if current_step <= MARTINGALE_STEPS:
        return current_stake * MARTINGALE_MULTIPLIER
    return base_stake

def send_trade_order(email, stake, currency, contract_type_param, barrier_offset_1, barrier_offset_2=None):
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
            "symbol": SYMBOL,
            "barrier": barrier_offset_1 
        }
    }
    
    if contract_type_param == "NOTOUCH":
        # NOTOUCH requires two barrier parameters: barrier and barrier2
        # For single-sided NOTOUCH, the lower barrier must be the negative offset
        trade_request["parameters"]["barrier"] = str(abs(float(barrier_offset_1))) # High Barrier
        trade_request["parameters"]["barrier2"] = str(-abs(float(barrier_offset_1))) # Low Barrier (always negative of the positive offset)
    
    elif contract_type_param == "ONETOUCH":
        # ONETOUCH only takes one barrier (offset from the entry price)
        trade_request["parameters"]["barrier"] = barrier_offset_1
    
    try:
        ws_app.send(json.dumps(trade_request))
        print(f"💰 [TRADE] Sent {contract_type_param} | Barrier(s): {barrier_offset_1} / {barrier_offset_2 or ''} | Stake: {round(stake, 2):.2f} {currency}")
        current_data = get_session_data(email)
        current_data["is_in_trade"] = True
        save_session_data(email, current_data)
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass
        
def start_new_trade(email, current_data):
    """Initiates the two mixed contracts based on the current trend."""
    global BASE_CONTRACT_TYPE_1, BASE_CONTRACT_TYPE_2
    
    trend = analyze_trend(current_data['tick_history'])
    stake_to_use = current_data['current_stake']
    currency_to_use = current_data['currency']
    
    if trend == 'UPTREND':
        # Condition: If Uptrend (price went up over last 5 ticks) -> Enter NEGATIVE barriers
        notouch_barrier = "-0.1" # Notouch -0.1
        onetouch_barrier = "-0.2" # Onetouch -0.2
        trend_direction = "UP (Entered Negative Barriers)"
    elif trend == 'DOWNTREND':
        # Condition: If Downtrend (price went down over last 5 ticks) -> Enter POSITIVE barriers
        notouch_barrier = "+0.1" # Notouch +0.1
        onetouch_barrier = "+0.2" # Onetouch +0.2
        trend_direction = "DOWN (Entered Positive Barriers)"
    else:
        print("🟡 [ENTRY SKIP] Trend is flat or insufficient data (5 ticks required). Skipping trade.")
        return 

    # 1. Contract 1: NOTOUCH (+/- 0.1)
    # Note: NOTOUCH barrier is always relative to entry. We set barrier and barrier2 
    # based on the requested magnitude (0.1) but use the sign of notouch_barrier 
    # only for logging consistency.
    send_trade_order(
        email, 
        stake_to_use, 
        currency_to_use, 
        BASE_CONTRACT_TYPE_1, 
        notouch_barrier
    )
    
    # 2. Contract 2: ONETOUCH (+/- 0.2)
    send_trade_order(
        email, 
        stake_to_use, 
        currency_to_use, 
        BASE_CONTRACT_TYPE_2, 
        onetouch_barrier 
    )

    current_data['last_entry_time'] = int(time.time())
    current_data['last_entry_price'] = current_data['last_valid_tick_price']
    current_data["closed_contracts_pnl"] = 0.0
    current_data["closed_contracts_count"] = 0
    current_data["open_contract_ids"] = []
    
    save_session_data(email, current_data)
    
    entry_mode = f"Martingale Step {current_data['current_step']}" if current_data['current_step'] > 0 else "Base Entry @ Second 0"
    print(f"✅ [ENTRY @ {entry_mode}] Trend: {trend_direction}. Sent Contracts. Each Stake: {stake_to_use:.2f}")

def check_pnl_limits(email, contract_id, contract_profit):
    global TOTAL_CONTRACTS_PER_TRADE, MARTINGALE_STEPS, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    current_data["closed_contracts_pnl"] += contract_profit
    current_data["closed_contracts_count"] += 1
    
    if contract_id in current_data["open_contract_ids"]:
        current_data["open_contract_ids"].remove(contract_id)
        
    save_session_data(email, current_data)

    if current_data["closed_contracts_count"] < TOTAL_CONTRACTS_PER_TRADE:
        print(f"⌛ [PNL CHECK] Contract {contract_id} closed. Waiting for the remaining {TOTAL_CONTRACTS_PER_TRADE - current_data['closed_contracts_count']} contract(s)...")
        return
        
    total_profit_loss = current_data["closed_contracts_pnl"]
    last_stake = current_data['current_stake']
    current_data['current_profit'] += total_profit_loss
    
    current_data["closed_contracts_pnl"] = 0.0
    current_data["closed_contracts_count"] = 0
    current_data["open_contract_ids"] = []
    current_data["last_trade_closed_time"] = int(time.time())
    current_data["is_in_trade"] = False 
    
    if not current_data.get('is_running'): 
        save_session_data(email, current_data)
        return
        
    if total_profit_loss > 0:
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        save_session_data(email, current_data)
        
        print(f"✅ [WIN CYCLE] Both contracts closed (Total PNL: {total_profit_loss:.2f}). Resetting to base stake.")
        
    else:
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        # Check Stop Loss condition
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            stop_bot(email, clear_data=True, stop_reason="SL Reached")
            return
        
        new_stake = calculate_martingale_stake(
            current_data['base_stake'],
            last_stake,
            current_data['current_step']
        )
        
        current_data['current_stake'] = new_stake
        save_session_data(email, current_data)
        
        print(f"💸 [MARTINGALE] Lost cycle (Total PNL: {total_profit_loss:.2f}). Next stake calculated (x19): {new_stake:.2f}.")

    # Check Take Profit condition
    if current_data['current_profit'] >= current_data['tp_target']:
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
    
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}.")
    


def bot_core_logic(email, token, stake, tp, currency, account_type):
    
    global active_ws, RECONNECT_DELAY, ENTRY_SECOND
    
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, 
        "base_stake": stake, 
        "tp_target": tp,
        "is_running": True, 
        "current_stake": stake,
        "stop_reason": "Running",
        # Reset counters to base
        "consecutive_losses": 0,
        "current_step": 0,
        "total_wins": 0,
        "total_losses": 0,
        "current_profit": 0.0,
        
        "last_entry_time": 0,
        "last_trade_closed_time": 0,
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "currency": currency,
        "account_type": account_type,
        "open_contract_ids": [], 
        "open_prices": [],           
        "open_time": 0,              
        "closed_contracts_pnl": 0.0,
        "closed_contracts_count": 0,
        "last_valid_tick_price": 0.0,
        "tick_history": deque(maxlen=TREND_TICKS_COUNT), 
        "is_in_trade": False
    })
    save_session_data(email, session_data)

    def on_open_wrapper(ws_app):
        current_data = get_session_data(email) 
        ws_app.send(json.dumps({"authorize": current_data['api_token']}))
        ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        
        for contract_id in current_data.get('open_contract_ids', []):
            print(f"🔗 [RECONNECT] Resubscribing to Contract ID: {contract_id}")
            ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        
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
            current_price = float(data['tick']['quote'])
            current_epoch_time = int(data['tick']['epoch'])
            current_second = datetime.fromtimestamp(current_epoch_time, tz=timezone.utc).second
            
            current_data['last_valid_tick_price'] = current_price
            
            # 💡 New: Add tick to history
            current_data['tick_history'].append(current_price)
            save_session_data(email, current_data)
            
            # 1. Check Entry Condition (Second 0 and not currently in a trade)
            if current_second == ENTRY_SECOND and not current_data.get('is_in_trade'):
                if len(current_data['tick_history']) == TREND_TICKS_COUNT:
                    start_new_trade(email, current_data)
                else:
                    print(f"🟡 [ENTRY SKIP] Not enough tick data ({len(current_data['tick_history'])}/{TREND_TICKS_COUNT}) to determine trend. Skipping trade.")


        elif msg_type == 'buy':
            contract_id = data['buy']['contract_id']
            open_price = data['buy'].get('buy_price', 0.0)
            
            current_data['open_contract_ids'].append(contract_id) 
            current_data['open_prices'].append(open_price)
            save_session_data(email, current_data)
            
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
                contract_id = contract['contract_id']
                profit = contract['profit']
                
                check_pnl_limits(email, contract_id, profit)
                
                if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

    def on_close_wrapper(ws_app, code, msg):
        print(f"⚠ [PROCESS] WS closed for {email}. Reason: {code}/{msg}")

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
            
            ws.run_forever(ping_interval=20, ping_timeout=10)
            
        except Exception as e:
            print(f"❌ [ERROR] WebSocket failed for {email}: {e}")
        
        print(f"💤 [PROCESS] Connection lost. Retrying in {RECONNECT_DELAY} second...")
        time.sleep(RECONNECT_DELAY)
        
    print(f"🛑 [PROCESS] Bot process loop ended for {email}.")


# ==========================================================
# FLASK APP SETUP AND ROUTES (Unchanged)
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
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set strategy = base_contract_type_1 + "/" + base_contract_type_2 + " (Mixed Contracts Trend-Based | Martingale x" + martingale_multiplier|string + ")" %}

    <p class="status-running">✅ Bot is Running! (Strategy: {{ strategy_short }})</p>
    <p style="color:red; font-weight: bold;">⚠️ **Double Stake Mode:** Base Stake used for EACH contract (Total Risk = Base Stake x 2).</p>
    <p style="color:blue; font-weight: bold;">📊 Entry Condition: **Entry at Second 0** + Trend Analysis ({{ trend_ticks_count }} Ticks).</p>
    <p style="color:blue; font-weight: bold;">🎯 Contracts: **Notouch $\pm 0.1$ AND OneTouch $\pm 0.2$** (Trend-Based Barriers)</p>
    <p style="color:red; font-weight: bold;">⚠️ Martingale Multiplier: **x{{ martingale_multiplier|round(1) }}**</p>
    <p style="color:red; font-weight: bold;">⚠️ Max Consecutive Losses: **{{ max_consecutive_losses }}** (Max Steps: {{ martingale_steps }})</p>
    <p style="color:blue;">💡 Current Time Second: <span id="current-second">{{ current_second }}</span></p>
    <p style="color:red;">💡 Auto-Reconnect Delay: {{ reconnect_delay }} second.</p>
    
    {% if session_data.open_contract_ids %}
    <p style="color: #007bff; font-weight: bold;">Open Contracts: {{ session_data.open_contract_ids|length }} / {{ total_contracts_per_trade }}</p>
    <p class="contract-id">IDs: {{ session_data.open_contract_ids|join(', ') }}</p>
    {% else %}
    <p style="color: green; font-weight: bold;">Open Contracts: None</p>
    {% endif %}

    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake (Per Contract): {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Consecutive Losses: {{ max_consecutive_losses }})</p>
    <p>Stats: {{ session_data.total_wins }} Wins | {{ session_data.total_losses }} Losses</p>
    <p style="font-weight: bold; color: purple;">Last Tick Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
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
    function updateSecond() {
        var now = new Date();
        var seconds = now.getSeconds();
        var secondElement = document.getElementById('current-second');
        if (secondElement) {
            secondElement.textContent = seconds;
        }
    }

    function autoRefresh() {
        var isRunning = {{ 'true' if session_data and session_data.is_running else 'false' }};
        
        if (isRunning) {
            // Refresh every 1 second if running
            setTimeout(function() {
                window.location.reload();
            }, 1000); 
            // Also update the second count dynamically
            setInterval(updateSecond, 1000);
        }
    }
    
    updateSecond(); 
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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Running", "Displayed"]:
        reason = session_data["stop_reason"]
        
        if reason == "SL Reached": 
            flash(f"🛑 STOP: Max loss limit (Max {MAX_CONSECUTIVE_LOSSES} consecutive losses) reached! (SL Reached)", 'error')
        elif reason == "TP Reached": 
            flash(f"✅ GOAL: Take Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached)", 'success')
        elif reason.startswith("API Buy Error") or reason.startswith("WS Crash"): 
            flash(f"❌ Error: {reason}. Please check connection and restart the bot.", 'error')
            
        delete_session_data(email)
        
        return redirect(url_for('index')) 
        
    current_second = datetime.now(timezone.utc).second 

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        base_contract_type_1=BASE_CONTRACT_TYPE_1,
        base_contract_type_2=BASE_CONTRACT_TYPE_2,
        total_contracts_per_trade=TOTAL_CONTRACTS_PER_TRADE,
        martingale_multiplier=MARTINGALE_MULTIPLIER,
        duration=DURATION,
        trend_ticks_count=TREND_TICKS_COUNT,
        reconnect_delay=RECONNECT_DELAY,
        current_second=current_second,
        strategy_short=f"Notouch $\\pm 0.1$ / Touch $\\pm 0.2$ (Trend {TREND_TICKS_COUNT} Ticks | x19 Martingale)"
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
    global active_processes, MARTINGALE_MULTIPLIER, DURATION
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
    
    flash(f'Bot started successfully. Strategy: Trend-Based Notouch/Touch 5 Ticks (@ Second 0). Martingale (x{MARTINGALE_MULTIPLIER}).', 'success')
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

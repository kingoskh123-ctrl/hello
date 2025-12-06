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
import traceback 
from collections import Counter

# ==========================================================
# BOT CONSTANT SETTINGS (R_100 | Rise/Fall | Immediate Reversed Martingale x2.2 | 20 Ticks/4 Candles)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"      
DURATION = 5           # 💡 التعديل: مدة الصفقة 5 تيك
DURATION_UNIT = "t"    

# إعدادات المضاعفة والتحليل
TICK_SAMPLE_SIZE = 20            
CANDLE_SIZE = 5                  
MAX_CONSECUTIVE_LOSSES = 4       # 💡 التعديل: الحد الأقصى للخسائر 4
MARTINGALE_MULTIPLIER = 2.2      # 💡 التعديل: المضاعف x2.2
MAX_MARTINGALE_STEP = 4          # 💡 التعديل: الحد الأقصى لخطوات المضاعفة 4

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# ==========================================================
# GLOBAL STATE
# ==========================================================
active_processes = {}
active_ws = {}
is_contract_open = {} 
PROCESS_LOCK = Lock()
TRADE_LOCK = Lock() 

DEFAULT_SESSION_STATE = {
    "api_token": "",
    "base_stake": 0.35,              
    "tp_target": 10.0,
    "is_running": False,
    "current_profit": 0.0,
    "current_stake": 0.35,           
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
    
    "last_valid_tick_price": 0.0,
    "current_entry_id": None,                
    "open_contract_ids": [],                 
    "contract_profits": {},                  
    "last_trade_direction": "FALL",        
    "tick_prices_history": [0.0] * TICK_SAMPLE_SIZE, 
}

# --- Persistence functions ---
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
        if 'tick_prices_history' not in data or len(data['tick_prices_history']) != TICK_SAMPLE_SIZE: 
             data['tick_prices_history'] = [0.0] * TICK_SAMPLE_SIZE 
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
    global is_contract_open, active_processes, active_ws
    current_data = get_session_data(email)
    
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
    
    if stop_reason != "Running": save_session_data(email, current_data)

    with PROCESS_LOCK:
        if email in active_ws and active_ws[email]:
            try: active_ws[email].close() 
            except: pass
            del active_ws[email]

    with PROCESS_LOCK:
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                print(f"🛑 [INFO] Terminating Process for {email}...")
                process.terminate() 
                print(f"✅ [INFO] Process for {email} forcefully terminated.")
            
            del active_processes[email]

    if email in is_contract_open: is_contract_open[email] = False

    if clear_data:
        if stop_reason in ["SL Reached: Consecutive losses", "TP Reached", "API Buy Error", "Displayed"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        else:
            delete_session_data(email)
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    else:
        print(f"⚠ [INFO] WS closed for {email}. Attempting immediate reconnect.")
# --- End of Persistence and Control functions ---

# ==========================================================
# TRADING BOT FUNCTIONS
# ==========================================================

def calculate_martingale_stake(base_stake, current_step, multiplier):
    if current_step == 0:  
        return base_stake
    
    step = min(current_step, MAX_MARTINGALE_STEP)
    return base_stake * (multiplier ** step)

def get_opposite_direction(direction):
    """ يعكس اتجاه الصفقة """
    return "FALL" if direction == "RISE" else "RISE"

def send_rise_fall_order(email, stake, currency, direction):
    """ إرسال صفقة واحدة من نوع RISE أو FALL """
    global active_ws, DURATION, DURATION_UNIT, SYMBOL
    
    if email not in active_ws or active_ws[email] is None: 
        print(f"❌ [TRADE ERROR] Cannot send trade: WebSocket connection is inactive.")
        return False
        
    ws_app = active_ws[email]
    
    contract_type = direction 

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
        }
    }
    try:
        print(f"✅ [DEBUG] Sending BUY request for {contract_type} at {round(stake, 2):.2f}...")
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send {contract_type} order: {e}")
        return False


def apply_martingale_logic(email, entry_direction):
    """ منطق تسوية العقد وتحديث حالة المضاعفة (Immediate Reversed Martingale) """
    global is_contract_open
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return
    if len(current_data['contract_profits']) != 1:
        print("❌ [MARTINGALE ERROR] Only partial contract results found. Waiting for full settlement.")
        is_contract_open[email] = False
        return

    total_profit_loss = list(current_data['contract_profits'].values())[0]
    current_data['current_profit'] += total_profit_loss
    
    base_stake_used = current_data['base_stake']
    
    # Reset contract state
    current_data['current_entry_id'] = None
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    is_contract_open[email] = False # السماح بالدخول فوراً

    # ------------------- حالة الربح (Win) -------------------
    if total_profit_loss >= 0:
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        
        entry_result_tag = "WIN"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}.")
        
        # مسح سجل التيكات للبدء من جديد
        current_data['tick_prices_history'] = [0.0] * TICK_SAMPLE_SIZE
        
        if current_data['current_profit'] >= current_data['tp_target']:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason="TP Reached")
            return
            
    # ------------------- حالة الخسارة (Loss) -------------------
    else: 
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            # SL Reached - Stop and reset
            
            current_data['current_stake'] = current_data['base_stake']
            current_data['consecutive_losses'] = 0
            current_data['current_step'] = 0
            
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason="SL Reached: Consecutive losses")
            return
            
        # 1. Calculate new stake (Martingale Step)
        current_data['current_step'] = min(current_data['current_step'] + 1, MAX_MARTINGALE_STEP)
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        # 2. Get reversed direction
        reversed_direction = get_opposite_direction(entry_direction)
        current_data['last_trade_direction'] = reversed_direction # حفظ الاتجاه الجديد
        
        print(f"🔄 [LOSS - REVERSED MARTINGALE] PnL: {total_profit_loss:.2f}. Con. Loss: {current_data['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}. Next Stake: {round(new_stake, 2):.2f}. Reversing direction to {reversed_direction}.")

        # 3. IMMEDIATE RE-ENTRY
        
        current_data['current_entry_id'] = time.time()
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        is_contract_open[email] = True # قفل الإشارة حتى يتم تسوية الصفقة الجديدة

        if send_rise_fall_order(email, current_data['current_stake'], current_data['currency'], reversed_direction):
            pass
        else:
            stop_bot(email, clear_data=True, stop_reason="API Buy Error during Martingale")
        
    save_session_data(email, current_data)

def handle_contract_settlement(email, contract_id, profit_loss):
    current_data = get_session_data(email)
    
    if contract_id not in current_data['open_contract_ids']:
        return

    current_data['contract_profits'][contract_id] = profit_loss
    
    if contract_id in current_data['open_contract_ids']:
        current_data['open_contract_ids'].remove(contract_id)
        
    save_session_data(email, current_data)
    
    if not current_data['open_contract_ids'] and len(current_data['contract_profits']) == 1:
        entry_direction = current_data['last_trade_direction']
        apply_martingale_logic(email, entry_direction)

def get_candle_direction(prices):
    """ يحدد اتجاه الشمعة (Up/Down/Flat) بناءً على أول وآخر سعر في مجموعة 5 تيكات """
    if not prices or len(prices) != CANDLE_SIZE: return None
    # prices[0] هو السعر الأحدث (نهاية الشمعة)
    # prices[-1] هو السعر الأقدم (بداية الشمعة)
    if prices[0] > prices[-1]:
        return "UP"
    elif prices[0] < prices[-1]:
        return "DOWN"
    return "FLAT"


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Core bot logic """
    
    print(f"🚀🚀 [CORE START] Bot logic started for {email}. Checking settings...") 
    
    global is_contract_open, active_ws, TICK_SAMPLE_SIZE

    is_contract_open = {email: False}
    active_ws = {email: None}

    session_data = get_session_data(email)
    
    if session_data.get('consecutive_losses', 0) > MAX_CONSECUTIVE_LOSSES:
        session_data['consecutive_losses'] = 0
        session_data['current_step'] = 0
        session_data['current_stake'] = stake
        session_data['stop_reason'] = "SL State Cleared"
    
    initial_stake = session_data.get('current_stake', stake)
    if session_data.get('consecutive_losses') == 0:
        initial_stake = stake

    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": initial_stake, 
        "stop_reason": "Running", "last_entry_time": 0,
        "last_entry_price": 0.0, "last_tick_data": None, "currency": currency,
        "account_type": account_type, "last_valid_tick_price": 0.0,
        "current_entry_id": None, "open_contract_ids": [], "contract_profits": {},
        "last_trade_direction": "FALL", 
        "tick_prices_history": [0.0] * TICK_SAMPLE_SIZE,
    })
    save_session_data(email, session_data)

    try:
        while True:
            current_data = get_session_data(email)
            if not current_data.get('is_running'): break

            def on_open_wrapper(ws_app):
                current_data = get_session_data(email) 
                ws_app.send(json.dumps({"authorize": current_data['api_token']}))
                ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
                running_data = get_session_data(email)
                running_data['is_running'] = True
                save_session_data(email, running_data)
                print(f"✅ [PROCESS] Connection established for {email}.")
                
                if current_data['open_contract_ids']:
                    print(f"🔍 [RECOVERY CHECK] Found {len(current_data['open_contract_ids'])} contracts pending settlement. RE-SUBSCRIBING...")
                    is_contract_open[email] = True
                    for contract_id in current_data['open_contract_ids']:
                        if contract_id:
                            ws_app.send(json.dumps({
                                "proposal_open_contract": 1, 
                                "contract_id": contract_id, 
                                "subscribe": 1  
                            }))
                else:
                    is_contract_open[email] = False

            def on_message_wrapper(ws_app, message):
                data = json.loads(message)
                msg_type = data.get('msg_type')
                
                current_data = get_session_data(email)
                if not current_data.get('is_running'): return
                        
                if msg_type == 'tick':
                    try:
                        current_price = float(data['tick']['quote'])
                    except (KeyError, ValueError):
                        return
                        
                    # تحديث سجل الأسعار (أحدث سعر في البداية)
                    history = current_data.get('tick_prices_history', [0.0] * TICK_SAMPLE_SIZE)
                    history.insert(0, current_price) 
                    current_data['tick_prices_history'] = history[:TICK_SAMPLE_SIZE] 
                    
                    if len(history) < TICK_SAMPLE_SIZE or history[-1] == 0.0: 
                        save_session_data(email, current_data)
                        return

                    # 1. تحديد اتجاهات الشموع الأربعة (C1, C2, C3, C4)
                    candle_directions = []
                    for i in range(4): # 4 شموع
                        start_index = i * CANDLE_SIZE
                        end_index = start_index + CANDLE_SIZE
                        
                        # التيكات مرتبة من الأحدث (0) إلى الأقدم (19)
                        candle_prices = current_data['tick_prices_history'][start_index:end_index]
                        direction = get_candle_direction(candle_prices)
                        if direction == "FLAT":
                            candle_directions = []
                            break
                        candle_directions.append(direction)
                    
                    # 2. التحقق من شرط الدخول (النمط المزدوج)
                    if len(candle_directions) == 4:
                        C1, C2, C3, C4 = candle_directions[0], candle_directions[1], candle_directions[2], candle_directions[3]

                        # النمط الأول: Up, Down, Up, Down (C4 هو Down)
                        pattern_1_met = (
                            C1 == "UP" and C2 == "DOWN" and 
                            C3 == "UP" and C4 == "DOWN"
                        )

                        # النمط الثاني: Down, Up, Down, Up (C4 هو Up)
                        pattern_2_met = (
                            C1 == "DOWN" and C2 == "UP" and 
                            C3 == "DOWN" and C4 == "UP"
                        )
                        
                        pattern_met = pattern_1_met or pattern_2_met
                    else:
                        pattern_met = False
                    
                    current_data['last_valid_tick_price'] = current_price
                    current_data['last_tick_data'] = data['tick']
                    
                    
                    if not is_contract_open.get(email):
                        
                        if pattern_met:
                            
                            if pattern_1_met:
                                # C4 هو DOWN -> الدخول FALL
                                entry_direction = "FALL" 
                            elif pattern_2_met:
                                # C4 هو UP -> الدخول RISE
                                entry_direction = "RISE"

                            print(f"📊 [ENTRY CONDITION MET] Pattern: {C1}, {C2}, {C3}, {C4}. Entering {entry_direction} (Initial Trade).")
                            
                            # --- منطق بدء الصفقة الجديدة ---
                            stake = current_data['current_stake']
                            currency_to_use = current_data['currency']
                            
                            if current_data.get('consecutive_losses', 0) > MAX_CONSECUTIVE_LOSSES:
                                stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Max {MAX_CONSECUTIVE_LOSSES} Consecutive Losses reached.")
                                save_session_data(email, current_data)
                                return

                            current_data['current_entry_id'] = time.time()
                            current_data['open_contract_ids'] = []
                            current_data['contract_profits'] = {}
                            current_data['last_trade_direction'] = entry_direction # حفظ اتجاه الدخول

                            if send_rise_fall_order(email, stake, currency_to_use, entry_direction):
                                pass
                                
                            is_contract_open[email] = True

                            current_data['last_entry_time'] = int(time.time())
                            current_data['last_entry_price'] = current_data.get('last_valid_tick_price', 0.0)
                            # -------------------------------

                            current_data = get_session_data(email) 
                            
                    save_session_data(email, current_data) 

                elif msg_type == 'buy':
                    contract_id = data['buy']['contract_id']
                    current_data['open_contract_ids'] = [contract_id]
                    current_data['contract_profits'] = {} 
                    save_session_data(email, current_data)
                    
                    ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                    
                elif 'error' in data:
                    error_message = data['error'].get('message', 'Unknown Error')
                    print(f"❌❌ [API ERROR] Message: {error_message}. Trade failed.")
                    
                    if current_data['current_entry_id'] is not None:
                        time.sleep(1) 
                        is_contract_open[email] = False 
                        current_data['current_entry_id'] = None
                        save_session_data(email, current_data)
                        stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: {error_message}")


                elif msg_type == 'proposal_open_contract':
                    contract = data['proposal_open_contract']
                    if contract.get('is_sold') == 1:
                        contract_id = contract['contract_id']
                        handle_contract_settlement(email, contract_id, contract['profit'])
                        
                        if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

            def on_close_wrapper(ws_app, code, msg):
                print(f"⚠ [PROCESS] WS closed for {email}. RECONNECTING IMMEDIATELY.")
                is_contract_open[email] = False 

            def on_error_wrapper(ws_app, err):
                print(f"❌ [WS Critical Error {email}] {err}") 

            try:
                ws = websocket.WebSocketApp(
                    WSS_URL, on_open=on_open_wrapper, on_message=on_message_wrapper,
                    on_error=on_error_wrapper, 
                    on_close=on_close_wrapper
                )
                active_ws[email] = ws
                ws.run_forever(ping_interval=10, ping_timeout=5) 
                
            except Exception as e:
                print(f"❌ [ERROR] WebSocket failed for {email}: {e}")
            
            if get_session_data(email).get('is_running') is False: break
            
            print(f"💤 [PROCESS] Immediate Retrying connection for {email}...")
            time.sleep(0.5) 

        print(f"🛑 [PROCESS] Bot process loop ended for {email}.")
        
    except Exception as process_error:
        print(f"\n\n💥💥 [CRITICAL PROCESS CRASH] The entire bot process for {email} failed with an unhandled exception: {process_error}")
        traceback.print_exc()
        stop_bot(email, clear_data=True, stop_reason="Critical Python Crash")

# --- (FLASK APP SETUP AND ROUTES) ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False

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
    {# 💡 تحديث وصف الاستراتيجية #}
    {% set strategy = 'Rise/Fall (5 Ticks) | Entry: Reversed Candle Pattern (2 Patterns) on 20 Ticks | Immediate REVERSED Martingale x' + martingale_multiplier|string + ' (Max ' + max_consecutive_losses|string + ' Losses, Max Step ' + max_martingale_step|string + ')' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p style="font-weight: bold; color: {% if session_data.consecutive_losses > 0 %}red{% else %}green{% endif %};">
        Consecutive Losses: {{ session_data.consecutive_losses }} / {{ max_consecutive_losses }} 
        (Last Direction: {{ session_data.last_trade_direction }})
    </p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
    <p style="font-weight: bold; color: #ff5733;">Contracts Open: {{ session_data.open_contract_ids|length }}</p>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: red; color: white;">🛑 Stop Bot</button>
    </form>
{% else %}
    <p class="status-stopped">🛑 Bot is Stopped. Enter settings to start a new session.</p>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: #ff5733; color: white;">🧹 Force Stop & Clear Session</button>
        <input type="hidden" name="force_stop" value="true">
    </form>
    <hr>
    
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
        var refreshInterval = 1000; // 1000ms = 1 second
        
        if (isRunning) {
            setTimeout(function() {
                window.location.reload();
            }, refreshInterval);
        }
    }

    autoRefresh();
</script>
"""

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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)", "Displayed", "SL State Cleared"]:
        reason = session_data["stop_reason"]
        
        if reason.startswith("SL Reached"): flash(f"🛑 STOP: Max loss reached! ({reason})", 'error')
        elif reason == "TP Reached": flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached)", 'success')
        elif reason.startswith("API Buy Error"): flash(f"❌ API Error: {reason}. Check your token and account status.", 'error')
            
        session_data['stop_reason'] = "Displayed"
        save_session_data(email, session_data)
    
    contract_type_name = "Rise/Fall (4 Reversed Candles, 20 Ticks)"

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        max_martingale_step=MAX_MARTINGALE_STEP,
        duration=DURATION,
        tick_sample_size=TICK_SAMPLE_SIZE,
        symbol=SYMBOL,
        contract_type_name=contract_type_name
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
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                flash('Bot is already running. Please stop it manually first.', 'info')
                return redirect(url_for('index'))
            else:
                 # تنظيف أي عملية غير حية عالقة
                del active_processes[email]

    try:
        account_type = request.form['account_type']
        currency = "USD" if account_type == 'demo' else "tUSDT"
        token = request.form['token']
        stake = float(request.form['stake'])
        if stake < 0.35: raise ValueError("Stake too low")
        tp = float(request.form['tp'])
    except ValueError:
        flash("Invalid stake or TP value (Base Stake must be >= 0.35).", 'error')
        return redirect(url_for('index'))
    
    current_data = get_session_data(email)
    
    if current_data.get('stop_reason') == "SL Reached: Consecutive losses" or current_data.get('consecutive_losses', 0) > MAX_CONSECUTIVE_LOSSES:
        print(f"⚠️ [SL DETECTED] Resetting state before starting after SL was hit.")
        current_data['consecutive_losses'] = 0
        current_data['current_step'] = 0
        current_data['current_stake'] = stake 
        current_data['stop_reason'] = "SL State Cleared" 
        save_session_data(email, current_data)
        flash("🛑 تم إيقاف البوت مسبقًا بسبب تجاوز حد الخسارة (Stop Loss). تمت إعادة ضبط حالته إلى الأساس لبدء جولة جديدة.", 'error')


    process = Process(target=bot_core_logic, args=(email, token, stake, tp, currency, account_type))
    process.daemon = True
    process.start()
    
    with PROCESS_LOCK: active_processes[email] = process
    
    # 💡 تحديث وصف الاستراتيجية في رسالة Flash
    flash(f'Bot started successfully. Strategy: Rise/Fall (5 Ticks) on Reversed Candle Pattern / Immediate REVERSED Martingale x{MARTINGALE_MULTIPLIER} (Max {MAX_CONSECUTIVE_LOSSES} Losses, Max Step {MAX_MARTINGALE_STEP}).', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session: return redirect(url_for('auth_page'))
    
    email = session['email']
    is_force_stop = request.form.get('force_stop') == 'true'

    stop_bot(email, clear_data=True, stop_reason="Stopped Manually")
    
    if is_force_stop:
        flash('Session state forcefully cleared and process terminated.', 'success')
    else:
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

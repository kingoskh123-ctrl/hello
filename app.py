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
# BOT CONSTANT SETTINGS (R_100 | Single Digit Diff | Martingale)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"       
DURATION = 1           # مدة الصفقة 1 تيك
DURATION_UNIT = "t"    

# 💡 إعدادات التحليل (تعديل ليتناسب مع شرط 2 تيك)
TICK_SAMPLE_SIZE = 2           
MIN_REPETITION_COUNT = 0       # لم يعد يُستخدم، لكن نتركه 0

# إعدادات المضاعفة
MAX_CONSECUTIVE_LOSSES = 2     
MARTINGALE_MULTIPLIER = 14.0   
MAX_MARTINGALE_STEP = 1        

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
    "last_digits_history": [9] * TICK_SAMPLE_SIZE, 
    "last_barrier_digit": None 
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
        if 'last_digits_history' not in data or len(data['last_digits_history']) != TICK_SAMPLE_SIZE: 
             data['last_digits_history'] = [9] * TICK_SAMPLE_SIZE
        if 'last_barrier_digit' not in data:
            data['last_barrier_digit'] = None
            
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
        
    save_session_data(email, current_data)

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
        # عند الوقف التلقائي (SL/TP/API Error)، نترك البيانات محفوظة مؤقتاً لـ Flask index لتقوم بحذفها وإعادة التوجيه
        if stop_reason in ["SL Reached: Consecutive losses", "TP Reached", "API Buy Error"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept temporarily for display.")
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
    """ حساب قيمة الـ Stake في نظام المضاعفة """
    if current_step == 0:  
        return base_stake
    
    if current_step == 1:
        return base_stake * multiplier
        
    return base_stake 


def send_single_trade_order(email, stake, currency, contract_type, barrier):
    global active_ws, DURATION, DURATION_UNIT, SYMBOL
    
    if email not in active_ws or active_ws[email] is None: 
        print(f"❌ [TRADE ERROR] Cannot send trade: WebSocket connection is inactive.")
        return False
        
    ws_app = active_ws[email]
    
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
            "barrier": barrier 
        }
    }
    try:
        print(f"✅ [DEBUG] Sending BUY request for {contract_type} Barrier {barrier} at {round(stake, 2):.2f}...")
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send {contract_type} order: {e}")
        return False


def apply_martingale_logic(email):
    """ منطق المضاعفة (Single Trade Martingale) """
    global is_contract_open, MARTINGALE_MULTIPLIER, MAX_CONSECUTIVE_LOSSES, MAX_MARTINGALE_STEP
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    if len(current_data['contract_profits']) < 1 or current_data['open_contract_ids']:
        is_contract_open[email] = False 
        return

    total_profit_loss = sum(current_data['contract_profits'].values())
    current_data['current_profit'] += total_profit_loss
    base_stake_used = current_data['base_stake']
    
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=False, stop_reason="TP Reached") 
        return
        
    
    # ❌ حالة الخسارة (Loss)
    if total_profit_loss < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        entry_result_tag = "LOSS"
        
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=False, stop_reason=f"SL Reached: Consecutive losses") 
            return
            
        current_data['current_step'] = min(current_data['current_step'] + 1, MAX_MARTINGALE_STEP)
            
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        print(f"🔄 [LOSS - MARTINGALE] PnL: {total_profit_loss:.2f}. Con. Loss: {current_data['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}. Step: {current_data['current_step']}/{MAX_MARTINGALE_STEP}. Next Stake calculated: {round(new_stake, 2):.2f}. (Using previous barrier {current_data['last_barrier_digit']}).")
        
    # ✅ حالة الربح (Win)
    else: 
        current_data['total_wins'] += 1 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        # عند الربح، نعيد تعيين حاجز المضاعفة ليتم البحث عن إشارة جديدة
        current_data['last_barrier_digit'] = None 
        entry_result_tag = "WIN"
        
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}.")

    # مسح بيانات العقد وإعادة فتح الباب للدخول (للمضاعفة الفورية)
    current_data['current_entry_id'] = None
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    save_session_data(email, current_data) 
    
    is_contract_open[email] = False # السماح بالدخول فوراً في التيك التالي


def handle_contract_settlement(email, contract_id, profit_loss):
    current_data = get_session_data(email)
    
    if contract_id not in current_data['open_contract_ids']:
        return

    current_data['contract_profits'][contract_id] = profit_loss
    
    if contract_id in current_data['open_contract_ids']:
        current_data['open_contract_ids'].remove(contract_id)
        
    save_session_data(email, current_data)
    
    if not current_data['open_contract_ids'] and len(current_data['contract_profits']) == 1: 
        apply_martingale_logic(email)


def start_new_diff_trade(email, repeated_digit):
    """ إرسال صفقة DIGITDIFF ومسح سجل التيكات بعد الدخول الناجح """
    global is_contract_open, TICK_SAMPLE_SIZE
    
    current_data = get_session_data(email)
    stake = current_data['current_stake']
    currency_to_use = current_data['currency']
    
    if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES:
          stop_bot(email, clear_data=False, stop_reason=f"SL Reached: Consecutive losses") 
          return
    
    # تحديد الحاجز: إذا كنا في مرحلة المضاعفة، نستخدم الحاجز المخزن. وإلا، نستخدم الحاجز الجديد
    if current_data['consecutive_losses'] > 0 and current_data['last_barrier_digit'] is not None:
        barrier_to_use = current_data['last_barrier_digit']
        entry_source = "Martingale (Previous Loss)"
    elif repeated_digit is not None: # Base Stake Entry
        barrier_to_use = repeated_digit # في هذه الاستراتيجية يكون 9
        entry_source = f"Base Stake (New Signal: 9 + <6)"
    else:
        return

    current_data['current_entry_id'] = time.time()
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    contract_type = "DIGITDIFF"
    barrier = barrier_to_use
    
    entry_mode = "Base Stake" if current_data['consecutive_losses'] == 0 else f"Martingale Step {current_data['current_step']}"
    print(f"🧠 [SINGLE ENTRY - {entry_mode}] Source: {entry_source}. Stake: {round(stake, 2):.2f}. Barrier: {barrier}. Repetition Digit: {repeated_digit if repeated_digit is not None else 'N/A'}.")
    
    if send_single_trade_order(email, stake, currency_to_use, contract_type, barrier): 
        
        # حفظ الحاجز أولاً، ثم مسح سجل التيكات
        current_data['last_barrier_digit'] = barrier
        current_data['last_digits_history'] = [9] * TICK_SAMPLE_SIZE 
        print(f"🧹 [RESET] Tick history wiped. Bot waiting for {TICK_SAMPLE_SIZE} new ticks.")

        is_contract_open[email] = True 

        current_data['last_entry_time'] = int(time.time())
        current_data['last_entry_price'] = current_data.get('last_valid_tick_price', 0.0)

        save_session_data(email, current_data)
        return
        
    is_contract_open[email] = False 
    save_session_data(email, current_data) 


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Core bot logic """
    
    print(f"🚀🚀 [CORE START] Bot logic started for {email}. Checking settings...") 
    
    global is_contract_open, active_ws, TICK_SAMPLE_SIZE

    is_contract_open = {email: False}
    active_ws = {email: None}

    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": stake, "stop_reason": "Running", "last_entry_time": 0,
        "last_entry_price": 0.0, "last_tick_data": None, "currency": currency,
        "account_type": account_type, "last_valid_tick_price": 0.0,
        "current_entry_id": None, "open_contract_ids": [], "contract_profits": {},
        "last_digits_history": [9] * TICK_SAMPLE_SIZE,
    })
    
    if session_data['consecutive_losses'] > 0:
        step = session_data['consecutive_losses']
        current_stake = calculate_martingale_stake(stake, step, MARTINGALE_MULTIPLIER)
        session_data['current_stake'] = current_stake
        session_data['current_step'] = step
        print(f"🔍 [RECOVERY] Resuming in Martingale mode (Step {step}). Stake: {current_stake:.2f}. Using stored barrier {session_data['last_barrier_digit']}.")
    else:
        session_data['last_barrier_digit'] = None 

    save_session_data(email, session_data)

    try:
        while True:
            current_data = get_session_data(email)
            
            if not current_data.get('is_running'): break

            print(f"🔗 [PROCESS] Attempting to connect for {email} ({account_type.upper()}/{currency})...")

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
                        
                    T_new = int(str(current_price)[-1])
                    
                    history = current_data.get('last_digits_history', [9] * TICK_SAMPLE_SIZE)
                    history.insert(0, T_new) 
                    current_data['last_digits_history'] = history[:TICK_SAMPLE_SIZE] 
                    
                    
                    current_data['last_valid_tick_price'] = current_price
                    current_data['last_tick_data'] = data['tick']
                    
                    
                    # 2. التحقق من شرط الدخول (Martingale Pure Logic)
                    if not is_contract_open.get(email) and current_data['consecutive_losses'] < MAX_CONSECUTIVE_LOSSES:
                        
                        barrier_to_use = None
                        
                        # 💡 1. إذا كنا في مرحلة المضاعفة (خسارة سابقة): ندخل فوراً بالرقم المخزن
                        if current_data['consecutive_losses'] > 0 and current_data['last_barrier_digit'] is not None:
                            start_new_diff_trade(email, repeated_digit=None) 
                            current_data = get_session_data(email) 
                            
                        # 💡 2. إذا كنا في مرحلة الـ Base Stake: البحث عن إشارة (9) + (<6)
                        elif current_data['consecutive_losses'] == 0:
                            if len(history) < TICK_SAMPLE_SIZE: 
                                save_session_data(email, current_data)
                                return 

                            # T1_last_digit هو آخر تيك (الأحدث)
                            T1_last_digit = history[0]
                            # T2_last_digit هو التيك الذي سبقه (الأقدم)
                            T2_last_digit = history[1]
                            
                            # الشرط الجديد: التيك الأحدث هو 9، والتيك الذي سبقه أصغر من 6
                            if T1_last_digit == 9 and T2_last_digit < 6:
                                barrier_to_use = 9 # الحاجز الثابت هو 9
                            
                            if barrier_to_use is not None:
                                start_new_diff_trade(email, repeated_digit=barrier_to_use)
                                current_data = get_session_data(email) 
                            
                    save_session_data(email, current_data) 

                elif msg_type == 'buy':
                    contract_id = data['buy']['contract_id']
                    current_data['open_contract_ids'].append(contract_id)
                    save_session_data(email, current_data)
                    
                    ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                    
                elif 'error' in data:
                    error_message = data['error'].get('message', 'Unknown Error')
                    print(f"❌❌ [API ERROR] Message: {error_message}. Trade failed.")
                    
                    if current_data['current_entry_id'] is not None:
                        time.sleep(1) 
                        is_contract_open[email] = False 
                        
                        current_data['contract_profits'][f"error-{time.time()}"] = -current_data['current_stake']
                        apply_martingale_logic(email)
                        
                        # هنا يتم وقف البوت في الـ Multiprocess
                        stop_bot(email, clear_data=False, stop_reason=f"API Buy Error: {error_message}")


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
        stop_bot(email, clear_data=False, stop_reason="Critical Python Crash")

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
    {% set strategy = 'Pure Martingale (x' + martingale_multiplier|string + ') | Base Entry: DIGITDIFF on (Last Digit 9 + Previous Digit < 6) | Barrier: 9 | Subsequent Entries: Same Barrier as Loss' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Consecutive Losses: {{ session_data.consecutive_losses }} / {{ max_consecutive_losses }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
    <p style="font-weight: bold; color: #ff5733;">Contracts Open: {{ session_data.open_contract_ids|length }}</p>
    <p style="font-weight: bold; color: orange;">Last Barrier Digit: {{ session_data.last_barrier_digit if session_data.last_barrier_digit is not none else 'N/A' }}</p>
    
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
        
        // منع إعادة تحميل الصفحة إذا كانت متوقفة وتنتظر الإعدادات الجديدة
        var isStoppedForSettings = {{ 'true' if not session_data.is_running and session_data.stop_reason not in ["Running", "Disconnected (Auto-Retry)", "Displayed"] else 'false' }};
        
        if (isRunning || isStoppedForSettings) {
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
    
    # 💡 منطق التحقق من الوقف التلقائي وإعادة التوجيه
    if not session_data.get('is_running') and session_data.get("stop_reason") not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)", "Displayed"]:
        reason = session_data["stop_reason"]
        
        if reason.startswith("SL Reached"): 
            flash(f"🛑 STOP: Max loss reached! ({reason})", 'error')
            delete_session_data(email) # 🗑️ مسح البيانات بالكامل
            return redirect(url_for('index')) # إعادة توجيه لعرض صفحة الإعدادات
            
        elif reason == "TP Reached": 
            flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully!", 'success')
            delete_session_data(email) # 🗑️ مسح البيانات بالكامل
            return redirect(url_for('index')) # إعادة توجيه لعرض صفحة الإعدادات
            
        elif reason.startswith("API Buy Error"): 
            flash(f"❌ API Error: {reason}. Check your token and account status.", 'error')
            delete_session_data(email) # 🗑️ مسح البيانات بالكامل
            return redirect(url_for('index')) # إعادة توجيه لعرض صفحة الإعدادات
            
        # إذا لم يكن أحد الأسباب أعلاه (قد يكون سبب قديم تم عرضه سابقاً)
        session_data['stop_reason'] = "Displayed"
        save_session_data(email, session_data)
    
    contract_type_name = "Single DIGITDIFF"

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        max_martingale_step=MAX_MARTINGALE_STEP,
        min_repetition_count=MIN_REPETITION_COUNT,
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
    
    strategy_desc = 'Pure Martingale (x14.0) | Base Entry: DIGITDIFF on (Last Digit 9 + Previous Digit < 6) | Barrier: 9 | Subsequent Entries: Same Barrier as Loss.'
    flash(f'Bot started successfully. Strategy: {strategy_desc}', 'success')
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
        # عند البدء، نوقف أي عملية سابقة ونمسح بياناتها مباشرة
        delete_session_data(email)
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

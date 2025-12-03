import time
import json
import websocket
import os
import sys
from flask import Flask, request, render_template_string, redirect, url_for, session, flash
from datetime import datetime, timezone
from multiprocessing import Process
from threading import Lock

# ==========================================================
# BOT CONSTANT SETTINGS (R_100 | x6 | Last Digit 5 Trigger)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 5
DURATION_UNIT = "t"

# إعدادات المضاعفة والمخاطر
MARTINGALE_STEPS = 2               # الحد الأقصى لخطوات المضاعفة (الخطوة الأساسية + خطوتين مضاعفة)
MAX_CONSECUTIVE_LOSSES = 3         # الحد الأقصى لعدد الخسائر المتتالية قبل التوقف
MARTINGALE_MULTIPLIER = 6.0        # معامل المضاعفة الجديد (x6)

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# أنواع العقود Digit Over/Under
CONTRACT_TYPE_OVER = "DIGITOVER"
CONTRACT_TYPE_UNDER = "DIGITUNDER"
TARGET_DIGIT_OVER = 5
TARGET_DIGIT_UNDER = 4
ENTRY_DIGIT_TRIGGER = 5 # الرقم الذي يفعل الدخول (التيك الأخير = 5)

# ==========================================================

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
    "last_entry_price": 0.0,
    "last_tick_data": None,
    "currency": "USD",
    "account_type": "demo",
    
    "last_valid_tick_price": 0.0,
    "current_entry_id": None,
    "open_contract_ids": [], # قائمة لمعرفات العقود المفتوحة (للمرونة)
    "contract_profits": {},
    "last_barrier_value": str(ENTRY_DIGIT_TRIGGER)
}

# --- Persistence functions (Loading/Saving session data) ---
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
    
    with PROCESS_LOCK:
        if email in active_ws and active_ws[email]:
            try: active_ws[email].close() 
            except: pass
            del active_ws[email]

    if email in is_contract_open: is_contract_open[email] = False

    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
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
    """ منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة لعدد الخطوات """
    if current_step == 0: 
        return base_stake
    
    # يتم حساب الرهان الجديد بناءً على الرهان الأساسي (base_stake)
    return base_stake * (multiplier ** current_step)


def send_trade_order(email, stake, currency, contract_type_param, target_digit):
    """ إرسال طلب شراء واحد لعقود DIGIT OVER/UNDER """
    global active_ws, DURATION, DURATION_UNIT, SYMBOL
    
    if email not in active_ws or active_ws[email] is None: 
        print(f"❌ [TRADE ERROR] Cannot send trade: WebSocket connection is inactive.")
        return None
        
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
            "digit": target_digit  # تحديد الرقم المستهدف
        }
    }
    
    try:
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        return False


def apply_martingale_logic(email):
    """ يطبق منطق المضاعفة بناءً على نتائج الصفقتين المزدوجتين (Over/Under) """
    global is_contract_open, MARTINGALE_MULTIPLIER, MARTINGALE_STEPS, MAX_CONSECUTIVE_LOSSES, TARGET_DIGIT_OVER, TARGET_DIGIT_UNDER
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    results = list(current_data['contract_profits'].values())
    
    if not results or len(results) < 2:
        print("❌ [MARTINGALE ERROR] Incomplete results. Resetting stake to base.")
        total_profit = 0
    else:
        total_profit = sum(results)

    current_data['current_profit'] += total_profit
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
    
    base_stake_used = current_data['base_stake']
    
    # ❌ Double Loss Condition (خسارة مزدوجة)
    if total_profit < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1 # الانتقال إلى الخطوة التالية (مضاعفة)
        
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: {MAX_CONSECUTIVE_LOSSES} consecutive losses.")
            return
            
        if current_data['current_step'] > MARTINGALE_STEPS: 
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Exceeded {MARTINGALE_STEPS} Martingale steps.")
            return
        
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        
        current_data['current_stake_lower'] = new_stake
        current_data['current_stake_higher'] = new_stake 
        
        entry_tag = f"WAITING @ DIGIT {ENTRY_DIGIT_TRIGGER}"
        
        print(f"🔄 [DOUBLE LOSS] PnL: {total_profit:.2f}. Step {current_data['current_step']}. Next Stake ({MARTINGALE_MULTIPLIER}^{current_data['current_step']}) calculated: {round(new_stake, 2):.2f}. **Waiting for Digit {ENTRY_DIGIT_TRIGGER}.**")
        
    # ✅ Win or Split/Draw Condition (فوز أو تعادل)
    else: 
        current_data['total_wins'] += 1 if total_profit > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        
        current_data['current_stake_lower'] = base_stake_used
        current_data['current_stake_higher'] = base_stake_used 
        
        entry_result_tag = "WIN" if total_profit > 0 else "SPLIT/DRAW"
        entry_tag = f"WAITING @ DIGIT {ENTRY_DIGIT_TRIGGER}"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {base_stake_used:.2f}.")

    # إعادة تعيين متغيرات الدخول المزدوج
    current_data['current_entry_id'] = None
    current_data['contract_profits'] = {}
    
    # إزالة علامة is_contract_open للسماح بالدخول عند التيك التالي
    is_contract_open[email] = False 

    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Stake: {current_data['current_stake_lower']:.2f}, Strategy: DUAL D/OVER {TARGET_DIGIT_OVER} / D/UNDER {TARGET_DIGIT_UNDER} | Next Entry: {entry_tag}")
    
    save_session_data(email, current_data)
    
    
def handle_contract_settlement(email, contract_id, profit_loss):
    """ معالجة نتيجة عقد واحد وتجميعها مع العقد الآخر """
    current_data = get_session_data(email)
    
    # التحقق مما إذا كان العقد ضمن العقود المفتوحة التي نراقبها
    if contract_id not in current_data['open_contract_ids']:
        return

    current_data['contract_profits'][contract_id] = profit_loss
    
    # إزالة العقد من قائمة العقود المفتوحة المحفوظة
    if contract_id in current_data['open_contract_ids']:
        current_data['open_contract_ids'].remove(contract_id)
        
    save_session_data(email, current_data)
    
    if not current_data['open_contract_ids']:
        # إذا أصبحت القائمة فارغة، تم تسوية جميع العقود في الدخول المزدوج
        apply_martingale_logic(email)


def start_new_dual_trade(email):
    """ يرسل الصفقتين المزدوجتين في وقت واحد (Digit Over 5 و Digit Under 4) """
    global is_contract_open, CONTRACT_TYPE_OVER, CONTRACT_TYPE_UNDER, TARGET_DIGIT_OVER, TARGET_DIGIT_UNDER, MARTINGALE_STEPS
    
    current_data = get_session_data(email)
    stake_under = current_data['current_stake_lower']
    stake_over = current_data['current_stake_higher']
    currency_to_use = current_data['currency']
    
    # تحقق من تجاوز خطوات المضاعفة قبل الدخول
    if current_data['current_step'] > MARTINGALE_STEPS:
         stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Max {MARTINGALE_STEPS} Martingale steps reached.")
         return
         
    current_data['current_entry_id'] = time.time()
    current_data['open_contract_ids'] = [] # يتم تفريغها عند كل محاولة دخول
    current_data['contract_profits'] = {}
    
    entry_type_tag = "BASE ENTRY" if current_data['current_step'] == 0 else f"MARTINGALE STEP {current_data['current_step']}"
    
    print(f"🧠 [DUAL OVER/UNDER ENTRY - TICK LOGIC] {entry_type_tag} | Stake: {round(stake_under, 2):.2f}. Strategy: Over {TARGET_DIGIT_OVER} / Under {TARGET_DIGIT_UNDER}")
    
    # طلب Digit Over 5
    if send_trade_order(email, stake_over, currency_to_use, CONTRACT_TYPE_OVER, TARGET_DIGIT_OVER):
        pass
    
    # طلب Digit Under 4
    if send_trade_order(email, stake_under, currency_to_use, CONTRACT_TYPE_UNDER, TARGET_DIGIT_UNDER):
        pass
        
    is_contract_open[email] = True
    
    current_data['last_entry_time'] = int(time.time())
    current_data['last_entry_price'] = current_data.get('last_valid_tick_price', 0.0)

    save_session_data(email, current_data)


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Core bot logic """
    
    global is_contract_open, active_ws

    is_contract_open = {email: False}
    active_ws = {email: None}

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
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "currency": currency,
        "account_type": account_type,
        "last_valid_tick_price": 0.0,
        
        "current_entry_id": None, 
        "open_contract_ids": [], # يتم إعادة تعيينها هنا عند البدء
        "contract_profits": {},
        "last_barrier_value": str(ENTRY_DIGIT_TRIGGER)
    })
    save_session_data(email, session_data)

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
            is_contract_open[email] = False
            
            # منطق استعادة العقود المفتوحة
            if current_data.get('open_contract_ids'):
                is_contract_open[email] = True # لمنع الدخول الجديد حتى يتم تسوية العقود
                print(f"⚠️ [RECOVERY] Found {len(current_data['open_contract_ids'])} open contracts in session file. Monitoring them now.")
                
                for contract_id in current_data['open_contract_ids']:
                    # نرسل طلب مراقبة (Subscription) لكل عقد مفتوح
                    ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))

        def on_message_wrapper(ws_app, message):
            data = json.loads(message)
            msg_type = data.get('msg_type')
            
            current_data = get_session_data(email)
            if not current_data.get('is_running'): return
                
            if msg_type == 'tick':
                current_price = float(data['tick']['quote'])
                # tick_epoch = data['tick']['epoch']  # لم نعد نستخدمه
                
                # استخراج الرقم الأخير
                last_digit = -1 
                try:
                    price_str = str(current_price)
                    if '.' in price_str:
                        # الحصول على آخر رقم بعد النقطة
                        last_digit = int(price_str.split('.')[-1][-1]) 
                    else:
                        last_digit = int(price_str[-1])
                except Exception:
                    pass
                
                
                current_data['last_valid_tick_price'] = current_price
                current_data['last_tick_data'] = data['tick']
                
                save_session_data(email, current_data) 
                
                # === منطق الدخول الجديد: الدخول عندما يكون الرقم الأخير = 5 ===
                if not is_contract_open.get(email):
                    if last_digit == ENTRY_DIGIT_TRIGGER:
                        start_new_dual_trade(email)
                        print(f"✅ [ENTRY TRIGGER] Last Digit = {ENTRY_DIGIT_TRIGGER}. Starting Dual Trade.")
                        
                # === نهاية منطق الدخول ===

            elif msg_type == 'buy':
                contract_id = data['buy']['contract_id']
                
                # حفظ الـ contract_id في قائمة المفتوحة
                if current_data['current_entry_id'] is not None and contract_id not in current_data['open_contract_ids']:
                    current_data['open_contract_ids'].append(contract_id)
                
                save_session_data(email, current_data)
                
                ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                
            elif 'error' in data:
                error_code = data['error'].get('code', 'N/A')
                error_message = data['error'].get('message', 'Unknown Error')
                print(f"❌❌ [API ERROR] Code: {error_code}, Message: {error_message}. **Dual trade may be disrupted.**")
                
                if current_data['current_entry_id'] is not None and is_contract_open.get(email):
                    time.sleep(1) 
                    # إذا لم يتم فتح أي عقود، نفترض فشل الصفقة ونطبق منطق المضاعفة
                    if not current_data['open_contract_ids']: 
                        apply_martingale_logic(email)
                    else: 
                        print("⚠️ [TRADE FAILURE] Waiting for the other contract's result...")

            elif msg_type == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                if contract.get('is_sold') == 1:
                    contract_id = contract['contract_id']
                    handle_contract_settlement(email, contract_id, contract['profit'])
                    
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

        def on_close_wrapper(ws_app, code, msg):
            print(f"⚠ [PROCESS] WS closed for {email}. **RECONNECTING IMMEDIATELY.**")
            # لا نغير is_contract_open إذا كنا ننتظر نتائج عقود لضمان الاستمرارية
            if not get_session_data(email).get('open_contract_ids'):
                 is_contract_open[email] = False

        try:
            ws = websocket.WebSocketApp(
                WSS_URL, on_open=on_open_wrapper, on_message=on_message_wrapper,
                on_error=lambda ws, err: print(f"[WS Error {email}] {err}"),
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

# --- (FLASK APP SETUP AND ROUTES) ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False

# -----------------
# HTML Templates
# -----------------

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
    {% set timing_logic = "Always @ Last Digit " + entry_digit_trigger|string %}
    {% set strategy = "DUAL DIGIT OVER " + target_digit_over|string + " / UNDER " + target_digit_under|string + " (" + symbol + " - " + timing_logic + " - x" + martingale_multiplier|string + " Martingale, Max Steps " + martingale_steps|string + ")" %}
    
    <p class="status-running">✅ Bot is *Running*! (Auto-refreshing)</p>
    <p>Account Type: *{{ session_data.account_type.upper() }}* | Currency: *{{ session_data.currency }}*</p>
    <p>Net Profit: *{{ session_data.currency }} {{ session_data.current_profit|round(2) }}*</p>
    <p>Current Stake (Over/Under): *{{ session_data.currency }} {{ session_data.current_stake_lower|round(2) }}*</p>
    <p>Step: *{{ session_data.current_step }}* / {{ martingale_steps }} (Max Loss: {{ max_consecutive_losses }})</p>
    <p style="font-weight: bold; color: green;">Total Wins: *{{ session_data.total_wins }}* | Total Losses: *{{ session_data.total_losses }}*</p>
    <p style="font-weight: bold; color: purple;">Last Tick Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: *{{ strategy }}*</p>
    <p style="font-weight: bold; color: #ff5733;">Contracts Open: {{ session_data.open_contract_ids|length }}</p>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: red; color: white;">🛑 Stop Bot</button>
    </form>
{% else %}
    <p class="status-stopped">🛑 Bot is *Stopped*. Enter settings to start a new session.</p>
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
            }, 5000); 
        }
    }

    autoRefresh();
</script>
"""

# -----------------
# Flask Routes
# -----------------

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
            
        session_data['stop_reason'] = "Displayed"
        save_session_data(email, session_data)
        delete_session_data(email)

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        duration=DURATION,
        symbol=SYMBOL,
        target_digit_over=TARGET_DIGIT_OVER,
        target_digit_under=TARGET_DIGIT_UNDER,
        entry_digit_trigger=ENTRY_DIGIT_TRIGGER
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
    
    flash(f"Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: DUAL DIGIT OVER {TARGET_DIGIT_OVER} / UNDER {TARGET_DIGIT_UNDER} (R_100 - Always @ Last Digit {ENTRY_DIGIT_TRIGGER}) with x{MARTINGALE_MULTIPLIER} Martingale (Max {MARTINGALE_STEPS} Steps, Max {MAX_CONSECUTIVE_LOSSES} Losses)", 'success')
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
        # يتم إيقاف أي عمليات كانت تعمل قبل إعادة التشغيل
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)") 
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

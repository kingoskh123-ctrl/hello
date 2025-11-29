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
# BOT CONSTANT SETTINGS
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100" 
DURATION = 10                  # ⬅️ مدة الصفقة 10 تيكات
DURATION_UNIT = "t"
TICKS_TO_ANALYZE = 10          # عدد التيكات للتحليل (10)

# إعدادات المضاعفة
MARTINGALE_STEPS = 5           # يمكن زيادتها إذا كانت المضاعفة x6
MAX_CONSECUTIVE_LOSSES = 2     # ⬅️ سيتوقف بعد خسارة 3 صفقات متتالية (0 -> 1 -> 2 -> 3 (توقف))
MARTINGALE_MULTIPLIER = 6.0    # ⬅️ معامل المضاعفة الجديد (x6)
BARRIER_OFFSET = 0.1           # قيمة الحاجز (0.1)

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

CONTRACT_TYPE_ONETOUCH = "ONETOUCH"

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
    
    "last_valid_tick_price": 0.0,
    "current_entry_id": None, 
    "open_contract_ids": [], 
    "contract_profits": {}, 
    "last_barrier_value": BARRIER_OFFSET
}

# --- Persistence functions (UNCHANGED) ---
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
    
    return base_stake * (multiplier ** current_step)


def send_trade_order(email, stake, currency, contract_type_param, barrier_offset):
    """ إرسال طلب شراء واحد مع حاجز الإزاحة """
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
            "duration": DURATION, # ⬅️ تم التعديل إلى 10 تيكات
            "duration_unit": DURATION_UNIT, 
            "symbol": SYMBOL,
            "barrier": str(barrier_offset) 
        }
    }
    
    try:
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        return False


def apply_martingale_logic(email):
    """ يطبق منطق المضاعفة بناءً على نتيجة الصفقة الواحدة """
    global is_contract_open, MARTINGALE_MULTIPLIER, MARTINGALE_STEPS, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    results = list(current_data['contract_profits'].values())
    
    if not results or len(results) != 1:
        print("❌ [MARTINGALE ERROR] Incomplete results (Expected 1). Resetting stake to base.")
        total_profit = 0
    else:
        total_profit = results[0] 

    current_data['current_profit'] += total_profit
    
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
        
        # ⚠️ (منطق الإيقاف - إذا تم تجاوز الخطوات أو الخسائر المتتالية)
        # سيتم الإيقاف عند خسارة 3 صفقات متتالية (MAX_CONSECUTIVE_LOSSES = 2)
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: {MAX_CONSECUTIVE_LOSSES + 1} consecutive losses.")
            return

        if current_data['current_step'] > MARTINGALE_STEPS: 
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Exceeded {MARTINGALE_STEPS} Martingale steps.")
            return

        # حساب المضاعفة وتحديث الحالة
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        
        current_data['current_stake'] = new_stake
        
        # إعادة تعيين متغيرات تتبع الصفقة (للسماح بالصفقة الجديدة)
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        
        # حفظ البيانات وتشغيل الدخول الفوري
        save_session_data(email, current_data)
        
        print(f"🔄 [MARTINGALE IMMEDIATE ENTRY] PnL: {total_profit:.2f}. Step {current_data['current_step']}. New Stake: {round(new_stake, 2):.2f}. Requesting history and entering immediately.")
        
        # الدخول الفوري بطلب سجل جديد
        start_new_single_trade(email)
        return 

    # ✅ Win or Draw Condition
    else: 
        current_data['total_wins'] += 1 if total_profit > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        
        current_data['current_stake'] = base_stake_used
        
        entry_result_tag = "WIN" if total_profit > 0 else "DRAW"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {base_stake_used:.2f}.")

        # إعادة تعيين متغيرات الدخول 
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        
        # السماح بالدخول الأساسي عند الثانية 0 في الجولة القادمة
        is_contract_open[email] = False 
        
        save_session_data(email, current_data)
        
        currency = current_data.get('currency', 'USD')
        print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Stake: {current_data['current_stake']:.2f} | Next Entry: @ SEC 0")


def handle_contract_settlement(email, contract_id, profit_loss):
    """ معالجة نتيجة عقد واحد وتجميعها """
    current_data = get_session_data(email)
    
    if contract_id not in current_data['open_contract_ids']:
        return

    current_data['contract_profits'][contract_id] = profit_loss
    
    if contract_id in current_data['open_contract_ids']:
        current_data['open_contract_ids'].remove(contract_id)
        
    save_session_data(email, current_data)
    
    if not current_data['open_contract_ids']:
        apply_martingale_logic(email)
        

def execute_trade_from_history(email, ticks):
    """ يحلل قائمة التيكات المستلمة ويرسل صفقة ONETOUCH بناءً على استراتيجية الزخم """
    global is_contract_open, BARRIER_OFFSET, CONTRACT_TYPE_ONETOUCH, MARTINGALE_STEPS
    
    current_data = get_session_data(email)
    
    # 1. تحديد سعر الفتح (أقدم تيك) وسعر الإغلاق (أحدث تيك)
    open_price = ticks[0] 
    close_price = ticks[-1]
    
    # ⬅️ تطبيق منطق الاتجاه المباشر (Momentum)
    if open_price < close_price:
        # صاعد: يدخل One Touch بحاجز موجب
        contract_type_to_use = CONTRACT_TYPE_ONETOUCH
        barrier_to_use = f"+{BARRIER_OFFSET:.2f}" 
        strategy_tag = f"BULLISH -> ONETOUCH (+{BARRIER_OFFSET})"
        stake_to_use = current_data['current_stake']
        
    elif open_price > close_price:
        # هابط: يدخل One Touch بحاجز سالب
        contract_type_to_use = CONTRACT_TYPE_ONETOUCH
        barrier_to_use = f"-{BARRIER_OFFSET:.2f}"
        strategy_tag = f"BEARISH -> ONETOUCH (-{BARRIER_OFFSET})"
        stake_to_use = current_data['current_stake']
        
    else:
        print("⚠ [ENTRY SKIPPED] Open price equals Close price. Waiting.")
        is_contract_open[email] = False 
        current_data['current_entry_id'] = None
        save_session_data(email, current_data)
        return

    currency_to_use = current_data['currency']
    
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    entry_type_tag = "BASE ENTRY" if current_data['current_step'] == 0 else f"MARTINGALE STEP {current_data['current_step']}"
    entry_timing_tag = "IMMEDIATE" if current_data['current_step'] > 0 else "@ SEC 0"
    
    print(f"🧠 [SINGLE TRADE ENTRY - {entry_timing_tag}] {entry_type_tag} | Strategy: {strategy_tag} | Stake: {round(stake_to_use, 2):.2f}. Price: {open_price:.5f} -> {close_price:.5f}")
    
    # إرسال الصفقة
    if send_trade_order(email, stake_to_use, currency_to_use, contract_type_to_use, barrier_to_use):
        pass
        
    current_data['last_entry_time'] = int(time.time())
    current_data['last_entry_price'] = close_price 
    
    save_session_data(email, current_data)


def start_new_single_trade(email):
    """ يرسل طلب ticks_history لإجراء التحليل ومنع الدخول المزدوج """
    global active_ws, SYMBOL, TICKS_TO_ANALYZE
    
    current_data = get_session_data(email)
    
    if current_data['current_step'] > MARTINGALE_STEPS:
         is_contract_open[email] = False 
         return
    
    if is_contract_open.get(email): 
        return
    
    entry_timing_tag = "IMMEDIATE" if current_data['current_step'] > 0 else "@ SEC 0"
    print(f"⏳ [HISTORY REQUEST - {entry_timing_tag}] Requesting {TICKS_TO_ANALYZE} ticks for analysis...")

    # رسالة طلب السجل التاريخي (10 تيكات)
    history_request = {
        "ticks_history": SYMBOL,
        "end": "latest",
        "count": TICKS_TO_ANALYZE, 
        "style": "ticks",
        "id": "analysis_request" 
    }

    try:
        active_ws[email].send(json.dumps(history_request))
        is_contract_open[email] = True # نعتبرها مفتوحة مؤقتًا لمنع طلب آخر
        current_data['current_entry_id'] = time.time()
        save_session_data(email, current_data)
    except Exception as e:
        print(f"❌ [HISTORY ERROR] Could not send history request: {e}")
        is_contract_open[email] = False 


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
        "current_stake": stake, 
        "stop_reason": "Running",
        "last_entry_time": 0,
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "currency": currency,
        "account_type": account_type,
        "last_valid_tick_price": 0.0,
        
        "current_entry_id": None, 
        "open_contract_ids": [], 
        "contract_profits": {},
        "last_barrier_value": BARRIER_OFFSET
    })
    save_session_data(email, session_data)

    while True:
        current_data = get_session_data(email)
        
        if not current_data.get('is_running'): break

        print(f"🔗 [PROCESS] Attempting to connect for {email} ({account_type.upper()}/{currency})...")

        def on_open_wrapper(ws_app):
            current_data = get_session_data(email) 
            ws_app.send(json.dumps({"authorize": current_data['api_token']}))
            running_data = get_session_data(email)
            running_data['is_running'] = True
            save_session_data(email, running_data)
            print(f"✅ [PROCESS] Connection established for {email}.")
            is_contract_open[email] = False

        def on_message_wrapper(ws_app, message):
            data = json.loads(message)
            msg_type = data.get('msg_type')
            
            current_data = get_session_data(email)
            if not current_data.get('is_running'): return
            
            # --- معالجة ticks_history للتحليل والدخول ---
            if msg_type == 'history' and data.get('echo_req', {}).get('id') == 'analysis_request':
                if not current_data.get('is_running') or current_data['current_entry_id'] is None:
                    return 
                    
                prices = data['history']['prices']
                
                if len(prices) < TICKS_TO_ANALYZE:
                    print(f"❌ [ENTRY FAIL] Only {len(prices)} ticks received from history. Cannot analyze.")
                    is_contract_open[email] = False 
                    current_data['current_entry_id'] = None
                    save_session_data(email, current_data)
                    return

                execute_trade_from_history(email, prices)


            # --- معالجة التيكات المباشرة (لتحديث السعر وتحديد توقيت الدخول) ---
            elif msg_type == 'tick':
                current_price = float(data['tick']['quote'])
                tick_epoch = data['tick']['epoch'] 
                
                current_second = datetime.fromtimestamp(tick_epoch, tz=timezone.utc).second
                
                current_data['last_valid_tick_price'] = current_price
                current_data['last_tick_data'] = data['tick']
                save_session_data(email, current_data) 
                
                # === منطق طلب السجل الأساسي (Step 0) ===
                if not is_contract_open.get(email):
                    # الدخول الأساسي ينتظر الثانية 0 فقط
                    if current_data['current_step'] == 0 and current_second == 0:
                        start_new_single_trade(email)
                # === نهاية منطق الدخول ===


            elif msg_type == 'buy':
                contract_id = data['buy']['contract_id']
                current_data['open_contract_ids'].append(contract_id)
                save_session_data(email, current_data)
                
                ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                
            elif 'error' in data:
                error_code = data['error'].get('code', 'N/A')
                error_message = data['error'].get('message', 'Unknown Error')
                print(f"❌❌ [API ERROR] Code: {error_code}, Message: {error_message}. Trade may be disrupted.")
                
                if current_data['current_entry_id'] is not None and is_contract_open.get(email):
                    time.sleep(1) 
                    if not current_data['open_contract_ids']: 
                        # إذا فشل الشراء ولم يكن هناك عقد مفتوح، نطبق المضاعفة
                        apply_martingale_logic(email)
                    else: 
                        print("⚠ [TRADE FAILURE] Waiting for the open contract result...")

            elif msg_type == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                if contract.get('is_sold') == 1:
                    contract_id = contract['contract_id']
                    handle_contract_settlement(email, contract_id, contract['profit'])
                    
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

        def on_close_wrapper(ws_app, code, msg):
            print(f"⚠ [PROCESS] WS closed for {email}. RECONNECTING IMMEDIATELY.")
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
    {% set timing_logic = "Immediate after Loss / @ Sec 0 after Win" %}
    {% set strategy = "10-Tick MOMENTUM (" + symbol + " - " + timing_logic + " - ONETOUCH " + barrier_offset|string + " - x" + martingale_multiplier|string + " Martingale, Max Steps " + martingale_steps|string + ")" %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Loss: {{ max_consecutive_losses + 1 }})</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: purple;">Last Tick Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
    <p style="font-weight: bold; color: #ff5733;">Contracts Open: {{ session_data.open_contract_ids|length }}</p>
    
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
            }, 5000); 
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
            
            delete_session_data(email)
            session_data = get_session_data(email)
            
    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        duration=DURATION,
        barrier_offset=BARRIER_OFFSET,
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
    
    flash(f'Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: {TICKS_TO_ANALYZE}-Tick Momentum ({SYMBOL} - ONETOUCH $\pm${BARRIER_OFFSET}) with x{MARTINGALE_MULTIPLIER} Martingale (Max {MARTINGALE_STEPS} Steps, Max {MAX_CONSECUTIVE_LOSSES + 1} Losses)', 'success')
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

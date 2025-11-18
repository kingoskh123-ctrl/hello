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
# BOT CONSTANT SETTINGS (R_100 | x2.1 | 60 Ticks | Delayed Martingale)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"               
DURATION = 56                  # 56 ثانية (مدة الصفقة)
DURATION_UNIT = "s"            
TICKS_TO_ANALYZE = 60          # ⬅️ 60 تيك للتحليل (2 شمعة x 30 تيك)

# إعدادات المضاعفة
MARTINGALE_STEPS = 5           # الحد الأقصى لخطوات المضاعفة (Step 0, 1, 2, 3, 4, 5)
MAX_CONSECUTIVE_LOSSES = 6     # الحد الأقصى للخسائر المتتالية
MARTINGALE_MULTIPLIER = 2.1    # معامل المضاعفة
BARRIER_OFFSET = "0.03"        # قيمة الحاجز (مثلاً 0.03+)

RECONNECT_DELAY = 2            
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

CONTRACT_TYPE_HIGHER = "CALL" 
CONTRACT_TYPE_LOWER = "PUT"   

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
    "last_5_ticks": [],             
    "current_entry_id": None,             
    "open_contract_ids": [],              
    "contract_profits": {},               
    "last_barrier_value": BARRIER_OFFSET,
    "last_contract_type_used": None 
}

# --- Persistence functions (UNCHANGED) ---
def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE): return {}
    try:
        with open(ACTIVE_SESSIONS_FILE, 'r') as f:
            content = f.read()
            data = json.loads(content) if content else {}
            for email, session_data in data.items():
                if 'last_5_ticks' not in session_data:
                    session_data['last_5_ticks'] = []
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
            "duration": DURATION, 
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
    
    # ❌ Loss Condition (يتم تعيين المضاعفة والانتظار حتى الثانية 0 والتحليل الجديد)
    if total_profit < 0:
        
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1 
        
        # ⚠️ (منطق الإيقاف - إذا تم تجاوز الخطوات أو الخسائر المتتالية)
        if current_data['current_step'] > MARTINGALE_STEPS: 
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Exceeded {MARTINGALE_STEPS} Martingale steps.")
            return
            
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: {MAX_CONSECUTIVE_LOSSES} consecutive losses.")
            return

        # حساب المضاعفة وتحديث الحالة
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        
        current_data['current_stake_lower'] = new_stake
        current_data['current_stake_higher'] = new_stake 
        
        # إعادة تعيين متغيرات تتبع الصفقة
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        
        save_session_data(email, current_data)
        
        # ⬅️ **التعديل الهام:** يتم إنهاء الصفقة والسماح بالدخول عند الثانية 0 القادمة
        print(f"🔄 [MARTINGALE DELAYED ENTRY] PnL: {total_profit:.2f}. Step {current_data['current_step']}. New Stake: {round(new_stake, 2):.2f}. Waiting for next @ SEC 0 (New Analysis Required).")
        is_contract_open[email] = False # يسمح بالدخول عند الثانية 0
        
        return 

    # ✅ Win or Draw Condition (الربح أو التعادل) 
    else: 
        current_data['total_wins'] += 1 if total_profit > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        
        current_data['current_stake_lower'] = base_stake_used
        current_data['current_stake_higher'] = base_stake_used 
        
        entry_result_tag = "WIN" if total_profit > 0 else "DRAW"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {base_stake_used:.2f}.")

        # إعادة تعيين متغيرات الدخول 
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        current_data['last_contract_type_used'] = None 
        
        # السماح بالدخول الأساسي عند الثانية 0 في الجولة القادمة
        is_contract_open[email] = False 
        
        save_session_data(email, current_data)
        
        currency = current_data.get('currency', 'USD')
        print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Stake: {current_data['current_stake_higher']:.2f} | Next Entry: @ SEC 0 (Re-Analyze)")


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


def start_new_single_trade(email):
    """ 
    يتم تنفيذ هذه الدالة للصفقة الأساسية ولصفقات المضاعفة المؤجلة.
    الشرط: C1 عكس C2، والدخول باتجاه C2.
    """
    global is_contract_open, BARRIER_OFFSET, CONTRACT_TYPE_HIGHER, CONTRACT_TYPE_LOWER, TICKS_TO_ANALYZE
    
    current_data = get_session_data(email)
    
    ticks = current_data.get('last_5_ticks', [])
    
    # التحقق من توفر 60 تيك
    if len(ticks) < TICKS_TO_ANALYZE:
        if current_data['current_step'] == 0: 
            # رسالة التنبيه تظهر فقط في الخطوة 0 لتجنب الإزعاج
            print(f"❌ [ENTRY FAIL] Only {len(ticks)} ticks available (Need {TICKS_TO_ANALYZE} for 2 candles). Waiting for more ticks @ SEC 0.")
        is_contract_open[email] = False 
        return
    
    # 1. تحديد اتجاهات الشمعتين (30 تيك لكل شمعة)
    
    CANDLE_SIZE = TICKS_TO_ANALYZE // 2 # 30 تيك

    # الشمعة 1 (تيك 0 حتى 29)
    candle1_open = ticks[0]
    candle1_close = ticks[CANDLE_SIZE - 1] 
    is_candle1_up = candle1_close > candle1_open

    # الشمعة 2 (تيك 30 حتى 59)
    candle2_open = ticks[CANDLE_SIZE]      
    candle2_close = ticks[-1]                          
    is_candle2_up = candle2_close > candle2_open
    
    # 2. تحديد نوع الدخول بناءً على قاعدة التذبذب (C1 عكس C2) والدخول باتجاه C2
    contract_type_to_use = None
    barrier_to_use = None
    strategy_tag = None
    
    stake_to_use = current_data['current_stake_higher'] # يستخدم الرهان الحالي (قد يكون مضاعفاً)

    # ⬆️⬇️ الشمعة 1 صاعدة + الشمعة 2 هابطة -> ندخل PUT (باتجاه C2 الهابطة)
    if is_candle1_up and not is_candle2_up:
        contract_type_to_use = CONTRACT_TYPE_LOWER
        barrier_to_use = f"-{BARRIER_OFFSET}"
        strategy_tag = "C1 BULL + C2 BEAR -> FOLLOW C2 LOWER"
        
    # ⬇️⬆️ الشمعة 1 هابطة + الشمعة 2 صاعدة -> ندخل CALL (باتجاه C2 الصاعدة)
    elif not is_candle1_up and is_candle2_up:
        contract_type_to_use = CONTRACT_TYPE_HIGHER
        barrier_to_use = f"+{BARRIER_OFFSET}"
        strategy_tag = "C1 BEAR + C2 BULL -> FOLLOW C2 HIGHER"
        
    else:
        # لا يوجد تذبذب مطلوب (مثل 2 صاعدة أو 2 هابطة)
        print(f"⚠ [ENTRY SKIPPED @ SEC 0] No C1-C2 opposite pattern found. Current Step: {current_data['current_step']}")
        is_contract_open[email] = False
        return

    # 3. إرسال الصفقة 
    
    currency_to_use = current_data['currency']
    
    current_data['current_entry_id'] = time.time()
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    entry_type_tag = f"ENTRY (STEP {current_data['current_step']})"
    entry_timing_tag = "@ SEC 0"
    
    print(f"🧠 [SINGLE TRADE ENTRY - {entry_timing_tag}] {entry_type_tag} | Strategy: {strategy_tag} | Stake: {round(stake_to_use, 2):.2f}")
    
    # إرسال الصفقة
    if send_trade_order(email, stake_to_use, currency_to_use, contract_type_to_use, barrier_to_use):
        pass
        
    is_contract_open[email] = True
    
    current_data['last_entry_time'] = int(time.time())
    current_data['last_entry_price'] = candle2_close 
    
    # تخزين نوع العقد الذي تم إرساله
    current_data['last_contract_type_used'] = contract_type_to_use 

    save_session_data(email, current_data)


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Core bot logic """
    
    global is_contract_open, active_ws, RECONNECT_DELAY

    is_contract_open = {email: False}
    active_ws = {email: None}

    session_data = get_session_data(email)
    session_data['last_5_ticks'] = [] 
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
        "open_contract_ids": [],            
        "contract_profits": {},
        "last_barrier_value": BARRIER_OFFSET,
        "last_contract_type_used": None
    })
    save_session_data(email, session_data)

    # الحلقة الخارجية: تبقي العملية (Process) نشطة وتحاول إعادة الاتصال إذا انهار كائن WebSocketApp
    while True: 
        current_data = get_session_data(email)
        
        if not current_data.get('is_running'): break

        print(f"🔗 [PROCESS] Attempting to connect for {email} ({account_type.upper()}/{currency})...")

        # الحلقة الداخلية: تتعامل مع إنشاء اتصال WebSocket ومحاولات إعادة الاتصال التلقائية للمكتبة
        while current_data.get('is_running'): 
            
            def on_open_wrapper(ws_app):
                current_data = get_session_data(email) 
                ws_app.send(json.dumps({"authorize": current_data['api_token']}))
                ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
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
                    
                if msg_type == 'tick':
                    try:
                        current_price = float(data['tick']['quote'])
                        tick_epoch = data['tick']['epoch'] 
                        
                        current_second = datetime.fromtimestamp(tick_epoch, tz=timezone.utc).second
                        
                        current_data['last_valid_tick_price'] = current_price
                        current_data['last_tick_data'] = data['tick']
                        
                        current_data['last_5_ticks'].append(current_price)
                        if len(current_data['last_5_ticks']) > TICKS_TO_ANALYZE: 
                            current_data['last_5_ticks'].pop(0) 
                        
                        save_session_data(email, current_data) 
                        
                        # === منطق الدخول الأساسي (يعالج المضاعفة المؤجلة أيضاً) ===
                        if not is_contract_open.get(email):
                            if current_second == 0:
                                start_new_single_trade(email)
                        # === نهاية منطق الدخول ===
                    except KeyError:
                        pass 
                    except Exception as e:
                        print(f"❌ [TICK ERROR] Failed to process tick: {e}")
                        
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
                print(f"⚠ [PROCESS] WS closed for {email} (Code: {code}). Attempting reconnection...")
                is_contract_open[email] = False

            try:
                ws = websocket.WebSocketApp(
                    WSS_URL, on_open=on_open_wrapper, on_message=on_message_wrapper,
                    on_error=lambda ws, err: print(f"[WS Error {email}] {err}"),
                    on_close=on_close_wrapper
                )
                active_ws[email] = ws
                
                ws.run_forever(ping_interval=10, ping_timeout=5, reconnect=RECONNECT_DELAY)
                
            except Exception as e:
                print(f"❌ [CRITICAL ERROR] Uncaught exception in WS connection loop for {email}: {e}")
                is_contract_open[email] = False
            
            current_data = get_session_data(email)
            if not current_data.get('is_running'): break
            
            print(f"💤 [PROCESS] WS failed to reconnect, attempting to restart connection in {RECONNECT_DELAY} seconds...")
            time.sleep(RECONNECT_DELAY)

        if get_session_data(email).get('is_running') is False: break

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
    {% set timing_logic = "Delayed Martingale @ Sec 0 (New Analysis)" %}
    {% set strategy = ticks_to_analyze|string + "-Tick (C1 vs C2 Opposite -> Follow C2) (" + symbol + " - Entry @ Sec 0 | " + timing_logic + " with x" + martingale_multiplier|string + " Martingale, Max Steps " + martingale_steps|string + ")" %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake (Higher/Lower): {{ session_data.currency }} {{ session_data.current_stake_higher|round(2) }}</p>
    <p>Step: {{ session_data.current_step }} / {{ martingale_steps }} (Max Loss: {{ max_consecutive_losses }})</p>
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
        symbol=SYMBOL,
        ticks_to_analyze=TICKS_TO_ANALYZE
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
    
    flash(f'Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: {TICKS_TO_ANALYZE}-Tick (C1 vs C2 Opposite -> Follow C2) ({SYMBOL} - Entry @ Sec 0 | Delayed Martingale with x{MARTINGALE_MULTIPLIER} Martingale (Max {MARTINGALE_STEPS} Steps, Max {MAX_CONSECUTIVE_LOSSES} Losses))', 'success')
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

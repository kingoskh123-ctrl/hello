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
# BOT CONSTANT SETTINGS (الثوابت المطلوبة)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 1               
DURATION_UNIT = "t"        
MARTINGALE_STEPS = 1       
MAX_CONSECUTIVE_LOSSES = 2 
RECONNECT_DELAY = 0        # 0 ثانية للدخول الفوري (الخسارة)
WIN_DELAY = 20             # 20 ثانية تأخير بعد الربح
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# الاستراتيجية: DIGITDIFF مع حواجز ثابتة
BASE_CONTRACT_TYPE = "DIGITDIFF" 
BASE_BARRIER = 1             # حاجز الدخول الأساسي: DIGITDIFF 1
MARTINGALE_BARRIER = 8       # حاجز المضاعفة: DIGITDIFF 8
MARTINGALE_MULTIPLIER = 14.0 # 💡 تم التعديل إلى 14.0
# ==========================================================

# ==========================================================
# GLOBAL STATE (Shared between processes via File/Lock)
# ==========================================================
active_processes = {}
active_ws = {}
is_contract_open = {}
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
    "open_price": 0.0,           
    "open_time": 0,              
    "last_action_type": BASE_CONTRACT_TYPE, 
    "last_valid_tick_price": 0.0,
    "last_trade_barrier": BASE_BARRIER, 
}
# ==========================================================

# (وظائف إدارة الحالة الثابتة لم تتغير)
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
    
    # تحديث الحالة قبل إغلاق العملية
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
        save_session_data(email, current_data)

    # إنهاء العملية
    with PROCESS_LOCK:
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                print(f"🛑 [INFO] Closing WS for {email}...")
                
                # إغلاق WS لإخراج البوت من حلقة ws.run_forever()
                if email in active_ws and active_ws[email]:
                    try:
                        active_ws[email].close()
                    except:
                        pass
                
                time.sleep(0.5)
                
                # 💡 الإصلاح: لا تقم بإنهاء العملية بالكامل إذا كان السبب هو محاولة المضاعفة الفورية (Auto-Retry)
                if process.is_alive() and stop_reason != "Disconnected (Auto-Retry)":
                    print(f"🛑 [INFO] Forcing termination of process for {email}...")
                    process.terminate()
                    process.join()
                    # عند الإيقاف النهائي، نحذف العملية من القائمة
                    del active_processes[email]
                else:
                    # عند المضاعفة أو بعد الربح، يجب أن يعود is_running إلى True ليتابع اللوب الخارجي
                    current_data['is_running'] = True
                    save_session_data(email, current_data)
                    action_type = "Martingale retry" if current_data.get('current_step', 0) > 0 else "Win delay"
                    print(f"⚠ [INFO] Process for {email} remains alive for {action_type} logic.")


    # حذف WS من القائمة
    with PROCESS_LOCK:
        if email in active_ws: del active_ws[email]
    if email in is_contract_open: is_contract_open[email] = False

    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached", "API Buy Error"]:
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        elif stop_reason != "Disconnected (Auto-Retry)": # تأكد من عدم مسح البيانات إذا كان Auto-Retry
            delete_session_data(email)
            print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    else:
        # مسار المضاعفة/التأخير الفوري (Auto-Retry)
        print(f"⚠ [INFO] WS closed for {email}. Logic set for dynamic delay.")

# ==========================================================
# TRADING BOT FUNCTIONS
# ==========================================================

def calculate_martingale_stake(base_stake, current_stake, current_step):
    """ منطق المضاعفة (الرهان الخاسر × 14) """
    if current_step == 0:
        return base_stake
    if current_step <= MARTINGALE_STEPS:
        return current_stake * MARTINGALE_MULTIPLIER
    return base_stake

def send_trade_order(email, stake, currency, contract_type_param, barrier_value=None):
    global is_contract_open, active_ws, DURATION, DURATION_UNIT, SYMBOL
    
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
        is_contract_open[email] = True
        print(f"💰 [TRADE] Sent {contract_type_param} with Barrier: {barrier_value} | Stake: {round(stake, 2):.2f} {currency}")
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass

def check_pnl_limits(email, profit_loss, last_action_type, ws_app):
    """ منطق تحديث الحالة وتبديل الحواجز وتفعيل التأخير الديناميكي """
    global is_contract_open, BASE_BARRIER, MARTINGALE_BARRIER, RECONNECT_DELAY, WIN_DELAY
    
    is_contract_open[email] = False

    current_data = get_session_data(email)
    if not current_data.get('is_running'): return

    last_stake = current_data['current_stake']
    current_data['current_profit'] += profit_loss
    
    if profit_loss > 0:
        # حالة الربح:
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 # 💡 يجعل التأخير 20 ثانية في اللوب الخارجي
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        current_data['last_action_type'] = last_action_type
        current_data['last_trade_barrier'] = BASE_BARRIER # الحاجز التالي: 1
        save_session_data(email, current_data)
        
        # إغلاق الاتصال لتفعيل تأخير الـ 20 ثانية (WIN_DELAY)
        print(f"✅ [WIN] Closing connection to wait {WIN_DELAY} seconds before next base entry.")
        try:
            stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")
        except:
            pass
        return 
        
    else:
        # حالة الخسارة (بدء المضاعفة الفورية)
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        # التحقق من الحد الأقصى للخسارات المتتالية وخطوات المارتنجيل
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES or current_data['current_step'] > MARTINGALE_STEPS:
            stop_bot(email, clear_data=True, stop_reason="SL Reached")
            return
        
        # 1. حساب الرهان المضاعف الجديد
        new_stake = calculate_martingale_stake(
            current_data['base_stake'],
            last_stake,
            current_data['current_step']
        )
        
        # 2. تخزين الرهان الجديد ونوع الحركة. 
        current_data['current_stake'] = new_stake
        current_data['last_action_type'] = last_action_type
        current_data['last_trade_barrier'] = MARTINGALE_BARRIER # الحاجز التالي: 8
        save_session_data(email, current_data)
        
        # إغلاق اتصال WebSocket لإعادة تشغيل دورة الاتصال لـ 'Immediate Entry'
        print(f"💸 [MARTINGALE] Lost. Next stake calculated: {new_stake:.2f}. Closing connection to re-enter (Wait: {RECONNECT_DELAY}s)...")
        try:
            # stop_bot سيستخدم RECONNECT_DELAY (0 ثانية) في اللوب الخارجي
            stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")
        except Exception as e:
            print(f"❌ [MARTINGALE ERROR] Error engaging retry logic: {e}")
            pass
        return

    # التحقق من TP بعد الربح
    if current_data['current_profit'] >= current_data['tp_target']:
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
    
    rounded_last_stake = round(last_stake, 2)
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Strategy: {BASE_CONTRACT_TYPE}")


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ منطق البوت الأساسي مع التأخير الديناميكي """
    
    global is_contract_open, active_ws, BASE_CONTRACT_TYPE, RECONNECT_DELAY, WIN_DELAY

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
        "open_price": 0.0,           
        "open_time": 0,              
        "last_action_type": BASE_CONTRACT_TYPE, 
        "last_valid_tick_price": 0.0,
        "last_trade_barrier": BASE_BARRIER, 
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

        def on_message_wrapper(ws_app, message):
            data = json.loads(message)
            msg_type = data.get('msg_type')
            
            current_data = get_session_data(email)
            if not current_data.get('is_running'):
                ws_app.close()
                return
                
            if msg_type == 'tick':
                current_timestamp = int(data['tick']['epoch'])
                current_price = float(data['tick']['quote'])
                
                current_data['last_tick_data'] = {
                    "price": current_price,
                    "timestamp": current_timestamp
                }
                
                current_data['last_valid_tick_price'] = current_price
                save_session_data(email, current_data)
                
                # الدخول الفوري: لا نشتري إذا كان العقد مفتوحًا
                if is_contract_open.get(email) is True: 
                    return
                
                # منع الشراء المتعدد على نفس التيك
                if current_data['last_entry_time'] == current_timestamp: 
                    return
                
                # 1. منطق تحديد وقت الدخول
                dt_object = datetime.fromtimestamp(current_timestamp, tz=timezone.utc)
                current_second = dt_object.second
                is_base_entry_time = current_second == 0 or current_second == 30

                is_martingale_retry = current_data['current_step'] > 0
                
                # 💡 بوابة الدخول: نفذ إذا كانت مضاعفة فورية (retry) أو إذا كان الوقت 0 أو 30 والدخول أساسي (step 0)
                if not (is_martingale_retry or (is_base_entry_time and current_data['current_step'] == 0)):
                    return 

                # 2. تحديد الرقم الهدف (Barrier) ونوع الحركة
                if is_martingale_retry:
                    # المضاعفة: استخدام حاجز DIGITDIFF 8 المخزن مسبقاً
                    barrier_value = MARTINGALE_BARRIER
                    action_type_to_use = BASE_CONTRACT_TYPE
                    print(f"💡 [MARTINGALE] Immediate Retry. Using barrier: {barrier_value} ({action_type_to_use} {barrier_value})")
                else:
                    # الدخول الأساسي: استخدام حاجز DIGITDIFF 1
                    barrier_value = BASE_BARRIER
                    action_type_to_use = BASE_CONTRACT_TYPE
                    print(f"🎯 [BASE ENTRY] Time entry condition met (:{current_second}). Barrier set: {barrier_value}")
                
                # 💡 يتم تخزين الرقم الهدف المستخدم حالياً (سواء 1 أو 8)
                current_data['last_trade_barrier'] = barrier_value
                save_session_data(email, current_data)

                stake_to_use = current_data['current_stake']
                currency_to_use = current_data['currency']
                
                # 3. إرسال أمر الشراء
                send_trade_order(
                    email, 
                    stake_to_use, 
                    currency_to_use, 
                    action_type_to_use, 
                    barrier_value
                )
                
                current_data['last_entry_time'] = current_timestamp
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
                    last_action_type = get_session_data(email).get('last_action_type', BASE_CONTRACT_TYPE) 
                    
                    check_pnl_limits(email, contract['profit'], last_action_type, ws_app)
                    
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

        def on_close_wrapper(ws_app, code, msg):
            print(f"⚠ [PROCESS] WS closed for {email}. Dynamic delay logic engaged.")
            is_contract_open[email] = False

        try:
            ws = websocket.WebSocketApp(
                WSS_URL, on_open=on_open_wrapper, on_message=on_message_wrapper,
                on_error=lambda ws, err: print(f"[WS Error {email}] {err}"),
                on_close=on_close_wrapper
            )
            active_ws[email] = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
            
        except Exception as e:
            print(f"❌ [ERROR] WebSocket failed for {email}: {e}")
        
        current_data = get_session_data(email)
        
        # 💡 منطق التأخير الديناميكي
        if current_data.get('is_running') is False and current_data.get('stop_reason') != "Disconnected (Auto-Retry)": 
            break
        
        delay_seconds = RECONNECT_DELAY # الافتراضي: 0 ثانية (للمضاعفة الفورية عند الخسارة)
        
        # إذا كانت current_step تساوي 0، فهذا يعني أن الصفقة الأخيرة ربحت
        if current_data.get('current_step', 0) == 0:
            delay_seconds = WIN_DELAY # 20 ثانية انتظار بعد الربح

        print(f"💤 [PROCESS] Waiting {delay_seconds} seconds before retrying connection (Martingale: Step {current_data.get('current_step', 0)} / Barrier: {current_data.get('last_trade_barrier', 'N/A')})...")
        time.sleep(delay_seconds)

    print(f"🛑 [PROCESS] Bot process loop ended for {email}.")

# ==========================================================
# FLASK APP SETUP AND ROUTES
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
        direction: rtl;
        text-align: right;
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
        text-align: right;
        direction: rtl;
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
<h1>لوحة تحكم البوت | المستخدم: {{ email }}</h1>
<hr>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:{{ 'green' if category == 'success' else ('blue' if category == 'info' else 'red') }};">{{ message }}</p>
        {% endfor %}
        
        {% if session_data and session_data.stop_reason and session_data.stop_reason not in ["Running", "Disconnected (Auto-Retry)"] %}
            <p style="color:red; font-weight:bold;">آخر سبب للتوقف: {{ session_data.stop_reason }}</p>
        {% endif %}
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running or session_data.stop_reason == "Disconnected (Auto-Retry)" %}
    {% set strategy = base_contract_type + " (1:" + base_barrier|string + " & 8 | مضاعفة فورية x" + 14|string + ")" %}
    
    <p class="status-running">✅ البوت قيد التشغيل! (يتم التحديث تلقائيًا)</p>
    {% if session_data.stop_reason == "Disconnected (Auto-Retry)" %}
    {% set delay_msg = "انتظار 20 ثانية" if session_data.current_step == 0 else "إعادة دخول فوري" %}
    <p style="color:orange; font-weight:bold;">⚠ {{ delay_msg }} (خطوة {{ session_data.current_step }}/{{ martingale_steps }})</p>
    {% endif %}
    <p>نوع الحساب: {{ session_data.account_type.upper() }} | العملة: {{ session_data.currency }}</p>
    <p>صافي الربح: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>الرهان الحالي: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>الخطوة: {{ session_data.current_step }} / {{ martingale_steps }} (أقصى خسارة متتالية: {{ max_consecutive_losses }})</p>
    <p>الإحصائيات: {{ session_data.total_wins }} ربح | {{ session_data.total_losses }} خسارة</p>
    <p style="font-weight: bold; color: green;">سعر الدخول الأخير: {{ session_data.last_entry_price|round(5) }}</p>
    <p style="font-weight: bold; color: purple;">سعر التيك الأخير: {{ session_data.last_valid_tick_price|round(5) }}</p>
    {% if session_data.last_trade_barrier is not none %}
        <p style="font-weight: bold; color: blue;">الرقم الهدف المستخدم: {{ session_data.last_trade_barrier }}</p>
    {% endif %}
    <p style="font-weight: bold; color: #007bff;">الاستراتيجية الحالية: {{ strategy }}</p>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: red; color: white;">🛑 إيقاف البوت</button>
    </form>
{% else %}
    <p class="status-stopped">🛑 البوت متوقف. أدخل الإعدادات لبدء جلسة جديدة.</p>
    <form method="POST" action="{{ url_for('start_bot') }}">

        <label for="account_type">نوع الحساب:</label><br>
        <select id="account_type" name="account_type" required>
            <option value="demo" selected>تجريبي (USD)</option>
            <option value="live">حقيقي (tUSDT)</option>
        </select><br>

        <label for="token">رمز API من Deriv:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}" {% if session_data and session_data.api_token and session_data.is_running is not none %}readonly{% endif %}><br>
        
        <label for="stake">الرهان الأساسي (USD/tUSDT):</label><br>
        <input type="number" id="stake" name="stake" value="{{ session_data.base_stake|round(2) if session_data else 0.35 }}" step="0.01" min="0.35" required><br>
        
        <label for="tp">هدف الربح (USD/tUSDT):</label><br>
        <input type="number" id="tp" name="tp" value="{{ session_data.tp_target|round(2) if session_data else 10.0 }}" step="0.01" required><br>
        
        <button type="submit" style="background-color: green; color: white;">🚀 بدء البوت</button>
    </form>
{% endif %}
<hr>
<a href="{{ url_for('logout') }}" style="display: block; text-align: center; margin-top: 15px; font-size: 1.1em;">تسجيل الخروج</a>

<script>
    function autoRefresh() {
        var isRunning = {{ 'true' if session_data and session_data.is_running or session_data.stop_reason == "Disconnected (Auto-Retry)" else 'false' }};
        
        if (isRunning) {
            setTimeout(function() {
                window.location.reload();
            }, 1000); // 💡 تم التعديل إلى 1000 ميلي ثانية
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
        if reason == "SL Reached": flash(f"🛑 STOP: الحد الأقصى للخسارة ({MAX_CONSECUTIVE_LOSSES} خسارات متتالية أو تجاوز {MARTINGALE_STEPS} خطوات مضاعفة) تم الوصول إليه! (SL Reached)", 'error')
        elif reason == "TP Reached": flash(f"✅ GOAL: هدف الربح ({session_data['tp_target']} {session_data.get('currency', 'USD')}) تم الوصول إليه بنجاح! (TP Reached)", 'success')
        elif reason.startswith("API Buy Error"): flash(f"❌ API Error: {reason}. Check your token and account status.", 'error')
            
        session_data['stop_reason'] = "Displayed"
        save_session_data(email, session_data)
        delete_session_data(email)

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        base_contract_type=BASE_CONTRACT_TYPE, # 💡 تمرير الثوابت الجديدة
        base_barrier=BASE_BARRIER,
        duration=DURATION  
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
    global active_processes, BASE_CONTRACT_TYPE
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
    
    flash(f'Bot started successfully. Currency: {currency}. Account: {account_type.upper()}. Strategy: {BASE_CONTRACT_TYPE} 1 & 8 (x{MARTINGALE_MULTIPLIER} Dynamic Martingale)', 'success')
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

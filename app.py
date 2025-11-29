import time
import json
import websocket 
import os
import sys
import fcntl
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, g, make_response
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock
import traceback 
from collections import Counter

# ==========================================================
# BOT CONSTANT SETTINGS 
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"       
DURATION = 1           # مدة الصفقة: 1 تيك
DURATION_UNIT = "t"    

# إعدادات المضاعفة
MAX_CONSECUTIVE_LOSSES = 2    
MARTINGALE_MULTIPLIER = 14.0 # معامل المضاعفة: x14.0

# إعدادات استراتيجية DIGITDIFF
CONTRACT_TYPE_BASE = "DIGITDIFF"
DIFF_BASE = 1                 # الرقم المختلف للصفقة الأساسية
DIFF_MARTINGALE = 8           # الرقم المختلف لصفقة المضاعفة

# الثواني المحددة للدخول (تطبق فقط عند الربح)
ENTRY_SECONDS = [0, 10, 20, 30, 40, 50] 

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# ==========================================================
# GLOBAL STATE AND CONTROL FUNCTIONS 
# ==========================================================
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
    "last_two_digits": [9, 9],
    "last_digits_history": [],
    "last_prices_history": [],
    "max_loss": 2, 
    "last_trade_result": "WIN" # يستخدم لتحديد ما إذا كان الدخول التالي يجب أن ينتظر شرط الثانية
}

active_processes = {}
PROCESS_LOCK = Lock()
TRADE_LOCK = Lock() 

# --- Persistence functions ---
def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE): return {}
    try:
        with open(ACTIVE_SESSIONS_FILE, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            content = f.read()
            fcntl.flock(f, fcntl.LOCK_UN)
            return json.loads(content) if content else {}
    except: return {}

def save_session_data(email, session_data):
    all_sessions = load_persistent_sessions()
    all_sessions[email] = session_data
    try:
        with open(ACTIVE_SESSIONS_FILE, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(all_sessions, f, indent=4)
            fcntl.flock(f, fcntl.LOCK_UN)
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
    try:
        with open(ACTIVE_SESSIONS_FILE, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(all_sessions, f, indent=4)
            fcntl.flock(f, fcntl.LOCK_UN)
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

    if clear_data and stop_reason in ["SL Reached: Consecutive losses", "TP Reached"]: 
        delete_session_data(email) 
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data CLEARED from database.")
    elif clear_data and stop_reason != "Disconnected (Auto-Retry)":
        delete_session_data(email)
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from database.")
    else:
        print(f"⚠ [INFO] Process closed for {email}. Attempting immediate reconnect.")
# --- End of Persistence and Control functions ---

# ==========================================================
# TRADING BOT FUNCTIONS 
# ==========================================================

def calculate_martingale_stake(base_stake, current_step, multiplier):
    """ منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة (x14.0) لعدد الخطوات """
    if current_step == 0: 
        return base_stake
    return base_stake * (multiplier ** current_step)


# ⬅️ تم تبسيط هذه الدالة لمعالجة النتيجة فقط
def process_trade_result(email, contract_info):
    """ يعالج نتيجة العقد ويحدث حالة البوت لتحديد الصفقة التالية """
    global MARTINGALE_MULTIPLIER, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return current_data
    
    total_profit_loss = contract_info.get('profit', 0.0) # جلب الربح/الخسارة
    max_losses_for_check = current_data.get('max_loss', MAX_CONSECUTIVE_LOSSES)
    base_stake_used = current_data['base_stake']

    # 🛑 التحقق من Take Profit (TP)
    current_data['current_profit'] += total_profit_loss
    if current_data['current_profit'] >= current_data['tp_target']:
        current_data['stop_reason'] = "TP Reached"
        current_data['is_running'] = False
        save_session_data(email, current_data)
        return current_data 
        
    # ❌ حالة الخسارة (Loss) 
    if total_profit_loss < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        current_data['last_trade_result'] = "LOSS"
        
        # 🛑 التحقق من Max Consecutive Losses (الإيقاف التام)
        if current_data['consecutive_losses'] >= max_losses_for_check: 
            current_data['stop_reason'] = "SL Reached: Consecutive losses"
            current_data['is_running'] = False
            
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        print(f"🔄 [LOSS] PnL: {total_profit_loss:.2f}. Consecutive: {current_data['consecutive_losses']}/{max_losses_for_check}. Next Stake (x{MARTINGALE_MULTIPLIER}^{current_data['current_step']}) calculated: {round(new_stake, 2):.2f}.")
        
    # ✅ حالة الربح (Win) أو التعادل (Draw)
    else: 
        current_data['total_wins'] += 1 if total_profit_loss > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        current_data['last_trade_result'] = "WIN" if total_profit_loss > 0 else "DRAW"
        
        print(f"✅ [ENTRY RESULT] {current_data['last_trade_result']}. PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}.")

    # مسح بيانات العقد
    current_data['open_contract_ids'] = []
    
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Con. Loss: {current_data['consecutive_losses']}/{max_losses_for_check}, Stake: {current_data['current_stake']:.2f}")
    
    save_session_data(email, current_data)
    
    return current_data 


def sync_send_and_recv(ws, request_data, expect_msg_type, timeout=15):
    """ يرسل طلب وينتظر الرد المتوقع في إطار زمني محدد. """
    try:
        ws.settimeout(timeout)
        ws.send(json.dumps(request_data))
        
        while True:
            response = json.loads(ws.recv())
            
            if 'error' in response:
                print(f"❌ [API Error] Received error for {expect_msg_type} request: {response['error'].get('message', 'Unknown API Error')}")
                return response 
                
            if response.get('msg_type') == expect_msg_type:
                return response
            
    except websocket.WebSocketTimeoutException:
        print(f"❌ [WS Timeout] Timed out waiting for {expect_msg_type}.")
        return {'error': {'message': f"Connection Timeout waiting for {expect_msg_type}"}}
    except Exception as e:
        print(f"❌ [SYNC ERROR] Failed to send/receive: {e}. Check network.")
        return {'error': {'message': f"Connection Error: {e}"}}

# ⬅️ تم إعادة بناء دالة bot_core_logic بالكامل لتعمل ضمن جلسة واحدة
def bot_core_logic(email, token, stake, tp, currency, account_type, max_loss):
    """ Core bot logic (Single session per trade cycle) """
    
    global CONTRACT_TYPE_BASE, DIFF_BASE, DIFF_MARTINGALE, ENTRY_SECONDS

    print(f"🚀🚀 [CORE START] Bot logic started for {email} (Strategy: DIGIT DIFF {DIFF_BASE}/{DIFF_MARTINGALE} | DURATION: {DURATION} Ticks | Multiplier: x{MARTINGALE_MULTIPLIER}).")
    
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": stake, "stop_reason": "Running", "currency": currency,
        "account_type": account_type, "max_loss": max_loss,
        "current_step": session_data.get('current_step', 0), 
        "consecutive_losses": session_data.get('consecutive_losses', 0),
        "open_contract_ids": [], "contract_profits": {},
        "last_entry_price": 0.0, "last_valid_tick_price": 0.0
    })
    # تم تصحيح الخطأ السابق
    save_session_data(email, session_data) 
    
    
    while session_data.get('is_running'):
        
        ws = None
        trade_params = None
        
        try:
            # 1. 🔗 الاتصال والترخيص (مرة واحدة لكل دورة)
            print(f"🔗 [PROCESS] Attempting to CONNECT and AUTHORIZE...")
            ws = websocket.create_connection(WSS_URL, timeout=10) 
            
            auth_response = sync_send_and_recv(ws, {"authorize": token}, "authorize")
            if 'error' in auth_response:
                stop_bot(email, clear_data=True, stop_reason=f"Auth Error: {auth_response['error']['message']}")
                return

            # تحديث البيانات بعد التسوية في الدورة السابقة
            current_data = get_session_data(email) 
            if not current_data.get('is_running'): break
            
            is_martingale_step = current_data.get('consecutive_losses', 0) > 0 
            last_result_was_win = current_data.get('last_trade_result', 'WIN') in ['WIN', 'DRAW']
            
            # 2. 🧠 تحديد نوع الدخول (Base or Martingale)
            
            # أ. حالة المضاعفة الفورية (Martingale)
            if is_martingale_step:
                print(f"🧠 [MARTINGALE ENTRY] Preparing IMMEDIATE Martingale Entry (DIFF {DIFF_MARTINGALE}).")
                trade_params = {
                    "contract_type": CONTRACT_TYPE_BASE,
                    "digit": DIFF_MARTINGALE
                }
            
            # ب. حالة الدخول الأساسي (مشروط بالوقت) - فقط إذا كانت النتيجة السابقة ربح/تعادل
            elif last_result_was_win:
                current_second = datetime.now(timezone.utc).second
                if current_second in ENTRY_SECONDS:
                    print(f"🧠 [BASE ENTRY] Second {current_second} matched. Preparing Base Entry (DIFF {DIFF_BASE}).")
                    trade_params = {
                        "contract_type": CONTRACT_TYPE_BASE,
                        "digit": DIFF_BASE
                    }
                else:
                    # انتظار شرط الثانية
                    print(f"⏳ [WAIT] Awaiting next matching SECOND ({ENTRY_SECONDS}). Current: {current_second}")
                    time.sleep(0.5) 
                    continue # العودة لبداية الحلقة لجلب البيانات الجديدة والتحقق من التوقف

            # 3. 🛒 تنفيذ الصفقة (Buy)
            if trade_params:
                
                total_stake = current_data['current_stake'] 
                stake_per_trade = total_stake 
                currency_to_use = current_data['currency']
                
                if stake_per_trade < 0.35:
                     stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: Stake ({stake_per_trade}) is less than minimum 0.35.")
                     return
                
                params = trade_params
                contract_type_full = f"{params['contract_type']}{params['digit']}"
                entry_type = "Base Stake" if not is_martingale_step else f"Martingale Step {current_data['current_step'] + 1}" # +1 لأننا سنبدأ المضاعفة هنا

                trade_request = {
                    "buy": 1, "price": stake_per_trade,
                    "parameters": {
                        "amount": stake_per_trade, 
                        "basis": "stake", 
                        "contract_type": params['contract_type'],
                        "currency": currency_to_use, 
                        "duration": DURATION, 
                        "duration_unit": DURATION_UNIT,
                        "symbol": SYMBOL, 
                        "barrier": params['digit'] 
                    }
                }
                
                print(f"🧠 [ENTRY] Type: {entry_type}. Stake: {stake_per_trade:.2f}. Contract: {contract_type_full}. Sending BUY request...")
                buy_response = sync_send_and_recv(ws, trade_request, "buy", timeout=15)
                
                if 'error' in buy_response:
                    print(f"❌ [API Buy Error] {contract_type_full} failed: {buy_response['error']['message']}. Halting cycle.")
                    stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: {buy_response['error']['message']}")
                    return
                
                contract_id = buy_response['buy']['contract_id']
                print(f"⏳ [SETTLEMENT] Contract {contract_id} opened. Waiting for result...")
                
                # 4. ⚖️ الانتظار للتسوية (في نفس الجلسة)
                # ننتظر حتى نحصل على رسالة "proposal_open_contract" مع is_sold=1
                while True:
                    response = json.loads(ws.recv())
                    
                    if response.get('msg_type') == 'proposal_open_contract' and response['proposal_open_contract'].get('is_sold') == 1:
                        contract_info = response['proposal_open_contract']
                        
                        # 5. تطبيق المنطق ومعالجة النتيجة
                        updated_data = process_trade_result(email, contract_info)
                        session_data = updated_data # تحديث session_data للحلقة التالية والتحقق من التوقف
                        
                        if not updated_data.get('is_running'):
                            # تم الوصول إلى TP/SL
                            stop_bot(email, clear_data=True, stop_reason=updated_data['stop_reason'])
                            return
                        break 
                    
                    if 'error' in response:
                        print(f"❌ [SETTLEMENT ERROR] Error during settlement wait: {response['error'].get('message', 'Unknown API Error')}. Retrying connection next cycle.")
                        time.sleep(2) 
                        break # كسر حلقة الانتظار للمحاولة مجددا في دورة جديدة
                    
                    # لمنع استهلاك المعالج
                    time.sleep(0.01)

            else:
                # إذا لم يتم الدخول بسبب شرط الثواني في وضع Base
                time.sleep(0.1)

        except websocket.WebSocketTimeoutException:
            print("❌ [WS Timeout] Connection operation timed out. Retrying connection next cycle.")
            time.sleep(RECONNECT_DELAY)
        except Exception as process_error:
            print(f"\n\n💥💥 [CRITICAL PROCESS CRASH] The entire bot process for {email} failed with an unhandled exception: {process_error}")
            traceback.print_exc()
            time.sleep(RECONNECT_DELAY)
        finally:
            # 6. 🚪 إغلاق الاتصال بعد كل دورة تداول
            if ws:
                try: ws.close()
                except: pass

    print(f"🛑 [PROCESS] Bot process loop ended for {email}.")

# --- FLASK APP SETUP AND ROUTES (لوحة التحكم) ---

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
        
        {% if session_data and session_data.stop_reason and session_data.stop_reason not in ["Running", "Displayed"] %}
            <p style="color:red; font-weight:bold;">Last Reason: {{ session_data.stop_reason }} (Data Cleared)</p>
        {% elif session_data and session_data.stop_reason == "Displayed" %}
             <p style="color:red; font-weight:bold;">Last Stop Reason (Cleared): {{ session_data.stop_reason }}</p>
        {% endif %}
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set strategy = 'DIGIT DIFF (Base: ' + diff_base|string + ' @ ' + entry_seconds|string + 's | Martingale: ' + diff_martingale|string + ' IMMEDIATE) - DURATION: ' + duration|string + ' TICK - Multiplier: x' + martingale_multiplier|string + ' - SL/TP Triggers Auto Stop & Clear' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing every 1 second)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Consecutive Losses: {{ session_data.consecutive_losses }} / {{ session_data.max_loss }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
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
        
        <label for="stake">Base Stake (Minimum 0.35):</label><br>
        <input type="number" id="stake" name="stake" value="{{ session_data.base_stake|round(2) if session_data else 0.35 }}" step="0.01" min="0.35" required><br>
        
        <label for="tp">TP Target (USD/tUSDT):</label><br>
        <input type="number" id="tp" name="tp" value="{{ session_data.tp_target|round(2) if session_data else 10.0 }}" step="0.01" required><br>
        
        <label for="max_loss">Max Consecutive Losses (2 means 1 Martingale step):</label><br>
        <input type="number" id="max_loss" name="max_loss" value="{{ session_data.max_loss if session_data.get('max_loss') is not none else 2 }}" step="1" min="1" required><br>

        <button type="submit" style="background-color: green; color: white;">🚀 Start Bot</button>
    </form>
{% endif %}
<hr>
<a href="{{ url_for('logout') }}" style="display: block; text-align: center; margin-top: 15px; font-size: 1.1em;">Logout</a>

<script>
    function autoRefresh() {
        var isRunning = {{ 'true' if session_data and session_data.is_running else 'false' }};
        
        if (isRunning) {
            // تحديث كل 1 ثانية
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
    
    global MAX_CONSECUTIVE_LOSSES, DURATION, ENTRY_SECONDS, MARTINGALE_MULTIPLIER, DIFF_BASE, DIFF_MARTINGALE
    max_losses_for_display = session_data.get('max_loss', MAX_CONSECUTIVE_LOSSES)

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Running", "Displayed", "Disconnected (Auto-Retry)"]:
        reason = session_data["stop_reason"]
        
        if reason.startswith("SL Reached"): flash(f"🛑 STOP: Max consecutive losses reached! ({reason}). Session data cleared.", 'error')
        elif reason == "TP Reached": flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached). Session data cleared.", 'success')
        elif reason.startswith("API Buy Error") or reason.startswith("Auth Error") or reason.startswith("Critical"): flash(f"❌ Critical Error: {reason}. Check your token and connection.", 'error')
            
        session_data['stop_reason'] = "Displayed"
        if not reason.startswith("SL Reached") and reason != "TP Reached":
            save_session_data(email, session_data)
    
    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        max_consecutive_losses=max_losses_for_display,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        duration=DURATION,
        symbol=SYMBOL,
        entry_seconds=ENTRY_SECONDS,
        diff_base=DIFF_BASE,
        diff_martingale=DIFF_MARTINGALE
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
    global active_processes, MAX_CONSECUTIVE_LOSSES, MARTINGALE_MULTIPLIER, DIFF_BASE, DIFF_MARTINGALE, DURATION
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
        
        if stake < 0.35: raise ValueError("Stake too low for single entry (minimum 0.35 required).")
            
        tp = float(request.form['tp'])
        max_loss = int(request.form['max_loss'])
        if max_loss < 1: max_loss = 1
        
    except ValueError as e:
        flash(f"Invalid stake, TP, or Max Loss value: {e}. (Base Stake must be >= 0.35, Max Loss >= 1).", 'error')
        return redirect(url_for('index'))
    
    MAX_CONSECUTIVE_LOSSES = max_loss
    current_data['max_loss'] = max_loss
    
    current_data['api_token'] = token
    current_data['base_stake'] = stake
    current_data['tp_target'] = tp
    current_data['current_stake'] = stake
    current_data['currency'] = currency
    current_data['account_type'] = account_type
    current_data['last_trade_result'] = "WIN" # لضمان بدء الدخول الأساسي المشروط بالثانية
    save_session_data(email, current_data) 

    process = Process(target=bot_core_logic, args=(email, token, stake, tp, currency, account_type, max_loss))
    process.daemon = True
    process.start()
    
    with PROCESS_LOCK: active_processes[email] = process
    
    flash(f'Bot started successfully. Strategy: DIGIT DIFF (Base: {DIFF_BASE} @ {ENTRY_SECONDS}s | Martingale: {DIFF_MARTINGALE} IMMEDIATE), Max Loss: {max_loss}, Martingale: x{MARTINGALE_MULTIPLIER} - DURATION: {DURATION} Tick - **SL/TP Triggers Auto Stop & Clear**.', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session: return redirect(url_for('auth_page'))
    
    stop_bot(session['email'], clear_data=True, stop_reason="Stopped Manually")
    
    response = make_response(redirect(url_for('index')))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    flash('Bot stopped and session data cleared. Refreshing to show current status.', 'success')
    return response

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

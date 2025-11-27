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
# BOT CONSTANT SETTINGS (R_100 | Double H/L | Last Digit Match | x19.0)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"       
DURATION = 5           # مدة الصفقة 5 تيك 
DURATION_UNIT = "t"    

# إعدادات المضاعفة والتحليل
TICK_SAMPLE_SIZE = 2 # للتحقق من تطابق الرقم الأخير
MAX_CONSECUTIVE_LOSSES = 2    
MARTINGALE_MULTIPLIER = 19.0 # ⬅️ معامل المضاعفة x19.0

# الثواني المسموح بها للدخول
ENTRY_SECONDS = [0, 10, 20, 30, 40, 50] 

# إعدادات العقد 
# نوع العقد الأساسي: CALL (لـ HIGHER) و PUT (لـ LOWER)
BARRIER_OFFSET = 0.05 # قيمة الحاجز (+0.05 و -0.05) ⬅️ تم التعديل

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# ==========================================================
# GLOBAL STATE 
# ==========================================================
active_processes = {}
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
    "last_two_digits": [9, 9],
    "last_digits_history": [],
    "last_prices_history": []
}

# --- Persistence functions (وظائف حفظ واسترجاع الحالة) ---
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
    global active_processes
    current_data = get_session_data(email)
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason
    
    if stop_reason not in ["SL Reached: Consecutive losses", "TP Reached"]: 
        save_session_data(email, current_data)

    with PROCESS_LOCK:
        if email in active_processes:
            process = active_processes[email]
            if process.is_alive():
                print(f"🛑 [INFO] Terminating Process for {email}...")
                process.terminate() 
            del active_processes[email]

    # ⬅️ هذا الجزء يضمن مسح البيانات نهائياً عند الوصول للحد الأقصى للخسارة أو الربح
    if clear_data and stop_reason in ["SL Reached: Consecutive losses", "TP Reached"]: 
        delete_session_data(email) 
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data CLEARED from database.")
    elif clear_data:
        delete_session_data(email)
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from database.")
    else:
        print(f"⚠ [INFO] Process closed for {email}. Attempting immediate reconnect.")
# --- End of Persistence and Control functions ---

# ==========================================================
# TRADING BOT FUNCTIONS (دوال منطق التداول)
# ==========================================================

def check_entry_condition(prices, last_digits):
    """
    التحقق من شرط الدخول لـ Double HIGHER/LOWER.
    الشرط: تطابق الرقم الأخير في آخر 2 تيك.
    """
    global TICK_SAMPLE_SIZE, BARRIER_OFFSET
    
    if len(last_digits) != TICK_SAMPLE_SIZE:
        return None
        
    digit_t1 = last_digits[0]
    digit_t2 = last_digits[1]
    
    # 1. التحقق من تطابق الرقم الأخير (الشرط الأساسي)
    if digit_t1 == digit_t2:
        # إذا تطابق الرقم، نجهز صفقتين (HIGHER و LOWER)
        # الصفقة 1 (HIGHER): نوع CALL، حاجز موجب
        # الصفقة 2 (LOWER): نوع PUT، حاجز سالب
        return [
            {"contract_type": "CALL", "barrier": f"+{BARRIER_OFFSET}"},
            {"contract_type": "PUT", "barrier": f"-{BARRIER_OFFSET}"}
        ]
    else:
        return None


def calculate_martingale_stake(base_stake, current_step, multiplier):
    """ منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة (x19.0) لعدد الخطوات """
    if current_step == 0: 
        return base_stake
    # المضاعف X^Step
    return base_stake * (multiplier ** current_step)


def apply_martingale_logic(email):
    """ يطبق منطق المضاعفة المشروطة بعد تسوية العقود """
    global MARTINGALE_MULTIPLIER, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    # يتم تجميع أرباح/خسائر كلتا الصفقتين (HIGHER و LOWER)
    if len(current_data['contract_profits']) != 2:
        return

    total_profit_loss = sum(current_data['contract_profits'].values())
    current_data['current_profit'] += total_profit_loss
    
    # 🛑 1. التحقق من Take Profit (TP)
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return
        
    base_stake_used = current_data['base_stake']
    
    # ❌ حالة الخسارة (Loss) 
    if total_profit_loss < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        # 🛑 2. التحقق من Max Consecutive Losses (الإيقاف التام)
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason="SL Reached: Consecutive losses")
            return
            
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        print(f"🔄 [LOSS] PnL: {total_profit_loss:.2f}. Consecutive: {current_data['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}. Next Total Stake (x{MARTINGALE_MULTIPLIER}^{current_data['current_step']}) calculated: {round(new_stake, 2):.2f}. Awaiting next ENTRY_SECOND.")
        
    # ✅ حالة الربح (Win)
    else: 
        current_data['total_wins'] += 1 if total_profit_loss > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        
        entry_result_tag = "WIN" if total_profit_loss > 0 else "DRAW/SPLIT"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}. Awaiting next ENTRY_SECOND.")

    # مسح بيانات العقد
    current_data['current_entry_id'] = None
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Con. Loss: {current_data['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}, Total Stake: {current_data['current_stake']:.2f}, Strategy: Double H/L")
    
    save_session_data(email, current_data) 


def handle_contract_settlement(email, contract_id, profit_loss):
    """ معالجة نتيجة عقد واحد """
    current_data = get_session_data(email)
    
    if contract_id not in current_data['open_contract_ids']:
        return

    current_data['contract_profits'][contract_id] = profit_loss
    
    if contract_id in current_data['open_contract_ids']:
        current_data['open_contract_ids'].remove(contract_id)
        
    save_session_data(email, current_data)
    
    # نطبق منطق المضاعفة فقط بعد تسوية كلتا الصفقتين (يجب أن يكون لدينا صفقتان في contract_profits)
    if not current_data['open_contract_ids'] and len(current_data['contract_profits']) == 2:
        apply_martingale_logic(email)


def sync_send_and_recv(ws, request_data, expect_msg_type, timeout=10):
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


def bot_core_logic(email, token, stake, tp, currency, account_type):
    """ Core bot logic (Synchronous Polling) """
    
    print(f"🚀🚀 [CORE START] Bot logic started for {email} (Synchronous Polling).")
    
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": stake, "stop_reason": "Running", "last_entry_time": 0,
        "last_entry_price": 0.0, "last_tick_data": None, "currency": currency,
        "account_type": account_type, "last_valid_tick_price": 0.0,
        "current_entry_id": None, "open_contract_ids": [], "contract_profits": {},
        "last_two_digits": [9, 9],
        "last_digits_history": [],
        "last_prices_history": []
    })
    save_session_data(email, session_data)
    
    while True:
        current_data = get_session_data(email)
        if not current_data.get('is_running'): break
        
        is_contract_pending = current_data.get('open_contract_ids')

        # --- منطق الانتظار والتحقق من الثواني (مطبق الآن على جميع حالات الدخول) ---
        if not is_contract_pending:
            now = datetime.now()
            current_second = now.second
            
            # تحقق مما إذا كانت الثانية الحالية هي إحدى ثواني الدخول المحددة
            if current_second not in ENTRY_SECONDS:
                next_entry_second = min([s for s in ENTRY_SECONDS if s > current_second], default=ENTRY_SECONDS[0])
                
                if next_entry_second > current_second:
                    wait_time = next_entry_second - current_second
                else: 
                    wait_time = (60 - current_second) + next_entry_second 

                if wait_time > 0 and wait_time < 60:
                    time.sleep(wait_time + 0.1) 
                    continue 
            
            time.sleep(0.5) 
            
        elif is_contract_pending:
            # حالة وجود عقود مفتوحة
            time.sleep(0.5)
            pass

        # --- نهاية منطق الانتظار ---

        # --- بداية دورة الاتصال والمعالجة (للدخول أو الاستعادة) ---
        
        ws = None
        try:
            print(f"🔗 [PROCESS] Attempting to CONNECT...")
            ws = websocket.create_connection(WSS_URL, timeout=10) 
            
            # أ. الترخيص
            auth_response = sync_send_and_recv(ws, {"authorize": token}, "authorize")
            if 'error' in auth_response:
                stop_bot(email, clear_data=True, stop_reason=f"Auth Error: {auth_response['error']['message']}")
                break
            
            # ب. منطق الاستعادة (إذا كان هناك عقد/عقود مفتوحة)
            if is_contract_pending:
                for contract_id in list(current_data['open_contract_ids']):
                    print(f"🔍 [RECOVERY] Contract ID {contract_id} pending settlement. Resubscribing...")
                    
                    settlement_response = sync_send_and_recv(
                        ws, 
                        {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}, 
                        "proposal_open_contract"
                    )
                    
                    if 'error' in settlement_response:
                        print(f"❌ [RECOVERY ERROR] Cannot retrieve contract status for {contract_id}: {settlement_response['error']['message']}")
                        continue

                    contract_info = settlement_response['proposal_open_contract']
                    if contract_info.get('is_sold') == 1:
                        handle_contract_settlement(email, contract_id, contract_info['profit'])
                        print(f"✅ [RECOVERY] Contract {contract_id} settled successfully. Logic applied.")
                        
                        if 'subscription_id' in settlement_response:
                            ws.send(json.dumps({"forget": settlement_response['subscription_id']}))
                    else:
                        print(f"⚠ [RECOVERY] Contract {contract_id} still open. Will retry settlement next loop.")
                
                continue 

            # --- منطق التداول العادي (لا يوجد عقود مفتوحة) ---

            # 2. جلب 2 تيك تاريخي
            history_request = {
                "ticks_history": SYMBOL,
                "end": "latest",
                "count": TICK_SAMPLE_SIZE, # القيمة 2
                "style": "ticks"
            }
            history_response = sync_send_and_recv(ws, history_request, "history", timeout=10)
            
            if 'error' in history_response:
                print(f"❌ [HISTORY ERROR] Failed to get ticks history: {history_response['error']['message']}")
                continue 
            
            if not history_response.get('history') or 'prices' not in history_response['history']:
                print("❌ [DATA ERROR] Received history response is missing 'prices' array. Skipping entry.")
                continue

            prices = [float(p) for p in history_response['history']['prices'] if p is not None]
            
            if len(prices) < TICK_SAMPLE_SIZE:
                 print(f"❌ [DATA ERROR] Received only {len(prices)} ticks, expected {TICK_SAMPLE_SIZE}. Skipping entry.")
                 continue
            
            last_digits = [int(str(p)[-1]) for p in prices]
            
            # تحديث الحالة للتحليل/العرض
            current_data['last_digits_history'] = last_digits
            current_data['last_prices_history'] = prices
            current_data['last_valid_tick_price'] = prices[-1]
            save_session_data(email, current_data)

            # 3. التحليل واتخاذ القرار 
            trade_params = check_entry_condition(prices, last_digits)
            
            print(f"🧠 [ANALYSIS] Last 2 Tick Digits: {last_digits}. Condition (Digits Match) Met? {'✅ YES' if trade_params else '❌ NO'}.")

            if trade_params:
                
                if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
                    stop_bot(email, clear_data=True, stop_reason="SL Reached: Max Consecutive Losses reached.")
                    continue
                
                # 4. تنفيذ الصفقتين (Buy)
                total_stake = current_data['current_stake']
                stake_per_trade = round(total_stake / len(trade_params), 2) # تقسيم الرهان على 2 صفقة
                currency_to_use = current_data['currency']
                
                
                newly_opened_contracts = []
                
                print(f"🧠 [DOUBLE ENTRY] Total Stake: {total_stake:.2f}. Stake per trade: {stake_per_trade:.2f}. Types: HIGHER/LOWER (+/- {BARRIER_OFFSET}).")

                for params in trade_params:
                    trade_request = {
                        "buy": 1, "price": stake_per_trade,
                        "parameters": {
                            "amount": stake_per_trade, "basis": "stake", "contract_type": params['contract_type'],
                            "currency": currency_to_use, "duration": DURATION, "duration_unit": DURATION_UNIT,
                            "symbol": SYMBOL, "barrier": params['barrier']
                        }
                    }
                    
                    print(f"   [ENTRY {params['contract_type']}] Sending BUY request...")
                    buy_response = sync_send_and_recv(ws, trade_request, "buy", timeout=15)
                    
                    if 'error' in buy_response:
                        print(f"❌ [API Buy Error] {params['contract_type']} failed: {buy_response['error']['message']}. Halting cycle.")
                        stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: {buy_response['error']['message']}")
                        return
                    
                    contract_id = buy_response['buy']['contract_id']
                    newly_opened_contracts.append(contract_id)
                
                # 5. تحديث الحالة بعد نجاح الصفقتين
                current_data['open_contract_ids'] = newly_opened_contracts
                current_data['current_entry_id'] = time.time()
                current_data['last_digits_history'] = []
                current_data['last_prices_history'] = []
                save_session_data(email, current_data)
                
                print(f"⏳ [SETTLEMENT] Successfully opened 2 contracts: {newly_opened_contracts}. Waiting for settlement...")
                
                # 6. انتظار تسوية الصفقتين
                # نعتمد على منطق الاستعادة في الحلقة التالية لمعالجة التسوية
                
            else:
                print("❌ [SKIP] Condition not met (Digits not matching). Awaiting next entry second.")

            
        except websocket.WebSocketTimeoutException:
            print("❌ [WS Timeout] Connection operation timed out. Retrying connection next cycle.")
        except Exception as process_error:
            print(f"\n\n💥💥 [CRITICAL PROCESS CRASH] The entire bot process for {email} failed with an unhandled exception: {process_error}")
            traceback.print_exc()
            stop_bot(email, clear_data=True, stop_reason="Critical Python Crash")
        finally:
            if ws:
                try:
                    # 7. إغلاق الاتصال بعد العملية
                    ws.close()
                    print("🛑 [PROCESS] Connection CLOSED.")
                except:
                    pass

    print(f"🛑 [PROCESS] Bot process loop ended for {email}.")

# --------------------------------------------------------------------------------------------------

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
    {% set entry_timing = 'Fixed Seconds (' + entry_seconds|join(', ') + ') ONLY' %}
    {% set strategy = 'Double H/L (R_100 - Condition: Last Digit Match in ' + tick_sample_size|string + ' Ticks, Barriers: +/- ' + barrier_offset|string + ', Timing: ' + entry_timing + ' / Martingale on Loss - x' + martingale_multiplier|string + ' Martingale, Max ' + max_consecutive_losses|string + ' Losses - AUTO STOP & CLEAR)' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing every 1 second)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Total Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Stake Per Trade: {{ session_data.currency }} {{ (session_data.current_stake / 2)|round(2) }}</p>
    <p>Consecutive Losses: {{ session_data.consecutive_losses }} / {{ max_consecutive_losses }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: purple;">Last Digits Sampled: {{ session_data.last_digits_history }} | Prices: {{ session_data.last_prices_history }}</p>
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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Running", "Displayed", "Disconnected (Auto-Retry)"]:
        reason = session_data["stop_reason"]
        
        if reason.startswith("SL Reached"): flash(f"🛑 STOP: Max consecutive losses reached! ({reason}). Session data cleared.", 'error')
        elif reason == "TP Reached": flash(f"✅ GOAL: Profit target ({session_data['tp_target']} {session_data.get('currency', 'USD')}) reached successfully! (TP Reached). Session data cleared.", 'success')
        elif reason.startswith("API Buy Error") or reason.startswith("Auth Error") or reason.startswith("Critical"): flash(f"❌ Critical Error: {reason}. Check your token and connection.", 'error')
            
        session_data['stop_reason'] = "Displayed"
        # ⬅️ لا نحفظ هنا لنتأكد من أننا لم نقم بمسحها في SL/TP
        if not reason.startswith("SL Reached") and reason != "TP Reached":
            save_session_data(email, session_data)
    
    contract_type_name = "Double H/L"

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        duration=DURATION,
        tick_sample_size=TICK_SAMPLE_SIZE,
        symbol=SYMBOL,
        contract_type_name=contract_type_name,
        entry_seconds=ENTRY_SECONDS,
        barrier_offset=BARRIER_OFFSET
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
    
    flash(f'Bot started successfully. Strategy: Double H/L (Digits Match), Barriers: +/- {BARRIER_OFFSET}, Duration: {DURATION} Ticks, Max Loss: {MAX_CONSECUTIVE_LOSSES}, Martingale: x{MARTINGALE_MULTIPLIER} - SL/TP Triggers Auto Stop & Clear.', 'success')
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

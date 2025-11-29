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
# BOT CONSTANT SETTINGS (R_100 | Digit DIFF 5 | x14.0 | D: 1 Tick)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"       
DURATION = 1           
DURATION_UNIT = "t"    

# إعدادات المضاعفة والتحليل
TICK_SAMPLE_SIZE = 1 # ⬅️ تحليل 1 تيك فقط
MAX_CONSECUTIVE_LOSSES = 2    # ⬅️ أقصى خسارة متتالية (افتراضي)
MARTINGALE_MULTIPLIER = 14.0 # ⬅️ معامل المضاعفة

# الثواني المحددة للدخول (تطبق على جميع الصفقات)
ENTRY_SECONDS = [0, 10, 20, 30, 40, 50] 

BARRIER_OFFSET = 0.1 

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"

# ==========================================================
# GLOBAL STATE AND CONTROL FUNCTIONS (الحالة العامة ووظائف التحكم)
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
    "max_loss": 2, # ⬅️ تم التعديل
}

active_processes = {}
PROCESS_LOCK = Lock()
TRADE_LOCK = Lock() 

# --- Persistence functions (وظائف حفظ واسترجاع الحالة) ---
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
# TRADING BOT FUNCTIONS (دوال منطق التداول)
# ==========================================================

def check_entry_condition(prices, last_digits):
    """
    التحقق من شرط الدخول: الرقم الأخير هو 9 ليدخل صفقة DIGITDIFF 5.
    (يتم التحقق من آخر رقم واحد فقط)
    """
    if not last_digits:
        return []

    # نحصل على الرقم الأخير
    last_digit = last_digits[-1]

    # الشرط: إذا كان الرقم الأخير هو 9
    if last_digit == 9:
        # تجهيز صفقة DIGITDIFF 5 (الربح إذا كان آخر رقم ليس 5)
        return [
            {"contract_type": "DIGITDIFF", "digit": 5}
        ]
    
    return []


def calculate_martingale_stake(base_stake, current_step, multiplier):
    """ منطق المضاعفة: ضرب الرهان الأساسي في معامل المضاعفة (x14.0) لعدد الخطوات """
    if current_step == 0: 
        return base_stake
    return base_stake * (multiplier ** current_step)


def apply_martingale_logic(email):
    """ يطبق منطق المضاعفة المشروطة بعد تسوية العقود """
    global MARTINGALE_MULTIPLIER, MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return
    
    max_losses_for_check = current_data.get('max_loss', MAX_CONSECUTIVE_LOSSES)

    if len(current_data['contract_profits']) != 1: 
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
        if current_data['consecutive_losses'] >= max_losses_for_check: 
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason="SL Reached: Consecutive losses")
            return
            
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        # **هنا:** ننتظر الشرط والثانية المحددة للدخول التالي (المضاعفة المشروطة)
        print(f"🔄 [LOSS] PnL: {total_profit_loss:.2f}. Consecutive: {current_data['consecutive_losses']}/{max_losses_for_check}. Next Stake (x{MARTINGALE_MULTIPLIER}^{current_data['current_step']}) calculated: {round(new_stake, 2):.2f}. Awaiting next matching SECOND ({ENTRY_SECONDS}) and tick (Conditional Martingale Entry).")
        
    # ✅ حالة الربح (Win)
    else: 
        current_data['total_wins'] += 1 if total_profit_loss > 0 else 0 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        
        # **هنا:** ننتظر الشرط والثانية المحددة للدخول التالي
        entry_result_tag = "WIN" if total_profit_loss > 0 else "DRAW"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}. Resetting. Awaiting next matching SECOND ({ENTRY_SECONDS}) and tick (Conditional Base Entry).")

    # مسح بيانات العقد
    current_data['current_entry_id'] = None
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    currency = current_data.get('currency', 'USD')
    print(f"[LOG {email}] PNL: {currency} {current_data['current_profit']:.2f}, Con. Loss: {current_data['consecutive_losses']}/{max_losses_for_check}, Stake: {current_data['current_stake']:.2f}, Strategy: Digit DIFF 5 (Fully Conditional)")
    
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
    
    # نطبق منطق المضاعفة فوراً بعد تسوية الصفقة
    if not current_data['open_contract_ids'] and len(current_data['contract_profits']) == 1:
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


def bot_core_logic(email, token, stake, tp, currency, account_type, max_loss):
    """ Core bot logic (Synchronous Polling) """
    
    print(f"🚀🚀 [CORE START] Bot logic started for {email} (Fully Conditional Strategy: Digit DIFF 5 on Last Digit 9).")
    
    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": stake, "stop_reason": "Running", "last_entry_time": 0,
        "last_entry_price": 0.0, "last_tick_data": None, "currency": currency,
        "account_type": account_type, "last_valid_tick_price": 0.0,
        "current_entry_id": None, "open_contract_ids": [], "contract_profits": {},
        "last_two_digits": [9, 9],
        "last_digits_history": [],
        "last_prices_history": [],
        "max_loss": max_loss 
    })
    save_session_data(email, session_data)
    
    ws = None
    
    while True:
        current_data = get_session_data(email)
        
        # 1. 🛑 التحقق الأول (في بداية الحلقة)
        if not current_data.get('is_running'): 
            print(f"🛑 [CORE SHUTDOWN] Flag set to STOP for {email}. Breaking loop.")
            if ws:
                try: ws.close()
                except: pass
            break 
        
        is_contract_pending = current_data.get('open_contract_ids')
        is_martingale_step = current_data.get('consecutive_losses', 0) > 0 
        max_losses_for_check = current_data.get('max_loss', MAX_CONSECUTIVE_LOSSES)

        # منطق الانتظار
        if not is_contract_pending:
            time.sleep(0.5) 
        elif is_contract_pending:
            time.sleep(0.5)
            pass

        try:
            print(f"🔗 [PROCESS] Attempting to CONNECT...")
            ws = websocket.create_connection(WSS_URL, timeout=10) 
            
            # 2. 🛑 التحقق الثاني (بعد الاتصال مباشرة)
            current_data = get_session_data(email)
            if not current_data.get('is_running'): 
                print("🛑 [CORE SHUTDOWN] Stopping after successful connect.")
                break

            # أ. الترخيص
            auth_response = sync_send_and_recv(ws, {"authorize": token}, "authorize")
            if 'error' in auth_response:
                stop_bot(email, clear_data=True, stop_reason=f"Auth Error: {auth_response['error']['message']}")
                break
            
            # 3. 🛑 التحقق الثالث (بعد الترخيص)
            current_data = get_session_data(email)
            if not current_data.get('is_running'): 
                print("🛑 [CORE SHUTDOWN] Stopping after successful authorization.")
                break
            
            # ب. منطق الاستعادة (إذا كان هناك عقد مفتوح)
            if is_contract_pending:
                for contract_id in list(current_data['open_contract_ids']):
                    print(f"🔍 [RECOVERY] Contract ID {contract_id} pending settlement. Resubscribing...")
                    settlement_response = sync_send_and_recv(
                        ws, {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}, 
                        "proposal_open_contract"
                    )
                    if 'error' in settlement_response:
                        print(f"❌ [RECOVERY ERROR] Cannot retrieve contract status for {contract_id}: {settlement_response['error']['message']}")
                        continue
                    contract_info = settlement_response['proposal_open_contract']
                    if contract_info.get('is_sold') == 1:
                        handle_contract_settlement(email, contract_id, contract_info['profit'])
                        if 'subscription_id' in settlement_response:
                            ws.send(json.dumps({"forget": settlement_response['subscription_id']}))
                    else:
                        print(f"⚠ [RECOVERY] Contract {contract_id} still open. Will retry settlement next loop.")
                
                if current_data.get('open_contract_ids'): continue
                else: pass 
                
            # ⬅️ منطق التداول العادي (لا يوجد عقود مفتوحة)

            trade_params = []
            current_second = datetime.now(timezone.utc).second
            
            # 🛑 الشرط الأول: انتظار الثانية المحددة
            if current_second not in ENTRY_SECONDS: 
                print(f"⌛ [SKIP] Current second {current_second} is not in entry list {ENTRY_SECONDS}. Awaiting next entry second.")
                continue 
                
            else: 
                # الشرط الثاني: الثانية المحددة + التحليل (لجميع أنواع الدخول)

                # 2. جلب 1 تيك تاريخي فقط
                history_request = {
                    "ticks_history": SYMBOL, "end": "latest", "count": 1, "style": "ticks" # ⬅️ تحليل 1 تيك
                }
                history_response = sync_send_and_recv(ws, history_request, "history", timeout=10)
                
                if 'error' in history_response:
                    print(f"❌ [HISTORY ERROR] Failed to get ticks history: {history_response['error']['message']}. Skipping entry.")
                    continue 
                
                if not history_response.get('history') or 'prices' not in history_response['history']:
                    print("❌ [DATA ERROR] Received history response is missing 'prices' array. Skipping entry.")
                    continue

                prices = [float(p) for p in history_response['history']['prices'] if p is not None]
                
                if len(prices) < 1: # ⬅️ التحقق من 1 تيك
                    print(f"❌ [DATA ERROR] Received only {len(prices)} ticks, expected 1. Skipping entry.")
                    continue
                
                last_digits = [int(str(p)[-1]) for p in prices]
                
                # تحديث الحالة للتحليل/العرض
                current_data['last_digits_history'] = last_digits
                current_data['last_prices_history'] = prices
                current_data['last_valid_tick_price'] = prices[-1]
                save_session_data(email, current_data)

                # 3. التحليل واتخاذ القرار (باستخدام شرط الرقم 9)
                trade_params = check_entry_condition(prices, last_digits)


            # 4. تنفيذ الصفقة (Buy)
            if trade_params:
                
                # 4. 🛑 التحقق الرابع (قبل محاولة الشراء)
                current_data = get_session_data(email)
                if not current_data.get('is_running'): 
                    print("🛑 [CORE SHUTDOWN] Stopping before sending BUY request.")
                    break
                
                if current_data['consecutive_losses'] >= max_losses_for_check:
                    stop_bot(email, clear_data=True, stop_reason="SL Reached: Max Consecutive Losses reached.")
                    continue
                
                total_stake = current_data['current_stake'] 
                stake_per_trade = total_stake 
                currency_to_use = current_data['currency']
                
                if stake_per_trade < 0.35:
                     stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: Stake ({stake_per_trade}) is less than minimum 0.35.")
                     continue
                
                newly_opened_contracts = []
                params = trade_params[0]
                
                entry_type = "Base Stake" if not is_martingale_step else f"Martingale Step {current_data['current_step']}"
                print(f"🧠 [CONDITIONAL ENTRY] Type: {entry_type}. Stake: {stake_per_trade:.2f}. Contract: {params['contract_type']} {params['digit']} (Second: {current_second}, Last Digit: {last_digits[-1]}).")

                trade_request = {
                    "buy": 1, "price": stake_per_trade,
                    "parameters": {
                        "amount": stake_per_trade, "basis": "stake", "contract_type": params['contract_type'],
                        "currency": currency_to_use, "duration": DURATION, "duration_unit": DURATION_UNIT,
                        "symbol": SYMBOL, "barrier": params['digit']
                    }
                }
                
                print(f"   [ENTRY {params['contract_type']} {params['digit']}] Sending BUY request...")
                buy_response = sync_send_and_recv(ws, trade_request, "buy", timeout=15)
                
                if 'error' in buy_response:
                    print(f"❌ [API Buy Error] {params['contract_type']} {params['digit']} failed: {buy_response['error']['message']}. Halting cycle.")
                    stop_bot(email, clear_data=True, stop_reason=f"API Buy Error: {buy_response['error']['message']}")
                    return
                
                contract_id = buy_response['buy']['contract_id']
                newly_opened_contracts.append(contract_id)
                
                # 5. تحديث الحالة بعد نجاح الصفقة الواحدة
                current_data['open_contract_ids'] = newly_opened_contracts
                current_data['current_entry_id'] = time.time()
                current_data['last_digits_history'] = last_digits 
                current_data['last_prices_history'] = prices 
                save_session_data(email, current_data)
                
                print(f"⏳ [SETTLEMENT] Successfully opened 1 contract: {newly_opened_contracts}. Waiting for settlement...")
                
            else:
                # هذا يحدث إذا كنا في الثانية المحددة، ولم يتحقق شرط الرقم 9
                print(f"❌ [SKIP] Last Digit {last_digits[-1] if last_digits else 'N/A'} at second {current_second} did not meet entry condition (Required: 9). Awaiting next matching tick.")

            
        except websocket.WebSocketTimeoutException:
            print("❌ [WS Timeout] Connection operation timed out. Retrying connection next cycle.")
        except Exception as process_error:
            print(f"\n\n💥💥 [CRITICAL PROCESS CRASH] The entire bot process for {email} failed with an unhandled exception: {process_error}")
            traceback.print_exc()
            stop_bot(email, clear_data=True, stop_reason="Critical Python Crash")
        finally:
            if ws:
                try:
                    ws.close()
                    pass
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
    {% set strategy = 'Digit DIFF 5 (R_100 - Conditional Entry: Last Digit = 9 @ ' + entry_seconds|string + 's, Duration: 1 Tick / Martingale: x' + martingale_multiplier|string + ' - FULLY CONDITIONAL - SL/TP Triggers Auto Stop & Clear)' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing every 1 second)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p>Current Stake: {{ session_data.currency }} {{ session_data.current_stake|round(2) }}</p>
    <p>Consecutive Losses: {{ session_data.consecutive_losses }} / {{ session_data.max_loss }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: purple;">Last Digits Sampled: {{ session_data.last_digits_history }}</p>
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
        
        <label for="max_loss">Max Consecutive Losses (e.g. 2 to stop after the 2nd loss):</label><br>
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
    
    global MAX_CONSECUTIVE_LOSSES, DURATION, ENTRY_SECONDS, MARTINGALE_MULTIPLIER
    # تأكد من استخدام القيمة المخزنة
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
        tick_sample_size=TICK_SAMPLE_SIZE,
        symbol=SYMBOL,
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
    global active_processes, MAX_CONSECUTIVE_LOSSES
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
    save_session_data(email, current_data) 

    process = Process(target=bot_core_logic, args=(email, token, stake, tp, currency, account_type, max_loss))
    process.daemon = True
    process.start()
    
    with PROCESS_LOCK: active_processes[email] = process
    
    flash(f'Bot started successfully. Strategy: Digit DIFF 5 (1 Tick), Base Entry Conditional @ {ENTRY_SECONDS}s, Last Digit 9, Max Loss: {max_loss}, Martingale: x{MARTINGALE_MULTIPLIER} - **FULLY CONDITIONAL** - SL/TP Triggers Auto Stop & Clear.', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session: return redirect(url_for('auth_page'))
    
    stop_bot(session['email'], clear_data=True, stop_reason="Stopped Manually")
    
    flash('Bot stopped and session data cleared. Refreshing to show current status.', 'success')
    
    response = make_response(redirect(url_for('index')))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
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

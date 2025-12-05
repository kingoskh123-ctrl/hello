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
# BOT CONSTANT SETTINGS (R_100 | Digit Repetition 9 x2 Ticks | Conditional Martingale)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100" 
DURATION = 1              # مدة الصفقة 1 تيك
DURATION_UNIT = "t"       # الوحدة بالتيكات

# 💡 إعدادات تحليل التيكات الجديدة
TICK_SAMPLE_SIZE = 2      # عدد التيكات المطلوبة لتكوين الإشارة (تم التعديل)
REQUIRED_REPETITION_DIGIT = 9 # الرقم المطلوب تكراره (تم التعديل)
CONTRACT_TYPE = "DIGITUNDER" # نوع العقد (تم التعديل)
BARRIER = 8               # الحاجز (الرقم الأخير يجب أن يكون أقل من هذا الرقم) (تم التعديل)

# 💡 إعدادات المضاعفة الجديدة بناءً على طلبك
MAX_CONSECUTIVE_LOSSES = 3    # أقصى خسائر متتالية مسموح بها (تم التعديل)
MARTINGALE_MULTIPLIER = 5.5   # مضاعف الخسارة (تم التعديل)
MAX_MARTINGALE_STEP = 2       # أقصى خطوة مضاعفة (تم التعديل)

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
    
    # متغيرات حالة تحليل التيكات
    "last_digits_history": [0] * TICK_SAMPLE_SIZE, 
    "last_barrier_digit": BARRIER, 
    
    "current_ohlc_ticks": 0,
    "current_ohlc_open_price": 0.0,
    "ohlc_history": [],
    "last_trade_direction": None,
}

# --- Persistence functions (No change needed) ---
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
        
        # ضبط حجم سجل التيكات والحاجز
        if 'last_digits_history' not in data or len(data['last_digits_history']) != TICK_SAMPLE_SIZE: 
             data['last_digits_history'] = [0] * TICK_SAMPLE_SIZE
        if 'last_barrier_digit' not in data or data['last_barrier_digit'] is None:
            data['last_barrier_digit'] = BARRIER
            
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
        # الحذف الفوري للبيانات كما طلب المستخدم
        delete_session_data(email)
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data CLEARED.")
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
    
    return base_stake * (multiplier ** current_step)


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
        print(f"✅ [DEBUG] Sending BUY request for {contract_type} Barrier {barrier} at {round(stake, 2):.2f} (Duration: {DURATION} ticks)...")
        ws_app.send(json.dumps(trade_request))
        return True
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send {contract_type} order: {e}")
        return False


def apply_martingale_logic(email):
    """ منطق المضاعفة """
    global is_contract_open, MARTINGALE_MULTIPLIER, MAX_CONSECUTIVE_LOSSES, MAX_MARTINGALE_STEP, BARRIER, CONTRACT_TYPE
    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return

    if len(current_data['contract_profits']) < 1 or current_data['open_contract_ids']:
        is_contract_open[email] = False 
        return

    total_profit_loss = sum(current_data['contract_profits'].values())
    current_data['current_profit'] += total_profit_loss
    base_stake_used = current_data['base_stake']
    
    # ✅ حالة جني الأرباح (TP Reached)
    if current_data['current_profit'] >= current_data['tp_target']:
        current_data["is_running"] = False
        current_data["stop_reason"] = "TP Reached"
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached") 
        return
        
    
    # ❌ حالة الخسارة (Loss)
    if total_profit_loss < 0:
        current_data['total_losses'] += 1 
        current_data['consecutive_losses'] += 1
        entry_result_tag = "LOSS"
        
        # 🛑 حالة الوقف الخسارة (SL Reached)
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
            reason = f"SL Reached: Max {MAX_CONSECUTIVE_LOSSES} consecutive losses"
            current_data["is_running"] = False
            current_data["stop_reason"] = reason
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=reason) 
            return
            
        current_data['current_step'] = min(current_data['current_step'] + 1, MAX_MARTINGALE_STEP)
        
        new_stake = calculate_martingale_stake(base_stake_used, current_data['current_step'], MARTINGALE_MULTIPLIER)
        current_data['current_stake'] = new_stake
        
        # في حالة الخسارة، ندخل مباشرة للصفقة الجديدة بنفس النوع والحاجز (بدون انتظار إشارة)
        barrier_tag = BARRIER
        print(f"🔄 [LOSS - MARTINGALE] PnL: {total_profit_loss:.2f}. Con. Loss: {current_data['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}. Next Stake calculated: {round(new_stake, 2):.2f}. Retrying {CONTRACT_TYPE} {barrier_tag}...")
        
        # إرسال صفقة المضاعفة فوراً
        start_recovery_trade(email, contract_type=CONTRACT_TYPE, barrier=BARRIER)
        
    # ✅ حالة الربح (Win)
    else: 
        current_data['total_wins'] += 1 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = base_stake_used
        
        # إعادة تهيئة سجل التيكات للبدء في البحث عن الإشارة الجديدة
        current_data['last_digits_history'] = [0] * TICK_SAMPLE_SIZE
        entry_result_tag = "WIN"
        
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. PnL: {total_profit_loss:.2f}. Stake reset to base: {base_stake_used:.2f}. Waiting for new signal.")
        
        # مسح بيانات العقد وإعادة فتح الباب للدخول
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        save_session_data(email, current_data) 
        
        is_contract_open[email] = False 
    
    # إذا كانت خسارة متتالية، لا نفتح الباب للدخول (is_contract_open) حتى نرسل صفقة المضاعفة الفورية (والتي يتم إرسالها ضمن هذا المنطق)
    # إذا كانت ربح، نفتح الباب للبحث عن إشارة جديدة (is_contract_open=False)
    
    if total_profit_loss < 0:
        # مسح بيانات العقد وإعادة فتح الباب للدخول (لصفقة المضاعفة الفورية)
        current_data['current_entry_id'] = None
        current_data['open_contract_ids'] = []
        current_data['contract_profits'] = {}
        save_session_data(email, current_data) 


def start_recovery_trade(email, contract_type, barrier):
    """ يتم استدعاؤها لإرسال صفقة المضاعفة فوراً بعد الخسارة دون انتظار تيك جديد """
    global is_contract_open
    
    current_data = get_session_data(email)
    stake = current_data['current_stake']
    currency_to_use = current_data['currency']
    
    current_data['current_entry_id'] = time.time()
    
    print(f"🧠 [RECOVERY ENTRY] Stake: {round(stake, 2):.2f}. {contract_type} {barrier}.")
    
    if send_single_trade_order(email, stake, currency_to_use, contract_type, barrier): 
        
        is_contract_open[email] = True 
        current_data['last_entry_time'] = int(time.time())
        current_data['last_entry_price'] = current_data.get('last_valid_tick_price', 0.0)

        save_session_data(email, current_data)
        return
        
    is_contract_open[email] = False 
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
        apply_martingale_logic(email)


def start_new_trade_on_signal(email, contract_type, barrier):
    """ يتم استدعاؤها لإرسال الصفقة الأولى (Base Stake) عند وجود الإشارة """
    global is_contract_open
    
    current_data = get_session_data(email)
    stake = current_data['current_stake']
    currency_to_use = current_data['currency']
    
    # 🛑 يتم إيقاف البوت وحذف بياناته إذا تجاوز حد الـ SL (تحقق إضافي)
    if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES:
         reason = f"SL Reached: Max {MAX_CONSECUTIVE_LOSSES} consecutive losses"
         current_data["is_running"] = False
         current_data["stop_reason"] = reason
         save_session_data(email, current_data)
         stop_bot(email, clear_data=True, stop_reason=reason) 
         return
    
    
    entry_mode_tag = "Base Stake" 
    entry_source = f"{entry_mode_tag} (Signal: {TICK_SAMPLE_SIZE}x Digit {REQUIRED_REPETITION_DIGIT})"

    current_data['current_entry_id'] = time.time()
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    
    print(f"🧠 [SIGNAL ENTRY - {entry_mode_tag}] Source: {entry_source}. Stake: {round(stake, 2):.2f}. Contract: {contract_type} {barrier}.")
    
    if send_single_trade_order(email, stake, currency_to_use, contract_type, barrier): 
        
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
    
    global is_contract_open, active_ws, TICK_SAMPLE_SIZE, REQUIRED_REPETITION_DIGIT, CONTRACT_TYPE, BARRIER

    is_contract_open = {email: False}
    active_ws = {email: None}

    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp, "is_running": True, 
        "current_stake": stake, "stop_reason": "Running", "last_entry_time": 0,
        "last_entry_price": 0.0, "last_tick_data": None, "currency": currency,
        "account_type": account_type, "last_valid_tick_price": 0.0,
        "current_entry_id": None, "open_contract_ids": [], "contract_profits": {},
        "last_digits_history": [0] * TICK_SAMPLE_SIZE,
        "last_barrier_digit": BARRIER,
    })
    
    if session_data['consecutive_losses'] > 0:
        step = session_data['consecutive_losses']
        current_stake = calculate_martingale_stake(stake, step, MARTINGALE_MULTIPLIER)
        session_data['current_stake'] = current_stake
        session_data['current_step'] = step
        print(f"🔍 [RECOVERY] Resuming in Martingale mode (Step {step}). Stake: {current_stake:.2f}. Retrying {CONTRACT_TYPE} {BARRIER}...")
        
        # إرسال صفقة المضاعفة الفورية عند إعادة التشغيل
        start_recovery_trade(email, contract_type=CONTRACT_TYPE, barrier=BARRIER)
    else:
        session_data['last_digits_history'] = [0] * TICK_SAMPLE_SIZE 

    save_session_data(email, session_data)

    try:
        while True:
            current_data = get_session_data(email)
            
            if not current_data.get('is_running'): break

            print(f"🔗 [PROCESS] Attempting to connect for {email} ({account_type.upper()}/{currency})...")

            def on_open_wrapper(ws_app):
                current_data = get_session_data(email) 
                ws_app.send(json.dumps({"authorize": current_data['api_token']}))
                
                # نطلب التيكات مباشرة
                ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1})) 
                
                running_data = get_session_data(email)
                running_data['is_running'] = True
                save_session_data(email, running_data)
                print(f"✅ [PROCESS] Connection established for {email}.")
                
                # منطق استعادة العقود المفتوحة
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
                elif current_data['consecutive_losses'] == 0:
                    # نفتح الباب للبحث عن إشارة جديدة فقط إذا لم يكن هناك خسائر متتالية
                    is_contract_open[email] = False 
                else:
                    # إذا كان هناك خسائر متتالية، يتم إرسال صفقة المضاعفة الفورية (تم إرسالها بالفعل في منطق bot_core_logic)
                    is_contract_open[email] = True


            def on_message_wrapper(ws_app, message):
                data = json.loads(message)
                msg_type = data.get('msg_type')
                
                current_data = get_session_data(email)
                if not current_data.get('is_running'): return
                    
                if msg_type == 'tick':
                    try:
                        current_price = float(data['tick']['quote'])
                    except (KeyError, ValueError): return
                        
                    T_new = int(str(current_price)[-1]) # الرقم الأخير

                    current_data['last_valid_tick_price'] = current_price
                    current_data['last_tick_data'] = data['tick']

                    # 1. تحديث سجل الأرقام الأخيرة
                    current_data['last_digits_history'].append(T_new)
                    current_data['last_digits_history'] = current_data['last_digits_history'][-TICK_SAMPLE_SIZE:]
                    
                    
                    # 2. التحقق من شرط الدخول
                    if not is_contract_open.get(email):
                        
                        # الدخول يتم فقط عند عدم وجود خسائر متتالية (الـ Base Stake)
                        if current_data['consecutive_losses'] == 0:
                            
                            # شرط الإشارة: تكرار الرقم 9 لـ 2 تيكات
                            if len(current_data['last_digits_history']) == TICK_SAMPLE_SIZE:
                                history = current_data['last_digits_history']
                                
                                # التحقق: هل كل الأرقام متطابقة وتساوي الرقم 9؟
                                is_signal = all(d == REQUIRED_REPETITION_DIGIT for d in history)
                                
                                if is_signal:
                                    print(f"🔥 [SIGNAL FOUND] {TICK_SAMPLE_SIZE}x Digit {REQUIRED_REPETITION_DIGIT}. Entering {CONTRACT_TYPE} {BARRIER}.")
                                    start_new_trade_on_signal(email, contract_type=CONTRACT_TYPE, barrier=BARRIER)
                                
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
                        
                        # تسجيل الخسارة وتطبيق المضاعفة
                        current_data['contract_profits'][f"error-{time.time()}"] = -current_data['current_stake']
                        # apply_martingale_logic ستقوم بإرسال صفقة المضاعفة الفورية (Recovery Trade)
                        apply_martingale_logic(email)
                        
                        # إيقاف وحذف فوري للبيانات بسبب خطأ API (في حال الوصول لـ SL)
                        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES:
                            reason = f"API Buy Error: {error_message}"
                            current_data["is_running"] = False
                            current_data["stop_reason"] = reason
                            save_session_data(email, current_data)
                            stop_bot(email, clear_data=True, stop_reason=reason)


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
        # إيقاف وحذف فوري للبيانات بسبب crash
        reason = "Critical Python Crash"
        current_data = get_session_data(email)
        current_data["is_running"] = False
        current_data["stop_reason"] = reason
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason=reason)

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
    {% set strategy = 'Conditional Martingale (x' + martingale_multiplier|string + ') | Entry: ' + tick_sample_size|string + 'x Digit ' + required_digit|string + ' | Contract: ' + contract_type + ' ' + barrier|string + ' | Duration: ' + duration|string + ' Tick' %}
    
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
        
        // التحديث يتم فقط إذا كان البوت في حالة تشغيل
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
    
    contract_type_name = f"{CONTRACT_TYPE} {BARRIER}"

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER, 
        max_martingale_step=MAX_MARTINGALE_STEP,
        duration=DURATION,
        tick_sample_size=TICK_SAMPLE_SIZE,
        required_digit=REQUIRED_REPETITION_DIGIT,
        contract_type=CONTRACT_TYPE,
        barrier=BARRIER,
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
    
    strategy_desc = f'Martingale (x{MARTINGALE_MULTIPLIER}, Max Loss {MAX_CONSECUTIVE_LOSSES}) | Entry: {TICK_SAMPLE_SIZE}x Digit {REQUIRED_REPETITION_DIGIT} -> {CONTRACT_TYPE} {BARRIER} (1 Tick)'
    flash(f'Bot started successfully. Strategy: {strategy_desc}', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session: return redirect(url_for('auth_page'))
    
    email = session['email']
    is_force_stop = request.form.get('force_stop') == 'true'

    # يتم إيقاف العملية وحذف البيانات (clear_data=True هو الافتراضي)
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
        delete_session_data(email)
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")
        
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

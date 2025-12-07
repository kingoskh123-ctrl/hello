import time
import json
import websocket 
import multiprocessing 
import os 
import sys 
import fcntl 
from flask import Flask, request, render_template_string, redirect, url_for, session, flash
from datetime import datetime, timezone

# ==========================================================
# BOT CONSTANT SETTINGS (UPDATED)
# ==========================================================
WSS_URL_UNIFIED = "wss://blue.derivws.com/websockets/v3?app_id=16929" 
SYMBOL = "R_100"        
DURATION = 2            # 1 تيك
DURATION_UNIT = "t"     
MARTINGALE_STEPS = 3    # 🌟 خطوة مضاعفة واحدة فقط
MAX_CONSECUTIVE_LOSSES = 4 # 🌟 الحد الأقصى للخسائر المتتالية
RECONNECT_DELAY = 1      
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json" 
TICK_HISTORY_SIZE = 0 
MARTINGALE_MULTIPLIER = 3.0 # 🌟 المضاعف الجديد
CANDLE_TICK_SIZE = 0   
# الثواني تم تجاهلها للدخول الفوري ولكنها لا تزال موجودة
SYNC_SECONDS = [0, 14, 30, 44] 
# ==========================================================

# ==========================================================
# BOT RUNTIME STATE (Shared and Local)
# ==========================================================
flask_local_processes = {}
manager = multiprocessing.Manager() 

active_ws = {} 
is_contract_open = manager.dict() 

# تبقى الصفقة الافتراضية كما هي: DIGITDIFF 5
TRADE_STATE_DEFAULT = {"type": "DIGITOVER", "target_digit": 2} 

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
    "current_trade_state": TRADE_STATE_DEFAULT,
    "stop_reason": "Stopped Manually",
    "last_entry_time": 0,         
    "last_entry_price": 0.0,      
    "last_tick_data": None,       
    "tick_history": [],
    "last_losing_trade_type": "DIGITDIFF", 
    "open_contract_id": None, 
    "account_type": "demo", 
    "currency": "USD",
    "pending_time_signal": None,
    "pending_martingale": False, 
    "martingale_stake": 0.0,     
    "martingale_type": "DIGITDIFF",   
}
# ==========================================================

# ==========================================================
# PERSISTENT STATE MANAGEMENT FUNCTIONS (File I/O with Locks)
# ==========================================================
def get_file_lock(f):
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass

def release_file_lock(f):
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass

def load_persistent_sessions():
    if not os.path.exists(ACTIVE_SESSIONS_FILE):
        return {}
    
    with open(ACTIVE_SESSIONS_FILE, 'a+') as f:
        f.seek(0)
        get_file_lock(f)
        try:
            content = f.read()
            if content:
                data = json.loads(content)
            else:
                data = {}
        except json.JSONDecodeError:
            data = {}
        finally:
            release_file_lock(f)
            return data

def save_session_data(email, session_data):
    all_sessions = load_persistent_sessions()
    all_sessions[email] = session_data
    
    with open(ACTIVE_SESSIONS_FILE, 'w') as f:
        get_file_lock(f)
        try:
            json.dump(all_sessions, f, indent=4)
        except Exception as e:
            print(f"❌ ERROR saving session data: {e}")
        finally:
            release_file_lock(f)

def delete_session_data(email):
    all_sessions = load_persistent_sessions()
    if email in all_sessions:
        del all_sessions[email]
    
    with open(ACTIVE_SESSIONS_FILE, 'w') as f:
        get_file_lock(f)
        try:
            json.dump(all_sessions, f, indent=4)
        except Exception as e:
            print(f"❌ ERROR deleting session data: {e}")
        finally:
            release_file_lock(f)

def get_session_data(email):
    all_sessions = load_persistent_sessions()
    if email in all_sessions:
        data = all_sessions[email]
        for key, default_val in DEFAULT_SESSION_STATE.items():
            if key not in data:
                data[key] = default_val 
        return data
    
    return DEFAULT_SESSION_STATE.copy()

def load_allowed_users():
    if not os.path.exists(USER_IDS_FILE):
        with open(USER_IDS_FILE, 'w', encoding='utf-8') as f:
            f.write("test@example.com\n")
        return {"test@example.com"}
    try:
        with open(USER_IDS_FILE, 'r', encoding='utf-8') as f:
            users = {line.strip().lower() for line in f if line.strip()}
        return users
    except Exception as e:
        return set()
        
def stop_bot(email, clear_data=True, stop_reason="Stopped Manually"): 
    """ 
    توقف عملية البوت، وتحديث الحالة، ومسح البيانات القسري.
    """
    global is_contract_open 
    global flask_local_processes 

    current_data = get_session_data(email)
    current_data["is_running"] = False
    current_data["stop_reason"] = stop_reason 
    save_session_data(email, current_data) 
    
    if email in flask_local_processes:
        try:
            process = flask_local_processes[email]
            if process.is_alive():
                process.terminate() 
                process.join(timeout=2) 
            del flask_local_processes[email] 
            print(f"🛑 [INFO] Process for {email} forcefully terminated.")
        except Exception as e:
            print(f"❌ [ERROR] Could not terminate process for {email}: {e}")
            
    # يتم تحديث الحالة المشتركة
    if email in is_contract_open:
        is_contract_open[email] = False 

    if clear_data:
        delete_session_data(email) 
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
        
        temp_data = DEFAULT_SESSION_STATE.copy()
        temp_data["stop_reason"] = stop_reason
        save_session_data(email, temp_data) 
    else:
        save_session_data(email, current_data) 

# ==========================================================
# TRADING BOT FUNCTIONS (Runs inside a separate Process)
# ==========================================================

def calculate_martingale_stake(base_stake, current_step):
    if current_step == 0:
        return base_stake
    # المضاعفة تحدث فقط في الخطوات المسموحة
    if current_step <= MARTINGALE_STEPS:
        # استخدام MARTINGALE_MULTIPLIER المحدث (14.0)
        return base_stake * (MARTINGALE_MULTIPLIER ** current_step) 
    else:
        # تجاوز الحد الأقصى للخطوات، نبدأ من جديد بالرهان الأساسي
        return base_stake

def send_trade_order(email, stake, contract_type, currency_code):
    global is_contract_open 
    if email not in active_ws or active_ws[email] is None: return
    ws_app = active_ws[email]
    
    rounded_stake = round(stake, 2)
    
    # تحديد معامل الصفقة الخاص بـ DIGITDIFF 5
    if contract_type == "DIGITDIFF":
        contract_param = {
            "duration": DURATION, 
            "duration_unit": DURATION_UNIT, 
            "symbol": SYMBOL, 
            "contract_type": "DIGITDIFF",
            "barrier": 5 # الرقم الذي يجب أن يكون مختلفاً
        }
    else:
        print(f"❌ [TRADE ERROR] Invalid contract type for DIGITDIFF 5 strategy: {contract_type}")
        return

    trade_request = {
        "buy": 1, 
        "price": rounded_stake, 
        "parameters": {
            "amount": rounded_stake, 
            "basis": "stake",
            "currency": currency_code, 
            **contract_param
        }
    }
    try:
        ws_app.send(json.dumps(trade_request))
        # تعيين الحالة إلى True فوراً بعد إرسال الطلب لمنع الدخول المتعدد
        is_contract_open[email] = True 
        print(f"💰 [TRADE] Sent {contract_type} 5 ({currency_code}) with stake: {rounded_stake:.2f}")
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass

def check_pnl_limits(email, profit_loss, trade_type): 
    global is_contract_open 

    current_data = get_session_data(email)
    if not current_data.get('is_running'): return

    last_stake = current_data['current_stake'] 
    current_data['current_profit'] += profit_loss
    
    stop_triggered = False

    if profit_loss > 0:
        # 🟢 ربح: إعادة تعيين العدادات والرهان الأساسي
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        current_data['last_losing_trade_type'] = "DIGITDIFF" 
        current_data['pending_martingale'] = False # مسح حالة المضاعفة
        
        if current_data['current_profit'] >= current_data['tp_target']:
            stop_triggered = "TP Reached"
            
    else:
        # 🔴 خسارة: تحديث العدادات وتجهيز المضاعفة (التي ستنتظر تيك = 0)
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        current_data['last_losing_trade_type'] = trade_type
        
        # إعداد صفقة المضاعفة
        if current_data['current_step'] <= MARTINGALE_STEPS:
            new_stake = calculate_martingale_stake(current_data['base_stake'], current_data['current_step'])
            contract_type_to_use = "DIGITDIFF" 
            
            # نحدد الرهان والنوع ونُفَعِّل حالة الانتظار
            current_data['current_stake'] = new_stake
            current_data['pending_martingale'] = True # تفعيل حالة الانتظار لحين ظهور 0
            current_data['martingale_stake'] = new_stake
            current_data['martingale_type'] = contract_type_to_use
            # رسالة التنبيه تم تحديثها لتعكس الخطوات الجديدة: Max Step 1 = خسارتان متتاليتان
            print(f"⚠️ [MARTINGALE PENDING] Loss detected. Next trade: {contract_type_to_use} 5 @ {new_stake:.2f}. Awaiting 2nd digit = 0 or 1 decimal digit tick to execute.")
        else:
            # تجاوز الحد الأقصى للمضاعفة (Max Step 1 تجاوزت)
            current_data['current_stake'] = current_data['base_stake']
            current_data['pending_martingale'] = False
        
        # 🌟 التحقق من الحد الأقصى للخسائر (2 فقط)
        if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES: 
            stop_triggered = "SL Reached"
            
    # مسح الإشارة المعلقة 
    current_data['pending_time_signal'] = None
    current_data['tick_history'] = [] 
        
    save_session_data(email, current_data) 
    
    if stop_triggered:
        stop_bot(email, clear_data=True, stop_reason=stop_triggered) 
        is_contract_open[email] = False 
        return 
        
    # إغلاق الحالة المشتركة للسماح بالدخول الجديد (سواء انتظار مضاعفة أو دخول عادي)
    is_contract_open[email] = False 

    state = current_data['current_trade_state']
    rounded_last_stake = round(last_stake, 2)
    print(f"[LOG {email}] PNL: {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Last Stake: {rounded_last_stake:.2f}, State: {state['type']}")
    
    return current_data['pending_martingale'] 

# ==========================================================
# UTILITY FUNCTIONS FOR DIGITDIFF STRATEGY
# ==========================================================

def get_target_digit(price):
    """
    يستخرج الرقم الثاني بعد الفاصلة، مع افتراض 0 إذا كان هناك رقم عشري واحد فقط.
    """
    try:
        # تحويل السعر إلى سلسلة نصية
        price_str = str(price) 
        
        # فصل الجزء العشري
        if '.' in price_str:
            decimal_part = price_str.split('.')[-1]
            
            # الحالة 1: يوجد رقمان عشريان أو أكثر
            if len(decimal_part) >= 2:
                # الرقم الثاني بعد الفاصلة
                return int(decimal_part[1]) 
            
            # الحالة 2: يوجد رقم عشري واحد فقط (افتراض أن الثاني هو 0)
            elif len(decimal_part) == 1:
                return 0 # المرونة المطلوبة
        
        # في حالة عدم وجود جزء عشري
        return 0 
        
    except Exception as e:
        print(f"❌ Error processing price for Digit Check: {e}")
        return None 

def get_signal_digit_diff(price):
    """
    يحدد الإشارة: DIGITDIFF 5 فقط عندما يكون الرقم الثاني بعد الفاصلة يساوي 0 (مع مرونة الرقم العشري الواحد).
    """
    
    target_digit = get_target_digit(price)
    
    if target_digit is None:
        return None
        
    # الشرط: الدخول DIGITDIFF (5) فقط عندما تكون القيمة المستخرجة 0
    if target_digit == 0:
        # الصفقة هي DIGITDIFF 5
        return "DIGITDIFF" 
    else:
        # يتجاهل أي رقم آخر (1-9)
        return None 

# ==========================================================

def bot_core_logic(email, token, stake, tp, account_type, currency_code):
    """ Main bot logic for a single user/session. """
    global active_ws 
    global is_contract_open 
    global WSS_URL_UNIFIED
    
    WSS_URL = WSS_URL_UNIFIED

    active_ws[email] = None 
    
    if email not in is_contract_open:
        is_contract_open[email] = False
    else:
        data_from_file = get_session_data(email)
        if data_from_file.get('open_contract_id'):
            is_contract_open[email] = True
        else:
            is_contract_open[email] = False


    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp,
        "is_running": True, "current_stake": stake,
        "current_trade_state": TRADE_STATE_DEFAULT,
        "stop_reason": "Running",
        "last_entry_time": 0,
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "tick_history": [], 
        "last_losing_trade_type": "DIGITDIFF",
        "open_contract_id": None,
        "account_type": account_type,
        "currency": currency_code,
        "current_profit": 0.0,
        "current_step": 0,
        "consecutive_losses": 0,
        "total_wins": 0,
        "total_losses": 0,
        "pending_time_signal": None, 
        "pending_martingale": False, 
        "martingale_stake": 0.0, 
        "martingale_type": "DIGITDIFF",
    })
    save_session_data(email, session_data)

    while True: 
        current_data = get_session_data(email)
        
        if not current_data.get('is_running'):
            break

        print(f"🔗 [PROCESS] Attempting to connect for {email} to {WSS_URL}...")

        def on_open_wrapper(ws_app):
            ws_app.send(json.dumps({"authorize": current_data['api_token']})) 
            ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            
            running_data = get_session_data(email)
            
            contract_id = running_data.get('open_contract_id')
            if contract_id:
                ws_app.send(json.dumps({
                    "proposal_open_contract": 1, 
                    "contract_id": contract_id, 
                    "subscribe": 1
                }))
                is_contract_open[email] = True 
                print(f"🔄 [RECOVERY] Attempting to follow lost contract: {contract_id}")
            else:
                is_contract_open[email] = False

            running_data['is_running'] = True
            save_session_data(email, running_data)
            print(f"✅ [PROCESS] Connection established for {email}.")
            
        
        def send_martingale_trade(email, current_data):
            stake_to_use = current_data['martingale_stake']
            contract_type_to_use = current_data['martingale_type']
            currency_code = current_data['currency']
            
            entry_price = current_data['last_tick_data']['price']
            current_time_ms = time.time() * 1000

            # تحديث حالة البوت للدخول
            current_data['last_entry_price'] = entry_price
            current_data['last_entry_time'] = current_time_ms
            current_data['current_trade_state']['type'] = contract_type_to_use 
            current_data['current_stake'] = stake_to_use 
            current_data['pending_martingale'] = False # مسح حالة المضاعفة بعد التنفيذ
            save_session_data(email, current_data)

            send_trade_order(email, stake_to_use, contract_type_to_use, currency_code)
            print("🚀 [MARTINGALE EXECUTION] Executed upon qualifying tick (2nd digit = 0 or 1 decimal digit).")
        
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
                
                # 1. تحديث بيانات التيك الحالية (دائماً)
                current_data['last_tick_data'] = {
                    "price": current_price,
                    "timestamp": current_timestamp
                }
                current_seconds = datetime.fromtimestamp(current_timestamp).second
                save_session_data(email, current_data) 
                
                
                # 2. منطق التحليل والدخول (مشروط بظهور 0 فقط، سواء دخول أساسي أو مضاعفة)
                
                # التحقق من أن البوت جاهز للدخول
                if is_contract_open.get(email) is False:
                    
                    current_time_ms = time.time() * 1000
                    time_since_last_entry_ms = current_time_ms - current_data['last_entry_time']
                    # قيد زمني بسيط لمنع تكرار الإدخالات بنفس الميللي ثانية
                    is_time_gap_respected = time_since_last_entry_ms > 100 
                    
                    if not is_time_gap_respected:
                        return
                    
                    # الحصول على إشارة DIGITDIFF 5 من التيك الحالي
                    contract_type_to_use = get_signal_digit_diff(current_price)
                    
                    # الشرط الأساسي: هل تحقق شرط الرقم الثاني = 0 أو رقم عشري واحد؟
                    if contract_type_to_use == "DIGITDIFF":
                        
                        if current_data['pending_martingale']:
                            # 🚀 حالة المضاعفة: التنفيذ الآن
                            send_martingale_trade(email, current_data)
                            return
                        else:
                            # 🚀 حالة الدخول الأساسي: التنفيذ الآن
                            stake_to_use = current_data['base_stake']
                            entry_price = current_data['last_tick_data']['price']

                            # تحديث حالة البوت للدخول
                            current_data['last_entry_price'] = entry_price
                            current_data['last_entry_time'] = current_time_ms
                            current_data['current_trade_state']['type'] = contract_type_to_use 
                            current_data['current_stake'] = stake_to_use 
                            
                            save_session_data(email, current_data)

                            send_trade_order(email, stake_to_use, contract_type_to_use, current_data['currency'])
                            print(f"🚀 [BASE EXECUTION] DIGITDIFF 5 detected (2nd digit is 0 or 1 decimal) and executed immediately ({stake_to_use:.2f}).")
                            return
                        
                        # إذا لم تتحقق الإشارة، لا يوجد دخول.
                                
            elif msg_type == 'buy':
                contract_id = data['buy']['contract_id']
                
                current_data = get_session_data(email)
                current_data['open_contract_id'] = str(contract_id) 
                save_session_data(email, current_data)
                
                ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                
            elif msg_type == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                if contract.get('is_sold') == 1:
                    trade_type = contract.get('contract_type')
                    profit_loss = contract['profit']
                    
                    current_data = get_session_data(email)
                    current_data['open_contract_id'] = None
                    save_session_data(email, current_data) 
                    
                    # 3. معالجة النتيجة وإعداد المضاعفة (التي ستنتظر تيك = 0)
                    check_pnl_limits(email, profit_loss, trade_type) 
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))
                    
                    # لا تنفيذ فوري للمضاعفة هنا، فقط تحديث الحالة الانتظار في check_pnl_limits


            elif msg_type == 'authorize':
                print(f"✅ [AUTH {email}] Success. Account: {data['authorize']['loginid']}")

        def on_close_wrapper(ws_app, code, msg):
            print(f"❌ [WS Close {email}] Code: {code}, Message: {msg}")
            
        def on_ping_wrapper(ws, message):
            if not get_session_data(email).get('is_running'):
                ws.close()

        try:
            ws = websocket.WebSocketApp(
                WSS_URL_UNIFIED, on_open=on_open_wrapper, on_message=on_message_wrapper, 
                on_error=lambda ws, err: print(f"[WS Error {email}] {err}"),
                on_close=on_close_wrapper 
            )
            active_ws[email] = ws
            ws.on_ping = on_ping_wrapper 
            ws.run_forever(ping_interval=20, ping_timeout=10) 
            
        except Exception as e:
            print(f"❌ [ERROR] WebSocket failed for {email}: {e}")
        
        if get_session_data(email).get('is_running') is False:
            break
        
        print(f"💤 [PROCESS] Waiting {RECONNECT_DELAY} seconds before retrying connection for {email}...")
        time.sleep(RECONNECT_DELAY)

    print(f"🛑 [PROCESS] Bot process ended for {email}.")


# ==========================================================
# FLASK APP SETUP AND ROUTES (الواجهة لم يتم تغييرها)
# ==========================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False 

# --- HTML Templates (لضمان عمل الواجهة) ---

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
    .data-box {
        background-color: #f8f9fa;
        border: 1px solid #e9ecef;
        padding: 15px;
        border-radius: 5px;
        margin-bottom: 15px;
    }
</style>
<h1>Bot Control Panel | User: {{ email }}</h1>
<hr>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:{{ 'green' if category == 'success' else ('blue' if category == 'info' else 'red') }};">{{ message }}</p>
        {% endfor %}
    {% endif %}
{% endwith %}

{% if session_data and session_data.stop_reason and session_data.stop_reason != "Running" and session_data.stop_reason != "Stopped Manually" %}
    <p style="color:red; font-weight:bold;">Last Session Ended: {{ session_data.stop_reason }}</p>
{% endif %}


{% if session_data and session_data.is_running %}
    {% set martingale_mode = 'Martingale (Same Direction: DIFF 5) Awaiting 2nd Digit = 0 OR 1 Decimal Digit Tick' %}
        {% set strategy = 'DIGIT DIFF 5 (2nd Decimal Digit must be 0, flexible for 1 decimal) | Market: ' + SYMBOL + ' | Duration: ' + DURATION|string + ' Tick | Analysis & Entry: Immediate upon 2nd Digit = 0 or 1 decimal digit | Martingale x' + martingale_multiplier|string + ' (Max ' + max_consecutive_losses|string + ' Losses, Max Step ' + max_martingale_step|string + ')' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <div class="data-box">
        <p>Account Type: <b>{{ session_data.account_type.upper() }}</b> | Currency: <b>{{ session_data.currency }}</b></p>
        <p>Net Profit: <b>{{ session_data.currency }} {{ session_data.current_profit|round(2) }}</b></p>
        
        <p style="font-weight: bold; color: {% if session_data.open_contract_id %}#007bff{% else %}#555{% endif %};">
            Open Contract Status: 
            <b>{% if session_data.open_contract_id %}1{% else %}0{% endif %}</b>
        </p>
        
        <p style="font-weight: bold; color: {% if session_data.pending_martingale %}#ff5733{% else %}#555{% endif %};">
            Pending Signal: 
            <b>
                {% if session_data.pending_martingale %}1 (Martingale: {{ session_data.martingale_type }} 5 @ {{ session_data.martingale_stake|round(2) }})
                {% else %}0 (Awaiting next qualifying tick: 2nd digit = 0 or 1 decimal digit)
                {% endif %}
            </b>
        </p>

        <p>Current Stake: <b>{{ session_data.currency }} {{ session_data.current_stake|round(2) }}</b></p>
        <p style="font-weight: bold; color: {% if session_data.consecutive_losses > 0 %}red{% else %}green{% endif %};">
        Consecutive Losses: <b>{{ session_data.consecutive_losses }}</b> / {{ max_consecutive_losses }} 
        (Last Direction: <b>{{ session_data.last_losing_trade_type }} 5</b>)
        </p>
        <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
        <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
        {% if session_data.open_contract_id %}
            <p style="font-weight: bold; color: blue;">⚠️ Contract ID: {{ session_data.open_contract_id|string|truncate(8, True, '...') }} (Recovery Active)</p>
        {% endif %}
    </div>
    
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
            <option value="demo" {% if session_data.account_type == 'demo' %}selected{% endif %}>Demo (USD)</option>
            <option value="live" {% if session_data.account_type == 'live' %}selected{% endif %}>Live (tUSDT)</option>
        </select><br>

        <label for="token">Deriv API Token:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}"><br>
        
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
    var SYMBOL = "{{ SYMBOL }}";
    var DURATION = {{ DURATION }};
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

# ----------------- End of HTML Templates -----------------

@app.before_request
def check_user_status():
    if request.endpoint in ('login', 'auth_page', 'logout', 'static'):
        return

    if 'email' in session:
        email = session['email']
        allowed_users = load_allowed_users()
        
        if email.lower() not in allowed_users:
            session.pop('email', None) 
            flash('Your access has been revoked. Please log in again.', 'error')
            return redirect(url_for('auth_page')) 

@app.route('/')
def index():
    if 'email' not in session:
        return redirect(url_for('auth_page'))
    
    email = session['email']
    session_data = get_session_data(email)

    return render_template_string(CONTROL_FORM, 
        email=email,
        session_data=session_data,
        martingale_multiplier=MARTINGALE_MULTIPLIER,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        max_martingale_step=MARTINGALE_STEPS,
        datetime=datetime,
        timezone=timezone,
        sync_seconds=SYNC_SECONDS,
        SYMBOL=SYMBOL,
        DURATION=DURATION
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
    if 'email' in session:
        return redirect(url_for('index'))
    return render_template_string(AUTH_FORM)

@app.route('/start', methods=['POST'])
def start_bot():
    if 'email' not in session:
        return redirect(url_for('auth_page'))
    
    email = session['email']
    
    global flask_local_processes 
    
    if email in flask_local_processes and flask_local_processes[email].is_alive():
        flash('Bot is already running.', 'info')
        return redirect(url_for('index'))
        
    try:
        token = request.form['token']
        stake = float(request.form['stake'])
        tp = float(request.form['tp'])
        
        account_type = request.form['account_type']
        currency_code = "USD" if account_type == "demo" else "tUSDT"

    except ValueError:
        flash("Invalid stake or TP value.", 'error')
        return redirect(url_for('index'))
        
    process = multiprocessing.Process(target=bot_core_logic, 
                                      args=(email, token, stake, tp, account_type, currency_code))
    process.daemon = True
    process.start()
    
    flask_local_processes[email] = process
    
    flash('Bot process started successfully. Recovery mechanism is active.', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session:
        return redirect(url_for('auth_page'))
    
    email = session['email']
    
    if request.form.get('force_stop') == 'true':
        stop_bot(email, clear_data=True, stop_reason="Stopped Manually (Force Clear)") 
        flash('🧹 Force stop executed. Bot process terminated and session data cleared.', 'success')
    else:
        stop_bot(email, clear_data=True, stop_reason="Stopped Manually") 
        flash('🛑 Bot process stopped and session data cleared.', 'success')
        
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('email', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('auth_page'))


if __name__ == '__main__':
    load_allowed_users() 
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

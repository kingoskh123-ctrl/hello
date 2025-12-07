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
# BOT CONSTANT SETTINGS
# ==========================================================
WSS_URL_UNIFIED = "wss://blue.derivws.com/websockets/v3?app_id=16929" 
SYMBOL = "R_100"       
DURATION = 56           # 56 ثانية (مدة الصفقة)
DURATION_UNIT = "s"     
MARTINGALE_STEPS = 4    
MAX_CONSECUTIVE_LOSSES = 5 
RECONNECT_DELAY = 1      
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json" 
TICK_HISTORY_SIZE = 15 # 3 شموع * 5 تيك = 15 تيك
MARTINGALE_MULTIPLIER = 2.2 
CANDLE_TICK_SIZE = 5   # حجم الشمعة الواحدة 5 تيك
# ==========================================================

# ==========================================================
# BOT RUNTIME STATE (Shared and Local)
# ==========================================================
flask_local_processes = {}
manager = multiprocessing.Manager() 

active_ws = {} 
is_contract_open = {} 

TRADE_STATE_DEFAULT = {"type": "CALL"}  

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
    "last_losing_trade_type": "CALL",
    "open_contract_id": None, 
    "account_type": "demo", 
    "currency": "USD",
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
            
    if email in is_contract_open:
        del is_contract_open[email]

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

def calculate_martingale_stake(base_stake, current_stake, current_step):
    if current_step == 0:
        return base_stake
    # المضاعفة تحدث فقط في الخطوات المسموحة
    if current_step <= MARTINGALE_STEPS:
        return base_stake * (MARTINGALE_MULTIPLIER ** current_step) 
    else:
        # إذا تجاوزنا الحد الأقصى للخطوات، نبدأ من جديد بالرهان الأساسي
        return base_stake

def send_trade_order(email, stake, contract_type, currency_code):
    global is_contract_open 
    if email not in active_ws or active_ws[email] is None: return
    ws_app = active_ws[email]
    
    rounded_stake = round(stake, 2)
    
    trade_request = {
        "buy": 1, 
        "price": rounded_stake, 
        "parameters": {
            "amount": rounded_stake, 
            "basis": "stake",
            "contract_type": contract_type, 
            "currency": currency_code, 
            "duration": DURATION,
            "duration_unit": DURATION_UNIT, 
            "symbol": SYMBOL
        }
    }
    try:
        ws_app.send(json.dumps(trade_request))
        # 💡 تعيين الحالة إلى True فوراً بعد إرسال الطلب
        is_contract_open[email] = True 
        print(f"💰 [TRADE] Sent {contract_type} ({currency_code}) with stake: {rounded_stake:.2f}")
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass

def calculate_next_trade_params(email):
    """
    تحسب الرهان المضاعف، وتحدد الاتجاه المعكوس بناءً على آخر خسارة.
    """
    current_data = get_session_data(email)
    
    # 1. تحديد الرهان
    current_step = current_data['current_step']
    new_stake = calculate_martingale_stake(
        current_data['base_stake'],
        current_data['current_stake'],
        current_step
    )
    
    # 2. تحديد الاتجاه المعكوس
    last_losing_trade = current_data['last_losing_trade_type']
    if last_losing_trade == "CALL":
        contract_type_to_use = "PUT"
    else: 
        contract_type_to_use = "CALL"
        
    return new_stake, contract_type_to_use


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
        current_data['last_losing_trade_type'] = "CALL" # قيمة افتراضية للبدء
        
        if current_data['current_profit'] >= current_data['tp_target']:
            stop_triggered = "TP Reached"
            
    else:
        # 🔴 خسارة: تحديث العدادات
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        current_data['last_losing_trade_type'] = trade_type
        
        # 💡 حساب الرهان الجديد لتخزينه، ولكن لا يتم الدخول به الآن
        if current_data['current_step'] <= MARTINGALE_STEPS:
            new_stake, _ = calculate_next_trade_params(email)
            current_data['current_stake'] = new_stake
        else:
            # إذا تجاوزنا الحد الأقصى للمضاعفة، ننتظر التوقف أو نعود للأساسي
            current_data['current_stake'] = current_data['base_stake']
        
        if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES: 
            stop_triggered = "SL Reached"
            
        
    save_session_data(email, current_data) 
    
    if stop_triggered:
        stop_bot(email, clear_data=True, stop_reason=stop_triggered) 
        is_contract_open[email] = False 
        return 
        
    is_contract_open[email] = False # إغلاق الحالة للسماح بالدخول الجديد

    state = current_data['current_trade_state']
    rounded_last_stake = round(last_stake, 2)
    print(f"[LOG {email}] PNL: {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Last Stake: {rounded_last_stake:.2f}, State: {state['type']}")

def bot_core_logic(email, token, stake, tp, account_type, currency_code):
    """ Main bot logic for a single user/session. """
    global active_ws 
    global is_contract_open 
    global WSS_URL_UNIFIED
    
    WSS_URL = WSS_URL_UNIFIED

    active_ws[email] = None 
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
        "last_losing_trade_type": "CALL",
        "account_type": account_type,
        "currency": currency_code,
        # إعادة تعيين حالة الجلسة عند البداية
        "current_profit": 0.0,
        "current_step": 0,
        "consecutive_losses": 0,
        "total_wins": 0,
        "total_losses": 0,
        "open_contract_id": None,
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
            
        def get_candle_info(ticks):
            """يستخرج الافتتاح (Open) والإغلاق (Close) من قائمة التيكات"""
            if len(ticks) < CANDLE_TICK_SIZE: 
                return None
            open_price = ticks[0]
            close_price = ticks[-1]
            is_rising = close_price > open_price
            return {"open": open_price, "close": close_price, "rising": is_rising}

        def is_engulfing(candle_engulfing, candle_engulfed):
            """
            يتحقق مما إذا كانت الشمعة المبتلعة (candle_engulfing) 
            ابتلعت بالكامل الشمعة السابقة (candle_engulfed)
            """
            if not candle_engulfing or not candle_engulfed:
                return False
                
            c_e = candle_engulfing
            c_d = candle_engulfed
            
            # يجب أن يكونا متعاكسين في الاتجاه
            if c_e['rising'] == c_d['rising']:
                return False
                
            if c_e['rising']:
                # شمعة صاعدة تبتلع شمعة هابطة (Bullish Engulfing)
                # إغلاق المبتلعة > افتتاح المبتلعة (صعود)
                # افتتاح المبتلعة < إغلاق المبتلعة (هبوط)
                return (c_e['close'] > c_d['open'] and c_e['open'] < c_d['close'])
            else:
                # شمعة هابطة تبتلع شمعة صاعدة (Bearish Engulfing)
                # إغلاق المبتلعة < افتتاح المبتلعة (هبوط)
                # افتتاح المبتلعة > إغلاق المبتلعة (صعود)
                return (c_e['close'] < c_d['open'] and c_e['open'] > c_d['close'])


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
                
                current_data['tick_history'].append(current_price)
                if len(current_data['tick_history']) > TICK_HISTORY_SIZE:
                    current_data['tick_history'] = current_data['tick_history'][-TICK_HISTORY_SIZE:]
                
                current_second = datetime.fromtimestamp(current_timestamp, tz=timezone.utc).second
                # 💡 توقيت الدخول: فقط عند الثانية 0
                is_entry_time = current_second == 0 
                
                save_session_data(email, current_data) 
                
                # 1. إذا كان العقد مفتوحاً حالياً، نتجاهل أي إشارة دخول جديدة
                if is_contract_open.get(email) is True: 
                    return 
                    
                time_since_last_entry = current_timestamp - current_data['last_entry_time']
                
                # 2. منطق الدخول (الأساسي أو المضاعف)
                # نتحقق من الشروط: مضى وقت الصفقة (56 ثانية) + نحن عند الثانية 0
                if time_since_last_entry >= DURATION and is_entry_time: 
                    
                    if len(current_data['tick_history']) < TICK_HISTORY_SIZE: 
                        return 

                    tick_history = current_data['tick_history']
                    c_size = CANDLE_TICK_SIZE
                    
                    # 💡 تقسيم الـ 15 تيك إلى 3 شموع (كل شمعة 5 تيك)
                    candlestick_1_ticks = tick_history[0:c_size]
                    candlestick_2_ticks = tick_history[c_size:c_size*2]
                    candlestick_3_ticks = tick_history[c_size*2:c_size*3]
                    
                    c1_info = get_candle_info(candlestick_1_ticks)
                    c2_info = get_candle_info(candlestick_2_ticks)
                    c3_info = get_candle_info(candlestick_3_ticks)

                    if not c1_info or not c2_info or not c3_info: return
                    
                    # 💡 الشرط الجديد: ابتلاع الشمعة الثالثة للثانية
                    is_c3_engulfing_c2 = is_engulfing(c3_info, c2_info)

                    pattern_detected = False
                    contract_type_to_use = None
                    
                    if is_c3_engulfing_c2:
                        # إذا حدث الابتلاع، نتجاهل الشمعة الأولى ونتبع اتجاه الشمعة المبتلعة (C3)
                        
                        if c3_info['rising']:
                            # ابتلاع صعودي (Bullish Engulfing) -> ندخل CALL
                            contract_type_to_use = "CALL"
                        else:
                            # ابتلاع هبوطي (Bearish Engulfing) -> ندخل PUT
                            contract_type_to_use = "PUT"
                        
                        pattern_detected = True
                        
                    if not pattern_detected:
                        return 

                    # 3. تحديد الرهان والاتجاه بناءً على الخطوة الحالية
                    
                    if current_data['current_step'] == 0:
                        # الدخول الأساسي
                        stake_to_use = current_data['base_stake']
                        # contract_type_to_use تم تحديده بالفعل بناءً على الابتلاع
                        
                    elif current_data['current_step'] > 0 and current_data['current_step'] <= MARTINGALE_STEPS:
                        # الدخول المضاعف (يجب أن يعكس اتجاه الصفقة الخاسرة السابقة)
                        stake_to_use, intended_martingale_type = calculate_next_trade_params(email)
                        
                        # نستخدم الاتجاه المضاعف المعاكس للخسارة السابقة، بغض النظر عن النمط الحالي
                        contract_type_to_use = intended_martingale_type

                    else:
                        # تجاوز خطوات المضاعفة
                        return
                        
                    entry_price = current_data['last_tick_data']['price']
                    current_data['last_entry_price'] = entry_price
                    current_data['last_entry_time'] = current_timestamp
                    current_data['current_trade_state']['type'] = contract_type_to_use 
                    current_data['current_stake'] = stake_to_use 
                    save_session_data(email, current_data)

                    send_trade_order(email, stake_to_use, contract_type_to_use, current_data['currency'])
                        
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
                    
                    current_data = get_session_data(email)
                    current_data['open_contract_id'] = None
                    save_session_data(email, current_data) 
                    
                    # معالجة النتيجة، ولا يتم الدخول المضاعف إلا عند ظهور نمط جديد
                    check_pnl_limits(email, contract['profit'], trade_type) 
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

            elif msg_type == 'authorize':
                print(f"✅ [AUTH {email}] Success. Account: {data['authorize']['loginid']}")

        def on_close_wrapper(ws_app, code, msg):
            print(f"❌ [WS Close {email}] Code: {code}, Message: {msg}")
            
        def on_ping_wrapper(ws, message):
            if not get_session_data(email).get('is_running'):
                ws.close()

        try:
            ws = websocket.WebSocketApp(
                WSS_URL, on_open=on_open_wrapper, on_message=on_message_wrapper, 
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
# FLASK APP SETUP AND ROUTES
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
    {% set martingale_mode = 'Deferred Martingale (Awaits next pattern) | Reverse Last Loss' %}
    {% set strategy = '3-Candle Engulfing Pattern (5 Ticks/Candle) | Entry: Second 0 | Duration: 56s | ' + martingale_mode + ' x' + martingale_multiplier|string + ' (Max ' + max_consecutive_losses|string + ' Losses, Max Step ' + max_martingale_step|string + ')' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    <div class="data-box">
        <p>Account Type: <b>{{ session_data.account_type.upper() }}</b> | Currency: <b>{{ session_data.currency }}</b></p>
        <p>Net Profit: <b>{{ session_data.currency }} {{ session_data.current_profit|round(2) }}</b></p>
        
        <p style="font-weight: bold; color: {% if session_data.open_contract_id %}#007bff{% else %}#555{% endif %};">
            Open Contract Status: 
            <b>{% if session_data.open_contract_id %}1{% else %}0{% endif %}</b>
        </p>

        <p>Current Stake: <b>{{ session_data.currency }} {{ session_data.current_stake|round(2) }}</b></p>
        <p style="font-weight: bold; color: {% if session_data.consecutive_losses > 0 %}red{% else %}green{% endif %};">
        Consecutive Losses: <b>{{ session_data.consecutive_losses }}</b> / {{ max_consecutive_losses }} 
        (Last Direction: <b>{{ session_data.last_losing_trade_type }}</b>)
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
        timezone=timezone
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

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
DURATION = 5            # 5 تيكات (مدة الصفقة)
DURATION_UNIT = "t"     
MARTINGALE_STEPS = 4    
MAX_CONSECUTIVE_LOSSES = 5 
RECONNECT_DELAY = 1      
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json" 
TICK_HISTORY_SIZE = 35 # 7 شموع * 5 تيك = 35 تيك
MARTINGALE_MULTIPLIER = 2.2 
CANDLE_TICK_SIZE = 5   # حجم الشمعة الواحدة 5 تيك
# الثواني المسموحة لتنفيذ الصفقة (والتحليل الآن)
SYNC_SECONDS = [0, 14, 30, 44] 
# ==========================================================

# ==========================================================
# BOT RUNTIME STATE (Shared and Local)
# ==========================================================
flask_local_processes = {}
manager = multiprocessing.Manager() 

active_ws = {} 
is_contract_open = manager.dict() 

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
    "pending_time_signal": None,
    "pending_martingale": False, 
    "martingale_stake": 0.0,     
    "martingale_type": "CALL",   
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
        # تعيين الحالة إلى True فوراً بعد إرسال الطلب لمنع الدخول المتعدد
        is_contract_open[email] = True 
        print(f"💰 [TRADE] Sent {contract_type} ({currency_code}) with stake: {rounded_stake:.2f}")
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
        current_data['last_losing_trade_type'] = "CALL" 
        current_data['pending_martingale'] = False # مسح حالة المضاعفة
        
        if current_data['current_profit'] >= current_data['tp_target']:
            stop_triggered = "TP Reached"
            
    else:
        # 🔴 خسارة: تحديث العدادات وتجهيز المضاعفة الفورية
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        current_data['last_losing_trade_type'] = trade_type
        
        # إعداد صفقة المضاعفة الفورية
        if current_data['current_step'] <= MARTINGALE_STEPS:
            new_stake = calculate_martingale_stake(current_data['base_stake'], current_data['current_step'])
            contract_type_to_use = "PUT" if trade_type == "CALL" else "CALL"
            
            current_data['current_stake'] = new_stake
            current_data['pending_martingale'] = True # تفعيل حالة الانتظار
            current_data['martingale_stake'] = new_stake
            current_data['martingale_type'] = contract_type_to_use
            print(f"⚠️ [MARTINGALE PENDING] Loss detected. Next trade: {contract_type_to_use} @ {new_stake:.2f}. Will execute immediately upon receiving contract result.")
        else:
            current_data['current_stake'] = current_data['base_stake']
            current_data['pending_martingale'] = False
        
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
        
    # إغلاق الحالة المشتركة للسماح بالدخول الجديد (سواء مضاعفة أو دخول عادي)
    is_contract_open[email] = False 

    state = current_data['current_trade_state']
    rounded_last_stake = round(last_stake, 2)
    print(f"[LOG {email}] PNL: {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Last Stake: {rounded_last_stake:.2f}, State: {state['type']}")
    
    # إرجاع ما إذا كانت المضاعفة جاهزة للتنفيذ الفوري
    return current_data['pending_martingale'] 

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
        "last_losing_trade_type": "CALL",
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
        "martingale_type": "CALL",
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

        def check_alternating_candles(candle_infos):
            """
            يتحقق مما إذا كانت سلسلة الـ 7 شموع متناوبة في الاتجاه 
            ويحدد نوع الصفقة بناءً على اتجاه الشمعة الأخيرة (Follow Last Direction).
            """
            if len(candle_infos) != 7:
                return None, None 
            
            directions = [c['rising'] for c in candle_infos]
            
            # 1. التحقق من التناوب
            is_alternating = all(directions[i] != directions[i+1] for i in range(6))
            
            if not is_alternating:
                return None, None
                
            # 2. تحديد نوع الصفقة: الدخول في نفس اتجاه الشمعة السابعة والأخيرة
            last_candle_is_rising = directions[6]
            
            if last_candle_is_rising: 
                # الشمعة الأخيرة صاعدة، الدخول صعوداً
                return True, "CALL" 
            
            else:
                # الشمعة الأخيرة هابطة، الدخول هبوطاً
                return True, "PUT" 
        
        # دالة مساعدة لتنفيذ صفقة المضاعفة فوراً
        def send_martingale_trade(email, current_data):
            stake_to_use = current_data['martingale_stake']
            contract_type_to_use = current_data['martingale_type']
            currency_code = current_data['currency']
            
            # يجب أن يكون هناك تيك أخير لتسجيل سعر الدخول ووقت الدخول
            if not current_data['last_tick_data']:
                print("❌ [MARTINGALE ERROR] Cannot execute martingale: No last tick data.")
                return

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
            print("🚀 [MARTINGALE EXECUTION] Executed immediately after contract loss notification.")
        
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
                
                # 1. تحديث بيانات التيك الحالية وتاريخ التيكات (دائماً)
                current_data['last_tick_data'] = {
                    "price": current_price,
                    "timestamp": current_timestamp
                }
                current_data['tick_history'].append(current_price)
                if len(current_data['tick_history']) > TICK_HISTORY_SIZE:
                    current_data['tick_history'] = current_data['tick_history'][-TICK_HISTORY_SIZE:]
                
                current_seconds = datetime.fromtimestamp(current_timestamp).second
                save_session_data(email, current_data) # حفظ قائمة التيكات المحدثة
                
                
                # 2. منطق التحليل والدخول (مقيد بالوقت) - للدخول العادي
                
                # التحقق من أن البوت جاهز للدخول (لا توجد صفقة مفتوحة ولا صفقة مضاعفة معلقة)
                if is_contract_open.get(email) is False and current_data['pending_martingale'] is False:
                    
                    # 🌟 الشرط الحاسم: هل ثواني التيك الحالي هي إحدى الثواني المسموحة؟
                    if current_seconds in SYNC_SECONDS: 
                        
                        is_pattern_ready = len(current_data['tick_history']) == TICK_HISTORY_SIZE
                        
                        if is_pattern_ready:
                            
                            current_time_ms = time.time() * 1000
                            time_since_last_entry_ms = current_time_ms - current_data['last_entry_time']
                            is_time_gap_respected = time_since_last_entry_ms > 1000 
                            
                            if not is_time_gap_respected:
                                return

                            tick_history = current_data['tick_history']
                            c_size = CANDLE_TICK_SIZE
                            
                            # استخراج معلومات الشموع السبع للتحليل الفوري
                            candle_infos = []
                            for i in range(7):
                                start_index = i * c_size
                                end_index = (i + 1) * c_size
                                ticks = tick_history[start_index : end_index]
                                info = get_candle_info(ticks)
                                if info:
                                    candle_infos.append(info)
                                else:
                                    break
                            
                            if len(candle_infos) != 7: return
                            
                            # التحقق من نمط التناوب وتحديد الاتجاه
                            pattern_detected, contract_type_to_use = check_alternating_candles(candle_infos)
                            
                            if pattern_detected:
                                
                                # تنفيذ الصفقة فوراً (لأننا الآن عند ثواني التزامن)
                                stake_to_use = current_data['base_stake']
                                entry_price = current_data['last_tick_data']['price']

                                # تحديث حالة البوت للدخول
                                current_data['last_entry_price'] = entry_price
                                current_data['last_entry_time'] = current_time_ms
                                current_data['current_trade_state']['type'] = contract_type_to_use 
                                current_data['current_stake'] = stake_to_use 
                                
                                # مسح التاريخ لبدء جمع 35 تيك جديد للصفقة التالية 
                                current_data['tick_history'] = [] 
                                save_session_data(email, current_data)

                                send_trade_order(email, stake_to_use, contract_type_to_use, current_data['currency'])
                                print(f"🚀 [SIGNAL EXECUTION] Pattern detected and executed at second {current_seconds} ({contract_type_to_use} @ {stake_to_use:.2f}).")
                                return
                                
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
                    
                    # 3. معالجة النتيجة وإعداد المضاعفة
                    martingale_ready = check_pnl_limits(email, profit_loss, trade_type) 
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))
                    
                    # 4. تنفيذ المضاعفة فوراً إذا كانت خسارة
                    if martingale_ready:
                        # يجب إعادة تحميل البيانات للتأكد من أنها أحدث حالة بعد check_pnl_limits
                        updated_data = get_session_data(email) 
                        send_martingale_trade(email, updated_data)


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
    {% set martingale_mode = 'Instant Martingale immediately after Loss' %}
    {% set analysis_sync = sync_seconds|join(', ') %}
    {% set strategy = '7-Candle Alternating Pattern (Follow Last Direction) | Analysis & Entry Sync: ' + analysis_sync + ' | Duration: 5 Ticks | ' + martingale_mode + ' x' + martingale_multiplier|string + ' (Max ' + max_consecutive_losses|string + ' Losses, Max Step ' + max_martingale_step|string + ')' %}
    
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
                {% if session_data.pending_martingale %}1 (Martingale: {{ session_data.martingale_type }})
                {% else %}0 (Analysis is only performed at {{ analysis_sync }} seconds)
                {% endif %}
            </b>
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
        timezone=timezone,
        sync_seconds=SYNC_SECONDS
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

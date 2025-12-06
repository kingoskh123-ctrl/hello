import time
import json
import websocket 
# ⚠️ استخدام multiprocessing لإدارة العمليات المنفصلة
import multiprocessing 
import os 
import sys 
import fcntl 
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, g
from datetime import timedelta, datetime, timezone

# ==========================================================
# BOT CONSTANT SETTINGS
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"       
DURATION = 5          
DURATION_UNIT = "t" 
MARTINGALE_STEPS = 4
MAX_CONSECUTIVE_LOSSES = 5 
RECONNECT_DELAY = 1      
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json" 
TICK_HISTORY_SIZE = 15 # تحليل 15 تيك
# ==========================================================

# ==========================================================
# BOT RUNTIME STATE (Multiprocess Shared Cache)
# ==========================================================

# ⚠️ Manager لإدارة البيانات المشتركة بين عملية Flask وعمليات البوت
manager = multiprocessing.Manager()
active_processes = manager.dict() # لتخزين العمليات النشطة

# هذه المتغيرات ستبقى خاصة بالعملية (Process) التي تعمل فيها
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
    "last_losing_trade_type": "CALL" 
}
# ==========================================================

# ==========================================================
# PERSISTENT STATE MANAGEMENT FUNCTIONS (Fixed for File Locking)
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
        # Ensure default keys exist
        for key, default_val in DEFAULT_SESSION_STATE.items():
            if key not in data:
                data[key] = default_val
        return data
    
    return DEFAULT_SESSION_STATE.copy()

def load_allowed_users():
    if not os.path.exists(USER_IDS_FILE):
        print(f"❌ ERROR: Missing {USER_IDS_FILE} file.")
        return set()
    try:
        with open(USER_IDS_FILE, 'r', encoding='utf-8') as f:
            users = {line.strip().lower() for line in f if line.strip()}
        return users
    except Exception as e:
        print(f"❌ ERROR reading {USER_IDS_FILE}: {e}")
        return set()
        
def stop_bot(email, clear_data=True, stop_reason="Stopped Manually"): 
    """ Stop the bot process (process termination if necessary) and update state. """
    global is_contract_open 
    global active_processes
    
    # 1. تحديث is_running state (للإشارة إلى العملية بأن تتوقف)
    current_data = get_session_data(email)
    if current_data.get("is_running") is True:
        current_data["is_running"] = False
        current_data["stop_reason"] = stop_reason 
        save_session_data(email, current_data) 

    # 2. إنهاء العملية (Process) من الـ Manager إذا كانت لا تزال نشطة
    if clear_data and email in active_processes:
        try:
            process = active_processes[email]
            if process.is_alive():
                process.terminate() # إنهاء العملية قسراً
                process.join() 
            del active_processes[email]
            print(f"🛑 [INFO] Process for {email} forcefully terminated.")
        except Exception as e:
            print(f"❌ [ERROR] Could not terminate process for {email}: {e}")
            
    if email in is_contract_open:
        is_contract_open[email] = False

    # 3. حذف أو حفظ البيانات
    if clear_data:
        if stop_reason in ["SL Reached", "TP Reached"]:
              print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data kept for display.")
        else:
             delete_session_data(email)
             print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}) and session data cleared from file.")
    else:
        # حالة إعادة الاتصال التلقائي (في هذه الحالة، يجب أن تحاول العملية نفسها إعادة الاتصال)
        pass

# ==========================================================
# TRADING BOT FUNCTIONS (Runs inside a separate Process)
# ==========================================================

def calculate_martingale_stake(base_stake, current_stake, current_step):
    """ Martingale logic: multiply the losing stake by 2.2 """
    if current_step == 0:
        return base_stake
        
    if current_step <= MARTINGALE_STEPS:
        return current_stake * 2.2 
    else:
        return base_stake

def send_trade_order(email, stake, contract_type):
    """ Send the actual trade order. """
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
            "currency": "USD", "duration": DURATION,
            "duration_unit": DURATION_UNIT, "symbol": SYMBOL
        }
    }
    try:
        ws_app.send(json.dumps(trade_request))
        is_contract_open[email] = True 
        print(f"💰 [TRADE] Sent {contract_type} with rounded stake: {rounded_stake:.2f}")
    except Exception as e:
        print(f"❌ [TRADE ERROR] Could not send trade order: {e}")
        pass

def re_enter_immediately(email, last_loss_stake):
    """ Prepares state for the Martingale stake. """
    current_data = get_session_data(email)
    
    new_stake = calculate_martingale_stake(
        current_data['base_stake'],
        last_loss_stake,
        current_data['current_step'] 
    )

    current_data['current_stake'] = new_stake
    save_session_data(email, current_data)


def check_pnl_limits(email, profit_loss, trade_type): 
    """ Update statistics and handle Martingale Reversal logic. """
    global is_contract_open 
    
    is_contract_open[email] = False 

    current_data = get_session_data(email)
    if not current_data.get('is_running'): return

    last_stake = current_data['current_stake'] 

    current_data['current_profit'] += profit_loss
    
    if profit_loss > 0:
        # 1. Win: Reset
        current_data['total_wins'] += 1
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        current_data['last_losing_trade_type'] = "CALL" 
        
    else:
        # 2. Loss: Martingale setup
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        # 2.1. حفظ نوع الصفقة الخاسرة لتطبيق العكس في الصفقة التالية
        current_data['last_losing_trade_type'] = trade_type
        
        # 2.2. Check Stop Loss (SL) limits
        if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES: 
            stop_bot(email, clear_data=True, stop_reason="SL Reached") 
            return 
        
        # 2.3. Immediate re-entry preparation
        save_session_data(email, current_data) 
        re_enter_immediately(email, last_stake) 
        return

    # 3. Check Take Profit (TP)
    if current_data['current_profit'] >= current_data['tp_target']:
        stop_bot(email, clear_data=True, stop_reason="TP Reached") 
        return
    
    save_session_data(email, current_data)
        
    state = current_data['current_trade_state']
    rounded_last_stake = round(last_stake, 2)
    print(f"[LOG {email}] PNL: {current_data['current_profit']:.2f}, Step: {current_data['current_step']}, Last Stake: {rounded_last_stake:.2f}, State: {state['type']}")


def bot_core_logic(email, token, stake, tp):
    """ Main bot logic with auto-reconnect loop. """
    # ⚠️ إعادة تعريف active_ws و is_contract_open محلياً لكل عملية
    global active_ws 
    global is_contract_open 
    global active_processes # للوصول إلى Manager

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
        "last_losing_trade_type": "CALL"
    })
    save_session_data(email, session_data)

    while True: 
        current_data = get_session_data(email)
        
        if not current_data.get('is_running'):
            break

        print(f"🔗 [PROCESS] Attempting to connect for {email}...")

        def on_open_wrapper(ws_app):
            ws_app.send(json.dumps({"authorize": current_data['api_token']})) 
            ws_app.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            running_data = get_session_data(email)
            running_data['is_running'] = True
            save_session_data(email, running_data)
            print(f"✅ [PROCESS] Connection established for {email}.")
            is_contract_open[email] = False 
            
        def is_rising(ticks):
            return ticks[-1] > ticks[0] 

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
                
                # 1. تحديث آخر تيك تم استلامه
                current_data['last_tick_data'] = {
                    "price": current_price,
                    "timestamp": current_timestamp
                }
                
                # 2. تحديث سجل التيكات
                current_data['tick_history'].append(current_price)
                if len(current_data['tick_history']) > TICK_HISTORY_SIZE:
                    current_data['tick_history'] = current_data['tick_history'][-TICK_HISTORY_SIZE:]
                
                # ⚠️ الثواني المحدثة: 0، 14، 30، 44
                current_second = datetime.fromtimestamp(current_timestamp, tz=timezone.utc).second
                is_entry_time = current_second in [0, 14, 30, 44] 
                
                save_session_data(email, current_data) 
                
                if is_contract_open.get(email) is True: 
                    return 
                    
                
                time_since_last_entry = current_timestamp - current_data['last_entry_time']
                
                # ⚠️ إذا تجاوزنا 14 ثانية ووصلنا إلى نقطة دخول جديدة
                if time_since_last_entry >= 14 and is_entry_time: 
                    
                    if len(current_data['tick_history']) < TICK_HISTORY_SIZE:
                        return 

                    tick_history = current_data['tick_history']
                    
                    # 3. تحليل النمط (3 شمعات × 5 تيك)
                    candlestick_1 = tick_history[0:5]
                    candlestick_2 = tick_history[5:10]
                    candlestick_3 = tick_history[10:15]
                    
                    c1_rising = is_rising(candlestick_1)
                    c2_rising = is_rising(candlestick_2)
                    c3_rising = is_rising(candlestick_3)

                    # 4. تحديد نوع العقد بناءً على النمط
                    contract_type_to_use = None
                    
                    # صاعد-هابط-صاعد -> CALL
                    if c1_rising and not c2_rising and c3_rising:
                        contract_type_to_use = "CALL"
                        
                    # هابط-صاعد-هابط -> PUT
                    elif not c1_rising and c2_rising and not c3_rising:
                        contract_type_to_use = "PUT"
                        
                    if contract_type_to_use is None:
                        return 
                        
                    # 5. تطبيق منطق المارتينجال المعكوس (إذا كان هناك خسارة سابقة)
                    if current_data['current_step'] > 0:
                        last_losing_trade = current_data['last_losing_trade_type']
                        
                        if last_losing_trade == "CALL":
                            contract_type_to_use = "PUT"
                        else: 
                            contract_type_to_use = "CALL"


                    # 6. إرسال الصفقة
                    stake_to_use = current_data['current_stake']
                    entry_price = current_data['last_tick_data']['price']
                    current_data['last_entry_price'] = entry_price
                    current_data['last_entry_time'] = current_timestamp
                    current_data['current_trade_state']['type'] = contract_type_to_use 
                    save_session_data(email, current_data)

                    send_trade_order(email, stake_to_use, contract_type_to_use)
                        
            elif msg_type == 'buy':
                contract_id = data['buy']['contract_id']
                ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
            elif msg_type == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                if contract.get('is_sold') == 1:
                    trade_type = contract.get('contract_type')
                    check_pnl_limits(email, contract['profit'], trade_type) 
                    if 'subscription_id' in data: ws_app.send(json.dumps({"forget": data['subscription_id']}))

        def on_close_wrapper(ws_app, code, msg):
            # إغلاق عملية الـ WS فقط، والسماح للحلقة الخارجية بإعادة الاتصال إذا كانت is_running=True
            print(f"❌ [WS Close {email}] Code: {code}, Message: {msg}")

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
        
        if get_session_data(email).get('is_running') is False:
            break
        
        print(f"💤 [PROCESS] Waiting {RECONNECT_DELAY} seconds before retrying connection for {email}...")
        time.sleep(RECONNECT_DELAY)

    # ⚠️ عند انتهاء العملية بالكامل، يجب إزالتها من القاموس المشترك
    if email in active_processes:
        del active_processes[email]
    print(f"🛑 [PROCESS] Bot process ended for {email}.")


# ==========================================================
# FLASK APP SETUP AND ROUTES
# ==========================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False 

# ----------------- HTML Templates (Auth and Control) -----------------
# (لم يتم تغيير محتوى القوالب HTML، فقط لاستعراضهم في الكود الكامل)

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
    p {
        font-size: 1.1em;
        line-height: 1.6;
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
    form button {
        padding: 12px 20px;
        border: none;
        border-radius: 5px;
        cursor: pointer;
        font-size: 1.1em;
        margin-top: 15px;
        width: 100%;
    }
    input[type="text"], input[type="number"], input[type="email"] {
        width: 98%;
        padding: 10px;
        margin-top: 5px;
        margin-bottom: 10px;
        border: 1px solid #ccc;
        border-radius: 4px;
        box-sizing: border-box;
        text-align: left;
    }
    .data-box {
        background-color: #f8f9fa;
        border: 1px solid #e9ecef;
        padding: 15px;
        border-radius: 5px;
        margin-bottom: 15px;
    }
    .live-data {
        color: #007bff;
        font-weight: bold;
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

{% if session_data and session_data.is_running %}
    {% set current_state = session_data.current_trade_state %}
    
    <p class="status-running">✅ Bot is **Running**! (Auto-refreshing)</p>
    
    <div class="data-box">
        <h3>📊 Live Tick Data</h3>
        {% if session_data.last_tick_data %}
            <p>Last Price: <span class="live-data">${{ session_data.last_tick_data.price|round(4) }}</span></p>
            <p>Timestamp: <span class="live-data">{{ datetime.fromtimestamp(session_data.last_tick_data.timestamp, tz=timezone.utc).strftime('%H:%M:%S') }} UTC</span></p>
        {% else %}
            <p>Awaiting first tick...</p>
        {% endif %}
        <p>Ticks Collected: **{{ session_data.tick_history|length }}** / {{ TICK_HISTORY_SIZE }}</p>
        {% if session_data.tick_history|length >= TICK_HISTORY_SIZE %}
            <p style="color: green; font-weight: bold;">Pattern Ready for Analysis.</p>
        {% endif %}
    </div>
    
    <div class="data-box">
        <h3>📈 Trading Stats</h3>
        <p>Net Profit: **${{ session_data.current_profit|round(2) }}**</p>
        <p>Current Stake: **${{ session_data.current_stake|round(2) }}**</p>
        <p>Step: **{{ session_data.current_step }}** / {{ martingale_steps }}</p>
        <p>Stats: **{{ session_data.total_wins }}** Wins | **{{ session_data.total_losses }}** Losses</p>
        {% if session_data.current_step > 0 %}
             <p style="font-weight: bold; color: orange;">Martingale Reversal Active: Last Loss was {{ session_data.last_losing_trade_type }}</p>
        {% else %}
             <p style="font-weight: bold; color: #007bff;">Strategy: **Pattern Follow**</p>
        {% endif %}
    </div>
    
    <form method="POST" action="{{ url_for('stop_route') }}">
        <button type="submit" style="background-color: red; color: white;">🛑 Stop Bot</button>
    </form>
{% else %}
    <p class="status-stopped">🛑 Bot is **Stopped**. Enter settings to start a new session.</p>
    <form method="POST" action="{{ url_for('start_bot') }}">
        <label for="token">Deriv API Token:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}" {% if session_data and session_data.api_token and session_data.is_running is not none %}readonly{% endif %}><br>
        
        <label for="stake">Base Stake (USD):</label><br>
        <input type="number" id="stake" name="stake" value="{{ session_data.base_stake|round(2) if session_data else 0.35 }}" step="0.01" min="0.35" required><br>
        
        <label for="tp">TP Target (USD):</label><br>
        <input type="number" id="tp" name="tp" value="{{ session_data.tp_target|round(2) if session_data else 10.0 }}" step="0.01" required><br>
        
        <button type="submit" style="background-color: green; color: white;">🚀 Start Bot</button>
    </form>
{% endif %}
<hr>
<a href="{{ url_for('logout') }}" style="display: block; text-align: center; margin-top: 15px; font-size: 1.1em;">Logout</a>

<script>
    // Conditional auto-refresh JavaScript code
    function autoRefresh() {
        var isRunning = {{ 'true' if session_data and session_data.is_running else 'false' }};
        
        if (isRunning) {
            setTimeout(function() {
                window.location.reload();
            }, 1000);
        }
    }

    autoRefresh();
</script>
"""

# ----------------- Flask Routes -----------------
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

    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)", "Displayed"]:
        
        reason = session_data["stop_reason"]
        
        if reason == "SL Reached":
            flash(f"🛑 STOP: الحد الأقصى للخسارة ({MAX_CONSECUTIVE_LOSSES} خسارات متتالية) تم الوصول إليه! (SL Reached)", 'error')
        elif reason == "TP Reached":
            flash(f"✅ GOAL: هدف الربح ({session_data['tp_target']} $) تم الوصول إليه بنجاح! (TP Reached)", 'success')
            
        session_data['stop_reason'] = "Displayed" 
        save_session_data(email, session_data) 
        
        if reason in ["SL Reached", "TP Reached"]:
            delete_session_data(email)


    return render_template_string(CONTROL_FORM, 
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        TICK_HISTORY_SIZE=TICK_HISTORY_SIZE,
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
    
    # ⚠️ التحقق من العمليات النشطة باستخدام القاموس المشترك
    global active_processes
    if email in active_processes and active_processes[email].is_alive():
        flash('Bot is already running.', 'info')
        return redirect(url_for('index'))
        
    try:
        current_data = get_session_data(email)
        if current_data.get('api_token') and request.form.get('token') == current_data['api_token']:
            token = current_data['api_token']
        else:
            token = request.form['token']

        stake = float(request.form['stake'])
        tp = float(request.form['tp'])
    except ValueError:
        flash("Invalid stake or TP value.", 'error')
        return redirect(url_for('index'))
        
    # ⚠️ تشغيل عملية (Process) جديدة
    process = multiprocessing.Process(target=bot_core_logic, args=(email, token, stake, tp))
    process.daemon = True
    process.start()
    
    # حفظ العملية في القاموس المشترك
    active_processes[email] = process
    
    flash('Bot process started successfully. It runs in a separate process.', 'success')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session:
        return redirect(url_for('auth_page'))
    
    # سيتم إيقاف العملية عبر دالة stop_bot المعدلة
    stop_bot(session['email'], clear_data=True, stop_reason="Stopped Manually") 
    flash('Bot process stopped and session data cleared.', 'success')
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('email', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('auth_page'))


if __name__ == '__main__':
    # ⚠️ ملاحظة: يجب أن يكون ملف user_ids.txt موجوداً يحتوي على الإيميلات المصرح لها
    port = int(os.environ.get("PORT", 5000))
    # use_reloader=False ضرورية لتجنب تشغيل العمليات مرتين
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

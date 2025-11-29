import time
import json
import websocket
import os
import sys
import fcntl
import threading
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, g
from datetime import timedelta, datetime, timezone
from multiprocessing import Process
from threading import Lock

# ==========================================================
# BOT CONSTANT SETTINGS (R_100 | x0 Martingale | 5 Ticks Analysis | 5 Ticks Duration)
# ==========================================================
WSS_URL = "wss://blue.derivws.com/websockets/v3?app_id=16929"
SYMBOL = "R_100"
DURATION = 5 # ⬅️ 5 تيكات مدة العقد
DURATION_UNIT = "t" # ⬅️ وحدة المدة هي التيك (t)
TICKS_TO_ANALYZE = 5 # ⬅️ 5 تيك للتحليل

# إعدادات الإيقاف التام بعد الخسارة الأولى
MARTINGALE_STEPS = 0 # تعطيل المضاعفة
MAX_CONSECUTIVE_LOSSES = 0 # ⬅️ التوقف بعد خسارة متتالية واحدة (1 > 0)
MARTINGALE_MULTIPLIER = 2.1
BARRIER_OFFSET = "0.7" # ⬅️ حاجز الإزاحة (قيمة الحاجز في العقد)
MOMENTUM_THRESHOLD = "0.5" # ⬅️ الحد الأدنى لفرق السعر المطلوب للزخم (P_diff)

RECONNECT_DELAY = 1
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json"
LAST_TRADE_TIME_FILE = "last_trade_time.json"

CONTRACT_TYPE_HIGHER = "PUT"
CONTRACT_TYPE_LOWER = "CALL"

# ==========================================================

# ==========================================================
# GLOBAL STATE
# ==========================================================
active_processes = {}
active_ws = {}
is_contract_open = {}
PROCESS_LOCK = Lock()
TRADE_LOCK = Lock()
LAST_TRADE_TIME = {} # ⬅️ لتتبع آخر عملية تداول

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
    "last_barrier_value": BARRIER_OFFSET
}

# --- Persistence functions ---

def load_last_trade_time():
    if not os.path.exists(LAST_TRADE_TIME_FILE): return {}
    try:
        with open(LAST_TRADE_TIME_FILE, 'r') as f:
            return json.load(f)
    except: return {}

def save_last_trade_time(email, timestamp):
    all_times = load_last_trade_time()
    all_times[email] = timestamp
    with open(LAST_TRADE_TIME_FILE, 'w') as f:
        json.dump(all_times, f, indent=4)

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
    
    all_times = load_last_trade_time()
    if email in all_times: del all_times[email]
    with open(LAST_TRADE_TIME_FILE, 'w') as f:
        json.dump(all_times, f, indent=4)


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

    if clear_data and stop_reason in ["SL Reached: Single Loss Stop.", "TP Reached", "API Buy Error", "Stopped Manually"]:
        print(f"🛑 [INFO] Bot for {email} stopped ({stop_reason}). Data cleared from file.")
        delete_session_data(email)
    else:
        print(f"⚠ [INFO] Bot for {email} stopped ({stop_reason}). Data kept.")
# --- End of Persistence and Control functions ---

# ==========================================================
# TRADING BOT FUNCTIONS (Single Operation)
# ==========================================================

def send_trade_order(email, ws_app, stake, currency, contract_type_param, barrier_offset):
    """ إرسال طلب شراء واحد مع حاجز الإزاحة """
    global DURATION, DURATION_UNIT, SYMBOL

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


def apply_martingale_logic(email, total_profit):
    """ يطبق منطق الإيقاف التام بعد الخسارة أو جني الأرباح """
    global MAX_CONSECUTIVE_LOSSES
    current_data = get_session_data(email)

    if not current_data.get('is_running'): return

    current_data['current_profit'] += total_profit

    # 💰 شرط جني الأرباح (TP)
    if current_data['current_profit'] >= current_data['tp_target']:
        save_session_data(email, current_data)
        stop_bot(email, clear_data=True, stop_reason="TP Reached")
        return

    base_stake_used = current_data['base_stake']

    # ❌ Loss Condition (شرط الخسارة)
    if total_profit < 0:
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1

        # 🛑 الإيقاف بعد أول خسارة
        if current_data['consecutive_losses'] > MAX_CONSECUTIVE_LOSSES: # (1 > 0)
            save_session_data(email, current_data)
            stop_bot(email, clear_data=True, stop_reason=f"SL Reached: Single Loss Stop.")
            return

        print(f"🛑 [SINGLE LOSS STOP] PnL: {total_profit:.2f}. SL Reached. Stopping Bot.")

    # ✅ Win or Draw Condition (الربح أو التعادل)
    else:
        current_data['total_wins'] += 1 if total_profit > 0 else 0
        current_data['current_step'] = 0
        current_data['consecutive_losses'] = 0

        entry_result_tag = "WIN" if total_profit > 0 else "DRAW"
        print(f"✅ [ENTRY RESULT] {entry_result_tag}. Total PnL: {total_profit:.2f}. Stake reset to base: {base_stake_used:.2f}.")

    # إعادة تعيين متغيرات الدخول بغض النظر عن النتيجة (للبداية النظيفة للصفقة التالية)
    current_data['current_stake_lower'] = base_stake_used
    current_data['current_stake_higher'] = base_stake_used
    current_data['current_entry_id'] = None
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    save_session_data(email, current_data)


def analyze_and_trade(email, ws_app, ticks_history, last_tick_price):
    """ يحلل آخر 5 تيك ويبدأ صفقة بناءً على الاتجاه وفرق السعر المطلوب """
    global BARRIER_OFFSET, CONTRACT_TYPE_HIGHER, CONTRACT_TYPE_LOWER, MARTINGALE_STEPS, MOMENTUM_THRESHOLD

    current_data = get_session_data(email)
    
    if not current_data.get('is_running'): return None # توقف البوت من واجهة المستخدم

    if current_data['current_step'] > MARTINGALE_STEPS: return None

    ticks = ticks_history

    # 1. التحقق من توفر 5 تيك
    if len(ticks) < TICKS_TO_ANALYZE:
        print(f"❌ [ENTRY FAIL] Only {len(ticks)} ticks available (Need {TICKS_TO_ANALYZE}). Skipping trade.")
        return False # ⬅️ لم يتم الدخول

    # 2. تحديد الأسعار والمومنتوم
    first_tick_price = ticks[0]
    last_tick_price = ticks[-1]

    price_difference = last_tick_price - first_tick_price
    absolute_diff = abs(price_difference)

    required_diff = float(MOMENTUM_THRESHOLD) # 0.5
    barrier_offset_value = BARRIER_OFFSET # 0.7

    # 3. تطبيق شرط الفرق في السعر ونوع العقد
    contract_type_to_use = None
    barrier_to_use = None
    strategy_tag = None

    stake_to_use = current_data['current_stake_higher']

    # ⬆️ إذا كان الاتجاه صاعداً وفرق السعر ≥ 0.5
    if price_difference > 0 and absolute_diff >= required_diff:
        contract_type_to_use = CONTRACT_TYPE_HIGHER
        barrier_to_use = f"+{barrier_offset_value}"
        strategy_tag = f"5 BULLISH Ticks (Diff >= {required_diff}) -> HIGHER +{barrier_offset_value}"

    # ⬇️ إذا كان الاتجاه هابطاً وفرق السعر ≥ 0.5
    elif price_difference < 0 and absolute_diff >= required_diff:
        contract_type_to_use = CONTRACT_TYPE_LOWER
        barrier_to_use = f"-{barrier_offset_value}"
        strategy_tag = f"5 BEARISH Ticks (Diff >= {required_diff}) -> LOWER -{barrier_offset_value}"

    else:
        print(f"⚠ [ENTRY SKIPPED] Momentum not strong enough ({absolute_diff:.5f} < {required_diff}). Closing connection immediately.")
        ws_app.close() # ⬅️ الإغلاق الفوري هنا
        return False # ⬅️ لم يتم الدخول

    # 4. إرسال الصفقة
    currency_to_use = current_data['currency']
    current_data['current_entry_id'] = time.time()
    current_data['open_contract_ids'] = []
    current_data['contract_profits'] = {}
    current_data['last_entry_time'] = int(time.time())
    current_data['last_entry_price'] = last_tick_price
    save_session_data(email, current_data)

    print(f"🧠 [SINGLE TRADE ENTRY] Strategy: {strategy_tag} | Stake: {round(stake_to_use, 2):.2f}")

    if send_trade_order(email, ws_app, stake_to_use, currency_to_use, contract_type_to_use, barrier_to_use):
        return True # ⬅️ تم الدخول
    
    return False # ⬅️ لم يتم الدخول


def single_trade_operation(email):
    """ تقوم بعملية اتصال، جمع تيكات، تحليل، تداول (إذا لزم الأمر)، وانتظار النتيجة، ثم إغلاق الاتصال. """

    current_data = get_session_data(email)
    if not current_data.get('is_running'):
        print(f"[OP] Bot not running for {email}. Exiting operation.")
        return

    # 1. الاتصال والترخيص
    ticks_buffer = []
    last_price = 0.0
    contract_result = None
    
    ws_app = websocket.WebSocketApp(
        WSS_URL,
        on_error=lambda ws, err: print(f"[WS Error {email}] {err}")
    )

    def on_open(ws):
        ws.send(json.dumps({"authorize": current_data['api_token']}))
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        print(f"🔗 [OP] Connection opened and subscribed for {email}.")
    
    def on_message(ws, message):
        nonlocal ticks_buffer, last_price, contract_result
        data = json.loads(message)
        msg_type = data.get('msg_type')
        
        # إذا تم الإيقاف أثناء الاتصال
        if not get_session_data(email).get('is_running'):
            ws.close()
            return

        if msg_type == 'tick':
            try:
                price = float(data['tick']['quote'])
                last_price = price
                
                # تجميع 5 تيكات
                ticks_buffer.append(price)
                if len(ticks_buffer) > TICKS_TO_ANALYZE:
                    ticks_buffer.pop(0)
                
                # عند جلب 5 تيكات، نبدأ التحليل والدخول
                if len(ticks_buffer) == TICKS_TO_ANALYZE:
                    ws.send(json.dumps({"forget_all": "ticks"})) # نوقف اشتراك التيكات
                    
                    if analyze_and_trade(email, ws, ticks_buffer, last_price):
                        pass # تم الدخول، ننتظر نتائج العقد
                    else:
                        pass # لم يتم الدخول وتم إغلاق الاتصال بواسطة analyze_and_trade
                        
            except Exception as e:
                print(f"❌ [TICK ERROR] Failed to process tick: {e}")

        elif msg_type == 'buy':
            contract_id = data['buy']['contract_id']
            current_data = get_session_data(email)
            current_data['open_contract_ids'].append(contract_id)
            save_session_data(email, current_data)

            # طلب تتبع نتيجة العقد
            ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))

        elif msg_type == 'proposal_open_contract':
            contract = data['proposal_open_contract']
            if contract.get('is_sold') == 1:
                profit_loss = contract['profit']
                contract_result = profit_loss # تخزين النتيجة
                
                # إغلاق الاتصال فور الحصول على النتيجة
                if 'subscription_id' in data: ws.send(json.dumps({"forget": data['subscription_id']}))
                ws.close() 

        elif 'error' in data:
            error_code = data['error'].get('code', 'N/A')
            error_message = data['error'].get('message', 'Unknown Error')
            print(f"❌❌ [API ERROR] Code: {error_code}, Message: {error_message}. Closing connection.")
            contract_result = -current_data['base_stake'] # افتراض خسارة في حالة خطأ الشراء
            ws.close()
            
    # تشغيل الاتصال
    ws_app.on_open = on_open
    ws_app.on_message = on_message
    
    # Run the WS connection in a separate thread to handle blocking
    wst = threading.Thread(target=lambda: ws_app.run_forever(), daemon=True)
    wst.start()
    
    # Wait for the connection to close or a reasonable timeout (e.g., 20 seconds for contract completion)
    wst.join(timeout=DURATION * 4 + 5) # 5 تيك * 4 + 5 ثواني احتياط (للتأكد من الإغلاق في حالة الخطأ)
    
    # 2. معالجة النتيجة
    if contract_result is not None:
        apply_martingale_logic(email, contract_result)
        current_data = get_session_data(email)
        current_data['last_valid_tick_price'] = last_price
        save_session_data(email, current_data)
        
    elif get_session_data(email).get('is_running') and last_price != 0.0:
        # إذا انتهى الاتصال (بسبب إغلاق فوري لعدم وجود زخم)
        current_data = get_session_data(email)
        current_data['last_valid_tick_price'] = last_price
        save_session_data(email, current_data)
        print(f"⚠ [OP] Operation completed: No entry or closed on contract failure/timeout.")

# --- (FLASK APP SETUP AND ROUTES) ---

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
    {% endif %}
{% endwith %}


{% if session_data and session_data.is_running %}
    {% set strategy = ticks_to_analyze|string + "-Tick Analysis (Momentum " + momentum_threshold + " | Barrier " + barrier_offset + " | Duration " + duration|string + " Ticks) (Stop on 1st Loss)" %}
    {% set next_trade_in = 10 - (current_time % 10) %}
    
    <p class="status-running">✅ Bot is Running! (Next Trade Attempt in {{ next_trade_in }} seconds)</p>
    <p>Account Type: {{ session_data.account_type.upper() }} | Currency: {{ session_data.currency }}</p>
    <p>Net Profit: {{ session_data.currency }} {{ session_data.current_profit|round(2) }}</p>
    <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
    <p style="font-weight: bold; color: purple;">Last Tick Price: {{ session_data.last_valid_tick_price|round(5) }}</p>
    <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>

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
            // التحديث كل 1000 مللي ثانية (1 ثانية) عندما يكون البوت قيد التشغيل
            setTimeout(function() {
                window.location.reload();
            }, 1000); 
        } 
        // لا يوجد أمر 'else' هنا، مما يعني أن التحديث يتوقف تماماً عند توقف البوت.
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

    # ⬅️ منطق الإيقاف التلقائي وإعادة التوجيه (للتأكد من مسح البيانات وعرض واجهة الإعدادات)
    if not session_data.get('is_running') and "stop_reason" in session_data and session_data["stop_reason"] not in ["Stopped Manually", "Running", "Disconnected (Auto-Retry)"]:
        reason = session_data["stop_reason"]
        if reason.startswith("SL Reached"):
            flash(f"🛑 تم الوصول لحد وقف الخسارة! (البوت توقف بعد خسارة واحدة).", 'error')
        elif reason == "TP Reached":
            flash(f"✅ تم الوصول لهدف الربح ({session_data['tp_target']} {session_data.get('currency', 'USD')}) بنجاح!", 'success')
        elif reason.startswith("API Buy Error"):
            flash(f"❌ خطأ في API: {reason}. توقف البوت بسبب خطأ.", 'error')

        delete_session_data(email) 
        return redirect(url_for('index'))
    
    current_time = int(time.time())
    
    # ⬅️ منطق التشغيل المتقطع (Triggering the Single Trade Operation)
    if session_data.get('is_running'):
        # يجب أن يتم التداول فقط عند الثواني 0, 10, 20, 30, 40, 50
        current_second = current_time % 60
        ALLOWED_ENTRY_SECONDS = [0, 10, 20, 30, 40, 50]
        
        should_run = current_second in ALLOWED_ENTRY_SECONDS
        
        # التأكد من عدم التداول مرتين في نفس الثانية المسموحة
        last_trade_time = load_last_trade_time().get(email, 0)
        
        if should_run and (current_time - last_trade_time) > 9: # 10 seconds interval
            print(f"⏰ [TRIGGER] Allowed second ({current_second}). Starting single trade operation for {email}.")
            
            # استخدام Process لحماية العملية من الإنهاء التلقائي لـ Gunicorn
            trade_process = Process(target=single_trade_operation, args=(email,))
            trade_process.start()
            
            save_last_trade_time(email, current_time)

    return render_template_string(CONTROL_FORM,
        email=email,
        session_data=session_data,
        martingale_steps=MARTINGALE_STEPS,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        martingale_multiplier=MARTINGALE_MULTIPLIER,
        duration=DURATION,
        barrier_offset=BARRIER_OFFSET,
        momentum_threshold=MOMENTUM_THRESHOLD,
        symbol=SYMBOL,
        ticks_to_analyze=TICKS_TO_ANALYZE,
        current_time=current_time
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
    if 'email' not in session: return redirect(url_for('auth_page'))
    email = session['email']

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
    
    # تحديث البيانات لبدء التشغيل
    current_data.update({
        "api_token": token,
        "base_stake": stake,
        "tp_target": tp,
        "is_running": True,
        "current_stake_lower": stake,
        "current_stake_higher": stake,
        "stop_reason": "Running",
        "currency": currency,
        "account_type": account_type,
    })
    save_session_data(email, current_data)
    save_last_trade_time(email, 0) # إعادة تعيين وقت آخر تداول للسماح بالتشغيل الفوري

    strategy_desc = f"5-Tick Momentum (R_100 - P_diff >= {MOMENTUM_THRESHOLD} - Barrier: {BARRIER_OFFSET} - Stop on 1st Loss)"
    flash(f'Bot started successfully in intermittent mode. Currency: {currency}. Account: {account_type.upper()}. Strategy: {strategy_desc}', 'success')
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
    # تأكد من إيقاف أي جلسات سابقة كانت تعمل بشكل مستمر
    all_sessions = load_persistent_sessions()
    for email in list(all_sessions.keys()):
        stop_bot(email, clear_data=False, stop_reason="Disconnected (Auto-Retry)")

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

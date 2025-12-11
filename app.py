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
# BOT CONSTANT SETTINGS (Setup for R_100 T1 D2=9 & T4 D2=4/5 -> T4 D2=9 | OVER5 & UNDER4 | 2 Ticks)
# ==========================================================
WSS_URL_UNIFIED = "wss://blue.derivws.com/websockets/v3?app_id=16929" 
SYMBOL = "R_100"        
DURATION = 2            
DURATION_UNIT = "t"     
# 🛑 تحديث: خطوة مضاعفة واحدة فقط (صفقة أساسية + صفقة مضاعفة)
MARTINGALE_STEPS = 1    
# 🛑 تحديث: يتوقف بعد خسارتين متتاليتين
MAX_CONSECUTIVE_LOSSES = 2 
RECONNECT_DELAY = 1      
USER_IDS_FILE = "user_ids.txt"
ACTIVE_SESSIONS_FILE = "active_sessions.json" 
# 🌟 تحليل 4 تيكات
TICK_HISTORY_SIZE = 4   
MARTINGALE_MULTIPLIER = 6.0 
CANDLE_TICK_SIZE = 0   
SYNC_SECONDS = [] 
TRADE_CONFIGS = [
    {"type": "DIGITOVER", "target_digit": 5, "label": "OVER_5"},
    {"type": "DIGITUNDER", "target_digit": 4, "label": "UNDER_4"}
]

# ==========================================================
# BOT RUNTIME STATE 
# ==========================================================
flask_local_processes = {}
manager = multiprocessing.Manager() 

active_ws = {} 
is_contract_open = manager.dict() 

TRADE_STATE_DEFAULT = TRADE_CONFIGS 

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
    "open_contract_ids": [], 
    "account_type": "demo", 
    "currency": "USD",
    "pending_martingale": False, 
    "martingale_stake": 0.0,     
    "martingale_config": TRADE_CONFIGS, 
    "display_t1_price": 0.0, 
    "display_t4_price": 0.0, 
    "last_entry_d2": None, 
    "current_total_stake": 0.0, 
    # 💡 حقول التأخير المتدحرج
    "pending_delayed_entry": False, 
    "entry_t1_d2": None, 
}

# (.... Persistent State Management Functions - No change ....)
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
# TRADING BOT FUNCTIONS 
# ==========================================================

def calculate_martingale_stake(base_stake, current_step):
    """
    يحسب قيمة الرهان للمضاعفة (تراكمية بعامل 6.0).
    """
    if current_step == 0:
        return base_stake
    
    # يتم تطبيق المضاعف بشكل أُسي لخطوات المضاعفة (الحد الأقصى 1)
    if current_step <= MARTINGALE_STEPS: 
        return base_stake * (MARTINGALE_MULTIPLIER ** current_step) 
    
    # إذا تجاوزنا خطوة المضاعفة، نعود للرهان الأساسي
    else:
        return base_stake


def send_trade_orders(email, base_stake, trade_configs, currency_code, is_martingale=False):
    """
    يرسل أوامر شراء متعددة (صفقتين) في نفس اللحظة.
    """
    global is_contract_open 
    if email not in active_ws or active_ws[email] is None: return
    ws_app = active_ws[email]
    
    current_data = get_session_data(email)
    
    # الرهان الجديد بناءً على الخطوة الحالية
    stake_per_contract = calculate_martingale_stake(base_stake, current_data['current_step'])
    rounded_stake = round(stake_per_contract, 2)
    
    current_data['current_stake'] = rounded_stake 
    current_data['current_total_stake'] = rounded_stake * len(trade_configs) 
    current_data['last_entry_price'] = current_data['last_tick_data']['price'] if current_data.get('last_tick_data') else 0.0
    current_data['last_entry_time'] = time.time() * 1000
    
    # يتم تتبع D2 للتيك الأحدث (T4)
    entry_digits = get_target_digits(current_data['last_entry_price'])
    current_data['last_entry_d2'] = entry_digits[1] if len(entry_digits) > 1 else 'N/A'
    
    current_data['open_contract_ids'] = [] 
    
    entry_msg = f"MARTINGALE STEP {current_data['current_step']}" if is_martingale else "BASE SIGNAL"
    
    # عرض D2 لـ T1 (من سجل الانتظار) و T4 (الدخول)
    t1_d2_entry = current_data['entry_t1_d2'] if current_data['entry_t1_d2'] is not None else 'N/A'
    t4_d2_entry = current_data['last_entry_d2']
    
    print(f"\n💰 [TRADE START] T1 D2: {t1_d2_entry} | T4 D2: {t4_d2_entry} | Total Stake: {current_data['current_total_stake']:.2f} ({entry_msg})")


    # إرسال الطلبين
    for config in trade_configs:
        contract_type = config['type']
        target_digit = config['target_digit']
        label = config['label']
        
        trade_request = {
            "buy": 1, 
            "price": rounded_stake, 
            "parameters": {
                "amount": rounded_stake, 
                "basis": "stake",
                "currency": currency_code, 
                "duration": DURATION,  
                "duration_unit": DURATION_UNIT, 
                "symbol": SYMBOL, 
                "contract_type": contract_type,
                "barrier": str(target_digit) 
            }
        }
        
        try:
            ws_app.send(json.dumps(trade_request))
            print(f"   [-- {label}] Sent {contract_type} (Barrier: {target_digit}) @ {rounded_stake:.2f} {currency_code}")
        except Exception as e:
            print(f"❌ [TRADE ERROR] Could not send trade order for {label}: {e}")
            pass
            
    is_contract_open[email] = True 
    if is_martingale:
         current_data['pending_martingale'] = False
         
    save_session_data(email, current_data)


def check_pnl_limits(email, contract_results): 
    """
    تتحقق من نتائج الصفقتين المزدوجتين وتطبق منطق المضاعفة/التوقف.
    """
    global is_contract_open 

    current_data = get_session_data(email)
    if not current_data.get('is_running'): 
        is_contract_open[email] = False
        return

    total_profit_loss = sum(result['profit'] for result in contract_results)
    overall_loss = total_profit_loss < 0
    last_total_stake = current_data['current_total_stake'] 
    current_data['current_profit'] += total_profit_loss
    
    stop_triggered = False

    if not overall_loss:
        # 🟢 حالة الربح الإجمالي (أو التعادل)
        current_data['total_wins'] += 1 
        current_data['current_step'] = 0 
        current_data['consecutive_losses'] = 0
        current_data['current_stake'] = current_data['base_stake']
        current_data['pending_martingale'] = False 
        current_data['martingale_config'] = TRADE_CONFIGS 
        current_data['current_total_stake'] = current_data['base_stake'] * len(TRADE_CONFIGS) 
        
        if current_data['current_profit'] >= current_data['tp_target']:
            stop_triggered = "TP Reached"
            
    else:
        # 🔴 حالة الخسارة الإجمالية (MARTINGALE/STOP)
        current_data['total_losses'] += 1
        current_data['consecutive_losses'] += 1
        current_data['current_step'] += 1
        
        # التحقق من MARTINGALE_STEPS (1 خطوة)
        if current_data['current_step'] <= MARTINGALE_STEPS:
            # تفعيل وضع انتظار المضاعفة 
            new_stake = calculate_martingale_stake(current_data['base_stake'], current_data['current_step'])
            
            current_data['current_stake'] = new_stake
            current_data['pending_martingale'] = True 
            current_data['martingale_stake'] = new_stake
            current_data['current_total_stake'] = new_stake * len(TRADE_CONFIGS)
            current_data['martingale_config'] = TRADE_CONFIGS 
            
            # إعادة تفعيل وضع الانتظار المتدحرج للبحث عن الإشارة
            current_data['pending_delayed_entry'] = False 
            current_data['entry_t1_d2'] = None
            current_data['tick_history'] = [] 
            
            print(f"🚨 [MARTINGALE DELAYED] Overall Loss Detected. Pending Step {current_data['current_step']} @ Stake per contract: {new_stake:.2f} (Total: {current_data['current_total_stake']:.2f}). Waiting for T1 D2=9 & T4 D2=4/5 signal...")

        else:
            # تجاوز عدد خطوات المضاعفة (العودة إلى الرهان الأساسي للدخول التالي)
            current_data['current_stake'] = current_data['base_stake']
            current_data['pending_martingale'] = False
            current_data['current_total_stake'] = current_data['base_stake'] * len(TRADE_CONFIGS)
            current_data['current_step'] = 0
            current_data['consecutive_losses'] = 0

        
        # التحقق من MAX_CONSECUTIVE_LOSSES (2 خسارة)
        if current_data['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES: 
            stop_triggered = f"SL Reached ({MAX_CONSECUTIVE_LOSSES} Consecutive Losses)"
            
    current_data['tick_history'] = [] # مسح السجل لبدء سلسلة تحليل جديدة 
        
    save_session_data(email, current_data) 
    
    print(f"[LOG {email}] PNL: {current_data['current_profit']:.2f}, Last Total PL: {total_profit_loss:.2f}, Step: {current_data['current_step']}, Last Total Stake: {last_total_stake:.2f}")

    if stop_triggered:
        stop_bot(email, clear_data=True, stop_reason=stop_triggered) 
        is_contract_open[email] = False 
        return 
        
    if current_data['current_step'] == 0 or current_data['pending_martingale']:
        is_contract_open[email] = False 

# ==========================================================
# UTILITY FUNCTIONS FOR PRICE MOVEMENT ANALYSIS 
# ==========================================================

def get_target_digits(price):
    """
    يستخرج الأرقام العشرية من سعر التيك. (نحن مهتمون بالرقم الثاني D2)
    """
    try:
        # تنسيق السعر لضمان 3 خانات عشرية على الأقل لـ R_100
        formatted_price = "{:.3f}".format(float(price)) 
        
        if '.' in formatted_price:
            parts = formatted_price.split('.')
            decimal_part = parts[1] # الجزء العشري
            
            # تحويل كل حرف إلى رقم
            digits = [int(d) for d in decimal_part if d.isdigit()]
            return digits
        
        return [0] 
        
    except Exception as e:
        print(f"Error calculating target digits: {e}")
        return [0] 

# 💡 الدالة المعدلة: T1 D2=9 و T4 D2=4 أو 5
def get_initial_signal_check(tick_history):
    """
    يتحقق من الإشارة الأولية بناءً على تحليل T1 (أقدم) و T4 (الأحدث).
    الشروط: T1 D2 = 9 و T4 D2 = 4 أو 5.
    (هذه الإشارة تضع البوت في وضع الانتظار)
    """
    if len(tick_history) != 4:
        return False
    
    # تحديد التيكات (T1=index 0, T4=index 3)
    tick_T1_price = tick_history[0]['price'] # التيك الأقدم 
    tick_T4_price = tick_history[3]['price'] # التيك الأحدث 
    
    # استخلاص الأرقام العشرية
    digits_T1 = get_target_digits(tick_T1_price)
    digits_T4 = get_target_digits(tick_T4_price)
    
    # التحقق من وجود D2
    if len(digits_T1) < 2 or len(digits_T4) < 2:
        return False
        
    # D2 هو العنصر رقم [1]
    digit_T1_D2 = digits_T1[1] 
    digit_T4_D2 = digits_T4[1] 
    
    # 2. شرط T1: الرقم الثاني يجب أن يكون 9
    condition_T1_is_9 = (digit_T1_D2 == 9)
    
    # 3. شرط T4: الرقم الثاني يجب أن يكون 4 أو 5
    condition_T4_is_4_or_5 = (digit_T4_D2 == 4 or digit_T4_D2 == 5)
    
    if condition_T1_is_9 and condition_T4_is_4_or_5:
        return digit_T1_D2 # نرجع قيمة 9 لتخزينها
    else:
        return False

# ==========================================================
# CORE BOT LOGIC 
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
        if data_from_file.get('open_contract_ids'):
            is_contract_open[email] = True
        else:
            is_contract_open[email] = False


    session_data = get_session_data(email)
    session_data.update({
        "api_token": token, "base_stake": stake, "tp_target": tp,
        "is_running": True, "current_stake": session_data.get("current_stake", stake), # Use existing stake if running
        "current_trade_state": TRADE_STATE_DEFAULT,
        "stop_reason": "Running",
        "last_entry_time": 0,
        "last_entry_price": 0.0,
        "last_tick_data": None,
        "tick_history": session_data.get("tick_history", []), # Keep history if available
        "open_contract_ids": session_data.get("open_contract_ids", []), # Keep open contracts if available
        "account_type": account_type,
        "currency": currency_code,
        "current_profit": session_data.get("current_profit", 0.0),
        "current_step": session_data.get("current_step", 0),
        "consecutive_losses": session_data.get("consecutive_losses", 0),
        "total_wins": session_data.get("total_wins", 0),
        "total_losses": session_data.get("total_losses", 0),
        "pending_martingale": session_data.get("pending_martingale", False), 
        "martingale_stake": session_data.get("martingale_stake", 0.0), 
        "martingale_config": TRADE_CONFIGS, 
        "display_t1_price": 0.0, 
        "display_t4_price": 0.0, 
        "last_entry_d2": None,
        "current_total_stake": session_data.get("current_total_stake", stake * len(TRADE_CONFIGS)),
        # 💡 حقول التأخير المتدحرج
        "pending_delayed_entry": session_data.get("pending_delayed_entry", False),
        "entry_t1_d2": session_data.get("entry_t1_d2", None),
    })
    
    if session_data['current_step'] > 0 and not session_data['pending_martingale']: 
        session_data['pending_martingale'] = True 
        session_data['current_stake'] = calculate_martingale_stake(session_data['base_stake'], session_data['current_step'])
        session_data['martingale_stake'] = session_data['current_stake']
        session_data['current_total_stake'] = session_data['martingale_stake'] * len(TRADE_CONFIGS)
    
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
            
            contract_ids = running_data.get('open_contract_ids', [])
            if contract_ids:
                for contract_id in contract_ids:
                    ws_app.send(json.dumps({
                        "proposal_open_contract": 1, 
                        "contract_id": contract_id, 
                        "subscribe": 1
                    }))
                is_contract_open[email] = True 
                print(f"🔄 [RECOVERY] Attempting to follow {len(contract_ids)} lost contracts.")
            else:
                is_contract_open[email] = False

            running_data['is_running'] = True
            save_session_data(email, running_data)
            print(f"✅ [PROCESS] Connection established for {email}.")
            
        
        def execute_multi_trade(email, current_data, is_martingale=False):
            base_stake_to_use = current_data['base_stake']
            
            currency_code = current_data['currency']
            trade_configs_to_use = TRADE_CONFIGS
            
            send_trade_orders(email, base_stake_to_use, trade_configs_to_use, currency_code, is_martingale=is_martingale)
            

        contract_results_map = {} 

        def on_message_wrapper(ws_app, message):
            nonlocal contract_results_map
            data = json.loads(message)
            msg_type = data.get('msg_type')
            
            current_data = get_session_data(email) 
            
            if not current_data.get('is_running'):
                ws_app.close() 
                return
                
            if msg_type == 'tick':
                current_timestamp = int(data['tick']['epoch'])
                current_price = float(data['tick']['quote'])
                
                tick_data = {
                    "price": current_price,
                    "timestamp": current_timestamp
                }
                current_data['last_tick_data'] = tick_data
                
                # تحديث سجل التيكات
                current_data['tick_history'].append(tick_data)
                
                # نحتفظ بـ 4 تيكات فقط
                if len(current_data['tick_history']) > TICK_HISTORY_SIZE: 
                    # 💡 لا نقص السجل هنا، بل ندحرج التيكات في منطق الدخول أدناه
                    pass 
                
                # 🌟 تحديث قيم T1 و T4 للعرض
                if len(current_data['tick_history']) >= TICK_HISTORY_SIZE:
                    current_data['display_t1_price'] = current_data['tick_history'][0]['price'] # T1 هو الأقدم (index 0)
                    current_data['display_t4_price'] = current_data['tick_history'][-1]['price'] # التيك الأحدث (T4/T5/T6...)
                else:
                    current_data['display_t1_price'] = 0.0 
                    current_data['display_t4_price'] = current_price 
                
                # منطق الدخول (سواء كان أساسي أو مضاعف)
                if is_contract_open.get(email) is False:
                    
                    current_time_ms = time.time() * 1000
                    time_since_last_entry_ms = current_time_ms - current_data['last_entry_time']
                    is_time_gap_respected = time_since_last_entry_ms > 100 
                    
                    if not is_time_gap_respected:
                        # ⚠️ إذا كان هناك تيك جديد وصل بسرعة، نتجاهله لتجنب التكرار
                        if len(current_data['tick_history']) > TICK_HISTORY_SIZE:
                             current_data['tick_history'] = current_data['tick_history'][:-1]
                        save_session_data(email, current_data) 
                        return
                    
                    # 1. إذا كان حجم السجل 4 أو أكثر
                    if len(current_data['tick_history']) >= TICK_HISTORY_SIZE:
                        
                        # التيك الأحدث هو التيك الذي سيتم فحصه (سواء كان T4 أو T5 أو T6...)
                        tick_T_latest_price = current_data['tick_history'][-1]['price'] 
                        digits_T_latest = get_target_digits(tick_T_latest_price)
                        digit_T_latest_D2 = digits_T_latest[1] if len(digits_T_latest) >= 2 else 'N/A'
                        
                        # A) في حالة الانتظار (نحن ننتظر T D2=9)
                        if current_data['pending_delayed_entry']:
                            
                            is_entry_condition_met = (digit_T_latest_D2 == 9)
                            
                            if is_entry_condition_met:
                                # 🚀 شروط الدخول تحققت
                                print(f"🚀 [DELAYED ENTRY MET] T D2=9 met. Executing trade (Step: {current_data['current_step']}).")
                                
                                is_martingale = current_data['current_step'] > 0
                                execute_multi_trade(email, current_data, is_martingale=is_martingale)
                                
                                # إعادة التعيين والتحرك للخطوة التالية
                                current_data['pending_delayed_entry'] = False 
                                current_data['entry_t1_d2'] = None
                                current_data['tick_history'] = [] # مسح السجل بعد الدخول
                                
                            else:
                                # لم يتحقق شرط T D2=9: ندحرج السجل 
                                current_data['tick_history'].pop(0) 

                        # B) في حالة البحث عن الإشارة الأولية (الإشارة التي تضعنا في حالة الانتظار)
                        elif len(current_data['tick_history']) == TICK_HISTORY_SIZE:
                            
                            # نتحقق من T1 D2=9 و T4 D2=4/5 فقط عندما يكون حجم السجل 4 بالضبط.
                            initial_t1_d2 = get_initial_signal_check(current_data['tick_history'])
                            
                            if initial_t1_d2 is not False:
                                # 🔔 الإشارة الأولية تحققت (T1 D2=9, T4 D2=4/5)، نبدأ الانتظار
                                current_data['pending_delayed_entry'] = True
                                current_data['entry_t1_d2'] = initial_t1_d2 # لحفظ T1 D2=9
                                
                                print(f"🔔 INITIAL SIGNAL FOUND: T1 D2=9 & T4 D2={digit_T_latest_D2}. Starting rolling delay wait for T D2=9...")
                                
                            # ندحرج السجل استعداداً للتيك التالي (سواء وجدنا إشارة أولية أم لا)
                            current_data['tick_history'].pop(0) 
                
                save_session_data(email, current_data) 
                                
            elif msg_type == 'buy':
                contract_id = str(data['buy']['contract_id'])
                
                current_data = get_session_data(email)
                current_data['open_contract_ids'].append(contract_id) 
                
                # نرسل طلب متابعة العقد فوراً
                ws_app.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
                
                save_session_data(email, current_data)
                
            elif msg_type == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                contract_id = str(contract.get('contract_id'))
                
                if contract.get('is_sold') == 1:
                    # العقد اكتمل
                    profit_loss = contract.get('profit', 0.0)
                    contract_type = contract.get('contract_type')
                    
                    # 🌟 تخزين النتيجة
                    contract_results_map[contract_id] = {
                        "profit": profit_loss, 
                        "type": contract_type
                    }
                    
                    # حذف العقد المكتمل من القائمة المفتوحة
                    current_data = get_session_data(email)
                    if contract_id in current_data['open_contract_ids']:
                        current_data['open_contract_ids'].remove(contract_id)
                        save_session_data(email, current_data)
                    
                    if 'subscription_id' in data: 
                        ws_app.send(json.dumps({"forget": data['subscription_id']}))

                    # 🌟 التحقق إذا اكتملت جميع العقود
                    if not current_data['open_contract_ids']:
                        
                        results_list = list(contract_results_map.values())
                        
                        # تطبيق منطق المضاعفة/التوقف
                        check_pnl_limits(email, results_list)
                        
                        # مسح الخريطة بعد معالجة الدورة
                        contract_results_map = {} 
                        is_contract_open[email] = False

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
# FLASK APP SETUP AND ROUTES 
# ==========================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET_KEY', 'VERY_STRONG_SECRET_KEY_RENDER_BOT')
app.config['SESSION_PERMANENT'] = False 

# (.... LOGIN_FORM - No change ....)
LOGIN_FORM = """
<!doctype html>
<title>Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    body { font-family: Arial, sans-serif; padding: 20px; max-width: 400px; margin: auto; }
    h1 { color: #007bff; }
    input[type="email"], input[type="submit"] {
        width: 100%;
        padding: 10px;
        margin-top: 5px;
        margin-bottom: 10px;
        border: 1px solid #ccc;
        border-radius: 4px;
        box-sizing: border-box;
    }
    input[type="submit"] {
        background-color: #007bff;
        color: white;
        cursor: pointer;
        font-size: 1.1em;
    }
    .note { margin-top: 15px; padding: 10px; background-color: #f8f9fa; border-radius: 4px; }
</style>
<h1>Bot Login</h1>

{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for category, message in messages %}
            <p style="color:{{ 'green' if category == 'success' else ('blue' if category == 'info' else 'red') }};">{{ message }}</p>
        {% endfor %}
    {% endif %}
{% endwith %}

<form method="POST" action="{{ url_for('login_route') }}">
    <label for="email">Email Address:</label><br>
    <input type="email" id="email" name="email" required><br>
    <input type="submit" value="Login">
</form>
<div class="note">
    <p>💡 Note: This is a placeholder login. Only users listed in <code>user_ids.txt</code> can log in.</p>
</div>
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
    .data-box {
        background-color: #f8f9fa;
        border: 1px solid #e9ecef;
        padding: 15px;
        border-radius: 5px;
        margin-bottom: 15px;
    }
    .tick-box {
        display: flex;
        justify-content: space-around;
        padding: 10px;
        background-color: #e9f7ff;
        border: 1px solid #007bff;
        border-radius: 4px;
        margin-bottom: 10px;
        font-weight: bold;
        font-size: 1.1em;
    }
    .current-digit {
        color: #ff5733; 
        font-size: 1.2em;
    }
    .info-label {
        font-weight: normal;
        color: #555;
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
    {% set strategy = '4-Tick Rolling Delay: (T1 D2=9 & T4 D2=4/5) -> WAIT FOR T D2=9 | OVER 5 & UNDER 4 | Martingale: DELAYED (Steps=' + max_martingale_step|string + ', Multiplier=' + martingale_multiplier|string + ')' %}
    
    <p class="status-running">✅ Bot is Running! (Auto-refreshing)</p>
    
    {# 🌟 Display T1 D2 and T4 D2 #}
    <div class="tick-box">
        <div>
            <span class="info-label">T1 Price:</span> <b>{% if session_data.display_t1_price %}{{ "%0.3f"|format(session_data.display_t1_price) }}{% else %}N/A{% endif %}</b>
            <br>
            <span class="info-label">T1 D2:</span> 
            <b class="current-digit">
            {% set price_str = "%0.3f"|format(session_data.display_t1_price) %}
            {% set price_parts = price_str.split('.') %} 
            {% if price_parts|length > 1 and price_parts[-1]|length >= 2 %}
                {{ price_parts[-1][1] }}
            {% else %}
                N/A
            {% endif %}
            </b>
        </div>
        <div>
            <span class="info-label">Current Price:</span> <b>{% if session_data.display_t4_price %}{{ "%0.3f"|format(session_data.display_t4_price) }}{% else %}N/A{% endif %}</b>
            <br>
            <span class="info-label">Current D2:</span>
            <b class="current-digit">
            {% set price_str = "%0.3f"|format(session_data.display_t4_price) %}
            {% set price_parts = price_str.split('.') %} 
            {% if price_parts|length > 1 and price_parts[-1]|length >= 2 %}
                {{ price_parts[-1][1] }}
            {% else %}
                N/A
            {% endif %}
            </b>
        </div>
    </div>
    
    <div class="data-box">
        <p>Asset: <b>{{ SYMBOL }}</b> | Account: <b>{{ session_data.account_type.upper() }}</b></p>
        <p>Net Profit: <b>{{ session_data.currency }} {{ session_data.current_profit|round(2) }}</b></p>
        
        <p style="font-weight: bold; color: {% if session_data.open_contract_ids %}#007bff{% else %}#555{% endif %};">
            Open Contracts Status: 
            <b>{% if session_data.open_contract_ids %}{{ session_data.open_contract_ids|length }}{% else %}0{% endif %} (Over 5 & Under 4)</b>
        </p>
        
        <p style="font-weight: bold; color: {% if session_data.pending_delayed_entry %}#ff9900{% elif session_data.current_step > 0 %}#ff5733{% else %}#555{% endif %};">
            Delay/Martingale Status: 
            <b>
                {% if session_data.pending_delayed_entry %}
                    DELAYED ENTRY ACTIVE (T1 D2={{ session_data.entry_t1_d2 if session_data.entry_t1_d2 is not none else 'N/A' }}). Waiting for Current D2=9.
                    {% if session_data.current_step > 0 %}
                    (MARTINGALE STEP {{ session_data.current_step }})
                    {% endif %}
                {% elif session_data.current_step > 0 %}
                    MARTINGALE STEP {{ session_data.current_step }} @ Stake/Contract: {{ session_data.current_stake|round(2) }} (Total: {{ session_data.current_total_stake|round(2) }}) (Searching for Signal)
                {% else %}
                    BASE STAKE @ Stake/Contract: {{ session_data.base_stake|round(2) }} (Total: {{ session_data.current_total_stake|round(2) }}) (Searching for Signal)
                {% endif %}
            </b>
        </p>

        <p>Current Stake per Contract: <b>{{ session_data.currency }} {{ session_data.current_stake|round(2) }}</b></p>
        <p style="font-weight: bold; color: {% if session_data.consecutive_losses > 0 %}red{% else %}green{% endif %};">
        Consecutive Losses: <b>{{ session_data.consecutive_losses }}</b> / {{ max_consecutive_losses }} 
        (Last Entry D2: <b>{{ session_data.last_entry_d2 if session_data.last_entry_d2 is not none else 'N/A' }}</b>)
        </p>
        <p style="font-weight: bold; color: green;">Total Wins: {{ session_data.total_wins }} | Total Losses: {{ session_data.total_losses }}</p>
        <p style="font-weight: bold; color: #007bff;">Current Strategy: {{ strategy }}</p>
        {% if session_data.open_contract_ids %}
            <p style="font-weight: bold; color: blue;">⚠️ {{ session_data.open_contract_ids|length }} Contracts Open (Recovery Active)</p>
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

        <label for="account_type">Account Type ({{ SYMBOL }}):</label><br>
        <select id="account_type" name="account_type" required>
            <option value="demo" {% if session_data.account_type == 'demo' %}selected{% endif %}>Demo (USD)</option>
            <option value="live" {% if session_data.account_type == 'live' %}selected{% endif %}>Live (tUSDT)</option>
        </select><br>

        <label for="token">Deriv API Token:</label><br>
        <input type="text" id="token" name="token" required value="{{ session_data.api_token if session_data else '' }}"><br>
        
        <label for="stake">Base Stake PER CONTRACT (USD/tUSDT):</label><br>
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
    var TICK_HISTORY_SIZE = {{ TICK_HISTORY_SIZE }}; 
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

@app.route('/', methods=['GET', 'POST'])
def login_route():
    if 'email' in session:
        return redirect(url_for('control_panel'))
        
    if request.method == 'POST':
        email = request.form.get('email', '').lower()
        allowed_users = load_allowed_users()
        
        if email in allowed_users:
            session['email'] = email
            flash('Login successful!', 'success')
            return redirect(url_for('control_panel'))
        else:
            flash('Invalid user or email address not authorized.', 'error')
            return redirect(url_for('login_route'))

    return render_template_string(LOGIN_FORM)

@app.route('/control')
def control_panel():
    if 'email' not in session:
        return redirect(url_for('login_route'))
        
    email = session['email']
    session_data = get_session_data(email)
    
    is_proc_running = email in flask_local_processes and flask_local_processes[email].is_alive()
    
    if session_data.get('is_running') and not is_proc_running:
        print(f"🔄 [RECOVERY] Process {email} found dead, resetting state to stopped.")
        session_data['is_running'] = False
        session_data['stop_reason'] = "Process Crashed or Terminated"
        save_session_data(email, session_data)

    context = {
        'email': email,
        'session_data': session_data,
        'SYMBOL': SYMBOL,
        'DURATION': DURATION,
        'TICK_HISTORY_SIZE': TICK_HISTORY_SIZE,
        'martingale_multiplier': MARTINGALE_MULTIPLIER,
        'max_consecutive_losses': MAX_CONSECUTIVE_LOSSES,
        'max_martingale_step': MARTINGALE_STEPS,
    }
    
    return render_template_string(CONTROL_FORM, **context)

@app.route('/start', methods=['POST'])
def start_bot():
    if 'email' not in session:
        return redirect(url_for('login_route'))
    
    email = session['email']
    
    # إيقاف العملية السابقة قبل البدء لضمان التنظيف
    stop_bot(email, clear_data=False, stop_reason="Restarting...") 
    
    token = request.form.get('token').strip()
    try:
        stake = float(request.form.get('stake'))
        tp = float(request.form.get('tp'))
    except (TypeError, ValueError):
        flash('Invalid Stake or TP value.', 'error')
        return redirect(url_for('control_panel'))
        
    account_type = request.form.get('account_type', 'demo')
    
    # تحديد العملة بناءً على نوع الحساب
    currency_code = "USD" if account_type == 'demo' else "tUSDT" 

    # بدء عملية البوت
    proc = multiprocessing.Process(
        target=bot_core_logic, 
        args=(email, token, stake, tp, account_type, currency_code)
    )
    proc.start()
    
    flask_local_processes[email] = proc
    
    # تحديث البيانات المخزنة فوراً
    data = get_session_data(email)
    data.update({
        "is_running": True, 
        "api_token": token,
        "base_stake": stake, 
        "tp_target": tp,
        "account_type": account_type,
        "currency": currency_code,
        "current_stake": stake,
        "stop_reason": "Running"
    })
    
    # إعادة تعيين إحصائيات التداول عند بدء جلسة جديدة
    data["current_profit"] = 0.0
    data["consecutive_losses"] = 0
    data["current_step"] = 0
    data["total_wins"] = 0
    data["total_losses"] = 0
    data["pending_martingale"] = False
    data["pending_delayed_entry"] = False # إعادة تعيين حالة التأخير
    
    save_session_data(email, data)

    flash('🚀 Bot started successfully!', 'success')
    return redirect(url_for('control_panel'))

@app.route('/stop', methods=['POST'])
def stop_route():
    if 'email' not in session:
        return redirect(url_for('login_route'))
        
    email = session['email']
    force_stop = 'force_stop' in request.form
    
    stop_bot(email, clear_data=force_stop, stop_reason="Stopped Manually")
    
    if force_stop:
        flash('🛑 Bot forcefully stopped and session data cleared!', 'info')
    else:
        flash('🛑 Bot stopped successfully.', 'info')
        
    return redirect(url_for('control_panel'))

@app.route('/logout')
def logout():
    if 'email' in session:
        email = session['email']
        # إيقاف العملية دون مسح البيانات
        stop_bot(email, clear_data=False, stop_reason="Logged Out") 
        session.pop('email', None)
        flash('👋 You have been logged out.', 'info')
        
    return redirect(url_for('login_route'))

if __name__ == '__main__':
    print("--- Starting Deriv Digit Bot Microservice ---")
    load_allowed_users() 
    all_sessions = load_persistent_sessions()
    print(f"📝 Loaded {len(all_sessions)} persistent sessions.")

    # تعطيل use_reloader لتجنب تشغيل العمليات مرتين عند وضع debug=False
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

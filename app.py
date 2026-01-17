import websocket, json, time, multiprocessing, os
from flask import Flask, render_template_string, request
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURATION ---
TOKEN = "8264292822:AAHAn5uB-K2Oxjm1HQHZ2NwZvXLDE7SVFm0"
MONGO_URI = "mongodb+srv://charbelnk111_db_user:Mano123mano@cluster0.2gzqkc8.mongodb.net/?appName=Cluster0"

bot = telebot.TeleBot(TOKEN)
db_client = MongoClient(MONGO_URI)
db = db_client['trading_bot']
sessions_col = db['active_sessions'] 

manager = multiprocessing.Manager()

def get_initial_state():
    return {
        "email": "", "api_token": "", "initial_stake": 0.0, "current_stake": 0.0, "tp": 0.0, 
        "currency": "USD", "is_running": False, "chat_id": None,
        "total_profit": 0.0, "win_count": 0, "loss_count": 0, "is_trading": False,
        "consecutive_losses": 0, "active_contract": None, "start_time": 0
    }

state = manager.dict(get_initial_state())

# --- AUTHORIZATION LOGIC ---
def is_authorized(email):
    email = email.strip().lower()
    if not os.path.exists("user_ids.txt"): 
        with open("user_ids.txt", "w") as f: f.write("")
    with open("user_ids.txt", "r") as f:
        auth_emails = [line.strip().lower() for line in f.readlines()]
    if email not in auth_emails: return False
    
    user_data = sessions_col.find_one({"email": email})
    if user_data and "expiry_date" in user_data:
        try:
            expiry_time = datetime.strptime(user_data["expiry_date"], "%Y-%m-%d %H:%M")
            return datetime.now() <= expiry_time
        except: return False
    return False

# --- DATA RESET & WIPE ---
def reset_and_stop(state_proxy, text):
    if state_proxy["chat_id"]:
        try:
            report = (f"🛑 **SESSION TERMINATED**\n"
                      f"━━━━━━━━━━━━━━\n"
                      f"✅ Wins: `{state_proxy['win_count']}` | ❌ Losses: `{state_proxy['loss_count']}`\n"
                      f"💰 Profit: **{state_proxy['total_profit']:.2f}**\n"
                      f"📝 Reason: {text}\n"
                      f"━━━━━━━━━━━━━━")
            bot.send_message(state_proxy["chat_id"], report, parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())
        except: pass
    initial = get_initial_state()
    for k, v in initial.items(): state_proxy[k] = v

# --- RESULT CHECKER (18 SEC WAIT) ---
def check_result(state_proxy):
    if not state_proxy["active_contract"] or time.time() - state_proxy["start_time"] < 18:
        return
    try:
        ws_temp = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929", timeout=10)
        ws_temp.send(json.dumps({"authorize": state_proxy["api_token"]}))
        ws_temp.recv()
        ws_temp.send(json.dumps({"proposal_open_contract": 1, "contract_id": state_proxy["active_contract"]}))
        res = json.loads(ws_temp.recv())
        ws_temp.close()
        
        contract = res.get("proposal_open_contract", {})
        if contract.get("is_expired") == 1:
            profit = float(contract.get("profit", 0))
            if profit > 0:
                state_proxy["win_count"] += 1
                state_proxy["consecutive_losses"] = 0
                state_proxy["current_stake"] = state_proxy["initial_stake"]
                icon = "✅ WIN"
            else:
                state_proxy["loss_count"] += 1
                state_proxy["consecutive_losses"] += 1
                state_proxy["current_stake"] *= 19 # Martingale x19
                icon = "❌ LOSS"
            
            state_proxy["total_profit"] += profit
            state_proxy["active_contract"] = None 
            state_proxy["is_trading"] = False

            stats_msg = (f"{icon} (**{profit:.2f}**)\n"
                         f"━━━━━━━━━━━━━━\n"
                         f"✅ Wins: `{state_proxy['win_count']}` | ❌ Losses: `{state_proxy['loss_count']}`\n"
                         f"🔄 Consecutive: `{state_proxy['consecutive_losses']}/2`\n"
                         f"💰 Net Profit: **{state_proxy['total_profit']:.2f}**")
            bot.send_message(state_proxy["chat_id"], stats_msg, parse_mode="Markdown")

            if state_proxy["consecutive_losses"] >= 2:
                reset_and_stop(state_proxy, "Stop Loss: 2 Consecutive Losses.")
            elif state_proxy["total_profit"] >= state_proxy["tp"]:
                reset_and_stop(state_proxy, "Target Profit Reached!")
    except: pass

# --- TRADING ENGINE (ENGULFING LOGIC) ---
def main_loop(state_proxy):
    ws_persistent = None
    while True:
        try:
            if state_proxy["is_running"] and not state_proxy["is_trading"]:
                if ws_persistent is None:
                    ws_persistent = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929", timeout=10)
                    ws_persistent.send(json.dumps({"authorize": state_proxy["api_token"]}))
                    ws_persistent.recv()
                
                ws_persistent.send(json.dumps({"ticks_history": "R_100", "count": 3, "end": "latest", "style": "ticks"}))
                prices = json.loads(ws_persistent.recv()).get("history", {}).get("prices", [])
                
                if len(prices) >= 3:
                    t1, t2, t3 = prices[-3], prices[-2], prices[-1]
                    contract_type = None
                    barrier = ""

                    if t2 < t1 and t3 > t1:
                        contract_type = "CALL"
                        barrier = "-0.8"
                    elif t2 > t1 and t3 < t1:
                        contract_type = "PUT"
                        barrier = "+0.8"

                    if contract_type:
                        if not is_authorized(state_proxy["email"]):
                            reset_and_stop(state_proxy, "Unauthorized.")
                            continue
                        
                        req = {"proposal": 1, "amount": state_proxy["current_stake"], "basis": "stake", 
                               "contract_type": contract_type, "barrier": barrier, "currency": state_proxy["currency"], 
                               "duration": 5, "duration_unit": "t", "symbol": "R_100"}
                        ws_persistent.send(json.dumps(req))
                        res_p = json.loads(ws_persistent.recv()).get("proposal")
                        if res_p:
                            ws_persistent.send(json.dumps({"buy": res_p["id"], "price": state_proxy["current_stake"]}))
                            res_b = json.loads(ws_persistent.recv())
                            if "buy" in res_b:
                                state_proxy["active_contract"] = res_b["buy"]["contract_id"]
                                state_proxy["start_time"] = time.time()
                                state_proxy["is_trading"] = True
                                bot.send_message(state_proxy["chat_id"], f"🎯 Signal: {contract_type} {barrier}")
                                ws_persistent.close(); ws_persistent = None
            elif state_proxy["is_trading"]:
                check_result(state_proxy)
            time.sleep(1)
        except:
            if ws_persistent: ws_persistent.close()
            ws_persistent = None; time.sleep(1)

# --- FLASK ADMIN PANEL HTML ---
@app.route('/')
def home():
    emails = []
    if os.path.exists("user_ids.txt"):
        with open("user_ids.txt", "r") as f:
            emails = [line.strip() for line in f.readlines() if line.strip()]
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Admin Panel</title>
        <style>
            body { font-family: Arial; text-align: center; background: #f4f4f4; }
            table { margin: 20px auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
            th, td { padding: 10px; border-bottom: 1px solid #ddd; }
            button { cursor: pointer; background: #28a745; color: white; border: none; padding: 5px 10px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <h2>User Management</h2>
        <table>
            <tr><th>Email</th><th>Set Expiry</th></tr>
            {% for email in emails %}
            <tr>
                <td>{{ email }}</td>
                <td>
                    <form method="POST" action="/update_expiry">
                        <input type="hidden" name="email" value="{{ email }}">
                        <select name="duration">
                            <option value="1">1 Day</option>
                            <option value="30">30 Days</option>
                            <option value="36500">Lifetime</option>
                        </select>
                        <button type="submit">Activate</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, emails=emails)

@app.route('/update_expiry', methods=['POST'])
def update_expiry():
    email = request.form.get('email').lower()
    days = int(request.form.get('duration'))
    expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    sessions_col.update_one({"email": email}, {"$set": {"expiry_date": expiry_date}}, upsert=True)
    return f"Success: {email} activated until {expiry_date} <br><a href='/'>Back</a>"

# --- BOT COMMANDS ---
@bot.message_handler(commands=['start'])
def welcome(m):
    bot.send_message(m.chat.id, "👋 Welcome! Enter your email:")
    bot.register_next_step_handler(m, login)

def login(m):
    email = m.text.strip().lower()
    if is_authorized(email):
        state["email"] = email; state["chat_id"] = m.chat.id
        bot.send_message(m.chat.id, "✅ Logged In!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('Demo 🛠️', 'Live 💰'))
    else: bot.send_message(m.chat.id, "🚫 Not Authorized.")

@bot.message_handler(func=lambda m: m.text in ['Demo 🛠️', 'Live 💰'])
def ask_token(m):
    state["currency"] = "USD" if "Demo" in m.text else "tUSDT"
    bot.send_message(m.chat.id, "Enter API Token:")
    bot.register_next_step_handler(m, save_token)

def save_token(m):
    state["api_token"] = m.text.strip()
    bot.send_message(m.chat.id, "Initial Stake:")
    bot.register_next_step_handler(m, save_stake)

def save_stake(m):
    try:
        val = float(m.text)
        state["initial_stake"] = val; state["current_stake"] = val
        bot.send_message(m.chat.id, "Target Profit:")
        bot.register_next_step_handler(m, save_tp)
    except: pass

def save_tp(m):
    try:
        state["tp"] = float(m.text); state["is_running"] = True
        bot.send_message(m.chat.id, "🚀 Running! x19 Martingale | 18s Wait", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('STOP 🛑'))
    except: pass

@bot.message_handler(func=lambda m: m.text == 'STOP 🛑')
def stop_all(m): reset_and_stop(state, "Manual Stop.")

if __name__ == '__main__':
    multiprocessing.Process(target=main_loop, args=(state,), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    multiprocessing.Process(target=lambda: app.run(host='0.0.0.0', port=port), daemon=True).start()
    bot.infinity_polling()

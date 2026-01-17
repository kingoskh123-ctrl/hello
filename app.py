import websocket, json, time, multiprocessing, os
from flask import Flask, render_template_string, request, redirect
import telebot
from telebot import types
from pymongo import MongoClient
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURATION ---
TOKEN = "8433565422:AAFSk6WKbN9UgZx3-a6brmcM3jlBpsL2HvM"
MONGO_URI = "mongodb+srv://charbelnk111_db_user:Mano123mano@cluster0.2gzqkc8.mongodb.net/?appName=Cluster0"

bot = telebot.TeleBot(TOKEN)
db_client = MongoClient(MONGO_URI)
db = db_client['Trading_System_V2']
users_col = db['Authorized_Users']

manager = multiprocessing.Manager()

def get_initial_state():
    return {
        "email": "", "api_token": "", "initial_stake": 0.0, "current_stake": 0.0, "tp": 0.0, 
        "currency": "USD", "is_running": False, "chat_id": None,
        "total_profit": 0.0, "win_count": 0, "loss_count": 0, "is_trading": False,
        "consecutive_losses": 0, "active_contract": None, "start_time": 0,
        "last_processed_tick": 0.0, "is_live": False
    }

state = manager.dict(get_initial_state())

# --- AUTHORIZATION ---
def is_authorized(email):
    email = email.strip().lower()
    if not os.path.exists("user_ids.txt"): return False
    with open("user_ids.txt", "r") as f:
        auth_emails = [line.strip().lower() for line in f.readlines()]
    if email not in auth_emails: return False
    user_data = users_col.find_one({"email": email})
    if user_data and "expiry_date" in user_data:
        try:
            expiry_time = datetime.strptime(user_data["expiry_date"], "%Y-%m-%d %H:%M")
            return datetime.now() <= expiry_time
        except: return False
    return False

def reset_and_stop(state_proxy, text):
    if state_proxy["chat_id"]:
        try:
            report = (f"🛑 **SESSION TERMINATED**\n"
                      f"━━━━━━━━━━━━━━\n"
                      f"✅ Wins: `{state_proxy['win_count']}` | ❌ Losses: `{state_proxy['loss_count']}`\n"
                      f"💰 Final Profit: **{state_proxy['total_profit']:.2f}**\n"
                      f"📝 Reason: {text}")
            bot.send_message(state_proxy["chat_id"], report, parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())
        except: pass
    initial = get_initial_state()
    for k, v in initial.items(): state_proxy[k] = v

# --- RESULT CHECK (18s) ---
def check_result(state_proxy):
    if not state_proxy["active_contract"] or time.time() - state_proxy["start_time"] < 18:
        return
    try:
        ws_temp = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929", timeout=15)
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
                state_proxy["current_stake"] *= 19 
                icon = "❌ LOSS"
            
            state_proxy["total_profit"] += profit
            state_proxy["active_contract"] = None 
            state_proxy["is_trading"] = False

            stats_msg = (f"{icon} (**{profit:.2f}**)\n"
                         f"━━━━━━━━━━━━━━\n"
                         f"✅ Wins: `{state_proxy['win_count']}` | ❌ Losses: `{state_proxy['loss_count']}`\n"
                         f"🔄 Consecutive Losses: `{state_proxy['consecutive_losses']}/2`\n"
                         f"💰 Net Profit: **{state_proxy['total_profit']:.2f}**")
            bot.send_message(state_proxy["chat_id"], stats_msg, parse_mode="Markdown")

            if state_proxy["consecutive_losses"] >= 2:
                reset_and_stop(state_proxy, "Stopped: 2 Consecutive Losses.")
            elif state_proxy["total_profit"] >= state_proxy["tp"]:
                reset_and_stop(state_proxy, "Target Profit Reached!")
    except: pass

# --- MAIN LOOP ---
def main_loop(state_proxy):
    ws_persistent = None
    while True:
        try:
            if state_proxy["is_running"] and not state_proxy["is_trading"]:
                if ws_persistent is None:
                    ws_persistent = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929", timeout=10)
                    ws_persistent.send(json.dumps({"authorize": state_proxy["api_token"]}))
                    ws_persistent.recv()
                
                ws_persistent.send(json.dumps({"ticks_history": "R_100", "count": 10, "end": "latest", "style": "ticks"}))
                prices = json.loads(ws_persistent.recv()).get("history", {}).get("prices", [])
                
                if len(prices) >= 10:
                    current_close = prices[-1]
                    if current_close == state_proxy["last_processed_tick"]:
                        time.sleep(1); continue

                    c1_diff = prices[4] - prices[0]
                    c2_diff = prices[9] - prices[5]
                    
                    sig = None
                    if c1_diff >= 0.6 and c2_diff >= 0.6: sig = "CALL"
                    elif c1_diff <= -0.6 and c2_diff <= -0.6: sig = "PUT"

                    if sig:
                        state_proxy["last_processed_tick"] = current_close
                        req = {"proposal": 1, "amount": state_proxy["current_stake"], "basis": "stake", 
                               "contract_type": sig, "barrier": "-0.8" if sig=="CALL" else "+0.8", 
                               "currency": state_proxy["currency"], "duration": 5, "duration_unit": "t", "symbol": "R_100"}
                        
                        ws_persistent.send(json.dumps(req))
                        prop_res = json.loads(ws_persistent.recv())
                        res_p = prop_res.get("proposal")
                        
                        if res_p:
                            ws_persistent.send(json.dumps({"buy": res_p["id"], "price": state_proxy["current_stake"]}))
                            buy_data = json.loads(ws_persistent.recv())
                            if "buy" in buy_data:
                                bot.send_message(state_proxy["chat_id"], f"🎯 **Signal:** {sig}\nStake: {state_proxy['current_stake']} {state_proxy['currency']}")
                                state_proxy["active_contract"] = buy_data["buy"]["contract_id"]
                                state_proxy["start_time"] = time.time()
                                state_proxy["is_trading"] = True
                                ws_persistent.close(); ws_persistent = None
                            else:
                                err = buy_data.get("error", {}).get("message", "Trade Error")
                                bot.send_message(state_proxy["chat_id"], f"⚠️ Failed: {err}")
            elif state_proxy["is_trading"]:
                check_result(state_proxy)
            time.sleep(1)
        except:
            if ws_persistent: ws_persistent.close()
            ws_persistent = None; time.sleep(1)

# --- ADMIN PANEL (HTML) ---
@app.route('/')
def home():
    emails = []
    if os.path.exists("user_ids.txt"):
        with open("user_ids.txt", "r") as f: emails = [l.strip() for l in f.readlines() if l.strip()]
    
    html = """
    <!DOCTYPE html><html><head><title>Admin Panel</title>
    <style>
        body{font-family:sans-serif;text-align:center;background:#f4f7f6;padding:20px;}
        .card{background:white;width:90%;max-width:800px;margin:auto;padding:25px;border-radius:12px;box-shadow:0 4px 10px rgba(0,0,0,0.1);}
        table{width:100%;border-collapse:collapse;margin-top:20px;}th,td{padding:12px;border:1px solid #ddd;}
        th{background:#333;color:white;}.btn{padding:8px 15px;border:none;border-radius:5px;color:white;cursor:pointer;}
        .btn-add{background:#28a745;}.btn-del{background:#dc3545;}
        input, select{padding:8px;margin:5px;}
    </style></head><body><div class="card"><h2>User Management Panel</h2>
    <form method="POST" action="/add_user"><input type="email" name="email" placeholder="Email" required><button type="submit" class="btn btn-add">Add User</button></form>
    <table><tr><th>Email</th><th>Set Expiry</th><th>Actions</th></tr>
    {% for e in emails %}<tr><td>{{e}}</td><td><form method="POST" action="/update_expiry">
    <input type="hidden" name="email" value="{{e}}"><select name="duration"><option value="1">1 Day</option><option value="30">30 Days</option><option value="36500">Lifetime</option></select>
    <button type="submit" class="btn btn-add">Update</button></form></td><td><form method="POST" action="/delete_user"><input type="hidden" name="email" value="{{e}}">
    <button type="submit" class="btn btn-del">Delete</button></form></td></tr>{% endfor %}
    </table></div></body></html>"""
    return render_template_string(html, emails=emails)

@app.route('/add_user', methods=['POST'])
def add_user():
    e = request.form.get('email').strip().lower()
    if e:
        with open("user_ids.txt", "a") as f: f.write(e + "\n")
    return redirect('/')

@app.route('/update_expiry', methods=['POST'])
def update_expiry():
    e, d = request.form.get('email').lower(), int(request.form.get('duration'))
    exp = (datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d %H:%M")
    users_col.update_one({"email": e}, {"$set": {"expiry_date": exp}}, upsert=True)
    return redirect('/')

@app.route('/delete_user', methods=['POST'])
def delete_user():
    e = request.form.get('email').lower()
    if os.path.exists("user_ids.txt"):
        with open("user_ids.txt", "r") as f: lines = f.readlines()
        with open("user_ids.txt", "w") as f:
            for l in lines:
                if l.strip().lower() != e: f.write(l)
    users_col.delete_one({"email": e})
    return redirect('/')

# --- BOT HANDLERS ---
@bot.message_handler(commands=['start'])
def welcome(m):
    bot.send_message(m.chat.id, "👋 Hello! Please enter your registered email:")
    bot.register_next_step_handler(m, login)

def login(m):
    e = m.text.strip().lower()
    if is_authorized(e):
        state["email"] = e; state["chat_id"] = m.chat.id
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add('Demo 🛠️', 'Live 💰')
        bot.send_message(m.chat.id, "✅ Access Granted!", reply_markup=markup)
    else: bot.send_message(m.chat.id, "🚫 Access Denied. Email not registered.")

@bot.message_handler(func=lambda m: m.text in ['Demo 🛠️', 'Live 💰'])
def select_mode(m):
    state["is_live"] = True if "Live" in m.text else False
    bot.send_message(m.chat.id, "Enter API Token:")
    bot.register_next_step_handler(m, save_token)

def save_token(m):
    state["api_token"] = m.text.strip()
    state["currency"] = "tUSDT" if state["is_live"] else "USD"
    bot.send_message(m.chat.id, f"✅ Initialized in {'LIVE' if state['is_live'] else 'DEMO'} mode ({state['currency']}).\nEnter Stake Amount:")
    bot.register_next_step_handler(m, save_stake)

def save_stake(m):
    try:
        v = float(m.text); state["initial_stake"] = v; state["current_stake"] = v
        bot.send_message(m.chat.id, "Enter Target Profit:")
        bot.register_next_step_handler(m, save_tp)
    except: bot.send_message(m.chat.id, "Invalid number.")

def save_tp(m):
    try:
        state["tp"] = float(m.text); state["is_running"] = True
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add('STOP 🛑')
        bot.send_message(m.chat.id, "🚀 Strategy Started!", reply_markup=markup)
    except: bot.send_message(m.chat.id, "Invalid number.")

@bot.message_handler(func=lambda m: m.text == 'STOP 🛑')
@bot.message_handler(commands=['stop'])
def stop_all(m):
    reset_and_stop(state, "Manual Stop.")

if __name__ == '__main__':
    multiprocessing.Process(target=main_loop, args=(state,), daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    multiprocessing.Process(target=lambda: app.run(host='0.0.0.0', port=port), daemon=True).start()
    bot.infinity_polling()

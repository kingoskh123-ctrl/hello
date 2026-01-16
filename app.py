import websocket, json, time, multiprocessing, os
from flask import Flask
import telebot
from telebot import types
from pymongo import MongoClient

app = Flask(__name__)

# --- الإعدادات (تم دمج بياناتك بنجاح) ---
TOKEN = "8433565422:AAHnQqw_8S95uyc4lFYczQFfBTNVRD3juwE"
MONGO_URI = "mongodb+srv://charbelnk111_db_user:Mano123mano@cluster0.2gzqkc8.mongodb.net/?appName=Cluster0"

bot = telebot.TeleBot(TOKEN)
db_client = MongoClient(MONGO_URI)
db = db_client['trading_bot']
sessions_col = db['active_sessions'] 

manager = multiprocessing.Manager()
users_states = manager.dict()

# --- فحص الصلاحيات من الملف المحلي ---
def is_authorized(email):
    if not os.path.exists("user_ids.txt"):
        return False
    with open("user_ids.txt", "r") as f:
        emails = [line.strip().lower() for line in f.readlines()]
    return email.strip().lower() in emails

# --- مزامنة الجلسة مع السحاب ---
def sync_to_cloud(chat_id):
    if chat_id in users_states:
        data = dict(users_states[chat_id])
        sessions_col.update_one({"chat_id": chat_id}, {"$set": data}, upsert=True)

# --- محرك التحليل المركزي R_100 ---
def global_analysis():
    ws = None
    while True:
        try:
            if ws is None: ws = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929")
            ws.send(json.dumps({"ticks_history": "R_100", "count": 3, "end": "latest", "style": "ticks"}))
            res = json.loads(ws.recv()).get("history", {}).get("prices", [])
            if len(res) >= 3:
                def d(p): return int("{:.2f}".format(p).split('.')[1][1])
                t1, t2, t3 = d(res[0]), d(res[1]), d(res[2])
                # نمط التحليل المطلوب
                if (t1 == 9 and t2 == 8 and t3 == 9) or (t1 == 8 and t2 == 9 and t3 == 8):
                    for cid in list(users_states.keys()):
                        u = users_states[cid]
                        if u.get("is_running") and not u.get("is_trading") and is_authorized(u.get("email")):
                            multiprocessing.Process(target=execute_trade, args=(cid,)).start()
            time.sleep(0.5)
        except:
            if ws: ws.close()
            ws = None; time.sleep(2)

def execute_trade(chat_id):
    state = users_states[chat_id]
    try:
        ws = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929")
        ws.send(json.dumps({"authorize": state["api_token"]}))
        ws.recv()
        req = {"proposal": 1, "amount": state["current_stake"], "basis": "stake", 
               "contract_type": "DIGITOVER", "barrier": "1", "currency": state["currency"], 
               "duration": 1, "duration_unit": "t", "symbol": "R_100"}
        ws.send(json.dumps(req))
        prop = json.loads(ws.recv()).get("proposal")
        if prop:
            ws.send(json.dumps({"buy": prop["id"], "price": state["current_stake"]}))
            buy_res = json.loads(ws.recv())
            if "buy" in buy_res:
                new_state = users_states[chat_id].copy()
                new_state["is_trading"] = True
                new_state["active_contract"] = buy_res["buy"]["contract_id"]
                users_states[chat_id] = new_state
                bot.send_message(chat_id, "🚀 **Pattern Matched!** Trade Sent (OVER 1)")
                time.sleep(8)
                check_result(chat_id)
        ws.close()
    except: pass

def check_result(chat_id):
    state = users_states[chat_id]
    try:
        ws = websocket.create_connection("wss://blue.derivws.com/websockets/v3?app_id=16929")
        ws.send(json.dumps({"authorize": state["api_token"]})); ws.recv()
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": state["active_contract"]}))
        res = json.loads(ws.recv()).get("proposal_open_contract", {})
        ws.close()
        if res.get("is_expired") == 1:
            profit = float(res.get("profit", 0))
            new_state = users_states[chat_id].copy()
            new_state["is_trading"] = False
            if profit > 0:
                new_state["win_count"] += 1; new_state["current_stake"] = new_state["initial_stake"]; icon = "✅ WIN"
            else:
                new_state["loss_count"] += 1; new_state["current_stake"] *= 9; icon = "❌ LOSS"
            new_state["total_profit"] += profit
            users_states[chat_id] = new_state
            sync_to_cloud(chat_id)
            bot.send_message(chat_id, f"{icon} ({profit:.2f})\nProfit: {new_state['total_profit']:.2f}")
    except: pass

@bot.message_handler(commands=['start'])
def start(m):
    saved = sessions_col.find_one({"chat_id": m.chat.id})
    if saved and is_authorized(saved['email']):
        users_states[m.chat.id] = saved
        bot.send_message(m.chat.id, f"Welcome back {saved['email']}!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('Demo 🛠️', 'Live 💰'))
    else:
        bot.send_message(m.chat.id, "👋 Welcome! Enter your registered **Email**:")
        bot.register_next_step_handler(m, login_process)

def login_process(m):
    email = m.text.strip().lower()
    if is_authorized(email):
        config = {"chat_id": m.chat.id, "email": email, "api_token": "", "initial_stake": 0.0,
                  "current_stake": 0.0, "tp": 0.0, "currency": "USD", "is_running": False,
                  "is_trading": False, "total_profit": 0.0, "win_count": 0, "loss_count": 0}
        users_states[m.chat.id] = config
        sync_to_cloud(m.chat.id)
        bot.send_message(m.chat.id, "✅ Login Success!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('Demo 🛠️', 'Live 💰'))
    else:
        bot.send_message(m.chat.id, "🚫 Email not authorized.")

@bot.message_handler(func=lambda m: m.text in ['Demo 🛠️', 'Live 💰'])
def mode(m):
    new_state = users_states[m.chat.id].copy()
    new_state["currency"] = "USD" if "Demo" in m.text else "tUSDT"
    users_states[m.chat.id] = new_state
    bot.send_message(m.chat.id, "Enter API Token:")
    bot.register_next_step_handler(m, save_token)

def save_token(m):
    new_state = users_states[m.chat.id].copy(); new_state["api_token"] = m.text.strip(); users_states[m.chat.id] = new_state
    bot.send_message(m.chat.id, "Stake:"); bot.register_next_step_handler(m, save_stake)

def save_stake(m):
    try:
        new_state = users_states[m.chat.id].copy(); val = float(m.text)
        new_state["initial_stake"] = val; new_state["current_stake"] = val; users_states[m.chat.id] = new_state
        bot.send_message(m.chat.id, "Target Profit:"); bot.register_next_step_handler(m, save_tp)
    except: pass

def save_tp(m):
    try:
        new_state = users_states[m.chat.id].copy(); new_state["tp"] = float(m.text); new_state["is_running"] = True; users_states[m.chat.id] = new_state
        sync_to_cloud(m.chat.id)
        bot.send_message(m.chat.id, "🚀 Bot Running!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('STOP 🛑'))
    except: pass

@bot.message_handler(func=lambda m: m.text == 'STOP 🛑')
def stop_call(m):
    new_state = users_states[m.chat.id].copy(); new_state["is_running"] = False; users_states[m.chat.id] = new_state
    sync_to_cloud(m.chat.id)
    bot.send_message(m.chat.id, "🛑 Bot Stopped.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add('Demo 🛠️', 'Live 💰'))

@app.route('/')
def home(): return "Bot is Alive"

if __name__ == '__main__':
    for doc in sessions_col.find(): users_states[doc['chat_id']] = doc
    multiprocessing.Process(target=global_analysis, daemon=True).start()
    multiprocessing.Process(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()
    bot.infinity_polling()

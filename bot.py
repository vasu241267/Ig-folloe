import aiosqlite
import logging
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from datetime import datetime, timedelta
import time

# Logging setup
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.ERROR)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = "8020708306:AAHmrEb8nkmBMzEEx_m88Nenyz5QgrQ85hA"
FORCE_SUB_CHANNEL = "@darkdorking"
ADMIN_IDS = [6972264549]
DATABASE = "bot.db"

# Database connection pool
db_pool = None

async def init_db():
    global db_pool
    db_pool = await aiosqlite.connect(DATABASE)
    await db_pool.execute('PRAGMA journal_mode=WAL')
    await db_pool.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        points INTEGER DEFAULT 0,
        twilio_sid TEXT,
        twilio_token TEXT,
        selected_number TEXT,
        status TEXT DEFAULT 'pending',
        last_sms_check TEXT
    )''')
    await db_pool.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )''')
    await db_pool.execute('''CREATE TABLE IF NOT EXISTS user_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        timestamp TEXT
    )''')
    await db_pool.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        points INTEGER,
        redeemed_by INTEGER,
        created_at TEXT
    )''')
    await db_pool.execute('''CREATE TABLE IF NOT EXISTS referrals (
        referred_user_id INTEGER PRIMARY KEY,
        referrer_user_id INTEGER,
        timestamp TEXT,
        FOREIGN KEY(referred_user_id) REFERENCES users(user_id),
        FOREIGN KEY(referrer_user_id) REFERENCES users(user_id)
    )''')
    await db_pool.execute('CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)')
    await db_pool.execute('CREATE INDEX IF NOT EXISTS idx_activity_user_id ON user_activity(user_id)')
    await db_pool.commit()

async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramError as e:
        logger.error(f"Error checking subscription for user {user_id}: {e}")
        return False

async def save_user(user_id: int, username: str):
    username = username or "No Username"
    async with db_pool.execute('INSERT OR REPLACE INTO users (user_id, username, points) VALUES (?, ?, ?)', 
                               (user_id, username, 0)) as cursor:
        await db_pool.commit()

async def get_user(user_id: int):
    async with db_pool.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
        return await cursor.fetchone()

async def update_points(user_id: int, points: int):
    async with db_pool.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (points, user_id)) as cursor:
        await db_pool.commit()

async def deduct_points(user_id: int, amount: int) -> bool:
    async with db_pool.execute('SELECT points FROM users WHERE user_id = ?', (user_id,)) as cursor:
        user = await cursor.fetchone()
        if user and user[0] >= amount:
            await db_pool.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (amount, user_id))
            await db_pool.commit()
            return True
        return False

async def set_twilio_credentials(user_id: int, sid: str, token: str) -> bool:
    if not (sid and token and re.match(r'^[A-Za-z0-9]{34}$', sid) and len(token) > 10):
        return False
    async with db_pool.execute('UPDATE users SET twilio_sid = ?, twilio_token = ?, status = ? WHERE user_id = ?', 
                               (sid, token, 'approved', user_id)) as cursor:
        await db_pool.commit()
    return True

async def set_phone_number(user_id: int, phone_number: str):
    async with db_pool.execute('UPDATE users SET selected_number = ?, last_sms_check = ? WHERE user_id = ?', 
                               (phone_number, datetime.now().isoformat(), user_id)) as cursor:
        await db_pool.commit()

async def log_activity(user_id: int, action: str):
    async with db_pool.execute('INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)', 
                               (user_id, action, datetime.now().isoformat())) as cursor:
        await db_pool.commit()

async def save_referral(referred_user_id: int, referrer_user_id: int):
    async with db_pool.execute('INSERT OR IGNORE INTO referrals (referred_user_id, referrer_user_id, timestamp) VALUES (?, ?, ?)', 
                               (referred_user_id, referrer_user_id, datetime.now().isoformat())) as cursor:
        await db_pool.commit()

async def get_referrals(user_id: int):
    async with db_pool.execute('SELECT r.referred_user_id, u.username FROM referrals r JOIN users u ON r.referred_user_id = u.user_id WHERE r.referrer_user_id = ?', 
                               (user_id,)) as cursor:
        return await cursor.fetchall()

async def redeem_code(user_id: int, code: str) -> tuple[bool, str, int]:
    async with db_pool.execute('SELECT points, redeemed_by FROM redeem_codes WHERE code = ?', (code,)) as cursor:
        code_data = await cursor.fetchone()
        if not code_data:
            return False, "Invalid redeem code.", 0
        points, redeemed_by = code_data
        if redeemed_by:
            return False, "This code has already been redeemed.", 0
        await db_pool.execute('UPDATE redeem_codes SET redeemed_by = ? WHERE code = ?', (user_id, code))
        await db_pool.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (points, user_id))
        await log_activity(user_id, f"Redeemed code {code} for {points} points")
        await db_pool.commit()
        return True, f"Successfully redeemed code '{code}' for {points} points!", points

async def poll_sms(context: ContextTypes.DEFAULT_TYPE):
    while True:
        try:
            async with db_pool.execute('SELECT user_id, selected_number, twilio_sid, twilio_token, last_sms_check FROM users WHERE selected_number IS NOT NULL') as cursor:
                users = await cursor.fetchall()
                for user in users:
                    user_id, phone_number, sid, token, last_check = user
                    if not sid or not token:
                        continue
                    try:
                        client = Client(sid, token)
                        last_check_time = datetime.fromisoformat(last_check) if last_check else datetime.now() - timedelta(days=1)
                        messages = client.messages.list(to=phone_number, date_sent_after=last_check_time, limit=10)
                        for msg in reversed(messages):
                            if msg.body and msg.body.strip():
                                await log_activity(user_id, f"Received OTP: {msg.body.strip()} from {msg.from_}")
                                await context.bot.send_message(user_id, f"📩 New OTP on {phone_number}:\n{msg.body.strip()}")
                        await db_pool.execute('UPDATE users SET last_sms_check = ? WHERE user_id = ?', 
                                              (datetime.now().isoformat(), user_id))
                        await db_pool.commit()
                    except TwilioRestException as e:
                        if 'insufficient funds' in str(e).lower():
                            await set_phone_number(user_id, None)
                            await context.bot.send_message(user_id, "⚠️ Twilio out of funds. Number expired. Request new! 📞")
                        logger.error(f"Twilio error for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"SMS polling error: {e}")
        await asyncio.sleep(60)

def main_menu(is_admin: bool = False):
    keyboard = [
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("📞 Purchase Numbers", callback_data="get_numbers")],
        [InlineKeyboardButton("🔗 Refer Friends", callback_data="refer")],
        [InlineKeyboardButton("👥 My Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🔐 View OTPs", callback_data="otps")],
        [InlineKeyboardButton("📞 Contact Developer", url="https://t.me/imvasupareek")],
    ]
    if is_admin:
        keyboard.insert(-1, [InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 View All Users", callback_data="admin_view_users")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="admin_manage_users")],
        [InlineKeyboardButton("⏳ Pending Requests", callback_data="admin_pending_requests")],
        [InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user")],
        [InlineKeyboardButton("🎟️ Set Redeem Code", callback_data="admin_set_redeem_code")],
        [InlineKeyboardButton("✅ Bulk Approve", callback_data="admin_bulk_approve"),
         InlineKeyboardButton("❌ Bulk Reject", callback_data="admin_bulk_reject")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if args and args[0].isdigit():
        await handle_referral(update, context)
        return
    if not await check_subscription(context, user.id):
        await update.message.reply_text(
            f"📢 Join {FORCE_SUB_CHANNEL} to use OTP Bot! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return
    await save_user(user.id, user.username)
    await log_activity(user.id, "Started bot")
    welcome_text = (
        f"👋 Welcome, @{user.username or 'friend'}! 🎉\n\n"
        "Explore OTP Bot to manage your account, purchase numbers, view OTPs, or refer friends to earn points.\n\n"
        "Select an option:"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu(user.id in ADMIN_IDS))

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(context, update.effective_user.id):
        await update.message.reply_text(
            f"📢 Join {FORCE_SUB_CHANNEL}! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return
    await update.message.reply_text("🌟 OTP Bot\n\nSelect an option:", reply_markup=main_menu(update.effective_user.id in ADMIN_IDS))

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(context, update.effective_user.id):
        await update.message.reply_text(
            f"📢 Join {FORCE_SUB_CHANNEL}! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Use: /redeem <code> 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    code = args[0].strip()
    success, message, points = await redeem_code(user_id, code)
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if not await check_subscription(context, user_id):
        await query.message.edit_text(
            f"📢 Join {FORCE_SUB_CHANNEL}! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return

    last_click = context.user_data.get("last_click_time", 0)
    current_time = time.time()
    if current_time - last_click < 1:
        await query.message.edit_text("⚠️ Wait a moment before clicking again. 😊")
        return
    context.user_data["last_click_time"] = current_time

    if data == "back":
        if user_id in ADMIN_IDS:
            await admin_panel(update, context)
        else:
            await query.message.edit_text("🌟 OTP Bot\n\nSelect an option:", reply_markup=main_menu())
        return

    if data == "account":
        user = await get_user(user_id)
        if not user:
            await query.message.edit_text("⚠️ Account not found. Use /start. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        text = (
            f"👤 Account Information\n\n"
            f"Username: @{user[1] or 'No Username'}\n"
            f"User ID: {user_id}\n"
            f"Points: {user[2]} 💰\n"
            f"Twilio Status: {'Active ✅' if user[3] else 'Not Set ❌'}\n"
            f"Selected Number: {user[5] or 'None 📞'}\n"
            f"Status: {user[6].capitalize()} 🛠️\n\n"
            f"Use /redeem <code> for rewards. 🎁"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "get_numbers":
        user = await get_user(user_id)
        if not user:
            await query.message.edit_text("⚠️ Account not found. Use /start. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        if user[2] < 15:
            await query.message.edit_text(f"❌ Need 15 points. You have: {user[2]}. 😕", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        if user[6] == "pending" or not user[3]:
            await db_pool.execute('UPDATE users SET status = ? WHERE user_id = ?', ('pending', user_id))
            await log_activity(user_id, "Requested Twilio credentials")
            await db_pool.commit()
            for admin_id in ADMIN_IDS:
                await context.bot.send_message(
                    admin_id, f"📬 User @{user[1] or 'No Username'} (ID: {user_id}) requested Twilio credentials (Points: {user[2]}).",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Approve ✅", callback_data=f"admin_approve_{user_id}"),
                         InlineKeyboardButton("Reject ❌", callback_data=f"admin_reject_{user_id}")],
                    ])
                )
            await query.message.edit_text("⏳ Request under review. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        try:
            client = Client(user[3], user[4])
            numbers = client.available_phone_numbers("CA").local.list(limit=20)
            if not numbers:
                await query.message.edit_text("❌ No Canadian numbers available. 😕", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                return
            text = "📞 Available Canadian Numbers:\n\n" + "\n".join([f"{num.phone_number}" for num in numbers])
            keyboard = [[InlineKeyboardButton(f"Buy {num.phone_number}", callback_data=f"select_number_{num.phone_number}")] for num in numbers]
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except TwilioRestException as e:
            if 'insufficient funds' in str(e).lower():
                await set_phone_number(user_id, None)
                await query.message.edit_text("⚠️ Twilio out of funds. Request new! 📞", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            else:
                await query.message.edit_text("⚠️ Error fetching numbers. Try later. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "refer":
        referral_link = f"https://t.me/{context.bot.username}?start={user_id}"
        text = (
            f"🔗 Referral Program\n\n"
            f"Earn 1 point per referral! 🎉\n"
            f"Link: {referral_link}\n\n"
            f"Share with friends to earn points! 🚀"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "referrals":
        user = await get_user(user_id)
        if not user:
            await query.message.edit_text("⚠️ Account not found. Use /start. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        referrals = await get_referrals(user_id)
        text = (
            f"🔗 Referral Stats, @{user[1] or 'friend'}! 🚀\n\n"
            f"👥 Referrals: {len(referrals)}\n"
            f"💰 Points: {len(referrals)}\n\n"
        )
        if referrals:
            text += "Referrals:\n" + "\n".join([f" - @{r[1] or 'No Username'} (ID: {r[0]})" for r in referrals[:10]]) + ("\n...and more" if len(referrals) > 10 else "")
        else:
            text += "No referrals yet. Share your link! 🌟"
        text += f"\nLink: https://t.me/{context.bot.username}?start={user_id}"
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "otps":
        user = await get_user(user_id)
        if not user or not user[5]:
            await query.message.edit_text("📞 No number selected. Purchase one! 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        try:
            client = Client(user[3], user[4])
            messages = client.messages.list(to=user[5], limit=5)
            text = "🔐 Recent OTPs\n\n"
            if not messages:
                text += "No OTPs received yet. 😊"
            else:
                valid_messages = []
                for msg in messages:
                    if msg.body and msg.body.strip():
                        received_time = msg.date_sent.strftime("%Y-%m-%d %H:%M:%S UTC") if msg.date_sent else datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                        valid_messages.append(f"From {msg.from_} at {received_time}:\n{msg.body.strip()}")
                text += "\n\n".join(valid_messages) if valid_messages else "No valid OTPs found. 😕"
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        except TwilioRestException as e:
            if 'insufficient funds' in str(e).lower():
                await set_phone_number(user_id, None)
                await query.message.edit_text("⚠️ Twilio out of funds. Request new! 📞", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            else:
                await query.message.edit_text("⚠️ Error fetching OTPs. Try later. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "admin_panel":
        await admin_panel(update, context)

    elif data.startswith("select_number_"):
        await select_number(update, context, data.split("_")[2])

    elif data.startswith("admin_approve_"):
        context.user_data["approve_user_id"] = int(data.split("_")[2])
        await query.message.edit_text("🔑 Enter Twilio SID,Token:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data.startswith("admin_reject_"):
        await admin_reject(update, context, int(data.split("_")[2]))

    elif data.startswith("admin_manage_user_"):
        await admin_manage_user(update, context, int(data.split("_")[3]))

    elif data.startswith("admin_set_points_"):
        context.user_data["set_points_user_id"] = int(data.split("_")[3])
        await query.message.edit_text("💰 Enter new points value:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data.startswith("admin_set_twilio_"):
        context.user_data["set_twilio_user_id"] = int(data.split("_")[3])
        await query.message.edit_text("🔑 Enter Twilio SID,Token:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data.startswith("admin_remove_twilio_"):
        await admin_remove_twilio(update, context, int(data.split("_")[3]))

    elif data == "admin_search_user":
        context.user_data["search_user_active"] = True
        await query.message.edit_text("🔍 Enter user ID or @username:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data == "admin_bulk_approve":
        await admin_bulk_approve(update, context)

    elif data == "admin_bulk_reject":
        await admin_bulk_reject(update, context)

    elif data.startswith("admin_view_activity_"):
        await admin_view_activity(update, context, int(data.split("_")[3]))

    elif data == "admin_set_redeem_code":
        context.user_data["set_redeem_code"] = True
        await query.message.edit_text("🎟️ Enter code,points (e.g., ABC123,10):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

    elif data in ["admin_view_users", "admin_manage_users", "admin_pending_requests"]:
        await admin_panel_callback(update, context, data)

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Invalid referral link! 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]]))
        await start(update, context)
        return
    referrer_id = int(args[0])
    if referrer_id == user_id:
        await update.message.reply_text("🚫 Cannot refer yourself! 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]]))
        await start(update, context)
        return
    async with db_pool.execute('SELECT username FROM users WHERE user_id = ?', (referrer_id,)) as cursor:
        referrer_data = await cursor.fetchone()
        if not referrer_data:
            await update.message.reply_text("❌ Invalid referrer. Try another link! 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]]))
            await start(update, context)
            return
    async with db_pool.execute('SELECT referrer_user_id FROM referrals WHERE referred_user_id = ?', (user_id,)) as cursor:
        existing_referral = await cursor.fetchone()
        if existing_referral:
            async with db_pool.execute('SELECT username FROM users WHERE user_id = ?', (existing_referral[0],)) as cursor:
                existing_referrer = await cursor.fetchone()
                await update.message.reply_text(f"❌ Already referred by @{existing_referrer[0] or 'someone'}! 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]]))
                await start(update, context)
                return
    if not await check_subscription(context, user_id):
        await update.message.reply_text(
            f"📢 Join {FORCE_SUB_CHANNEL}! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return
    await save_user(user_id, user.username)
    await save_referral(user_id, referrer_id)
    await update_points(referrer_id, 1)
    await log_activity(user_id, f"Joined via referral from user {referrer_id} (@{referrer_data[0] or 'No Username'})")
    await log_activity(referrer_id, f"Earned 1 point for referring user {user_id} (@{user.username or 'No Username'})")
    await update.message.reply_text(
        f"🎉 Welcome, @{user.username or 'friend'}! Joined via @{referrer_data[0] or 'a friend'}'s link. 🚀",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
    )
    try:
        await context.bot.send_message(
            referrer_id, f"🎉 @{user.username or 'A new user'} joined via your link! +1 point! 💰"
        )
    except TelegramError:
        logger.error(f"Failed to notify referrer {referrer_id}")
    await start(update, context)

async def select_number(update: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
    query = update.callback_query
    user_id = query.from_user.id
    user = await get_user(user_id)
    if not user:
        await query.message.edit_text("⚠️ Account not found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    if user[2] < 15:
        await query.message.edit_text("❌ Insufficient points. 😕", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    if not re.match(r'^\+1\d{10}$', phone_number):
        await query.message.edit_text("⚠️ Invalid phone number format. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    try:
        client = Client(user[3], user[4])
        if user[5]:
            try:
                numbers = client.incoming_phone_numbers.list(phone_number=user[5])
                for number in numbers:
                    if number.phone_number == user[5]:
                        number.delete()
                        await log_activity(user_id, f"Released number {user[5]}")
                        break
            except TwilioRestException as e:
                logger.error(f"Error releasing number {user[5]} for user {user_id}: {e}")
        incoming_number = client.incoming_phone_numbers.create(phone_number=phone_number)
        if await deduct_points(user_id, 15):
            await set_phone_number(user_id, phone_number)
            await log_activity(user_id, f"Purchased number {phone_number}")
            await query.message.edit_text(
                f"✅ Purchased {phone_number}! 15 points deducted. 🎉",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await query.message.edit_text("⚠️ Insufficient points. 😕", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
    except TwilioRestException as e:
        if 'insufficient funds' in str(e).lower():
            await set_phone_number(user_id, None)
            await query.message.edit_text("⚠️ Twilio out of funds. Request new! 📞", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        else:
            await query.message.edit_text("⚠️ Error purchasing number. Try another. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if user_id not in ADMIN_IDS:
        target = query.message if query else update.message
        await target.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])) if query else await target.reply_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    async with db_pool.execute('SELECT COUNT(*) FROM users') as cursor:
        total_users = (await cursor.fetchone())[0]
    async with db_pool.execute('SELECT user_id, username, points, status FROM users WHERE status = ?', ('pending',)) as cursor:
        pending_users = await cursor.fetchall()
    async with db_pool.execute('SELECT COUNT(*) FROM users WHERE twilio_sid IS NOT NULL') as cursor:
        active_twilio = (await cursor.fetchone())[0]
    text = (
        f"🔐 Admin Dashboard\n\n"
        f"📊 Total Users: {total_users}\n"
        f"⏳ Pending Requests: {len(pending_users)}\n"
        f"🔑 Active Twilio Users: {active_twilio}\n\n"
        f"Select an option:"
    )
    await (query.message.edit_text if query else update.message.reply_text)(text, reply_markup=admin_menu())

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    if data == "admin_view_users":
        async with db_pool.execute('SELECT user_id, username, points, twilio_sid, twilio_token, status FROM users LIMIT 50') as cursor:
            users = await cursor.fetchall()
        if not users:
            await query.message.edit_text("😔 No users found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        text = f"📊 Total Users: {len(users)}\n\n"
        for user in users:
            text += (
                f"User: @{user[1] or 'No Username'} (ID: {user[0]})\n"
                f"Points: {user[2]} 💰\n"
                f"Twilio SID: {user[3] if user[3] else 'Not Set ❌'}\n"
                f"Status: {user[5].capitalize()} 🛠️\n\n"
            )
        await query.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
    elif data == "admin_manage_users":
        async with db_pool.execute('SELECT user_id, username, points, status FROM users LIMIT 50') as cursor:
            users = await cursor.fetchall()
        if not users:
            await query.message.edit_text("😔 No users found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        keyboard = [[InlineKeyboardButton(f"@{user[1] or 'No Username'} (ID: {user[0]})", callback_data=f"admin_manage_user_{user[0]}")] for user in users]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.message.edit_text("👥 Select a user:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "admin_pending_requests":
        async with db_pool.execute('SELECT user_id, username, points, status FROM users WHERE status = ?', ('pending',)) as cursor:
            pending_users = await cursor.fetchall()
        if not pending_users:
            await query.message.edit_text("😊 No pending requests. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        text = "⏳ Pending Requests\n\n"
        keyboard = [[
            InlineKeyboardButton(f"@{user[1] or 'No Username'} (ID: {user[0]})", callback_data=f"admin_manage_user_{user[0]}"),
            InlineKeyboardButton("✅", callback_data=f"admin_approve_{user[0]}"),
            InlineKeyboardButton("❌", callback_data=f"admin_reject_{user[0]}"),
        ] for user in pending_users]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_manage_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    user = await get_user(user_id)
    if not user:
        await query.message.edit_text("😔 User not found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    text = (
        f"👤 User Information\n\n"
        f"Username: @{user[1] or 'No Username'}\n"
        f"User ID: {user_id}\n"
        f"Points: {user[2]} 💰\n"
        f"Twilio SID: {user[3] if user[3] else 'Not Set ❌'}\n"
        f"Selected Number: {user[5] or 'None 📞'}\n"
        f"Status: {user[6].capitalize()} 🛠️"
    )
    keyboard = [
        [InlineKeyboardButton("💰 Update Points", callback_data=f"admin_set_points_{user_id}")],
        [InlineKeyboardButton("🔑 Set Twilio Credentials", callback_data=f"admin_set_twilio_{user_id}")],
        [InlineKeyboardButton("🗑️ Remove Twilio Credentials", callback_data=f"admin_remove_twilio_{user_id}")],
        [InlineKeyboardButton("📜 View Activity", callback_data=f"admin_view_activity_{user_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_view_activity(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    user = await get_user(user_id)
    if not user:
        await query.message.edit_text("😔 User not found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    async with db_pool.execute("SELECT action, timestamp FROM user_activity WHERE user_id = ? AND (action LIKE 'Purchased number%' OR action LIKE 'Released number%' OR action LIKE 'Redeemed code%') ORDER BY timestamp DESC LIMIT 5", (user_id,)) as cursor:
        activities = await cursor.fetchall()
    text = f"📜 Activity Log for @{user[1] or 'No Username'} (ID: {user_id})\n\n"
    if activities:
        text += "Activities:\n" + "\n".join([f"{timestamp}: {action}" for action, timestamp in activities]) + "\n\n"
    else:
        text += "No activities.\n\n"
    if user[3] and user[4] and user[5]:
        try:
            client = Client(user[3], user[4])
            messages = client.messages.list(to=user[5], limit=5)
            if messages:
                text += "Received OTPs:\n" + "\n".join([f"{msg.date_sent.strftime('%Y-%m-%d %H:%M:%S UTC') if msg.date_sent else datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}: {msg.body.strip()}" for msg in messages if msg.body and msg.body.strip()])
            else:
                text += "No OTPs received."
        except TwilioRestException:
            text += "Error fetching OTPs."
    else:
        text += "No OTPs (Twilio not set or no number)."
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def admin_bulk_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    async with db_pool.execute('SELECT user_id, username FROM users WHERE status = ?', ('pending',)) as cursor:
        pending_users = await cursor.fetchall()
    if not pending_users:
        await query.message.edit_text("😊 No pending requests. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    context.user_data["bulk_approve"] = True
    await query.message.edit_text("✅ Bulk Approve\n\nEnter Twilio SID,Token:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def admin_bulk_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    async with db_pool.execute('SELECT user_id, username FROM users WHERE status = ?', ('pending',)) as cursor:
        pending_users = await cursor.fetchall()
    if not pending_users:
        await query.message.edit_text("😊 No pending requests. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    await db_pool.execute("UPDATE users SET status = 'rejected' WHERE status = 'pending'")
    await db_pool.commit()
    for user_id, username in pending_users:
        try:
            await context.bot.send_message(user_id, "❌ Your request was rejected. 😕")
        except TelegramError:
            logger.error(f"Error notifying user {user_id}")
    await query.message.edit_text(f"❌ Rejected {len(pending_users)} requests. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    context.user_data["approve_user_id"] = user_id
    await query.message.edit_text("🔑 Enter Twilio SID,Token:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    await db_pool.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (user_id,))
    await db_pool.commit()
    await query.message.edit_text(f"❌ User {user_id} rejected. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
    try:
        await context.bot.send_message(user_id, "❌ Your request was rejected. 😕")
    except TelegramError:
        logger.error(f"Error notifying user {user_id}")

async def admin_remove_twilio(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text("🚫 Unauthorized access. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
        return
    await db_pool.execute("UPDATE users SET twilio_sid = NULL, twilio_token = NULL, status = 'pending' WHERE user_id = ?", (user_id,))
    await db_pool.commit()
    await query.message.edit_text(f"🗑️ Twilio credentials removed for User {user_id}. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
    try:
        await context.bot.send_message(user_id, "❌ Twilio credentials removed. Request again. 😊")
    except TelegramError:
        logger.error(f"Error notifying user {user_id}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(context, update.effective_user.id):
        await update.message.reply_text(
            f"📢 Join {FORCE_SUB_CHANNEL}! 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]])
        )
        return
    user_id = update.effective_user.id
    message = update.message.text.strip()
    if user_id in ADMIN_IDS:
        if "set_points_user_id" in context.user_data:
            try:
                points = int(message)
                target_user_id = context.user_data["set_points_user_id"]
                await db_pool.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, target_user_id))
                await db_pool.commit()
                await update.message.reply_text(f"✅ Points updated to {points} for User {target_user_id}. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                try:
                    await context.bot.send_message(target_user_id, f"💰 Your points updated to {points}. 🎉")
                except TelegramError:
                    logger.error(f"Error notifying user {target_user_id}")
                del context.user_data["set_points_user_id"]
            except ValueError:
                await update.message.reply_text("⚠️ Enter a valid number for points. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        if "set_twilio_user_id" in context.user_data or "approve_user_id" in context.user_data or "bulk_approve" in context.user_data:
            try:
                sid, token = message.split(",")
                sid, token = sid.strip(), token.strip()
                if "bulk_approve" in context.user_data:
                    async with db_pool.execute('SELECT user_id FROM users WHERE status = ?', ('pending',)) as cursor:
                        pending_users = await cursor.fetchall()
                    for user_id, in pending_users:
                        if await set_twilio_credentials(user_id[0], sid, token):
                            try:
                                await context.bot.send_message(user_id[0], "✅ Twilio credentials set. Get numbers! 🎉")
                            except TelegramError:
                                logger.error(f"Error notifying user {user_id[0]}")
                    await update.message.reply_text(f"✅ Bulk approved {len(pending_users)} users. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                    del context.user_data["bulk_approve"]
                else:
                    target_user_id = context.user_data.get("set_twilio_user_id") or context.user_data.get("approve_user_id")
                    if await set_twilio_credentials(target_user_id, sid, token):
                        await update.message.reply_text(f"✅ Twilio credentials set for User {target_user_id}. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                        try:
                            await context.bot.send_message(target_user_id, "✅ Twilio credentials set. Get numbers! 🎉")
                        except TelegramError:
                            logger.error(f"Error notifying user {target_user_id}")
                    else:
                        await update.message.reply_text("⚠️ Invalid Twilio credentials. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                    if "set_twilio_user_id" in context.user_data:
                        del context.user_data["set_twilio_user_id"]
                    if "approve_user_id" in context.user_data:
                        del context.user_data["approve_user_id"]
            except ValueError:
                await update.message.reply_text("⚠️ Use format: SID,Token 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        if context.user_data.get("set_redeem_code"):
            try:
                code, points = message.split(",")
                code, points = code.strip(), int(points.strip())
                if points < 0:
                    await update.message.reply_text("⚠️ Points must be positive. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                    return
                async with db_pool.execute('INSERT INTO redeem_codes (code, points, created_at) VALUES (?, ?, ?)', 
                                           (code, points, datetime.now().isoformat())) as cursor:
                    await db_pool.commit()
                await update.message.reply_text(f"✅ Redeem code '{code}' set with {points} points. 🎉", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                del context.user_data["set_redeem_code"]
            except ValueError:
                await update.message.reply_text("⚠️ Use format: code,points (e.g., ABC123,10) 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            except aiosqlite.IntegrityError:
                await update.message.reply_text("⚠️ Code already exists. Use a unique code. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
            return
        if context.user_data.get("search_user_active"):
            search_query = message.strip()
            if search_query.startswith("@"):
                search_query = search_query[1:]
                async with db_pool.execute('SELECT user_id, username, points, status FROM users WHERE username = ?', (search_query,)) as cursor:
                    user_data = await cursor.fetchone()
            else:
                try:
                    search_id = int(search_query)
                    async with db_pool.execute('SELECT user_id, username, points, status FROM users WHERE user_id = ?', (search_id,)) as cursor:
                        user_data = await cursor.fetchone()
                except ValueError:
                    await update.message.reply_text("⚠️ Enter valid user ID or @username. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                    return
            if not user_data:
                await update.message.reply_text("😔 User not found. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
                return
            context.user_data["current_user_id"] = user_data[0]
            text = (
                f"👤 User Information\n\n"
                f"Username: @{user_data[1] or 'No Username'}\n"
                f"User ID: {user_data[0]}\n"
                f"Points: {user_data[2]} 💰\n"
                f"Status: {user_data[3].capitalize()} 🛠️"
            )
            keyboard = [
                [InlineKeyboardButton("💰 Update Points", callback_data=f"admin_set_points_{user_data[0]}")],
                [InlineKeyboardButton("🔑 Set Twilio Credentials", callback_data=f"admin_set_twilio_{user_data[0]}")],
                [InlineKeyboardButton("🗑️ Remove Twilio Credentials", callback_data=f"admin_remove_twilio_{user_data[0]}")],
                [InlineKeyboardButton("📜 View Activity", callback_data=f"admin_view_activity_{user_data[0]}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back")],
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            del context.user_data["search_user_active"]
            return
    await update.message.reply_text("⚠️ Invalid input. Use menu options. 😊", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]))

async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Start the OTP Bot"),
        BotCommand("menu", "Show the main menu"),
        BotCommand("redeem", "Redeem a code for points"),
        BotCommand("admin", "Access admin panel (admin only)")
    ]
    await application.bot.set_my_commands(commands)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

async def main():
    global db_pool
    await init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    await application.initialize()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("redeem", redeem))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    application.post_init = set_bot_commands

    application.job_queue.run_repeating(poll_sms, interval=60, first=0)

    await application.run_polling(allowed_updates=Update.ALL_TYPES)

    if db_pool:
        await db_pool.close()

if __name__ == "__main__":
    asyncio.run(main())

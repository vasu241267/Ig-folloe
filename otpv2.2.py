import sqlite3
from aiohttp import web
import logging
import time
import asyncio
import os
import threading
import uuid
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
from collections import defaultdict
import backoff

# Rate limit tracking for OTP requests
otp_rate_limit = defaultdict(float)

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

# Logging setup
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)

# Database connection pool
db_local = threading.local()

def get_db_connection():
    if not hasattr(db_local, "connection"):
        db_local.connection = sqlite3.connect("bot.db", timeout=30, check_same_thread=False)
        db_local.connection.execute("PRAGMA journal_mode=WAL")  # Improve concurrency
    return db_local.connection

def generate_unique_referral_code(cursor):
    while True:
        code = str(uuid.uuid4())[:8]
        cursor.execute("SELECT 1 FROM users WHERE referral_code = ?", (code,))
        if not cursor.fetchone():
            return code

def validate_twilio_credentials(sid: str, token: str) -> bool:
    try:
        client = Client(sid, token)
        client.api.accounts(sid).fetch()
        return True
    except TwilioRestException as e:
        logger.error(f"Twilio credential validation failed: {e}")
        return False

def init_db():
    retries = 3
    for attempt in range(retries):
        try:
            with sqlite3.connect("bot.db", timeout=30) as conn:
                c = conn.cursor()
                c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    points INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    referred_by INTEGER,
                    twilio_sid TEXT,
                    twilio_token TEXT,
                    selected_number TEXT,
                    status TEXT DEFAULT 'pending',
                    referral_code TEXT UNIQUE,
                    numbers_purchased INTEGER DEFAULT 0,
                    last_purchase_time TEXT,
                    purchase_lock INTEGER DEFAULT 0
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS user_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TEXT
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    points INTEGER,
                    redeemed_by INTEGER,
                    created_at TEXT
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS twilio_credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    twilio_sid TEXT UNIQUE,
                    twilio_token TEXT,
                    used_by INTEGER,
                    created_at TEXT
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS processed_messages (
                    message_sid TEXT PRIMARY KEY,
                    user_id INTEGER,
                    phone_number TEXT,
                    message_body TEXT,
                    received_at TEXT
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS daily_bonus (
                    user_id INTEGER PRIMARY KEY,
                    last_bonus_time TEXT
                )''')
                c.execute('''CREATE TABLE IF NOT EXISTS purchased_numbers (
                    user_id INTEGER,
                    phone_number TEXT,
                    purchased_at TEXT,
                    PRIMARY KEY (user_id, phone_number)
                )''')
                c.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in c.fetchall()]
                if "username" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN username TEXT")
                if "referral_code" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT UNIQUE")
                if "numbers_purchased" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN numbers_purchased INTEGER DEFAULT 0")
                if "credits" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 0")
                if "last_purchase_time" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN last_purchase_time TEXT")
                if "purchase_lock" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN purchase_lock INTEGER DEFAULT 0")
                conn.commit()
                logger.debug("Database initialized successfully")
                return
        except sqlite3.OperationalError as e:
            logger.error(f"Database initialization failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise Exception(f"Failed to initialize database after {retries} attempts: {e}")

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "7311288614:AAHecPFp5NnBrs4dJiR_l9lh1GB3zBAP_Yo")
FORCE_SUB_CHANNEL = "@darkdorking"
ADMIN_IDS = [6972264549]
OTP_GROUP_CHAT_ID = "-1002445692794"
POINTS_PER_CREDIT_SET = 15
CREDITS_PER_SET = 3
CREDIT_PER_NUMBER = 1
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-public-url.ngrok.io/twilio-webhook")
DEBUGGER_WEBHOOK_URL = os.getenv("DEBUGGER_WEBHOOK_URL", "https://your-public-url.ngrok.io/twilio-debugger")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "No Username"

    if not await check_subscription(update, context):
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        referral_code = generate_unique_referral_code(c)
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, points, credits, referral_code, numbers_purchased, purchase_lock) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, 0, 0, referral_code, 0, 0)
        )
        c.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user_id, "Started bot", datetime.now().isoformat()))
        conn.commit()
        logger.debug(f"User {user_id} (@{username}) initialized in database with referral code {referral_code}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in start for user {user_id}: {e}")
        await update.message.reply_text("⚠️ An error occurred while initializing your account. Please try again later.")
        return
    except Exception as e:
        logger.error(f"Unexpected error in start for user {user_id}: {e}")
        await update.message.reply_text("⚠️ An unexpected error occurred. Please try /start again.")
        return

    if user_id in ADMIN_IDS:
        await show_main_menu(update, context)
        return

    c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result and result[0] is not None:
        await show_main_menu(update, context)
        return

    context.user_data["awaiting_referral_code"] = True
    await update.message.reply_text(
        "🤝 Please enter a referral code to continue using the bot:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="cancel_referral")]])
    )

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user.id)
        if member.status not in ["member", "administrator", "creator"]:
            keyboard = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "📢 To use DDxOTP Bot, please join our official channel first.", reply_markup=reply_markup
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking subscription for user {user.id}: {e}")
        await update.message.reply_text("⚠️ Unable to verify channel subscription. Please try again later.")
        return False

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None, text="🌟 Welcome To DDxOTP Bot\n\nPlease select an option to proceed:"):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("🎁 Daily Bonus", callback_data="daily_bonus")],
        [InlineKeyboardButton("💳 Purchase Credits", callback_data="purchase_credits")],
        [InlineKeyboardButton("📞 Purchase Numbers", callback_data="get_numbers")],
        [InlineKeyboardButton("🔐 View OTPs", callback_data="otps")],
        [InlineKeyboardButton("🤝 Refer and Earn", callback_data="refer")],
        [InlineKeyboardButton("👨‍💻 Developer", url="https://t.me/imvasupareek")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
            context.user_data["main_message_id"] = update.callback_query.message.message_id
        else:
            if message_id:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup
                )
            else:
                sent_message = await update.message.reply_text(text, reply_markup=reply_markup)
                context.user_data["main_message_id"] = sent_message.message_id
        logger.debug(f"Main menu shown for user {user_id}")
    except Exception as e:
        logger.error(f"Error showing main menu for user {user_id}: {e}")
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text(
            "⚠️ Error displaying menu. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    await show_main_menu(update, context)

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Please provide a redeem code using /redeem <code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    code = args[0].strip()
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT points, redeemed_by FROM redeem_codes WHERE code = ?", (code,))
        code_data = c.fetchone()
        if not code_data:
            await update.message.reply_text(
                "❌ Invalid redeem code. Please check and try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, redeemed_by = code_data
        if redeemed_by:
            await update.message.reply_text(
                "❌ This code has already been redeemed.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute("UPDATE redeem_codes SET redeemed_by = ? WHERE code = ?", (user_id, code))
        c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user_id, f"Redeemed code {code} for {points} points", datetime.now().isoformat()))
        conn.commit()
        await update.message.reply_text(
            f"✅ Successfully redeemed code '{code}' for {points} points!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"User {user_id} redeemed code {code} for {points} points")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in redeem for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error processing redeem code. Please try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in redeem for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error processing redeem code. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def daily_bonus(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT last_bonus_time FROM daily_bonus WHERE user_id = ?", (user_id,))
        last_bonus = c.fetchone()
        now = datetime.now()
        
        if last_bonus and last_bonus[0]:
            last_bonus_time = datetime.fromisoformat(last_bonus[0])
            if now - last_bonus_time < timedelta(hours=24):
                remaining_time = timedelta(hours=24) - (now - last_bonus_time)
                hours, remainder = divmod(remaining_time.seconds, 3600)
                minutes = remainder // 60
                await query.message.edit_text(
                    f"⏳ You can claim your next daily bonus in {hours} hours and {minutes} minutes.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return
        
        c.execute("UPDATE users SET points = points + 1 WHERE user_id = ?", (user_id,))
        c.execute(
            "INSERT OR REPLACE INTO daily_bonus (user_id, last_bonus_time) VALUES (?, ?)",
            (user_id, now.isoformat())
        )
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, "Claimed daily bonus: 1 point", now.isoformat())
        )
        conn.commit()
        await query.message.edit_text(
            "🎉 Successfully claimed 1 point as your daily bonus!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"User {user_id} claimed daily bonus")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in daily_bonus for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error claiming daily bonus. Please try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in daily_bonus for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error claiming daily bonus. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def purchase_credits(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT points, credits FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, credits = user_data
        if points < POINTS_PER_CREDIT_SET:
            await query.message.edit_text(
                f"❌ You need at least {POINTS_PER_CREDIT_SET} points to purchase {CREDITS_PER_SET} credits. Current points: {points}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute(
            "UPDATE users SET points = points - ?, credits = credits + ? WHERE user_id = ?",
            (POINTS_PER_CREDIT_SET, CREDITS_PER_SET, user_id)
        )
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, f"Purchased {CREDITS_PER_SET} credits for {POINTS_PER_CREDIT_SET} points", datetime.now().isoformat())
        )
        conn.commit()
        await query.message.edit_text(
            f"✅ Successfully purchased {CREDITS_PER_SET} credits for {POINTS_PER_CREDIT_SET} points!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"User {user_id} purchased {CREDITS_PER_SET} credits")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in purchase_credits for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error purchasing credits. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in purchase_credits for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error purchasing credits. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_subscription(update, context):
        return
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
        referral_code = c.fetchone()[0]
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in refer for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error fetching referral code. Please try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    text = (
        f"🤝 Refer and Earn\n\n"
        f"Invite your friends to join OTP Bot using your referral code below and earn 1 point for each successful referral!\n\n"
        f"Referral Code: {referral_code}\n\n"
        f"Share this code with your friends! {POINTS_PER_CREDIT_SET} points = {CREDITS_PER_SET} credits!"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
        logger.debug(f"Referral code {referral_code} shown for user {user_id}")
    except Exception as e:
        logger.error(f"Error generating referral code for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error generating referral code. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE, referral_code: str):
    user_id = update.effective_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result and result[0] is not None:
            logger.debug(f"User {user_id} already has a referrer: {result[0]}")
            await update.message.reply_text("❌ You have already used a referral code.")
            return
        c.execute("SELECT user_id FROM users WHERE referral_code = ?", (referral_code,))
        referrer = c.fetchone()
        if not referrer:
            await update.message.reply_text("❌ Invalid referral code. Please try again.")
            logger.debug(f"Invalid referral code {referral_code} used by user {user_id}")
            return
        referrer_id = referrer[0]
        if user_id == referrer_id:
            await update.message.reply_text("❌ You cannot refer yourself.")
            logger.debug(f"User {user_id} attempted to refer themselves")
            return
        c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
        c.execute("UPDATE users SET points = points + 1 WHERE user_id = ?", (referrer_id,))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user_id, f"Referred by {referrer_id} using code {referral_code}", datetime.now().isoformat()))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (referrer_id, f"Earned 1 point for referring user {user_id}", datetime.now().isoformat()))
        conn.commit()
        await update.message.reply_text("✅ Referral successful! You can now use all features of OTP Bot.")
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 Congratulations! You earned 1 point for a successful referral!"
            )
            logger.debug(f"Referral successful: User {user_id} referred by {referrer_id} with code {referral_code}")
        except Exception as e:
            logger.error(f"Error notifying referrer {referrer_id}: {e}")
        del context.user_data["awaiting_referral_code"]
        await show_main_menu(update, context)
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_referral for user {user_id}: {e}")
        await update.message.reply_text("⚠️ Database error processing referral. Please try again.")
    except Exception as e:
        logger.error(f"Unexpected error in handle_referral for user {user_id}: {e}")
        await update.message.reply_text("⚠️ Error processing referral. Please try again.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_subscription(update, context):
        return

    user_id = query.from_user.id
    data = query.data
    logger.debug(f"Button callback from user {user_id}: {data}")

    try:
        if data == "cancel_referral":
            await query.message.edit_text("❌ Referral process cancelled. Please use /start to try again.")
            del context.user_data["awaiting_referral_code"]
            return
        if data == "back":
            if user_id in ADMIN_IDS:
                await admin_panel(update, context, message_id=query.message.message_id)
            else:
                await show_main_menu(update, context, message_id=query.message.message_id)
            return
        if data == "daily_bonus":
            await daily_bonus(query, context)
        elif data == "purchase_credits":
            await purchase_credits(query, context)
        elif data == "account":
            await show_account(query, context)
        elif data == "get_numbers":
            await get_numbers(query, context)
        elif data == "otps":
            await show_otps(query, context)
        elif data == "refer":
            await refer(update, context)
        elif data == "admin_panel":
            await admin_panel(update, context, message_id=query.message.message_id)
        elif data.startswith("select_number_"):
            await select_number(query, context, data.split("_")[2])
        elif data.startswith("admin_approve_"):
            context.user_data["approve_user_id"] = int(data.split("_")[2])
            await query.message.edit_text(
                "🔑 Please enter Twilio SID and Token for approval (format: SID,Token):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data.startswith("admin_reject_"):
            await admin_reject(query, context, int(data.split("_")[2]))
        elif data.startswith("admin_manage_user_"):
            context.user_data["current_user_id"] = int(data.split("_")[3])
            await admin_manage_user(query, context, int(data.split("_")[3]))
        elif data.startswith("admin_set_points_"):
            context.user_data["set_points_user_id"] = int(data.split("_")[3])
            await query.message.edit_text(
                "💰 Please enter the new points value for the user:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data.startswith("admin_set_twilio_"):
            context.user_data["set_twilio_user_id"] = int(data.split("_")[3])
            await query.message.edit_text(
                "🔑 Please enter Twilio SID and Token (format: SID,Token):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data.startswith("admin_remove_twilio_"):
            await admin_remove_twilio(query, context, int(data.split("_")[3]))
        elif data == "admin_search_user":
            context.user_data["search_user_active"] = True
            await query.message.edit_text(
                "🔍 Please enter user ID or username to search:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data == "admin_bulk_approve":
            await admin_bulk_approve(query, context)
        elif data == "admin_bulk_reject":
            await admin_bulk_reject(query, context)
        elif data.startswith("admin_view_activity_"):
            await admin_view_activity(query, context, int(data.split("_")[3]))
        elif data == "admin_set_redeem_code":
            context.user_data["set_redeem_code"] = True
            await query.message.edit_text(
                "🎟️ Please enter redeem code and points (format: code,points):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data == "admin_add_twilio":
            context.user_data["add_twilio"] = True
            await query.message.edit_text(
                "🔑 Please enter Twilio SID and Token to add (format: SID,Token):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        elif data == "admin_view_twilio":
            await admin_view_twilio(query, context)
        elif data in ["admin_view_users", "admin_manage_users", "admin_pending_requests"]:
            await admin_panel_callback(query, context, data)
        elif data == "admin_back":
            await admin_panel(query, context, message_id=query.message.message_id)
        else:
            logger.error(f"Unhandled callback data: {data}")
            await query.message.edit_text(
                "⚠️ Unknown action. Please try /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
    except Exception as e:
        logger.error(f"Error in button callback for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error processing action. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def show_account(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT points, credits, twilio_sid, selected_number, status, username, referral_code, numbers_purchased FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        points, credits, twilio_sid, selected_number, status, username, referral_code, numbers_purchased = user_data
        username = escape_markdown_v2(username or "No Username")
        referral_code = escape_markdown_v2(referral_code or "None")
        selected_number = escape_markdown_v2(selected_number or "None 📞")
        status = escape_markdown_v2(status or "pending")
        twilio_status = escape_markdown_v2("Active ✅") if twilio_sid else escape_markdown_v2("Not Set ❌")
        
        redemption_text = f"You can redeem {CREDITS_PER_SET} credits with {POINTS_PER_CREDIT_SET} points." if points >= POINTS_PER_CREDIT_SET else f"Earn {POINTS_PER_CREDIT_SET} points to redeem {CREDITS_PER_SET} credits."
        
        text = (
            f"👤 *Account Information*\n\n"
            f"👤 *Username:* {escape_markdown_v2(f'@{username}')}\n"
            f"🆔 *User ID:* {user_id}\n"
            f"💰 *Points:* {escape_markdown_v2(str(points))}\n"
            f"💳 *Credits:* {escape_markdown_v2(str(credits))}\n"
            f"🔑 *Account Status:* {escape_markdown_v2(twilio_status)}\n"
            f"📞 *Selected Number:* {escape_markdown_v2(selected_number)}\n"
            f"🛠️ *Account Status:* {escape_markdown_v2(status.capitalize())}\n"
            f"🎟️ *Referral Code:* `{escape_markdown_v2(referral_code)}`\n\n"
            f"_{escape_markdown_v2(redemption_text)}_\n"
            f"_Use /redeem \\<code\\> to redeem points for exclusive rewards\\._"
        )

        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        logger.debug(f"Account info shown for user {user_id}")
    except Exception as e:
        logger.error(f"Unexpected error in show_account for user {user_id}: {e}")
        await query.message.edit_text(
            f"⚠️ Error fetching account info: {str(e)}. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

@backoff.on_exception(backoff.expo, TwilioRestException, max_tries=3, giveup=lambda e: e.status != 429)
async def show_otps(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    now = time.time()
    if now - otp_rate_limit[user_id] < 30:
        remaining = int(30 - (now - otp_rate_limit[user_id]))
        await query.message.edit_text(
            f"⏳ Please wait {remaining} seconds before checking OTPs again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    otp_rate_limit[user_id] = now

    bot = Bot(token=BOT_TOKEN)
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT twilio_sid, twilio_token, selected_number, username FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data or not user_data[2]:
            await query.message.edit_text(
                "📞 No number selected. Please purchase a number first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        twilio_sid, twilio_token, selected_number, username = user_data
        try:
            client = Client(twilio_sid, twilio_token)
            messages = client.messages.list(to=selected_number, limit=5)
            if not messages:
                text = "🔐 Recent OTPs\n\nNo OTPs received yet. Please check back later."
            else:
                text = "🔐 Recent OTPs\n\n"
                valid_messages = []
                for msg in messages:
                    if msg.body and msg.body.strip():
                        received_time = msg.date_sent.strftime("%Y-%m-%d %H:%M:%S UTC") if msg.date_sent else datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                        c.execute(
                            "SELECT message_sid FROM processed_messages WHERE message_sid = ? AND user_id = ?",
                            (msg.sid, user_id)
                        )
                        if c.fetchone():
                            valid_messages.append(f"From {escape_markdown_v2(msg.from_)} at {escape_markdown_v2(received_time)}:\n{escape_markdown_v2(msg.body.strip())}")
                            continue
                        valid_messages.append(f"From {escape_markdown_v2(msg.from_)} at {escape_markdown_v2(received_time)}:\n{escape_markdown_v2(msg.body.strip())}")
                        c.execute(
                            "INSERT OR IGNORE INTO processed_messages (message_sid, user_id, phone_number, message_body, received_at) VALUES (?, ?, ?, ?, ?)",
                            (msg.sid, user_id, msg.from_, msg.body.strip(), received_time)
                        )
                        c.execute(
                            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                            (user_id, f"Received OTP: {msg.body.strip()} from {msg.from_} at {received_time}", received_time)
                        )
                        try:
                            await bot.send_message(
                                chat_id=OTP_GROUP_CHAT_ID,
                                text=escape_markdown_v2(
                                    f"📩 OTP for @{username} (ID: {user_id})\n\nFrom: {msg.from_}\nMessage: {msg.body.strip()}\nTime: {received_time}"
                                ),
                                parse_mode="MarkdownV2"
                            )
                            logger.debug(f"OTP {msg.sid} sent to group chat {OTP_GROUP_CHAT_ID} for user {user_id}")
                        except Exception as e:
                            logger.error(f"Error sending OTP to group chat {OTP_GROUP_CHAT_ID}: {e}")
                    else:
                        logger.warning(f"Empty or malformed OTP message from {msg.from_} for user {user_id}")
                if valid_messages:
                    text += "\n\n".join(valid_messages)
                else:
                    text += "No valid OTP messages found."
            conn.commit()
        except TwilioRestException as e:
            logger.error(f"Twilio error in show_otps for user {user_id}: {e} (Status: {e.status}, Code: {e.code})")
            if e.status == 429:
                await query.message.edit_text(
                    "⚠️ Too many requests. Please wait and try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            elif e.status == 402:
                await query.message.edit_text(
                    "⚠️ Twilio account has insufficient funds. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    f"⚠️ Twilio error (Code: {e.code}). Please try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"OTPs retrieved for user {user_id}: {len(messages)} messages processed")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in show_otps for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching OTPs. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in show_otps for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error fetching OTPs. Please try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

@backoff.on_exception(backoff.expo, TwilioRestException, max_tries=3, giveup=lambda e: e.status != 429)
async def get_numbers(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT points, credits, twilio_sid, status, username, twilio_token FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, credits, twilio_sid, status, username, twilio_token = user_data
        
        if credits < CREDIT_PER_NUMBER:
            await query.message.edit_text(
                f"❌ You need at least {CREDIT_PER_NUMBER} credit to purchase a number. Current credits: {credits}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        c.execute("SELECT twilio_sid, twilio_token FROM twilio_credentials WHERE used_by IS NULL LIMIT 1")
        available_credentials = c.fetchone()
        if not twilio_sid and not available_credentials:
            c.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                      (user_id, "Requested Twilio credentials", datetime.now().isoformat()))
            conn.commit()
            for admin_id in ADMIN_IDS:
                keyboard = [
                    [
                        InlineKeyboardButton("Approve ✅", callback_data=f"admin_approve_{user_id}"),
                        InlineKeyboardButton("Reject ❌", callback_data=f"admin_reject_{user_id}"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    admin_id,
                    f"📬 User @{username} (ID: {user_id}) has requested Twilio credentials (Points: {points}, Credits: {credits}).",
                    reply_markup=reply_markup,
                )
            await query.message.edit_text(
                "⏳ No Twilio credentials available. Your request is under review by the admin. Please wait.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Twilio request submitted by user {user_id} due to no available credentials")
            return

        if not twilio_sid:
            twilio_sid, twilio_token = available_credentials
            c.execute("UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?", (twilio_sid, twilio_token, user_id))
            c.execute("UPDATE twilio_credentials SET used_by = ?, created_at = ? WHERE twilio_sid = ?", (user_id, datetime.now().isoformat(), twilio_sid))
            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                      (user_id, f"Assigned Twilio SID {twilio_sid}", datetime.now().isoformat()))
            conn.commit()
            await context.bot.send_message(
                user_id,
                "✅ Twilio credentials have been automatically set for you. You can now purchase numbers!"
            )
            logger.debug(f"Automatically assigned Twilio SID {twilio_sid} to user {user_id}")

        try:
            client = Client(twilio_sid, twilio_token)
            numbers = client.available_phone_numbers("CA").local.list(limit=20)
            if not numbers:
                await query.message.edit_text(
                    "❌ No Canadian numbers available at the moment.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return
            text = (
                f"📞 Available Canadian Numbers\n\n"
                f"✅ You can purchase numbers with your {credits} credits (1 credit per number).\n"
                f"⚠️ Note: Purchasing a new number will release your current number (if any).\n\n"
                f"Please select a number from the messages below:"
            )
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            for num in numbers:
                keyboard = [[InlineKeyboardButton(f"📞 Buy {num.phone_number}", callback_data=f"select_number_{num.phone_number}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.message.reply_text(f"{num.phone_number}", reply_markup=reply_markup)
                await asyncio.sleep(0.5)
            logger.debug(f"Canadian numbers displayed for user {user_id}")
        except TwilioRestException as e:
            logger.error(f"Twilio error in get_numbers for user {user_id}: {e} (Status: {e.status}, Code: {e.code})")
            if e.status == 429:
                await query.message.edit_text(
                    "⚠️ Too many requests. Please wait and try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            elif e.status == 402:
                await query.message.edit_text(
                    "⚠️ Twilio account has insufficient funds. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    f"⚠️ Twilio error (Code: {e.code}). Please try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in get_numbers for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching numbers. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in get_numbers for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error fetching numbers. Please try again later.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

@backoff.on_exception(backoff.expo, TwilioRestException, max_tries=3, giveup=lambda e: e.status != 429)
async def select_number(query: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT credits, twilio_sid, twilio_token, selected_number, last_purchase_time, purchase_lock FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        credits, twilio_sid, twilio_token, selected_number, last_purchase_time, purchase_lock = user_data

        if purchase_lock:
            await query.message.edit_text(
                "⏳ A purchase is already in progress. Please wait a moment and try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        c.execute("SELECT phone_number FROM purchased_numbers WHERE user_id = ? AND phone_number = ?", (user_id, phone_number))
        if c.fetchone():
            await query.message.edit_text(
                f"❌ You have already purchased the number {phone_number}. Please select a different number.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        if last_purchase_time:
            last_purchase = datetime.fromisoformat(last_purchase_time)
            now = datetime.now()
            if now - last_purchase < timedelta(minutes=5):
                remaining_time = timedelta(minutes=5) - (now - last_purchase)
                minutes, seconds = divmod(int(remaining_time.total_seconds()), 60)
                await query.message.edit_text(
                    f"⏳ Please wait {minutes} minute(s) and {seconds} second(s) before purchasing another number.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return

        if credits < CREDIT_PER_NUMBER:
            await query.message.edit_text(
                f"❌ Insufficient credits to purchase number. Need {CREDIT_PER_NUMBER} credit.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        c.execute("UPDATE users SET purchase_lock = 1 WHERE user_id = ?", (user_id,))
        conn.commit()

        try:
            client = Client(twilio_sid, twilio_token)
            # Validate phone number
            try:
                lookup = client.lookups.v2.phone_numbers(phone_number).fetch()
                if not lookup.valid:
                    c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
                    conn.commit()
                    await query.message.edit_text(
                        f"❌ Invalid phone number {phone_number}. Please select a different number.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
            except TwilioRestException as e:
                logger.error(f"Lookup error for phone number {phone_number}: {e} (Status: {e.status}, Code: {e.code})")
                c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
                await query.message.edit_text(
                    f"⚠️ Error validating phone number (Code: {e.code}). Please try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return

            try:
                existing_numbers = client.incoming_phone_numbers.list()
                for number in existing_numbers:
                    number.delete()
                    logger.debug(f"Released number {number.phone_number} for user {user_id}")
            except TwilioRestException as e:
                logger.error(f"Error releasing previous Twilio number for user {user_id}: {e} (Status: {e.status}, Code: {e.code})")
                c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
                await query.message.edit_text(
                    f"⚠️ Error releasing previous number (Code: {e.code}). Please try again.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return
            try:
                incoming_number = client.incoming_phone_numbers.create(
                    phone_number=phone_number,
                    sms_url=WEBHOOK_URL
                )
                c.execute(
                    "UPDATE users SET selected_number = ?, credits = credits - ?, numbers_purchased = numbers_purchased + 1, last_purchase_time = ?, purchase_lock = 0 WHERE user_id = ?",
                    (phone_number, CREDIT_PER_NUMBER, datetime.now().isoformat(), user_id),
                )
                c.execute(
                    "INSERT INTO purchased_numbers (user_id, phone_number, purchased_at) VALUES (?, ?, ?)",
                    (user_id, phone_number, datetime.now().isoformat())
                )
                c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                          (user_id, f"Purchased number {phone_number} for {CREDIT_PER_NUMBER} credit", datetime.now().isoformat()))
                conn.commit()
                await query.message.edit_text(
                    f"✅ Successfully purchased number {phone_number}! {CREDIT_PER_NUMBER} credit deducted. Remaining credits: {credits - CREDIT_PER_NUMBER}.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug(f"Number {phone_number} purchased by user {user_id}")
            except TwilioRestException as e:
                logger.error(f"Twilio error in select_number for user {user_id}: {e} (Status: {e.status}, Code: {e.code})")
                c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
                if e.status == 429:
                    await query.message.edit_text(
                        "⚠️ Too many requests. Please wait and try again later.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                elif e.status == 402:
                    await query.message.edit_text(
                        "⚠️ Twilio account has insufficient funds. Please contact the admin.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                else:
                    await query.message.edit_text(
                        f"⚠️ Error purchasing number (Code: {e.code}). Please try again later.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                return
        except Exception as e:
            logger.error(f"Unexpected error in select_number for user {user_id}: {e}")
            c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            await query.message.edit_text(
                "⚠️ Error purchasing number. Please try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in select_number for user {user_id}: {e}")
        c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            "⚠️ Database error purchasing number. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in select_number for user {user_id}: {e}")
        c.execute("UPDATE users SET purchase_lock = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            "⚠️ Error purchasing number. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        if update.callback_query:
            await update.callback_query.message.edit_text(
                "🚫 Unauthorized access.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await update.message.reply_text(
                "🚫 Unauthorized access.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        logger.warning(f"Unauthorized admin access attempt by user {user_id}")
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        total_users = 0
        pending_users = []
        active_twilio = 0
        available_twilio = 0
        try:
            c.execute("SELECT COUNT(*) FROM users")
            total_users = c.fetchone()[0]
            c.execute("SELECT user_id, username, points, status FROM users WHERE status = 'pending'")
            pending_users = c.fetchall()
            c.execute("SELECT COUNT(*) FROM users WHERE twilio_sid IS NOT NULL")
            active_twilio = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM twilio_credentials WHERE used_by IS NULL")
            available_twilio = c.fetchone()[0]
        except sqlite3.OperationalError as e:
            logger.error(f"Database error in admin_panel for user {user_id}: {e}")
            text = "⚠️ Database error loading statistics. Some features may be limited.\n\nSelect an option:"
        else:
            text = (
                f"🔐 Admin Dashboard\n\n"
                f"📊 Total Users: {total_users}\n"
                f"⏳ Pending Requests: {len(pending_users)}\n"
                f"🔑 Active Twilio Users: {active_twilio}\n"
                f"🔑 Available Twilio Credentials: {available_twilio}\n\n"
                f"Please select an option:"
            )
        keyboard = [
            [InlineKeyboardButton("📊 View Twilio Users", callback_data="admin_view_users")],
            [InlineKeyboardButton("👥 Manage Users", callback_data="admin_manage_users")],
            [InlineKeyboardButton("⏳ Pending Requests", callback_data="admin_pending_requests")],
            [InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user")],
            [InlineKeyboardButton("🎟️ Set Redeem Code", callback_data="admin_set_redeem_code")],
            [InlineKeyboardButton("🔑 Add Twilio Credentials", callback_data="admin_add_twilio")],
            [InlineKeyboardButton("🔑 View Twilio Credentials", callback_data="admin_view_twilio")],
            [InlineKeyboardButton("✅ Bulk Approve", callback_data="admin_bulk_approve"),
             InlineKeyboardButton("❌ Bulk Reject", callback_data="admin_bulk_reject")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
        elif message_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup
            )
        else:
            sent_message = await update.message.reply_text(text, reply_markup=reply_markup)
            context.user_data["admin_message_id"] = sent_message.message_id
        logger.debug(f"Admin panel accessed by user {user_id}")
    except Exception as e:
        logger.error(f"Unexpected error in admin_panel for user {user_id}: {e}")
        if update.callback_query:
            await update.callback_query.message.edit_text(
                "⚠️ Error loading admin panel. Please try /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await update.message.reply_text(
                "⚠️ Error loading admin panel. Please try /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )

async def admin_panel_callback(query: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.warning(f"Unauthorized admin callback access by user {query.from_user.id}")
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        if data == "admin_view_users":
            c.execute("SELECT user_id, username, points, credits, twilio_sid, twilio_token, status FROM users WHERE twilio_sid IS NOT NULL")
            users = c.fetchall()
            if not users:
                await query.message.edit_text(
                    "😔 No users with Twilio credentials found.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No Twilio users found for admin view users")
                return
            text = f"📊 Twilio Users: {len(users)}\n\n"
            for user in users:
                user_id, username, points, credits, twilio_sid, twilio_token, status = user
                username = escape_markdown_v2(username or "No Username")
                text += (
                    f"User: \\@{username} \\(ID: {user_id}\\)\n"
                    f"Points: {points} 💰\n"
                    f"Credits: {credits} 💳\n"
                    f"Twilio SID: {escape_markdown_v2(twilio_sid if twilio_sid else 'Not Set ❌')}\n"
                    f"Twilio Token: {escape_markdown_v2(twilio_token if twilio_token else 'Not Set ❌')}\n"
                    f"Status: {escape_markdown_v2(status.capitalize())} 🛠️\n\n"
                )
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
                parse_mode="MarkdownV2"
            )
            logger.debug(f"Twilio users viewed by admin {query.from_user.id}")
        elif data == "admin_manage_users":
            c.execute("SELECT user_id, username, points, credits, status FROM users")
            users = c.fetchall()
            if not users:
                await query.message.edit_text(
                    "😔 No users found.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No users found for admin manage users")
                return
            keyboard = []
            for user in users:
                username = escape_markdown_v2(user[1] or "No Username")
                button_text = f"\\@{username} \\(ID: {user[0]}\\)"
                keyboard.append([
                    InlineKeyboardButton(button_text, callback_data=f"admin_manage_user_{user[0]}")
                ])
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(
                "👥 Select a user to manage:",
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )
            logger.debug(f"Manage users displayed for admin {query.from_user.id}")
        elif data == "admin_pending_requests":
            c.execute("SELECT user_id, username, points, credits, status FROM users WHERE status = 'pending'")
            pending_users = c.fetchall()
            if not pending_users:
                await query.message.edit_text(
                    "😊 No pending requests at this time.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No pending requests found")
                return
            text = "⏳ Pending Requests\n\n"
            keyboard = []
            for user_id, username, points, credits, status in pending_users:
                username = escape_markdown_v2(username or "No Username")
                keyboard.append([
                    InlineKeyboardButton(f"\\@{username} \\(ID: {user_id}\\)", callback_data=f"admin_manage_user_{user_id}"),
                    InlineKeyboardButton("✅", callback_data=f"admin_approve_{user_id}"),
                    InlineKeyboardButton("❌", callback_data=f"admin_reject_{user_id}"),
                ])
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
            logger.debug(f"Pending requests displayed for admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_panel_callback: {e}")
        await query.message.edit_text(
            "⚠️ Database error processing admin action. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_panel_callback: {e}")
        await query.message.edit_text(
            "⚠️ Error processing admin action. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_manage_user(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, points, credits, twilio_sid, twilio_token, selected_number, status FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"User {user_id} not found for admin manage")
            return
        username, points, credits, twilio_sid, twilio_token, selected_number, status = user_data
        username = escape_markdown_v2(username or "No Username")
        selected_number = escape_markdown_v2(selected_number or "None 📞")
        status = escape_markdown_v2(status or "pending")
        twilio_status = escape_markdown_v2("Active ✅") if twilio_sid else escape_markdown_v2("Not Set ❌")
        text = (
            f"👤 *User Information*\n\n"
            f"Username: \\@{username}\n"
            f"User ID: {user_id}\n"
            f"Points: {points} 💰\n"
            f"Credits: {credits} 💳\n"
            f"Account Status: {twilio_status}\n"
            f"Selected Number: {selected_number}\n"
            f"Status: {status.capitalize()} 🛠️"
        )
        keyboard = [
            [InlineKeyboardButton("💰 Update Points", callback_data=f"admin_set_points_{user_id}")],
            [InlineKeyboardButton("🔑 Set Twilio Credentials", callback_data=f"admin_set_twilio_{user_id}")],
            [InlineKeyboardButton("🗑️ Remove Twilio Credentials", callback_data=f"admin_remove_twilio_{user_id}")],
            [InlineKeyboardButton("📜 View Activity", callback_data=f"admin_view_activity_{user_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        logger.debug(f"User {user_id} management options shown to admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_manage_user for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error managing user. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_manage_user for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error managing user. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_view_activity(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, twilio_sid, twilio_token, selected_number FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        username, twilio_sid, twilio_token, selected_number = user_data
        username = escape_markdown_v2(username or "No Username")
        c.execute("SELECT action, timestamp FROM user_activity WHERE user_id = ? AND action LIKE 'Purchased number%' OR action LIKE '%refer%' OR action LIKE 'Redeemed code%' OR action LIKE 'Referred by%' OR action LIKE 'Earned 1 point%' OR action LIKE 'Purchased % credits%' OR action LIKE 'Claimed daily bonus%' ORDER BY timestamp DESC LIMIT 5", (user_id,))
        purchase_activities = c.fetchall()
        text = f"📜 Activity Log for user \\@{username} \\(ID: {user_id}\\)\n\n"
        if purchase_activities:
            text += "Number, Credit, and Referral Activities:\n" + "\n".join([f"{escape_markdown_v2(timestamp)}: {escape_markdown_v2(action)}" for action, timestamp in purchase_activities]) + "\n\n"
        else:
            text += "No number, credit, or referral activities.\n\n"
        if twilio_sid and twilio_token and selected_number:
            try:
                client = Client(twilio_sid, twilio_token)
                messages = client.messages.list(to=selected_number, limit=5)
                if messages:
                    text += "Received OTPs:\n" + "\n".join([f"{escape_markdown_v2(msg.date_sent.strftime('%Y-%m-%d %H:%M:%S UTC') if msg.date_sent else datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'))}: {escape_markdown_v2(msg.body.strip())}" for msg in messages if msg.body and msg.body.strip()])
                else:
                    text += "No OTPs received."
            except TwilioRestException as e:
                logger.error(f"Twilio error in admin_view_activity for user {user_id}: {e} (Status: {e.status}, Code: {e.code})")
                if e.status == 429:
                    text += "⚠️ Too many requests to Twilio API."
                elif e.status == 402:
                    text += "⚠️ Twilio account has insufficient funds."
                else:
                    text += f"⚠️ Twilio error (Code: {e.code})."
        else:
            text += "No OTPs received (Twilio not set or no number selected)."
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode="MarkdownV2"
        )
        logger.debug(f"Activity log for user {user_id} viewed by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_view_activity for user {user_id}: {str(e)}")
        await query.message.edit_text(
            "⚠️ Error occurred viewing activity. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_view_activity for user {user_id}: {str(e)}")
        await query.message.edit_text(
            "⚠️ Error viewing activity. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_view_twilio(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, twilio_sid, used_by, created_at FROM twilio_credentials")
        credentials = c.fetchall()
        if not credentials:
            await query.message.edit_text(
                "😔 No Twilio credentials found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug("No Twilio credentials found for admin view")
            return
        text = f"🔑 Twilio Credentials: {len(credentials)}\n\n"
        for cred_id, twilio_sid, used_by, created_at in credentials:
            status = "Available" if used_by is None else f"Used by User ID: {used_by}"
            text += (
                f"ID: {cred_id}\n"
                f"SID: {escape_markdown_v2(twilio_sid)}\n"
                f"Status: {escape_markdown_v2(status)}\n"
                f"Created: {escape_markdown_v2(created_at)}\n\n"
            )
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode="MarkdownV2"
        )
        logger.debug(f"Twilio credentials viewed by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_view_twilio: {e}")
        await query.message.edit_text(
            "⚠️ Database error viewing Twilio credentials. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_view_twilio: {e}")
        await query.message.edit_text(
            "⚠️ Error viewing Twilio credentials. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_bulk_approve(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE status = 'pending'")
        pending_users = c.fetchall()
        if not pending_users:
            await query.message.edit_text(
                "😊 No pending requests to approve.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute("SELECT twilio_sid, twilio_token FROM twilio_credentials WHERE used_by IS NULL LIMIT ?", (len(pending_users),))
        available_credentials = c.fetchall()
        if not available_credentials:
            await query.message.edit_text(
                "⚠️ No Twilio credentials available. Please add credentials first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug("No Twilio credentials available for bulk approval")
            return

        approved_count = 0
        for (user_id, username), (twilio_sid, twilio_token) in zip(pending_users, available_credentials):
            c.execute(
                "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                (twilio_sid, twilio_token, user_id)
            )
            c.execute(
                "UPDATE twilio_credentials SET used_by = ?, created_at = ? WHERE twilio_sid = ?",
                (user_id, datetime.now().isoformat(), twilio_sid)
            )
            c.execute(
                "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                (user_id, f"Twilio credentials assigned via bulk approve", datetime.now().isoformat())
            )
            try:
                await context.bot.send_message(
                    user_id,
                    "✅ Your Twilio credentials have been approved! You can now purchase numbers."
                )
                logger.debug(f"User {user_id} (@{username}) approved in bulk with Twilio SID {twilio_sid}")
                approved_count += 1
            except Exception as e:
                logger.error(f"Error notifying user {user_id} for bulk approval: {e}")

        conn.commit()
        await query.message.edit_text(
            f"✅ Successfully approved {approved_count} pending requests!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk approved {approved_count} users by admin {query.from_user.id}")

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Database error during bulk approval. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Error during bulk approval. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_bulk_reject(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE status = 'pending'")
        pending_users = c.fetchall()
        if not pending_users:
            await query.message.edit_text(
                "😊 No pending requests to reject.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        rejected_count = 0
        for user_id, username in pending_users:
            c.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (user_id,))
            c.execute(
                "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                (user_id, "Twilio credentials request rejected", datetime.now().isoformat())
            )
            try:
                await context.bot.send_message(
                    user_id,
                    "❌ Your Twilio credentials request has been rejected by the admin."
                )
                logger.debug(f"User {user_id} (@{username}) rejected in bulk")
                rejected_count += 1
            except Exception as e:
                logger.error(f"Error notifying user {user_id} for bulk rejection: {e}")

        conn.commit()
        await query.message.edit_text(
            f"❌ Successfully rejected {rejected_count} pending requests!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk rejected {rejected_count} users by admin {query.from_user.id}")

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Database error during bulk rejection. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Error during bulk rejection. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_reject(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        username = user_data[0]
        c.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (user_id,))
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, "Twilio credentials request rejected", datetime.now().isoformat())
        )
        conn.commit()
        try:
            await context.bot.send_message(
                user_id,
                "❌ Your Twilio credentials request has been rejected by the admin."
            )
            logger.debug(f"User {user_id} (@{username}) rejected by admin {query.from_user.id}")
        except Exception as e:
            logger.error(f"Error notifying user {user_id} for rejection: {e}")
        await query.message.edit_text(
            f"❌ User @{username} (ID: {user_id}) request rejected.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error rejecting user. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error rejecting user. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_remove_twilio(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, twilio_sid FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        username, twilio_sid = user_data
        if not twilio_sid:
            await query.message.edit_text(
                f"😔 User @{username} (ID: {user_id}) has no Twilio credentials to remove.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute(
            "UPDATE users SET twilio_sid = NULL, twilio_token = NULL, status = 'pending', selected_number = NULL WHERE user_id = ?",
            (user_id,)
        )
        c.execute(
            "UPDATE twilio_credentials SET used_by = NULL WHERE twilio_sid = ?",
            (twilio_sid,)
        )
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, "Twilio credentials removed by admin", datetime.now().isoformat())
        )
        conn.commit()
        try:
            await context.bot.send_message(
                user_id,
                "🗑️ Your Twilio credentials have been removed by the admin. Please request new credentials."
            )
            logger.debug(f"Twilio credentials removed for user {user_id} (@{username}) by admin {query.from_user.id}")
        except Exception as e:
            logger.error(f"Error notifying user {user_id} for Twilio removal: {e}")
        await query.message.edit_text(
            f"🗑️ Twilio credentials removed for user @{username} (ID: {user_id}).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_remove_twilio for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error removing Twilio credentials. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_remove_twilio for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error removing Twilio credentials. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text
    if not await check_subscription(update, context):
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()

        if context.user_data.get("awaiting_referral_code"):
            await handle_referral(update, context, message.strip())
            return

        if user_id in ADMIN_IDS:
            if context.user_data.get("approve_user_id"):
                if "," not in message:
                    await update.message.reply_text(
                        "⚠️ Invalid format. Please enter Twilio SID and Token (format: SID,Token).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                sid, token = message.split(",", 1)
                sid, token = sid.strip(), token.strip()
                if not validate_twilio_credentials(sid, token):
                    await update.message.reply_text(
                        "❌ Invalid Twilio credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                approve_user_id = context.user_data["approve_user_id"]
                c.execute("SELECT username FROM users WHERE user_id = ?", (approve_user_id,))
                user_data = c.fetchone()
                if not user_data:
                    await update.message.reply_text(
                        "😔 User not found.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                username = user_data[0]
                c.execute(
                    "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                    (sid, token, approve_user_id)
                )
                c.execute(
                    "INSERT OR REPLACE INTO twilio_credentials (twilio_sid, twilio_token, used_by, created_at) VALUES (?, ?, ?, ?)",
                    (sid, token, approve_user_id, datetime.now().isoformat())
                )
                c.execute(
                    "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                    (approve_user_id, f"Twilio credentials approved by admin", datetime.now().isoformat())
                )
                conn.commit()
                try:
                    await context.bot.send_message(
                        approve_user_id,
                        "✅ Your Twilio credentials have been approved! You can now purchase numbers."
                    )
                    logger.debug(f"Twilio credentials approved for user {approve_user_id} (@{username}) by admin {user_id}")
                except Exception as e:
                    logger.error(f"Error notifying user {approve_user_id} for approval: {e}")
                await update.message.reply_text(
                    f"✅ Twilio credentials approved for user @{username} (ID: {approve_user_id}).",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["approve_user_id"]
                return

            if context.user_data.get("set_points_user_id"):
                try:
                    points = int(message.strip())
                    if points < 0:
                        raise ValueError("Points cannot be negative")
                    set_points_user_id = context.user_data["set_points_user_id"]
                    c.execute("SELECT username FROM users WHERE user_id = ?", (set_points_user_id,))
                    user_data = c.fetchone()
                    if not user_data:
                        await update.message.reply_text(
                            "😔 User not found.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                        )
                        return
                    username = user_data[0]
                    c.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, set_points_user_id))
                    c.execute(
                        "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                        (set_points_user_id, f"Points set to {points} by admin", datetime.now().isoformat())
                    )
                    conn.commit()
                    try:
                        await context.bot.send_message(
                            set_points_user_id,
                            f"💰 Your points have been updated to {points} by the admin."
                        )
                        logger.debug(f"Points set to {points} for user {set_points_user_id} (@{username}) by admin {user_id}")
                    except Exception as e:
                        logger.error(f"Error notifying user {set_points_user_id} for points update: {e}")
                    await update.message.reply_text(
                        f"✅ Points updated to {points} for user @{username} (ID: {set_points_user_id}).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    del context.user_data["set_points_user_id"]
                except ValueError:
                    await update.message.reply_text(
                        "⚠️ Invalid points value. Please enter a valid number.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                return

            if context.user_data.get("set_twilio_user_id"):
                if "," not in message:
                    await update.message.reply_text(
                        "⚠️ Invalid format. Please enter Twilio SID and Token (format: SID,Token).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                sid, token = message.split(",", 1)
                sid, token = sid.strip(), token.strip()
                if not validate_twilio_credentials(sid, token):
                    await update.message.reply_text(
                        "❌ Invalid Twilio credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                set_twilio_user_id = context.user_data["set_twilio_user_id"]
                c.execute("SELECT username FROM users WHERE user_id = ?", (set_twilio_user_id,))
                user_data = c.fetchone()
                if not user_data:
                    await update.message.reply_text(
                        "😔 User not found.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                username = user_data[0]
                c.execute(
                    "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                    (sid, token, set_twilio_user_id)
                )
                c.execute(
                    "INSERT OR REPLACE INTO twilio_credentials (twilio_sid, twilio_token, used_by, created_at) VALUES (?, ?, ?, ?)",
                    (sid, token, set_twilio_user_id, datetime.now().isoformat())
                )
                c.execute(
                    "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                    (set_twilio_user_id, f"Twilio credentials set by admin", datetime.now().isoformat())
                )
                conn.commit()
                try:
                    await context.bot.send_message(
                        set_twilio_user_id,
                        "✅ Your Twilio credentials have been updated by the admin."
                    )
                    logger.debug(f"Twilio credentials set for user {set_twilio_user_id} (@{username}) by admin {user_id}")
                except Exception as e:
                    logger.error(f"Error notifying user {set_twilio_user_id} for Twilio update: {e}")
                await update.message.reply_text(
                    f"✅ Twilio credentials set for user @{username} (ID: {set_twilio_user_id}).",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["set_twilio_user_id"]
                return

            if context.user_data.get("set_redeem_code"):
                if "," not in message:
                    await update.message.reply_text(
                        "⚠️ Invalid format. Please enter code and points (format: code,points).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                code, points = message.split(",", 1)
                code, points = code.strip(), points.strip()
                try:
                    points = int(points)
                    if points <= 0:
                        raise ValueError("Points must be positive")
                    c.execute("SELECT code FROM redeem_codes WHERE code = ?", (code,))
                    if c.fetchone():
                        await update.message.reply_text(
                            "❌ Redeem code already exists. Please use a different code.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                        )
                        return
                    c.execute(
                        "INSERT INTO redeem_codes (code, points, created_at) VALUES (?, ?, ?)",
                        (code, points, datetime.now().isoformat())
                    )
                    c.execute(
                        "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                        (user_id, f"Created redeem code {code} for {points} points", datetime.now().isoformat())
                    )
                    conn.commit()
                    await update.message.reply_text(
                        f"✅ Redeem code {code} created for {points} points.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    logger.debug(f"Redeem code {code} created by admin {user_id}")
                    del context.user_data["set_redeem_code"]
                except ValueError:
                    await update.message.reply_text(
                        "⚠️ Invalid points value. Please enter a valid number.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                return

            if context.user_data.get("add_twilio"):
                if "," not in message:
                    await update.message.reply_text(
                        "⚠️ Invalid format. Please enter Twilio SID and Token (format: SID,Token).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                sid, token = message.split(",", 1)
                sid, token = sid.strip(), token.strip()
                if not validate_twilio_credentials(sid, token):
                    await update.message.reply_text(
                        "❌ Invalid Twilio credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                c.execute(
                    "INSERT OR IGNORE INTO twilio_credentials (twilio_sid, twilio_token, created_at) VALUES (?, ?, ?)",
                    (sid, token, datetime.now().isoformat())
                )
                c.execute(
                    "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                    (user_id, f"Added Twilio credentials SID {sid}", datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ Twilio credentials (SID: {sid}) added successfully.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug(f"Twilio credentials SID {sid} added by admin {user_id}")
                del context.user_data["add_twilio"]
                return

            if context.user_data.get("search_user_active"):
                search_query = message.strip()
                c.execute(
                    "SELECT user_id, username, points, credits, status FROM users WHERE user_id = ? OR username = ? OR username = ?",
                    (search_query, search_query, search_query.lstrip("@"))
                )
                user_data = c.fetchone()
                if not user_data:
                    await update.message.reply_text(
                        "😔 User not found.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    del context.user_data["search_user_active"]
                    return
                user_id, username, points, credits, status = user_data
                username = escape_markdown_v2(username or "No Username")
                text = (
                    f"👤 *User Search Result*\n\n"
                    f"Username: \\@{username}\n"
                    f"User ID: {user_id}\n"
                    f"Points: {points} 💰\n"
                    f"Credits: {credits} 💳\n"
                    f"Status: {escape_markdown_v2(status.capitalize())} 🛠️"
                )
                keyboard = [
                    [InlineKeyboardButton("Manage User", callback_data=f"admin_manage_user_{user_id}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
                ]
                await update.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="MarkdownV2"
                )
                logger.debug(f"User {user_id} (@{username}) found via search by admin {update.effective_user.id}")
                del context.user_data["search_user_active"]
                return

        await update.message.reply_text(
            "⚠️ Please select an option from the menu.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_message for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error processing your request. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in handle_message for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error processing your request. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def twilio_webhook(request: web.Request):
    try:
        form_data = await request.post()
        message_sid = form_data.get("MessageSid")
        from_number = form_data.get("From")
        to_number = form_data.get("To")
        message_body = form_data.get("Body")
        received_at = datetime.now().isoformat()

        if not message_body or not message_body.strip():
            logger.warning(f"Empty message received in webhook: SID {message_sid}")
            return web.Response(status=200)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE selected_number = ?", (to_number,))
        user_data = c.fetchone()
        if not user_data:
            logger.warning(f"No user found for number {to_number} in webhook")
            return web.Response(status=200)

        user_id, username = user_data
        c.execute(
            "INSERT OR IGNORE INTO processed_messages (message_sid, user_id, phone_number, message_body, received_at) VALUES (?, ?, ?, ?, ?)",
            (message_sid, user_id, from_number, message_body.strip(), received_at)
        )
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, f"Received webhook OTP from {from_number}: {message_body.strip()}", received_at)
        )
        conn.commit()

        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(
                chat_id=OTP_GROUP_CHAT_ID,
                text=escape_markdown_v2(
                    f"📩 OTP for @{username} (ID: {user_id})\n\nFrom: {from_number}\nMessage: {message_body.strip()}\nTime: {received_at}"
                ),
                parse_mode="MarkdownV2"
            )
            logger.debug(f"Webhook OTP for user {user_id} sent to group chat {OTP_GROUP_CHAT_ID}")
        except Exception as e:
            logger.error(f"Error sending webhook OTP to group chat {OTP_GROUP_CHAT_ID}: {e}")

        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Error in twilio_webhook: {e}")
        return web.Response(status=500)

async def twilio_debugger(request: web.Request):
    try:
        data = await request.text()
        logger.debug(f"Twilio Debugger Webhook: {data}")
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Error in twilio_debugger: {e}")
        return web.Response(status=500)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and (update.message or update.callback_query):
        target = update.message or update.callback_query.message
        await target.reply_text(
            "⚠️ An error occurred. Please try again or contact @imvasupareek.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def setup_bot():
    try:
        bot = Bot(token=BOT_TOKEN)
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("menu", "Show main menu"),
            BotCommand("redeem", "Redeem a code for points")
        ]
        await bot.set_my_commands(commands)
        logger.debug("Bot commands set successfully")
    except Exception as e:
        logger.error(f"Error setting up bot commands: {e}")

import asyncio
import nest_asyncio

# Apply nest_asyncio to allow nested event loops in Alif IDE
nest_asyncio.apply()

async def main():
    try:
        init_db()
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("menu", menu))
        app.add_handler(CommandHandler("redeem", redeem))
        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(error_handler)

        web_app = web.Application()
        web_app.router.add_post("/twilio-webhook", twilio_webhook)
        web_app.router.add_post("/twilio-debugger", twilio_debugger)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()

        await setup_bot()
        await app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise

if __name__ == "__main__":
    # Get or create a new event loop
    loop = asyncio.get_event_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        # Ensure proper cleanup
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
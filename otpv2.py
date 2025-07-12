import sqlite3
from aiohttp import web
import logging
import time
import asyncio
import os
import threading
import uuid
from telegram import Bot
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"  # Removed < and >
    return ''.join(['\\' + c if c in escape_chars else c for c in text])

# Logging setup
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)
logger = logging.getLogger(__name__)

# Database connection pool
db_local = threading.local()
import re

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)



def get_db_connection():
    if not hasattr(db_local, "connection"):
        db_local.connection = sqlite3.connect("bot.db", timeout=10, check_same_thread=False)
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
            with sqlite3.connect("bot.db", timeout=10) as conn:
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
                    numbers_purchased INTEGER DEFAULT 0
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
BOT_TOKEN = "7905683098:AAGsm8_qFqxMcRYotSGZVXg0Ags6ZvueD20"
FORCE_SUB_CHANNEL = "@darkdorking"
ADMIN_IDS = [6972264549]
OTP_GROUP_CHAT_ID = "-1001900843229"
POINTS_PER_CREDIT_SET = 15  # Points required for 3 credits
CREDITS_PER_SET = 3  # Credits received for 15 points
CREDIT_PER_NUMBER = 1  # Credits required per number
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-public-url.ngrok.io/twilio-webhook")

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
            "INSERT OR IGNORE INTO users (user_id, username, points, credits, referral_code, numbers_purchased) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, 0, 0, referral_code, 0)
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

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None, text="🌟 Wlcome To DDxOTP Bot\n\nPlease select an option to proceed:"):
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

async def show_otps(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
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
                        valid_messages.append(f"From {escape_markdown_v2(msg.from_)} at {escape_markdown_v2(received_time)}:\n{escape_markdown_v2(msg.body.strip())}")
                        c.execute(
                            "INSERT OR IGNORE INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                            (
                                user_id,
                                f"Received OTP: {msg.body.strip()} from {msg.from_} at {received_time}",
                                received_time
                            )
                        )
                    else:
                        logger.warning(f"Empty or malformed OTP message from {msg.from_} for user {user_id}")
                if valid_messages:
                    text += "\n\n".join(valid_messages)
                else:
                    text += "No valid OTP messages found."
            conn.commit()
        except TwilioRestException as e:
            logger.error(f"Twilio error in show_otps for user {user_id}: {e}")
            if e.status == 402:
                await query.message.edit_text(
                    "⚠️ Api account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error fetching OTPs from Api. Please try again later.",
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

        # Check for available Twilio credentials
        c.execute("SELECT twilio_sid, twilio_token FROM twilio_credentials WHERE used_by IS NULL LIMIT 1")
        available_credentials = c.fetchone()
        if not twilio_sid and not available_credentials:
            c.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                      (user_id, "Requested Api credentials", datetime.now().isoformat()))
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
                "⏳ No Api credentials available. Your request is under review by the admin. Please wait.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Api request submitted by user {user_id} due to no available credentials")
            return

        # Assign Twilio credentials if not already set
        if not twilio_sid:
            twilio_sid, twilio_token = available_credentials
            c.execute("UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?", (twilio_sid, twilio_token, user_id))
            c.execute("UPDATE twilio_credentials SET used_by = ?, created_at = ? WHERE twilio_sid = ?", (user_id, datetime.now().isoformat(), twilio_sid))
            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                      (user_id, f"Assigned Twilio SID {twilio_sid}", datetime.now().isoformat()))
            conn.commit()
            await context.bot.send_message(
                user_id,
                "✅ Api credentials have been automatically set for you. You can now purchase numbers!"
            )
            logger.debug(f"Automatically assigned Api SID {twilio_sid} to user {user_id}")

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
                await asyncio.sleep(0.5)  # Small delay to avoid Telegram rate limits
            logger.debug(f"Canadian numbers displayed for user {user_id}")
        except TwilioRestException as e:
            logger.error(f"Api error in get_numbers for user {user_id}: {e}")
            if e.status == 402:
                await query.message.edit_text(
                    "⚠️ Api account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error fetching numbers from API. Please try again later.",
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

async def select_number(query: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT credits, twilio_sid, twilio_token, selected_number FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        credits, twilio_sid, twilio_token, selected_number = user_data
        if credits < CREDIT_PER_NUMBER:
            await query.message.edit_text(
                f"❌ Insufficient credits to purchase number. Need {CREDIT_PER_NUMBER} credit.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        client = Client(twilio_sid, twilio_token)
        try:
         existing_numbers = client.incoming_phone_numbers.list()
         for number in existing_numbers:
           number.delete()
           logger.debug(f"Released number {number.phone_number} for user {user_id}")

        
        except Exception as e:
            
            logger.error(f"Error releasing previous Api number for user {user_id}: {e}")
            await query.message.edit_text(
                "⚠️ Error releasing previous number. Please try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        try:
            incoming_number = client.incoming_phone_numbers.create(
                phone_number=phone_number,
                sms_url=WEBHOOK_URL
            )
            c.execute(
                "UPDATE users SET selected_number = ?, credits = credits - ?, numbers_purchased = numbers_purchased + 1 WHERE user_id = ?",
                (phone_number, CREDIT_PER_NUMBER, user_id),
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
            logger.error(f"Api error in select_number for user {user_id}: {e}")
            if e.status == 402:
                await query.message.edit_text(
                    "⚠️ Api account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error purchasing number from Api. Please try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in select_number for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error purchasing number. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in select_number for user {user_id}: {e}")
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
                logger.error(f"Twilio error in admin_view_activity for user {user_id}: {e}")
                if e.status == 402:
                    text += "⚠️ Twilio account has insufficient credits."
                else:
                    text += "⚠️ Error fetching OTPs from Twilio."
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
            return
        approved_count = 0
        for (user_id, username), (twilio_sid, twilio_token) in zip(pending_users, available_credentials):
            c.execute(
                "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                (twilio_sid, twilio_token, user_id),
            )
            c.execute(
                "UPDATE twilio_credentials SET used_by = ?, created_at = ? WHERE twilio_sid = ?",
                (user_id, datetime.now().isoformat(), twilio_sid),
            )
            try:
                await context.bot.send_message(user_id, "✅ Your Api credentials have been set. You can now get numbers!")
            except Exception as e:
                logger.error(f"Error notifying user {user_id}: {e}")
            approved_count += 1
        conn.commit()
        await query.message.edit_text(
            f"✅ Bulk approved {approved_count} users.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk approve completed by admin {query.from_user.id} for {approved_count} users")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Database error initiating bulk approve. Please try /start again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Error initiating bulk approve. Please try again.",
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
        c.execute("UPDATE users SET status = 'rejected' WHERE status = 'pending'")
        conn.commit()
        for user_id, username in pending_users:
            try:
                await context.bot.send_message(user_id, "❌ Your request was rejected by the admin.")
            except Exception as e:
                logger.error(f"Error notifying user {user_id}: {e}")
        await query.message.edit_text(
            f"❌ Successfully rejected {len(pending_users)} pending requests",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk reject completed by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Database error rejecting requests. Please try /start again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Error rejecting requests. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_approve(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    try:
        user_id = int(query.data.split("_")[2])
        context.user_data["approve_user_id"] = user_id
        await query.message.edit_text(
            "🔍 Please enter Twilio SID and Token for approval (format: SID,Token):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Admin {query.from_user.id} prompted to set Twilio credentials for user {user_id}")
    except Exception as e:
        logger.error(f"Error in admin_approve for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error initiating approval. Please try /start again.",
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
        c.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            f"User {user_id} rejected successfully.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        try:
            await context.bot.send_message(user_id, "❌ Your request was rejected by the admin.")
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
        logger.debug(f"User {user_id} rejected by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error rejecting user. Please try /start again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error handling user rejection. Please try again.",
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
        c.execute("SELECT twilio_sid FROM users WHERE user_id = ?", (user_id,))
        twilio_sid = c.fetchone()[0]
        if twilio_sid:
            c.execute("UPDATE twilio_credentials SET used_by = NULL, created_at = NULL WHERE twilio_sid = ?", (twilio_sid,))
        c.execute("UPDATE users SET twilio_sid = NULL, twilio_token = NULL, status = 'pending', selected_number = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            f"Twilio credentials for user {user_id} removed successfully.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        try:
            await context.bot.send_message(user_id, "❌ Your Api credentials have been removed by the admin. Please request again.")
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
        logger.debug(f"Api credentials removed for user {user_id} by admin {query.from_user.id}")
    except Exception as e:
        logger.error(f"Error in admin_remove_twilio for user {user_id}: {str(e)}")
        await query.message.edit_text(
            f"⚠️ Error removing Api credentials: {str(e)}. Try /start again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return

    user_id = update.message.from_user.id
    message = update.message.text.strip()
    logger.debug(f"Text message from user {user_id}: {message}")

    try:
        conn = get_db_connection()
        c = conn.cursor()

        if context.user_data.get("awaiting_referral_code"):
            await handle_referral(update, context, message)
            return

        if user_id in ADMIN_IDS and "set_points_user_id" in context.user_data:
            try:
                points = int(message)
                if points < 0:
                    await update.message.reply_text(
                        "⚠️ Points must be non-negative.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                target_user_id = context.user_data["set_points_user_id"]
                c.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, target_user_id))
                conn.commit()
                await update.message.reply_text(
                    f"✅ Points updated to {points} for user {target_user_id}.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                try:
                    await context.bot.send_message(target_user_id, f"✅ Your points have been updated to {points} by an admin.")
                except Exception as e:
                    logger.error(f"Error notifying user {target_user_id}: {e}")
                del context.user_data["set_points_user_id"]
                logger.debug(f"Admin {user_id} set points to {points} for user {target_user_id}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter a valid number for points.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        if user_id in ADMIN_IDS and "set_twilio_user_id" in context.user_data:
            try:
                twilio_sid, twilio_token = message.split(",")
                twilio_sid = twilio_sid.strip()
                twilio_token = twilio_token.strip()
                if not validate_twilio_credentials(twilio_sid, twilio_token):
                    await update.message.reply_text(
                        "⚠️ Invalid Api credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                target_user_id = context.user_data["set_twilio_user_id"]
                c.execute("SELECT twilio_sid FROM users WHERE user_id = ?", (target_user_id,))
                old_twilio_sid = c.fetchone()[0]
                if old_twilio_sid:
                    c.execute("UPDATE twilio_credentials SET used_by = NULL, created_at = NULL WHERE twilio_sid = ?", (old_twilio_sid,))
                c.execute(
                    "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                    (twilio_sid, twilio_token, target_user_id)
                )
                c.execute(
                    "INSERT OR REPLACE INTO twilio_credentials (twilio_sid, twilio_token, used_by, created_at) VALUES (?, ?, ?, ?)",
                    (twilio_sid, twilio_token, target_user_id, datetime.now().isoformat())
                )
                c.execute(
                    "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                    (target_user_id, f"Api credentials set by admin: {twilio_sid}", datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ Api credentials set for user {target_user_id}.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                try:
                    await context.bot.send_message(
                        target_user_id,
                        "Account Activated✅ Your Api credentials have been updated by an admin. You can now purchase numbers!"
                    )
                except Exception as e:
                    logger.error(f"Error notifying user {target_user_id}: {e}")
                del context.user_data["set_twilio_user_id"]
                logger.debug(f"Admin {user_id} set Twilio credentials for user {target_user_id}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter Twilio SID and Token in the format: SID,Token",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        if user_id in ADMIN_IDS and "approve_user_id" in context.user_data:
            try:
                twilio_sid, twilio_token = message.split(",")
                twilio_sid = twilio_sid.strip()
                twilio_token = twilio_token.strip()
                if not validate_twilio_credentials(twilio_sid, twilio_token):
                    await update.message.reply_text(
                        "⚠️ Invalid Twilio credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                target_user_id = context.user_data["approve_user_id"]
                c.execute("SELECT twilio_sid FROM users WHERE user_id = ?", (target_user_id,))
                old_twilio_sid = c.fetchone()[0]
                if old_twilio_sid:
                    c.execute("UPDATE twilio_credentials SET used_by = NULL, created_at = NULL WHERE twilio_sid = ?", (old_twilio_sid,))
                c.execute(
                    "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                    (twilio_sid, twilio_token, target_user_id)
                )
                c.execute(
                    "INSERT OR REPLACE INTO twilio_credentials (twilio_sid, twilio_token, used_by, created_at) VALUES (?, ?, ?, ?)",
                    (twilio_sid, twilio_token, target_user_id, datetime.now().isoformat())
                )
                c.execute(
                    "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                    (target_user_id, f"Api credentials approved by admin: {twilio_sid}", datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ User {target_user_id} approved with Api credentials.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                try:
                    await context.bot.send_message(
                        target_user_id,
                        "✅ Your Api credentials have been approved. You can now purchase numbers!"
                    )
                except Exception as e:
                    logger.error(f"Error notifying user {target_user_id}: {e}")
                del context.user_data["approve_user_id"]
                logger.debug(f"Admin {user_id} approved user {target_user_id} with Api credentials")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter Twilio SID and Token in the format: SID,Token",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        if user_id in ADMIN_IDS and "set_redeem_code" in context.user_data:
            try:
                code, points = message.split(",")
                code = code.strip()
                points = int(points.strip())
                if points < 1:
                    await update.message.reply_text(
                        "⚠️ Points must be positive.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                c.execute("INSERT OR REPLACE INTO redeem_codes (code, points, created_at) VALUES (?, ?, ?)",
                          (code, points, datetime.now().isoformat()))
                conn.commit()
                await update.message.reply_text(
                    f"✅ Redeem code '{code}' set with {points} points.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["set_redeem_code"]
                logger.debug(f"Admin {user_id} set redeem code {code} with {points} points")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter code and points in the format: code,points",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        if user_id in ADMIN_IDS and "add_twilio" in context.user_data:
            try:
                twilio_sid, twilio_token = message.split(",")
                twilio_sid = twilio_sid.strip()
                twilio_token = twilio_token.strip()
                if not validate_twilio_credentials(twilio_sid, twilio_token):
                    await update.message.reply_text(
                        "⚠️ Invalid Twilio credentials. Please try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                c.execute(
                    "INSERT OR REPLACE INTO twilio_credentials (twilio_sid, twilio_token, created_at) VALUES (?, ?, ?)",
                    (twilio_sid, twilio_token, datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ Twilio credentials {twilio_sid} added successfully.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["add_twilio"]
                logger.debug(f"Admin {user_id} added Twilio credentials {twilio_sid}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter Twilio SID and Token in the format: SID,Token",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        if user_id in ADMIN_IDS and "search_user_active" in context.user_data:
            search_term = message.strip()
            try:
                if search_term.isdigit():
                    c.execute("SELECT user_id, username, points, credits, status FROM users WHERE user_id = ?", (int(search_term),))
                else:
                    username = search_term.lstrip("@")
                    c.execute("SELECT user_id, username, points, credits, status FROM users WHERE username = ?", (username,))
                user_data = c.fetchone()
                if not user_data:
                    await update.message.reply_text(
                        "😔 User not found.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    logger.debug(f"User search by admin {user_id} for {search_term} returned no results")
                    return
                user_id, username, points, credits, status = user_data
                username = escape_markdown_v2(username or "No Username")
                status = escape_markdown_v2(status or "pending")
                text = (
                    f"👤 *User Found*\n\n"
                    f"Username: \\@{username}\n"
                    f"User ID: {user_id}\n"
                    f"Points: {points} 💰\n"
                    f"Credits: {credits} 💳\n"
                    f"Status: {status.capitalize()} 🛠️"
                )
                keyboard = [
                    [InlineKeyboardButton("👤 Manage User", callback_data=f"admin_manage_user_{user_id}")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")]
                ]
                await update.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="MarkdownV2"
                )
                del context.user_data["search_user_active"]
                logger.debug(f"User search by admin {user_id} for {search_term} successful")
            except sqlite3.OperationalError as e:
                logger.error(f"Database error in user search for {search_term}: {e}")
                await update.message.reply_text(
                    "⚠️ Database error during search. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            return

        await update.message.reply_text(
            "⚠️ Invalid command or input. Please use the menu options.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_text for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error processing your request. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in handle_text for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error processing your request. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        
        
async def forward_otps_to_group_periodically(application):
    while True:
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT user_id, twilio_sid, twilio_token, selected_number, username FROM users WHERE twilio_sid IS NOT NULL AND selected_number IS NOT NULL")
            users = c.fetchall()
            for user_id, sid, token, number, username in users:
                client = Client(sid, token)
                messages = client.messages.list(to=number, limit=5)
                for msg in messages:
                    received_time = msg.date_sent.strftime("%Y-%m-%d %H:%M:%S UTC") if msg.date_sent else datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
                    c.execute("SELECT 1 FROM processed_messages WHERE message_sid = ?", (msg.sid,))
                    if not c.fetchone():
                        masked_number = number[-10:-6] + "******"  # e.g. last 10 digits: 8388392216 → 8388******
                        await application.bot.send_message(
                            OTP_GROUP_CHAT_ID,
                            f"🔐 OTP for @{username} ({masked_number}):\n{msg.body.strip()}"
                             )

                        c.execute(
                            "INSERT INTO processed_messages (message_sid, user_id, phone_number, message_body, received_at) VALUES (?, ?, ?, ?, ?)",
                            (msg.sid, user_id, msg.from_, msg.body.strip(), received_time)
                        )
            conn.commit()
        except Exception as e:
            logger.error(f"Error in background OTP forwarder: {e}")
        await asyncio.sleep(10)  # Wait 10 seconds before next fetch
        

async def twilio_webhook(request: web.Request):
    try:
        xml_data = await request.text()
        logger.debug(f"Received Twilio webhook data: {xml_data}")
        root = ET.fromstring(xml_data)
        message_sid = root.find("MessageSid").text if root.find("MessageSid") is not None else None
        user_phone_number = root.find("To").text if root.find("To") is not None else None
        message_body = root.find("Body").text if root.find("Body") is not None else None
        from_number = root.find("From").text if root.find("From") is not None else None
        received_at = datetime.now().isoformat()

        if not all([message_sid, user_phone_number, message_body, from_number]):
            logger.error(f"Incomplete Twilio webhook data: SID={message_sid}, To={user_phone_number}, Body={message_body}, From={from_number}")
            return web.Response(status=400)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE selected_number = ?", (user_phone_number,))
        user = c.fetchone()
        if not user:
            logger.warning(f"No user found for phone number {user_phone_number}")
            return web.Response(status=404)

        user_id, username = user
        logger.debug(f"Found user {user_id} (@{username}) for phone {user_phone_number}")

        # Check if message has already been processed
        c.execute("SELECT message_sid FROM processed_messages WHERE message_sid = ?", (message_sid,))
        if c.fetchone():
            logger.debug(f"Message {message_sid} already processed for user {user_id}")
            return web.Response(status=200)

        # Store message in processed_messages table
        c.execute(
            "INSERT OR IGNORE INTO processed_messages (message_sid, user_id, phone_number, message_body, received_at) VALUES (?, ?, ?, ?, ?)",
            (message_sid, user_id, user_phone_number, message_body, received_at)
        )
        c.execute(
            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
            (user_id, f"Received OTP from {from_number}: {message_body}", received_at)
        )
        conn.commit()
        logger.debug(f"OTP {message_sid} stored in database for user {user_id}")

        bot = Bot(token=BOT_TOKEN)
        # Send OTP to individual user
        try:
            await bot.send_message(
                chat_id=user_id,
                text=escape_markdown_v2(
                    f"📩 New OTP received!\n\nFrom: {from_number}\nMessage: {message_body}\nTime: {received_at}"
                ),
                parse_mode="MarkdownV2"
            )
            logger.debug(f"OTP sent to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending OTP to user {user_id}: {e}", exc_info=True)

        # Send OTP to group chat
        try:
            logger.debug(f"Attempting to send OTP to group chat {OTP_GROUP_CHAT_ID}")
            await bot.send_message(
                chat_id=OTP_GROUP_CHAT_ID,
                text=escape_markdown_v2(
                    f"📩 OTP for @{username} (ID: {user_id})\n\nFrom: {from_number}\nMessage: {message_body}\nTime: {received_at}"
                ),
                parse_mode="MarkdownV2"
            )
            logger.debug(f"OTP successfully sent to group chat {OTP_GROUP_CHAT_ID} for user {user_id}")
        except Exception as e:
            logger.error(f"Error sending OTP to group chat {OTP_GROUP_CHAT_ID}: {e}", exc_info=True)

        logger.debug(f"Webhook processed successfully for user {user_id}: {message_body}")
        return web.Response(status=200)
    except ET.ParseError:
        logger.error(f"Failed to parse Twilio webhook XML: {xml_data}", exc_info=True)
        return web.Response(status=400)
    except Exception as e:
        logger.error(f"Unexpected error in twilio_webhook: {e}", exc_info=True)
        return web.Response(status=500)  
           
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    try:
        if update and (update.message or update.callback_query):
            target = update.callback_query.message if update.callback_query else update.message
            await target.reply_text(
                "⚠️ An error occurred. Please try /start or contact @imvasupareek.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
    except Exception as e:
        logger.error(f"Error in error_handler: {e}")
            
            
async def on_startup(app):
    app.create_task(forward_otps_to_group_periodically(app))

from aiohttp import web
from telegram import Update

async def health_check(request):
    return web.Response(text="OK", status=200)

async def telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.update_queue.put(update)
    return web.Response(text="OK")

async def send_startup_test_message():
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=OTP_GROUP_CHAT_ID,
            text=escape_markdown_v2("✅ Bot started successfully!"),
            parse_mode="MarkdownV2"
        )
        logger.info(f"Test message sent to group chat {OTP_GROUP_CHAT_ID} on bot startup")
    except Exception as e:
        logger.error(f"Error sending test message: {e}", exc_info=True)

async def setup_webhook():
    await app.bot.set_webhook(f"{WEBHOOK_URL}/telegram-webhook")

async def start_bot():
    init_db()

    global app
    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()

    # Start background OTP forwarder
    app.create_task(forward_otps_to_group_periodically(app))

    # Send startup test message
    await send_startup_test_message()

    # Set Telegram webhook
    await setup_webhook()

    # Start webserver
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_post("/telegram-webhook", telegram_webhook)
    web_app.router.add_post("/twilio-webhook", twilio_webhook)  # Already defined by you

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    await site.start()
    logger.info("🚀 Webserver running on port 8080")

if __name__ == "__main__":
    import asyncio
    asyncio.run(start_bot())

import sqlite3
from aiohttp import web
import logging
import time
import asyncio
import os
import threading
import uuid
from datetime import datetime
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
                    referred_by INTEGER,
                    twilio_sid TEXT,
                    twilio_token TEXT,
                    selected_number TEXT,
                    status TEXT DEFAULT 'pending',
                    referral_code TEXT UNIQUE
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
                c.execute("PRAGMA table_info(users)")
                columns = [col[1] for col in c.fetchall()]
                if "username" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN username TEXT")
                if "referral_code" not in columns:
                    c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT UNIQUE")
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
# TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are for fallback/testing; user-specific credentials are in DB
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FORCE_SUB_CHANNEL = "@darkdorking"
ADMIN_IDS = [6972264549]
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Fetch from environment variable
PORT = int(os.getenv("PORT", 8443))

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
            "INSERT OR IGNORE INTO users (user_id, username, points, referral_code) VALUES (?, ?, ?, ?)",
            (user_id, username, 0, referral_code)
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

    # Check if user already has a referrer
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
                "📢 To use OTP Bot, please join our official channel first.", reply_markup=reply_markup
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking subscription for user {user.id}: {e}")
        await update.message.reply_text("⚠️ Unable to verify channel subscription. Please try again later.")
        return False

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None, text="🌟 OTP Bot\n\nPlease select an option to proceed:"):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
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
        f"Share this code with your friends!"
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

        if data == "account":
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
        logger.error(f"Error in button callback for user {user_id}: {str(e)}")
        await query.message.edit_text(
            f"⚠️ An error occurred: {str(e)}. Please try /start.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )


def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!<>"
    return ''.join(['\\' + c if c in escape_chars else c for c in text])

async def show_account(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT points, twilio_sid, selected_number, status, username, referral_code FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return

        points, twilio_sid, selected_number, status, username, referral_code = user_data

        # Sanitize all fields for MarkdownV2
        username = escape_markdown_v2(username or "No Username")
        referral_code = escape_markdown_v2(referral_code or "None")
        selected_number = escape_markdown_v2(selected_number or "None 📞")
        status = escape_markdown_v2(status or "pending")
        twilio_status = escape_markdown_v2("Active ✅") if twilio_sid else escape_markdown_v2("Not Set ❌")


        text = (
    f"👤 *Account Information*\n\n"
    f"👤 *Username:* \\@{username}\n"
    f"🆔 *User ID:* {user_id}\n"
    f"💰 *Points:* {escape_markdown_v2(str(points))}\n"

    f"🔑 *Twilio Status:* {twilio_status}\n"
    f"📞 *Selected Number:* {selected_number}\n"
    f"🛠️ *Account Status:* {status.capitalize()}\n"
    f"🎟️ *Referral Code:* `{referral_code}`\n\n"
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
                        valid_messages.append(f"From {msg.from_} at {received_time}:\n{msg.body.strip()}")
                        c.execute(
                            "INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
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
                    "⚠️ Twilio account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error fetching OTPs from Twilio. Please try again later.",
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
        c.execute("SELECT points, twilio_sid, status, username, twilio_token FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, twilio_sid, status, username, twilio_token = user_data
        if points < 15:
            await query.message.edit_text(
                f"❌ You need at least 15 points to purchase a number. Current points: {points}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        if status == "pending" or not twilio_sid:
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
                    f"📬 User @{username} (ID: {user_id}) has requested Twilio credentials (Points: {points}).",
                    reply_markup=reply_markup,
                )
            await query.message.edit_text(
                "⏳ Your request for Twilio credentials is under review by the admin.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Twilio request submitted by user {user_id}")
            return
        try:
            client = Client(twilio_sid, twilio_token)
            numbers = client.available_phone_numbers("CA").local.list(limit=20)
            if not numbers:
                await query.message.edit_text(
                    "❌ No Canadian numbers available at the moment.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return
            await query.message.edit_text(
                "📞 Available Canadian Numbers:\n\nPlease select a number from the messages below:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            for num in numbers:
                keyboard = [[InlineKeyboardButton(f"Buy {num.phone_number}", callback_data=f"select_number_{num.phone_number}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.message.reply_text(f"📞 {num.phone_number}", reply_markup=reply_markup)
            logger.debug(f"Canadian numbers displayed for user {user_id}")
        except TwilioRestException as e:
            logger.error(f"Twilio error in get_numbers for user {user_id}: {e}")
            if e.status == 402:
                await query.message.edit_text(
                    "⚠️ Twilio account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error fetching numbers from Twilio. Please try again later.",
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
        c.execute("SELECT points, twilio_sid, twilio_token, selected_number FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, twilio_sid, twilio_token, selected_number = user_data
        if points < 15:
            await query.message.edit_text(
                "❌ Insufficient points to purchase a number.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        client = Client(twilio_sid, twilio_token)
        
            # ✅ Release any old number linked to this Twilio SID (required for trial accounts)
        try:
           existing_numbers = client.incoming_phone_numbers.list()
           for number in existing_numbers:
             number.delete()
             logger.debug(f"Released old number {number.phone_number} for user {user_id}")
        except Exception as e:
            logger.error(f"Error releasing previous Twilio number(s) for user {user_id}: {e}")
            await query.message.edit_text(
             "⚠️ Error releasing previous Twilio number. Please try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    )
            return

        try:
            incoming_number = client.incoming_phone_numbers.create(phone_number=phone_number)
            c.execute(
                "UPDATE users SET points = points - 15, selected_number = ? WHERE user_id = ?",
                (phone_number, user_id),
            )
            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                      (user_id, f"Purchased number {phone_number}", datetime.now().isoformat()))
            conn.commit()
            await query.message.edit_text(
                f"✅ Successfully purchased number {phone_number}! 15 points deducted.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Number {phone_number} purchased by user {user_id}")
        except TwilioRestException as e:
            logger.error(f"Twilio error in select_number for user {user_id}: {e}")
            if e.status == 402:
                await query.message.edit_text(
                    "⚠️ Twilio account has insufficient credits. Please contact the admin.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            else:
                await query.message.edit_text(
                    "⚠️ Error purchasing number from Twilio. Please try again later.",
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
        try:
            c.execute("SELECT COUNT(*) FROM users")
            total_users = c.fetchone()[0]
            c.execute("SELECT user_id, username, points, status FROM users WHERE status = 'pending'")
            pending_users = c.fetchall()
            c.execute("SELECT COUNT(*) FROM users WHERE twilio_sid IS NOT NULL")
            active_twilio = c.fetchone()[0]
        except sqlite3.OperationalError as e:
            logger.error(f"Database error in admin_panel for user {user_id}: {e}")
            text = "⚠️ Database error loading statistics. Some features may be limited.\n\nSelect an option:"
        else:
            text = (
                f"🔐 Admin Dashboard\n\n"
                f"📊 Total Users: {total_users}\n"
                f"⏳ Pending Requests: {len(pending_users)}\n"
                f"🔑 Active Twilio Users: {active_twilio}\n\n"
                f"Please select an option:"
            )
        keyboard = [
            [InlineKeyboardButton("📊 View All Users", callback_data="admin_view_users")],
            [InlineKeyboardButton("👥 Manage Users", callback_data="admin_manage_users")],
            [InlineKeyboardButton("⏳ Pending Requests", callback_data="admin_pending_requests")],
            [InlineKeyboardButton("🔍 Search User", callback_data="admin_search_user")],
            [InlineKeyboardButton("🎟️ Set Redeem Code", callback_data="admin_set_redeem_code")],
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
            c.execute("SELECT user_id, username, points, twilio_sid, twilio_token, status FROM users")
            users = c.fetchall()
            if not users:
                await query.message.edit_text(
                    "😔 No users found.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No users found for admin view users")
                return
            text = f"📊 Total Users: {len(users)}\n\n"
            for user in users:
                user_id, username, points, twilio_sid, twilio_token, status = user
                username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]') if username else "No Username"
                text += (
                    f"User: @{username} (ID: {user_id})\n"
                    f"Points: {points} 💰\n"
                    f"Twilio SID: {twilio_sid if twilio_sid else 'Not Set ❌'}\n"
                    f"Twilio Token: {twilio_token if twilio_token else 'Not Set ❌'}\n"
                    f"Status: {status.capitalize()} 🛠️\n\n"
                )
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Total users viewed by admin {query.from_user.id}")
        elif data == "admin_manage_users":
            c.execute("SELECT user_id, username, points, status FROM users")
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
              username = user[1] or 'No Username'
              safe_username = escape_markdown_v2(username)

              button_text = f"@{safe_username} (ID: {user[0]})"
              keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"admin_manage_user_{user[0]}")
    ])

            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text("👥 Select a user to manage:", reply_markup=reply_markup)
            logger.debug(f"Manage users displayed for admin {query.from_user.id}")
        elif data == "admin_pending_requests":
            c.execute("SELECT user_id, username, points, status FROM users WHERE status = 'pending'")
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
            for user_id, username, points, status in pending_users:
                username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]') if username else "No Username"
                keyboard.append([
                    InlineKeyboardButton(f"@{username} (ID: {user_id})", callback_data=f"admin_manage_user_{user_id}"),
                    InlineKeyboardButton("✅", callback_data=f"admin_approve_{user_id}"),
                    InlineKeyboardButton("❌", callback_data=f"admin_reject_{user_id}"),
                ])
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(text, reply_markup=reply_markup)
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
        c.execute("SELECT username, points, twilio_sid, twilio_token, selected_number, status FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"User {user_id} not found for admin manage")
            return
        username, points, twilio_sid, twilio_token, selected_number, status = user_data
        username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]') if username else "No Username"
        text = (
            f"👤 User Information\n\n"
            f"Username: @{username}\n"
            f"User ID: {user_id}\n"
            f"Points: {points} 💰\n"
            f"Twilio SID: {twilio_sid if twilio_sid else 'Not Set ❌'}\n"
            f"Twilio Token: {twilio_token if twilio_token else 'Not Set ❌'}\n"
            f"Selected Number: {selected_number or 'None 📞'}\n"
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
        username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]') if username else "No Username"
        c.execute("SELECT action, timestamp FROM user_activity WHERE user_id = ? AND action LIKE 'Purchased number%' OR action LIKE '%refer%' OR action LIKE 'Redeemed code%' OR action LIKE 'Referred by%' OR action LIKE 'Earned 1 point%' ORDER BY timestamp DESC LIMIT 5", (user_id,))
        purchase_activities = c.fetchall()
        text = f"📜 Activity Log for user @{username} (ID: {user_id})\n\n"
        if purchase_activities:
            text += "Number and Referral Activities:\n" + "\n".join([f"{timestamp}: {action}" for action, timestamp in purchase_activities]) + "\n\n"
        else:
            text += "No number or referral activities.\n\n"
        if twilio_sid and twilio_token and selected_number:
            try:
                client = Client(twilio_sid, twilio_token)
                messages = client.messages.list(to=selected_number, limit=5)
                if messages:
                    text += "Received OTPs:\n" + "\n".join([f"{msg.date_sent.strftime('%Y-%m-%d %H:%M:%S UTC') if msg.date_sent else datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}: {msg.body.strip()}" for msg in messages if msg.body and msg.body.strip()])
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
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
        text = "✅ Bulk Approve\n\nPlease enter Twilio SID and Token for approval (format: SID,Token):"
        context.user_data["bulk_approve"] = True
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk approve initiated by admin {query.from_user.id}")
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
        c.execute("UPDATE users SET twilio_sid = NULL, twilio_token = NULL, status = 'pending' WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            f"Twilio credentials for user {user_id} removed successfully.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        try:
            await context.bot.send_message(user_id, "❌ Your Twilio credentials have been removed by the admin. Please request again.")
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
        logger.debug(f"Twilio credentials removed for user {user_id} by admin {query.from_user.id}")
    except Exception as e:
        logger.error(f"Error in admin_remove_twilio for user {user_id}: {str(e)}")
        await query.message.edit_text(
            f"⚠️ Error removing Twilio credentials: {str(e)}. Try /start again.",
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
                    await context.bot.send_message(target_user_id, f"🎉 Your points have been updated to {points}.")
                except Exception as e:
                    logger.error(f"Error notifying user {target_user_id}: {e}")
                del context.user_data["set_points_user_id"]
                logger.debug(f"Points set to {points} for user {target_user_id} by admin {user_id}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter a valid integer for points.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )

        elif user_id in ADMIN_IDS and ("set_twilio_user_id" in context.user_data or "approve_user_id" in context.user_data or "bulk_approve" in context.user_data):
            try:
                if not message.count(",") == 1:
                    raise ValueError("Invalid format")
                sid, token = message.split(",")
                sid, token = sid.strip(), token.strip()
                if not sid or not token:
                    raise ValueError("SID or Token cannot be empty")
                if not validate_twilio_credentials(sid, token):
                    await update.message.reply_text(
                        "⚠️ Invalid Twilio credentials. Please check SID and Token and try again.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                if "bulk_approve" in context.user_data:
                    c.execute("SELECT user_id FROM users WHERE status = 'pending'")
                    pending_users = c.fetchall()
                    for user_id_tuple in pending_users:
                        c.execute(
                            "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                            (sid, token, user_id_tuple[0]),
                        )
                        try:
                            await context.bot.send_message(user_id_tuple[0], "✅ Your Twilio credentials have been set. You can now get numbers!")
                        except Exception as e:
                            logger.error(f"Error notifying user {user_id_tuple[0]}: {e}")
                    await update.message.reply_text(
                        f"✅ Bulk approved {len(pending_users)} users.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    del context.user_data["bulk_approve"]
                else:
                    target_user_id = context.user_data.get("set_twilio_user_id") or context.user_data.get("approve_user_id")
                    c.execute(
                        "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                        (sid, token, target_user_id),
                    )
                    await update.message.reply_text(
                        f"✅ Twilio credentials set for user {target_user_id}.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    try:
                        await context.bot.send_message(target_user_id, "✅ Your Twilio credentials have been set. You can now get numbers!")
                    except Exception as e:
                        logger.error(f"Error notifying user {target_user_id}: {e}")
                    if "set_twilio_user_id" in context.user_data:
                        del context.user_data["set_twilio_user_id"]
                    if "approve_user_id" in context.user_data:
                        del context.user_data["approve_user_id"]
                conn.commit()
                logger.debug(f"Twilio credentials set by admin {user_id}")
            except ValueError as e:
                await update.message.reply_text(
                    f"⚠️ Invalid format: {str(e)}. Please enter Twilio SID and Token in the format: SID,Token",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )

        elif user_id in ADMIN_IDS and context.user_data.get("set_redeem_code"):
            try:
                if not message.count(",") == 1:
                    raise ValueError("Invalid format")
                code, points = message.split(",")
                code, points = code.strip(), int(points.strip())
                if points < 0:
                    await update.message.reply_text(
                        "⚠️ Points must be positive.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                c.execute(
                    "INSERT INTO redeem_codes (code, points, created_at) VALUES (?, ?, ?)",
                    (code, points, datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ Redeem code '{code}' set with {points} points.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["set_redeem_code"]
                logger.debug(f"Redeem code {code} set with {points} points by admin {user_id}")
            except ValueError as e:
                await update.message.reply_text(
                    f"⚠️ Invalid format: {str(e)}. Please enter in format: code,points (e.g., ABC123,10)",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            except sqlite3.Error as e:
                await update.message.reply_text(
                    f"⚠️ Database error: {str(e)}. This redeem code may already exist. Use a unique code.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )

        elif user_id in ADMIN_IDS and context.user_data.get("search_user_active"):
            search_query = message.strip()
            if search_query.startswith("@"):
                search_query = search_query[1:]
                c.execute("SELECT user_id, username, points, status FROM users WHERE username = ?", (search_query,))
            else:
                try:
                    search_id = int(search_query)
                    c.execute("SELECT user_id, username, points, status FROM users WHERE user_id = ?", (search_id,))
                except ValueError:
                    await update.message.reply_text(
                        "⚠️ Please enter a valid user ID or username (e.g., @username or 123456789).",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
            user_data = c.fetchone()
            if not user_data:
                await update.message.reply_text(
                    "😔 User not found.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                return
            user_id, username, points, status = user_data
            context.user_data["current_user_id"] = user_id
            username = username.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]') if username else "No Username"
            text = (
                f"👤 User: @{username}\n"
                f"ID: {user_id}\n"
                f"Points: {points} 💰\n"
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
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
            del context.user_data["search_user_active"]
            logger.debug(f"User {user_id} searched by admin {user_id}")

        else:
            await update.message.reply_text(
                "⚠️ Invalid input. Please use menu options.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_text for user {user_id}: {str(e)}")
        await update.message.reply_text(
            "⚠️ Database error. Try /start again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in handle_text for user {user_id}: {str(e)}")
        await update.message.reply_text(
            "⚠️ Error processing request. Try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Start the OTP Bot"),
        BotCommand("menu", "Show main menu"),
        BotCommand("redeem", "Redeem code for points"),
        BotCommand("admin", "Access admin panel (admin only)")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set")


async def webhook(request):
    data = await request.json()
    update = Update.de_json(data, bot)
    await application.update_queue.put(update)
    return web.Response(text="OK")

async def health_check(_: web.Request):
    return web.Response(text="healthy")

async def main():
    try:
        init_db()

        global application
        global bot

        application = Application.builder().token(BOT_TOKEN).build()
        bot = application.bot

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu))
        application.add_handler(CommandHandler("redeem", redeem))
        application.add_handler(CommandHandler("admin", admin_panel))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.job_queue.run_once(set_bot_commands, 0)

        # Set webhook manually
        await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")

        # Set up aiohttp manually
        app = web.Application()
        app.router.add_post("/webhook", webhook)
        app.router.add_get("/health", health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()

        logger.info(f"Server started on port {PORT}")
        await application.start()
        await asyncio.Event().wait()

    except Exception as e:
        logger.error(f"Error in main: {e}")

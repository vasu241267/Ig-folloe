import sqlite3
import logging
import time
import asyncio
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
import os
from datetime import datetime

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Database setup with connection pooling
def init_db():
    retries = 3
    for attempt in range(retries):
        try:
            conn = sqlite3.connect("bot.db", timeout=10)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                points INTEGER DEFAULT 0,
                twilio_sid TEXT,
                twilio_token TEXT,
                selected_number TEXT,
                status TEXT DEFAULT 'pending'
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
            c.execute('''CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id INTEGER PRIMARY KEY,
                referrer_user_id INTEGER,
                timestamp TEXT,
                FOREIGN KEY(referred_user_id) REFERENCES users(user_id),
                FOREIGN KEY(referrer_user_id) REFERENCES users(user_id)
            )''')
            c.execute("PRAGMA table_info(users)")
            columns = [col[1] for col in c.fetchall()]
            if "username" not in columns:
                c.execute("ALTER TABLE users ADD COLUMN username TEXT")
            conn.commit()
            logger.debug("Database initialized successfully")
            return
        except sqlite3.OperationalError as e:
            logger.error(f"Database initialization failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise Exception(f"Failed to initialize database after {retries} attempts: {e}")
        finally:
            if 'conn' in locals():
                conn.close()

# Configuration
BOT_TOKEN = "8020708306:AAHmrEb8nkmBMzEEx_m88Nenyz5QgrQ85hA"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FORCE_SUB_CHANNEL = "@darkdorking"
ADMIN_IDS = [6972264549]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if args and args[0].isdigit():
        await handle_referral(update, context)
        return
    if not await check_subscription(update, context):
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        username = user.username or "No Username"
        c.execute("INSERT OR IGNORE INTO users (user_id, username, points) VALUES (?, ?, ?)", (user.id, username, 0))
        c.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user.id))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user.id, "Started bot", datetime.now().isoformat()))
        conn.commit()
        logger.debug(f"User {user.id} (@{username}) initialized in database")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in start for user {user.id}: {e}")
        await update.message.reply_text("⚠️ An error occurred while initializing your account. Please try again later. 😊")
        return
    except Exception as e:
        logger.error(f"Unexpected error in start for user {user.id}: {e}")
        await update.message.reply_text("⚠️ An unexpected error occurred. Please try /start again. 😊")
        return
    finally:
        if 'conn' in locals():
            conn.close()

    welcome_text = (
        f"👋 Welcome, @{user.username or 'friend'}! 🎉\n\n"
        "Thank you for joining OTP Bot! Explore our features to manage your account, purchase numbers, view OTPs, or refer friends to earn points.\n\n"
        "Select an option below to get started:"
    )
    await show_main_menu(update, context, text=welcome_text)

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user.id)
        if member.status not in ["member", "administrator", "creator"]:
            keyboard = [[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{FORCE_SUB_CHANNEL[1:]}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "📢 To use OTP Bot, please join our official channel first. 😊",
                reply_markup=reply_markup
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking subscription for user {user.id}: {e}")
        await update.message.reply_text("⚠️ Unable to verify channel subscription. Please try again later. 😊")
        return False

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None, text="🌟 OTP Bot\n\nPlease select an option to proceed:"):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("📞 Purchase Numbers", callback_data="get_numbers")],
        [InlineKeyboardButton("🔗 Refer Friends", callback_data="refer")],
        [InlineKeyboardButton("👥 My Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🔐 View OTPs", callback_data="otps")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="admin_panel")])
    keyboard.append([InlineKeyboardButton("📞 Contact Developer", url="https://t.me/imvasupareek")])
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
            "⚠️ An error occurred while loading the menu. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    await start(update, context)

async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Please provide a redeem code using /redeem <code> 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    code = args[0].strip()
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT points, redeemed_by FROM redeem_codes WHERE code = ?", (code,))
        code_data = c.fetchone()
        if not code_data:
            await update.message.reply_text(
                "❌ Invalid redeem code. Please check and try again. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, redeemed_by = code_data
        if redeemed_by:
            await update.message.reply_text(
                "❌ This code has already been redeemed. 😕",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute("UPDATE redeem_codes SET redeemed_by = ? WHERE code = ?", (user_id, code))
        c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user_id, f"Redeemed code {code} for {points} points", datetime.now().isoformat()))
        conn.commit()
        await update.message.reply_text(
            f"✅ Successfully redeemed code '{code}' for {points} points! 🎉",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"User {user_id} redeemed code {code} for {points} points")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in redeem for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error processing redeem code. Please try again later. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in redeem for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error processing redeem code. Please try again or contact @imvasupareek. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_subscription(update, context):
        return

    user_id = query.from_user.id
    data = query.data
    logger.debug(f"Button callback from user {user_id}: {data}")

    # Rate limiting
    last_click = context.user_data.get("last_click_time", 0)
    current_time = time.time()
    if current_time - last_click < 1:
        await query.message.edit_text("⚠️ Please wait a moment before clicking again. 😊")
        return
    context.user_data["last_click_time"] = current_time

    try:
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
        elif data == "refer":
            await refer(query, context)
        elif data == "referrals":
            await show_referrals(query, context)
        elif data == "otps":
            await show_otps(query, context)
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
                "⚠️ Unknown action. Please try /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
    except Exception as e:
        logger.error(f"Error in button callback for user {user_id}: {e}")
        await query.message.edit_text(
            f"⚠️ An error occurred: {str(e)}. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def show_account(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT points, twilio_sid, selected_number, status, username FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, twilio_sid, selected_number, status, username = user_data
        text = (
            f"👤 Account Information\n\n"
            f"Username: @{username or 'No Username'}\n"
            f"User ID: {user_id}\n"
            f"Points: {points} 💰\n"
            f"Twilio Status: {'Active ✅' if twilio_sid else 'Not Set ❌'}\n"
            f"Selected Number: {selected_number or 'None 📞'}\n"
            f"Account Status: {status.capitalize()} 🛠️\n\n"
            f"Use /redeem <code> to redeem points for exclusive rewards. 🎁"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        logger.debug(f"Account info shown for user {user_id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in show_account for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching account information. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def refer(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    referral_link = f"https://t.me/{context.bot.username}?start={user_id}"
    text = (
        f"🔗 Referral Program\n\n"
        f"Invite your friends to join OTP Bot and earn 1 point for each successful referral! 🎉\n"
        f"Your unique referral link: {referral_link}\n\n"
        f"Share this link with your friends, and once they join and start using the bot, you'll earn points! 🚀"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.edit_text(text, reply_markup=reply_markup)
    logger.debug(f"Referral link generated for user {user_id}")

async def show_referrals(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT referred_user_id, username FROM referrals r JOIN users u ON r.referred_user_id = u.user_id WHERE r.referrer_user_id = ?", (user_id,))
        referrals = c.fetchall()
        c.execute("SELECT points, username FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, username = user_data
        text = (
            f"🔗 Your Referral Stats, @{username or 'friend'}! 🚀\n\n"
            f"👥 Total Referrals: {len(referrals)}\n"
            f"💰 Points Earned from Referrals: {len(referrals)} (1 point per referral)\n\n"
        )
        if referrals:
            text += "Your Referrals:\n"
            for ref_id, ref_username in referrals:
                text += f" - @{ref_username or 'No Username'} (ID: {ref_id})\n"
        else:
            text += "You haven't referred anyone yet. Share your link to start earning! 🌟"
        text += f"\nYour Referral Link: https://t.me/{context.bot.username}?start={user_id}"
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        logger.debug(f"Referral stats shown for user {user_id}: {len(referrals)} referrals")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in show_referrals for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching referral stats. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in show_referrals for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error fetching referral stats. Please try again or contact @imvasupareek. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def show_otps(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT twilio_sid, twilio_token, selected_number, username FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data or not user_data[2]:
            await query.message.edit_text(
                "📞 No number selected. Please purchase a number first. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        twilio_sid, twilio_token, selected_number, username = user_data
        client = Client(twilio_sid, twilio_token)
        messages = client.messages.list(to=selected_number, limit=5)
        if not messages:
            text = "🔐 Recent OTPs\n\nNo OTPs received yet. Please check back later. 😊"
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
                text += "No valid OTP messages found. 😕"
        conn.commit()
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"OTPs retrieved for user {user_id}: {len(messages)} messages processed")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in show_otps for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching OTPs. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in show_otps for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error fetching OTPs. Please try again later. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    args = context.args
    logger.debug(f"Processing referral for user {user_id} (@{user.username or 'No Username'}) with args: {args}")

    # Parse referrer_id safely
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "❌ Invalid referral link! Please use a valid link shared by a friend. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
        )
        logger.debug(f"Invalid referral link provided by user {user_id}")
        await start(update, context)
        return

    referrer_id = int(args[0])
    
    # Prevent self-referral
    if referrer_id == user_id:
        await update.message.reply_text(
            "🚫 You cannot refer yourself! Share your link with friends to earn points! 🌟",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
        )
        logger.debug(f"User {user_id} attempted self-referral")
        await start(update, context)
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()

        # Ensure user is registered
        c.execute("INSERT OR IGNORE INTO users (user_id, username, points) VALUES (?, ?, ?)",
                 (user_id, user.username or "No Username", 0))
        c.execute("UPDATE users SET username = ? WHERE user_id = ?", (user.username or "No Username", user_id))

        # Check if referrer exists
        c.execute("SELECT username FROM users WHERE user_id = ?", (referrer_id,))
        referrer_data = c.fetchone()
        if not referrer_data:
            await update.message.reply_text(
                "❌ This referral link is invalid. The user who shared it doesn't exist. Try another link! 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
            )
            logger.debug(f"Referrer {referrer_id} not found for user {user_id}")
            conn.close()
            await start(update, context)
            return

        # Check if user was already referred
        c.execute("SELECT referrer_user_id FROM referrals WHERE referred_user_id = ?", (user_id,))
        existing_referral = c.fetchone()
        if existing_referral:
            c.execute("SELECT username FROM users WHERE user_id = ?", (existing_referral[0],))
            existing_referrer = c.fetchone()
            await update.message.reply_text(
                f"❌ You've already been referred by @{existing_referrer[0] or 'someone'}! Explore OTP Bot now! 🚀",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
            )
            logger.debug(f"User {user_id} already referred by {existing_referral[0]}")
            conn.close()
            await start(update, context)
            return

        # Check subscription before recording referral
        if not await check_subscription(update, context):
            conn.close()
            return

        # Record referral and award point
        c.execute("INSERT INTO referrals (referred_user_id, referrer_user_id, timestamp) VALUES (?, ?, ?)",
                 (user_id, referrer_id, datetime.now().isoformat()))
        c.execute("UPDATE users SET points = points + 1 WHERE user_id = ?", (referrer_id,))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                 (user_id, f"Joined via referral from user {referrer_id} (@{referrer_data[0] or 'No Username'})", datetime.now().isoformat()))
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                 (referrer_id, f"Earned 1 point for referring user {user_id} (@{user.username or 'No Username'})", datetime.now().isoformat()))
        conn.commit()

        # Notify new user
        await update.message.reply_text(
            f"🎉 Welcome aboard, @{user.username or 'friend'}! You joined via @{referrer_data[0] or 'a friend'}'s link. Start exploring OTP Bot! 🚀",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
        )
        logger.debug(f"Referral recorded: User {user_id} referred by {referrer_id}")

        # Notify referrer with retry logic
        async def send_notification_with_retry(bot, chat_id, message, max_retries=3):
            for attempt in range(max_retries):
                try:
                    await bot.send_message(chat_id, message)
                    logger.debug(f"Notification sent to referrer {chat_id}")
                    return True
                except Exception as e:
                    logger.error(f"Attempt {attempt + 1}/{max_retries} failed for referrer {chat_id}: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
            logger.error(f"Failed to notify referrer {chat_id} after {max_retries} attempts")
            return False

        await send_notification_with_retry(
            context.bot,
            referrer_id,
            f"🎉 Awesome, @{referrer_data[0] or 'friend'}! @{user.username or 'A new user'} joined using your referral link! You've earned 1 point! 💰 Keep sharing! 🚀"
        )

        conn.close()
        await start(update, context)

    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_referral for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Sorry, we hit a snag with your referral. Please try again later or contact @imvasupareek. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
        )
        await start(update, context)
    except Exception as e:
        logger.error(f"Unexpected error in handle_referral for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Something went wrong with your referral. Please try again or contact @imvasupareek. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="back")]])
        )
        await start(update, context)
    finally:
        if 'conn' in locals():
            conn.close()

async def get_numbers(query: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT points, twilio_sid, status, username, twilio_token FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. Please start the bot with /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, twilio_sid, status, username, twilio_token = user_data
        if points < 15:
            await query.message.edit_text(
                f"❌ You need at least 15 points to purchase a number. Current points: {points} 😕",
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
                    f"📬 User @{username or 'No Username'} (ID: {user_id}) has requested Twilio credentials (Points: {points}).",
                    reply_markup=reply_markup,
                )
            await query.message.edit_text(
                "⏳ Your request for Twilio credentials is under review by the admin. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"Twilio request submitted by user {user_id}")
            conn.close()
            return
        client = Client(twilio_sid, twilio_token)
        numbers = client.available_phone_numbers("CA").local.list(limit=20)
        if not numbers:
            await query.message.edit_text(
                "❌ No Canadian numbers available at the moment. 😕",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            conn.close()
            return
        text = "📞 Available Canadian Numbers:\n\n" + "\n".join([f"{num.phone_number}" for num in numbers])
        keyboard = [[InlineKeyboardButton(f"Buy {num.phone_number}", callback_data=f"select_number_{num.phone_number}")] for num in numbers]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        logger.debug(f"Canadian numbers displayed for user {user_id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in get_numbers for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error fetching numbers. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in get_numbers for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error fetching numbers. Please try again later. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def select_number(query: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
    user_id = query.from_user.id
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT points, twilio_sid, twilio_token, selected_number FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "⚠️ Account not found. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        points, twilio_sid, twilio_token, selected_number = user_data
        if points < 15:
            await query.message.edit_text(
                "❌ Insufficient points to purchase a number. 😕",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        client = Client(twilio_sid, twilio_token)
        if selected_number:
            try:
                numbers = client.incoming_phone_numbers.list(phone_number=selected_number)
                if numbers:
                    for number in numbers:
                        if number.phone_number == selected_number:
                            number.delete()
                            c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                                      (user_id, f"Released number {selected_number}", datetime.now().isoformat()))
                            logger.debug(f"Released number {selected_number} for user {user_id}")
                            break
            except Exception as e:
                logger.error(f"Error releasing number {selected_number} for user {user_id}: {e}")
                await query.message.edit_text(
                    "⚠️ Error releasing previous number. Please try again. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                conn.close()
                return
        incoming_number = client.incoming_phone_numbers.create(phone_number=phone_number)
        c.execute(
            "UPDATE users SET points = points - 15, selected_number = ? WHERE user_id = ?",
            (phone_number, user_id),
        )
        c.execute("INSERT INTO user_activity (user_id, action, timestamp) VALUES (?, ?, ?)",
                  (user_id, f"Purchased number {phone_number}", datetime.now().isoformat()))
        conn.commit()
        await query.message.edit_text(
            f"✅ Successfully purchased number {phone_number}! 15 points deducted. 🎉",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Number {phone_number} purchased by user {user_id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in select_number for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error purchasing number. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in select_number for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error purchasing number. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id=None):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        if update.callback_query:
            await update.callback_query.message.edit_text(
                "🚫 Unauthorized access. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await update.message.reply_text(
                "🚫 Unauthorized access. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        logger.warning(f"Unauthorized admin access attempt by user {user_id}")
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
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
                "⚠️ Error loading admin panel. Please try /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
        else:
            await update.message.reply_text(
                "⚠️ Error loading admin panel. Please try /start. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_panel_callback(query: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.warning(f"Unauthorized admin callback access by user {query.from_user.id}")
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        if data == "admin_view_users":
            c.execute("SELECT user_id, username, points, twilio_sid, twilio_token, status FROM users")
            users = c.fetchall()
            if not users:
                await query.message.edit_text(
                    "😔 No users found. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No users found for admin view users")
                return
            text = f"📊 Total Users: {len(users)}\n\n"
            for user in users:
                user_id, username, points, twilio_sid, twilio_token, status = user
                text += (
                    f"User: @{username or 'No Username'} (ID: {user_id})\n"
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
                    "😔 No users found. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No users found for admin manage users")
                return
            keyboard = [
                [InlineKeyboardButton(f"@{user[1] or 'No Username'} (ID: {user[0]})", callback_data=f"admin_manage_user_{user[0]}")]
                for user in users
            ]
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text("👥 Select a user to manage:", reply_markup=reply_markup)
            logger.debug(f"Manage users displayed for admin {query.from_user.id}")
        elif data == "admin_pending_requests":
            c.execute("SELECT user_id, username, points, status FROM users WHERE status = 'pending'")
            pending_users = c.fetchall()
            if not pending_users:
                await query.message.edit_text(
                    "😊 No pending requests at this time. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                logger.debug("No pending requests found")
                return
            text = "⏳ Pending Requests\n\n"
            keyboard = []
            for user_id, username, points, status in pending_users:
                keyboard.append([
                    InlineKeyboardButton(f"@{username or 'No Username'} (ID: {user_id})", callback_data=f"admin_manage_user_{user_id}"),
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
            "⚠️ Database error processing admin action. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_panel_callback: {e}")
        await query.message.edit_text(
            "⚠️ Error processing admin action. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_manage_user(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT username, points, twilio_sid, twilio_token, selected_number, status FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            logger.debug(f"User {user_id} not found for admin manage")
            return
        username, points, twilio_sid, twilio_token, selected_number, status = user_data
        text = (
            f"👤 User Information\n\n"
            f"Username: @{username or 'No Username'}\n"
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
        await query.message.edit_text(text, reply_markup=reply_markup)
        logger.debug(f"User {user_id} management options shown to admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_manage_user for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error managing user. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_manage_user for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error managing user. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_view_activity(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT username, twilio_sid, twilio_token, selected_number FROM users WHERE user_id = ?", (user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.message.edit_text(
                "😔 User not found. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        username, twilio_sid, twilio_token, selected_number = user_data
        c.execute("SELECT action, timestamp FROM user_activity WHERE user_id = ? AND action LIKE 'Purchased number%' OR action LIKE 'Released number%' OR action LIKE 'Redeemed code%' ORDER BY timestamp DESC LIMIT 5", (user_id,))
        purchase_activities = c.fetchall()
        text = f"📜 Activity Log for @{username or 'No Username'} (ID: {user_id})\n\n"
        if purchase_activities:
            text += "Number and Redeem Activities:\n" + "\n".join([f"{timestamp}: {action}" for action, timestamp in purchase_activities]) + "\n\n"
        else:
            text += "No number or redeem activities.\n\n"
        if twilio_sid and twilio_token and selected_number:
            client = Client(twilio_sid, twilio_token)
            messages = client.messages.list(to=selected_number, limit=5)
            if messages:
                text += "Received OTPs:\n" + "\n".join([f"{msg.date_sent.strftime('%Y-%m-%d %H:%M:%S UTC') if msg.date_sent else datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}: Received OTP: {msg.body.strip()}" for msg in messages if msg.body and msg.body.strip()])
            else:
                text += "No OTPs received."
        else:
            text += "No OTPs received (Twilio not set or no number selected)."
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Activity log for user {user_id} viewed by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_view_activity for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error viewing activity. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_view_activity for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error viewing activity. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_bulk_approve(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE status = 'pending'")
        pending_users = c.fetchall()
        if not pending_users:
            await query.message.edit_text(
                "😊 No pending requests to approve. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        text = "✅ Bulk Approve\n\nPlease enter Twilio SID and Token for all pending users (format: SID,Token):"
        context.user_data["bulk_approve"] = True
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk approve initiated by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Database error initiating bulk approve. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_approve: {e}")
        await query.message.edit_text(
            "⚠️ Error initiating bulk approve. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_bulk_reject(query: Update, context: ContextTypes.DEFAULT_TYPE):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM users WHERE status = 'pending'")
        pending_users = c.fetchall()
        if not pending_users:
            await query.message.edit_text(
                "😊 No pending requests to reject. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            return
        c.execute("UPDATE users SET status = 'rejected' WHERE status = 'pending'")
        conn.commit()
        for user_id, username in pending_users:
            try:
                await context.bot.send_message(user_id, "❌ Your request was rejected by the admin. 😕")
            except Exception as e:
                logger.error(f"Error notifying user {user_id}: {e}")
        await query.message.edit_text(
            f"❌ Successfully rejected {len(pending_users)} pending requests. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Bulk reject completed by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Database error rejecting requests. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_bulk_reject: {e}")
        await query.message.edit_text(
            "⚠️ Error rejecting requests. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_approve(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    try:
        context.user_data["approve_user_id"] = user_id
        await query.message.edit_text(
            "🔑 Please enter Twilio SID and Token for approval (format: SID,Token):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        logger.debug(f"Admin {query.from_user.id} prompted to set Twilio credentials for user {user_id}")
    except Exception as e:
        logger.error(f"Error in admin_approve for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error initiating approval. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )

async def admin_reject(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return
    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("UPDATE users SET status = 'rejected' WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            f"❌ User {user_id} rejected. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        try:
            await context.bot.send_message(user_id, "❌ Your request was rejected by the admin. 😕")
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
        logger.debug(f"User {user_id} rejected by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error rejecting user. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_reject for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error rejecting user. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def admin_remove_twilio(query: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if query.from_user.id not in ADMIN_IDS:
        await query.message.edit_text(
            "🚫 Unauthorized access. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        return

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        c.execute("UPDATE users SET twilio_sid = NULL, twilio_token = NULL, status = 'pending' WHERE user_id = ?", (user_id,))
        conn.commit()
        await query.message.edit_text(
            f"🗑️ Twilio credentials removed for User {user_id}. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
        try:
            await context.bot.send_message(user_id, "❌ Your Twilio credentials have been removed by the admin. Please request again. 😊")
        except Exception as e:
            logger.error(f"Error notifying user {user_id}: {e}")
        logger.debug(f"Twilio credentials removed for user {user_id} by admin {query.from_user.id}")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in admin_remove_twilio for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Database error removing Twilio credentials. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_remove_twilio for user {user_id}: {e}")
        await query.message.edit_text(
            "⚠️ Error removing Twilio credentials. Please try again. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return

    user_id = update.effective_user.id
    message = update.message.text
    logger.debug(f"Text message from user {user_id}: {message}")

    try:
        conn = sqlite3.connect("bot.db", timeout=10)
        c = conn.cursor()
        if user_id in ADMIN_IDS and "set_points_user_id" in context.user_data:
            points = int(message)
            target_user_id = context.user_data["set_points_user_id"]
            c.execute("UPDATE users SET points = ? WHERE user_id = ?", (points, target_user_id))
            conn.commit()
            await update.message.reply_text(
                f"✅ Points updated to {points} for User {target_user_id}. 😊",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
            )
            try:
                await context.bot.send_message(target_user_id, f"💰 Your points have been updated to {points}. 🎉")
            except Exception as e:
                logger.error(f"Error notifying user {target_user_id}: {e}")
            del context.user_data["set_points_user_id"]
            logger.debug(f"Points set to {points} for user {target_user_id} by admin {user_id}")
            conn.close()
            return

        if user_id in ADMIN_IDS and ("set_twilio_user_id" in context.user_data or "approve_user_id" in context.user_data or "bulk_approve" in context.user_data):
            try:
                sid, token = message.split(",")
                sid, token = sid.strip(), token.strip()
                if "bulk_approve" in context.user_data:
                    c.execute("SELECT user_id FROM users WHERE status = 'pending'")
                    pending_users = c.fetchall()
                    for user_id, in pending_users:
                        c.execute(
                            "UPDATE users SET twilio_sid = ?, twilio_token = ?, status = 'approved' WHERE user_id = ?",
                            (sid, token, user_id),
                        )
                        try:
                            await context.bot.send_message(user_id, "✅ Your Twilio credentials have been set. You can now get numbers! 🎉")
                        except Exception as e:
                            logger.error(f"Error notifying user {user_id}: {e}")
                    await update.message.reply_text(
                        f"✅ Bulk approved {len(pending_users)} users. 😊",
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
                        f"✅ Twilio credentials set for User {target_user_id}. 😊",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    try:
                        await context.bot.send_message(target_user_id, "✅ Your Twilio credentials have been set. You can now get numbers! 🎉")
                    except Exception as e:
                        logger.error(f"Error notifying user {target_user_id}: {e}")
                    if "set_twilio_user_id" in context.user_data:
                        del context.user_data["set_twilio_user_id"]
                    if "approve_user_id" in context.user_data:
                        del context.user_data["approve_user_id"]
                conn.commit()
                logger.debug(f"Twilio credentials set by admin {user_id}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter Twilio SID and Token in the format: SID,Token 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            finally:
                conn.close()
            return

        if user_id in ADMIN_IDS and context.user_data.get("set_redeem_code"):
            try:
                code, points = message.split(",")
                code, points = code.strip(), int(points.strip())
                if points < 0:
                    await update.message.reply_text(
                        "⚠️ Points must be a positive number. 😊",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    return
                c.execute(
                    "INSERT INTO redeem_codes (code, points, created_at) VALUES (?, ?, ?)",
                    (code, points, datetime.now().isoformat())
                )
                conn.commit()
                await update.message.reply_text(
                    f"✅ Redeem code '{code}' set with {points} points. 🎉",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                del context.user_data["set_redeem_code"]
                logger.debug(f"Redeem code {code} set with {points} points by admin {user_id}")
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Please enter in the format: code,points (e.g., ABC123,10) 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            except sqlite3.IntegrityError:
                await update.message.reply_text(
                    "⚠️ This redeem code already exists. Please use a unique code. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
            finally:
                conn.close()
            return

        if user_id in ADMIN_IDS and context.user_data.get("search_user_active"):
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
                        "⚠️ Please enter a valid user ID or username (e.g., @username or 123456789). 😊",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                    )
                    conn.close()
                    return
            user_data = c.fetchone()
            if not user_data:
                await update.message.reply_text(
                    "😔 User not found. 😊",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
                )
                conn.close()
                return
            user_id, username, points, status = user_data
            context.user_data["current_user_id"] = user_id
            text = (
                f"👤 User Information\n\n"
                f"Username: @{username or 'No Username'}\n"
                f"User ID: {user_id}\n"
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
            await update.message.reply_text(text, reply_markup=reply_markup)
            logger.debug(f"User {user_id} searched by admin {user_id}")
            conn.close()
            return

        await update.message.reply_text(
            "⚠️ Invalid input. Please use the menu options. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in handle_text for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Database error processing your request. Please try /start. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    except Exception as e:
        logger.error(f"Unexpected error in handle_text for user {user_id}: {e}")
        await update.message.reply_text(
            "⚠️ Error processing your request. Please try again or contact @imvasupareek. 😊",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        )
    finally:
        if 'conn' in locals():
            conn.close()

async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Start the OTP Bot"),
        BotCommand("menu", "Show the main menu"),
        BotCommand("redeem", "Redeem a code for points"),
        BotCommand("admin", "Access the admin panel (admin only)")
    ]
    await application.bot.set_my_commands(commands)
    logger.debug("Bot commands set successfully")

def main():
    try:
        init_db()
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu))
        application.add_handler(CommandHandler("redeem", redeem))
        application.add_handler(CommandHandler("admin", admin_panel))
        application.add_handler(CallbackQueryHandler(button_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.post_init = set_bot_commands
        logger.info("Bot started successfully")
        application.run_polling()
    except Exception as e:
        logger.error(f"Error in main: {e}")

if __name__ == "__main__":
    main()

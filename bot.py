import sqlite3
import logging
import os
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from instagrapi import Client
import hashlib
import json
import random
import string
from flask import Flask, request, jsonify
import asyncio

# Flask app for Koyeb
app = Flask(__name__)

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,  # Increased verbosity for debugging
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')  # Log to file for Koyeb debugging
    ]
)
logger = logging.getLogger(__name__)

# Database setup
def init_db():
    try:
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            insta_username TEXT,
            insta_password TEXT,
            coins INTEGER DEFAULT 0,
            session_json TEXT,
            referral_code TEXT UNIQUE,
            referred_by TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            insta_username TEXT,
            coins_spent INTEGER,
            followers_gained INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )''')
        conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
    finally:
        conn.close()

# Initialize database
init_db()

# Instagram client cache
insta_clients = {}

# Telegram application
application = None

# Generate random referral code
def generate_referral_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        c.execute("SELECT referral_code FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        referral_code = user[0] if user else generate_referral_code()
        
        welcome_msg = (
            "Welcome to the Instagram Follower Bot! 😎\n"
            f"Your referral code: {referral_code}\n"
            "Use /login <username> <password> [<referral_code>] to connect your Instagram account.\n"
            "Use /suggest to get users to follow and earn coins.\n"
            "Use /followed <username> to confirm follow and earn 10 coins.\n"
            "Use /campaign <coins> to promote your Instagram ID.\n"
            "Use /mycampaigns to view your active campaigns.\n"
            "Use /deactivate <campaign_id> to stop a campaign.\n"
            "Use /balance to check your coins.\n"
            "Use /logout to disconnect your Instagram account.\n"
            "Invite friends with your referral code to earn 10 coins per signup! 🎉"
        )
        await update.message.reply_text(welcome_msg)
        logger.info(f"User {telegram_id} sent /start")
    except Exception as e:
        logger.error(f"Error in /start: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Login command: /login <username> <password> [<referral_code>]
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Please provide username and password: /login <username> <password> [<referral_code>]")
            return
        
        username, password = args[0], args[1]
        referral_code = args[2] if len(args) > 2 else None
        
        cl = Client()
        cl.login(username, password)
        time.sleep(1)  # Delay to avoid Instagram rate limits
        session_json = json.dumps(cl.get_settings())
        
        # Encrypt password
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        
        # Check if new user for signup bonus
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        c.execute("SELECT coins FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        coins = 5 if not user else 0  # 5 coins signup bonus for new users
        
        # Generate or reuse referral code
        user_referral_code = generate_referral_code()
        
        # Save user
        c.execute("INSERT OR REPLACE INTO users (telegram_id, insta_username, insta_password, session_json, coins, referral_code, referred_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (telegram_id, username, hashed_password, session_json, coins, user_referral_code, referral_code))
        
        # Award referral coins
        if referral_code:
            c.execute("SELECT telegram_id FROM users WHERE referral_code = ?", (referral_code,))
            referrer = c.fetchone()
            if referrer and referrer[0] != telegram_id:
                c.execute("UPDATE users SET coins = coins + 10 WHERE telegram_id = ?", (referrer[0],))
                await context.bot.send_message(referrer[0], "You earned 10 coins for a referral! 🎉")
        
        conn.commit()
        
        insta_clients[telegram_id] = cl
        bonus_msg = "You received a 5-coin signup bonus! 🎁" if coins else ""
        await update.message.reply_text(f"Successfully logged in as {username}! {bonus_msg}")
        logger.info(f"User {telegram_id} logged in as {username}")
    except Exception as e:
        logger.error(f"Login error: {e}")
        await update.message.reply_text("Login failed. Check your credentials or try again later.")
    finally:
        conn.close()

# Suggest users to follow
async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT insta_username FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if not user:
            await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
            return
        
        c.execute("SELECT insta_username FROM campaigns WHERE active = 1 AND telegram_id != ?", (telegram_id,))
        campaigns = c.fetchall()
        if not campaigns:
            await update.message.reply_text("No active campaigns found to follow.")
            return
        
        suggested_user = random.choice(campaigns)[0]
        await update.message.reply_text(f"Suggested user to follow: {suggested_user}\nFollow them on Instagram and then use /followed {suggested_user} to earn 10 coins!")
        logger.info(f"User {telegram_id} received suggestion: {suggested_user}")
    except Exception as e:
        logger.error(f"Error in /suggest: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Followed command: /followed <username>
async def followed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Please provide the username you followed: /followed <username>")
            return
        
        followed_user = args[0]
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT insta_username, session_json FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if not user:
            await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
            return
        
        c.execute("SELECT id, telegram_id FROM campaigns WHERE insta_username = ? AND active = 1", (followed_user,))
        campaign = c.fetchone()
        if not campaign:
            await update.message.reply_text(f"No active campaign found for {followed_user}.")
            return
        
        cl = insta_clients.get(telegram_id)
        if not cl:
            cl = Client()
            cl.set_settings(json.loads(user[1]))
            insta_clients[telegram_id] = cl
        
        time.sleep(1)  # Delay to avoid Instagram rate limits
        followed_user_id = cl.user_id_from_username(followed_user)
        time.sleep(1)  # Additional delay
        is_following = cl.user_following(cl.user_id, followed_user_id)
        if is_following:
            c.execute("UPDATE users SET coins = coins + 10 WHERE telegram_id = ?", (telegram_id,))
            c.execute("UPDATE campaigns SET followers_gained = followers_gained + 1 WHERE id = ?", (campaign[0],))
            # Check if campaign is complete
            c.execute("SELECT coins_spent, followers_gained FROM campaigns WHERE id = ?", (campaign[0],))
            camp = c.fetchone()
            if camp[1] >= camp[0]:  # followers_gained >= coins_spent
                c.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign[0],))
                await context.bot.send_message(campaign[1], f"Your campaign for {followed_user} has reached {camp[1]} followers and is now complete! 🎉")
            conn.commit()
            await update.message.reply_text(f"Success! You earned 10 coins for following {followed_user}! 🎉")
            logger.info(f"User {telegram_id} followed {followed_user} and earned 10 coins")
        else:
            await update.message.reply_text(f"You are not following {followed_user}. Please follow them first.")
            logger.info(f"User {telegram_id} attempted to follow {followed_user} but is not following")
    except Exception as e:
        logger.error(f"Follow check error: {e}")
        await update.message.reply_text("Error verifying follow. Try again later.")
    finally:
        conn.close()

# Create campaign: /campaign <coins>
async def campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await update.message.reply_text("Please provide the number of coins: /campaign <coins>")
            return
        
        coins = int(args[0])
        if coins < 10:
            await update.message.reply_text("Minimum 10 coins required to start a campaign.")
            return
        
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT insta_username, coins FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if not user:
            await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
            return
        
        if user[1] < coins:
            await update.message.reply_text(f"You only have {user[1]} coins. Earn more by following users!")
            return
        
        c.execute("INSERT INTO campaigns (telegram_id, insta_username, coins_spent) VALUES (?, ?, ?)",
                  (telegram_id, user[0], coins))
        c.execute("UPDATE users SET coins = coins - ? WHERE telegram_id = ?", (coins, telegram_id))
        conn.commit()
        await update.message.reply_text(f"Campaign created! Your Instagram ID {user[0]} is now being promoted for {coins} followers (1 coin = 1 follower).")
        logger.info(f"User {telegram_id} created campaign for {user[0]} with {coins} coins")
    except Exception as e:
        logger.error(f"Error in /campaign: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# View campaigns: /mycampaigns
async def mycampaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT id, insta_username, coins_spent, followers_gained, active FROM campaigns WHERE telegram_id = ?", (telegram_id,))
        campaigns = c.fetchall()
        if not campaigns:
            await update.message.reply_text("You have no campaigns.")
            return
        
        msg = "Your Campaigns:\n"
        for camp in campaigns:
            status = "Active" if camp[4] else "Inactive"
            msg += f"ID: {camp[0]} | Username: {camp[1]} | Coins Spent: {camp[2]} | Followers Gained: {camp[3]} | Status: {status}\n"
        
        await update.message.reply_text(msg)
        logger.info(f"User {telegram_id} viewed campaigns")
    except Exception as e:
        logger.error(f"Error in /mycampaigns: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Deactivate campaign: /deactivate <campaign_id>
async def deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        args = context.args
        if len(args) != 1 or not args[0].isdigit():
            await update.message.reply_text("Please provide the campaign ID: /deactivate <campaign_id>")
            return
        
        campaign_id = int(args[0])
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT telegram_id FROM campaigns WHERE id = ? AND active = 1", (campaign_id,))
        campaign = c.fetchone()
        if not campaign or campaign[0] != telegram_id:
            await update.message.reply_text("Invalid campaign ID or you don't own this campaign.")
            return
        
        c.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign_id,))
        conn.commit()
        await update.message.reply_text(f"Campaign {campaign_id} deactivated.")
        logger.info(f"User {telegram_id} deactivated campaign {campaign_id}")
    except Exception as e:
        logger.error(f"Error in /deactivate: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Check balance: /balance
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("SELECT coins FROM users WHERE telegram_id = ?", (telegram_id,))
        user = c.fetchone()
        if not user:
            await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
            return
        await update.message.reply_text(f"Your balance: {user[0]} coins")
        logger.info(f"User {telegram_id} checked balance: {user[0]} coins")
    except Exception as e:
        logger.error(f"Error in /balance: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Logout command
async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        telegram_id = update.message.from_user.id
        conn = sqlite3.connect('insta_bot.db')
        c = conn.cursor()
        
        c.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        c.execute("UPDATE campaigns SET active = 0 WHERE telegram_id = ?", (telegram_id,))
        conn.commit()
        
        if telegram_id in insta_clients:
            del insta_clients[telegram_id]
        
        await update.message.reply_text("Successfully logged out.")
        logger.info(f"User {telegram_id} logged out")
    except Exception as e:
        logger.error(f"Error in /logout: {e}")
        await update.message.reply_text("An error occurred. Please try again.")
    finally:
        conn.close()

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("An error occurred. Please try again later.")

# Webhook endpoint
@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        logger.debug("Received webhook request")
        if request.is_json:
            json_data = request.get_json()
            update = Update.de_json(json_data, application.bot)
            if update:
                await application.process_update(update)
                logger.debug("Webhook update processed successfully")
                return jsonify({"status": "ok"}), 200
            else:
                logger.error("Invalid update received")
                return jsonify({"status": "error", "message": "Invalid update"}), 400
        else:
            logger.error("Webhook received non-JSON data")
            return jsonify({"status": "error", "message": "Invalid content type"}), 400
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Health check endpoint
@app.route('/')
def home():
    return "Bot is running!"

# Initialize bot
async def init_bot():
    global application
    try:
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not bot_token:
            logger.error("TELEGRAM_BOT_TOKEN environment variable not set")
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")
        
        application = Application.builder().token(bot_token).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("login", login))
        application.add_handler(CommandHandler("suggest", suggest))
        application.add_handler(CommandHandler("followed", followed))
        application.add_handler(CommandHandler("campaign", campaign))
        application.add_handler(CommandHandler("mycampaigns", mycampaigns))
        application.add_handler(CommandHandler("deactivate", deactivate))
        application.add_handler(CommandHandler("balance", balance))
        application.add_handler(CommandHandler("logout", logout))
        application.add_error_handler(error_handler)
        
        # Initialize application
        await application.initialize()
        await application.start()
        
        # Set webhook
        webhook_url = os.getenv('WEBHOOK_URL', 'https://unsightly-hinda-imdigitalvasu-3-80ce8ee1.koyeb.app/webhook')
        await application.bot.set_webhook(url=webhook_url, max_connections=40)
        logger.info(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.error(f"Bot initialization error: {e}")
        raise

# Run bot
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(init_bot())
        loop.run_forever()
    except Exception as e:
        logger.error(f"Bot run error: {e}")
    finally:
        loop.close()

if __name__ == '__main__':
    try:
        # Start bot in a separate thread
        import threading
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.start()
        logger.info("Bot thread started")
        
        # Run Flask app
        app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
    except Exception as e:
        logger.error(f"Main execution error: {e}")

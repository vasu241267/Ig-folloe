import sqlite3
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from instagrapi import Client
import hashlib
import json
import random
import string
from flask import Flask
import threading
import asyncio

# Flask app for Koyeb
app = Flask(__name__)

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup
def init_db():
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
    conn.close()

# Initialize database
init_db()

# Instagram client cache
insta_clients = {}

# Generate random referral code
def generate_referral_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Invite friends with your referral code to earn 10 coins per signup!"
    )
    await update.message.reply_text(welcome_msg)
    conn.close()

# Login command: /login <username> <password> [<referral_code>]
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Please provide username and password: /login <username> <password> [<referral_code>]")
        return
    
    username, password = args[0], args[1]
    referral_code = args[2] if len(args) > 2 else None
    
    try:
        cl = Client()
        cl.login(username, password)
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
        conn.close()
        
        insta_clients[telegram_id] = cl
        bonus_msg = "You received a 5-coin signup bonus! 🎁" if coins else ""
        await update.message.reply_text(f"Successfully logged in as {username}! {bonus_msg}")
    except Exception as e:
        logger.error(f"Login error: {e}")
        await update.message.reply_text("Login failed. Check your credentials or try again later.")

# Suggest users to follow
async def suggest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    conn = sqlite3.connect('insta_bot.db')
    c = conn.cursor()
    
    c.execute("SELECT insta_username FROM users WHERE telegram_id = ?", (telegram_id,))
    user = c.fetchone()
    if not user:
        await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
        conn.close()
        return
    
    c.execute("SELECT insta_username FROM campaigns WHERE active = 1 AND telegram_id != ?", (telegram_id,))
    campaigns = c.fetchall()
    if not campaigns:
        await update.message.reply_text("No active campaigns found to follow.")
        conn.close()
        return
    
    suggested_user = random.choice(campaigns)[0]
    await update.message.reply_text(f"Suggested user to follow: {suggested_user}\nFollow them on Instagram and then use /followed {suggested_user} to earn 10 coins!")
    
    conn.close()

# Followed command: /followed <username>
async def followed(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        conn.close()
        return
    
    c.execute("SELECT id, telegram_id FROM campaigns WHERE insta_username = ? AND active = 1", (followed_user,))
    campaign = c.fetchone()
    if not campaign:
        await update.message.reply_text(f"No active campaign found for {followed_user}.")
        conn.close()
        return
    
    try:
        cl = insta_clients.get(telegram_id)
        if not cl:
            cl = Client()
            cl.set_settings(json.loads(user[1]))
            insta_clients[telegram_id] = cl
        
        followed_user_id = cl.user_id_from_username(followed_user)
        is_following = cl.user_following(cl.user_id, followed_user_id)
        if is_following:
            c.execute("UPDATE users SET coins = coins + 10 WHERE telegram_id = ?", (telegram_id,))
            c.execute("UPDATE campaigns SET followers_gained = followers_gained + 1 WHERE id = ?", (campaign[0],))
            # Check if campaign is complete
            c.execute("SELECT coins_spent, followers_gained FROM campaigns WHERE id = ?", (campaign[0],))
            camp = c.fetchone()
            if camp[1] >= camp[0]:  # followers_gained >= coins_spent
                c.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign[0],))
                await context.bot.send_message(campaign[1], f"Your campaign for {followed_user} has reached {camp[1]} followers and is now complete!")
            conn.commit()
            await update.message.reply_text(f"Success! You earned 10 coins for following {followed_user}! 🎉")
        else:
            await update.message.reply_text(f"You are not following {followed_user}. Please follow them first.")
    except Exception as e:
        logger.error(f"Follow check error: {e}")
        await update.message.reply_text("Error verifying follow. Try again later.")
    
    conn.close()

# Create campaign: /campaign <coins>
async def campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        conn.close()
        return
    
    if user[1] < coins:
        await update.message.reply_text(f"You only have {user[1]} coins. Earn more by following users!")
        conn.close()
        return
    
    c.execute("INSERT INTO campaigns (telegram_id, insta_username, coins_spent) VALUES (?, ?, ?)",
              (telegram_id, user[0], coins))
    c.execute("UPDATE users SET coins = coins - ? WHERE telegram_id = ?", (coins, telegram_id))
    conn.commit()
    await update.message.reply_text(f"Campaign created! Your Instagram ID {user[0]} is now being promoted for {coins} followers (1 coin = 1 follower).")
    
    conn.close()

# View campaigns: /mycampaigns
async def mycampaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    conn = sqlite3.connect('insta_bot.db')
    c = conn.cursor()
    
    c.execute("SELECT id, insta_username, coins_spent, followers_gained, active FROM campaigns WHERE telegram_id = ?", (telegram_id,))
    campaigns = c.fetchall()
    if not campaigns:
        await update.message.reply_text("You have no campaigns.")
        conn.close()
        return
    
    msg = "Your Campaigns:\n"
    for camp in campaigns:
        status = "Active" if camp[4] else "Inactive"
        msg += f"ID: {camp[0]} | Username: {camp[1]} | Coins Spent: {camp[2]} | Followers Gained: {camp[3]} | Status: {status}\n"
    
    await update.message.reply_text(msg)
    conn.close()

# Deactivate campaign: /deactivate <campaign_id>
async def deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        conn.close()
        return
    
    c.execute("UPDATE campaigns SET active = 0 WHERE id = ?", (campaign_id,))
    conn.commit()
    await update.message.reply_text(f"Campaign {campaign_id} deactivated.")
    
    conn.close()

# Check balance: /balance
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    conn = sqlite3.connect('insta_bot.db')
    c = conn.cursor()
    
    c.execute("SELECT coins FROM users WHERE telegram_id = ?", (telegram_id,))
    user = c.fetchone()
    if not user:
        await update.message.reply_text("Please login first using /login <username> <password> [<referral_code>]")
    else:
        await update.message.reply_text(f"Your balance: {user[0]} coins")
    
    conn.close()

# Logout command
async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    conn = sqlite3.connect('insta_bot.db')
    c = conn.cursor()
    
    c.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    c.execute("UPDATE campaigns SET active = 0 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()
    
    if telegram_id in insta_clients:
        del insta_clients[telegram_id]
    
    await update.message.reply_text("Successfully logged out.")

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    await update.message.reply_text("An error occurred. Please try again later.")

# Bot setup
async def bot_main():
    # Replace with your actual Telegram Bot Token
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
    application = Application.builder().token(bot_token).build()
    
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
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

# Flask route for Koyeb health check
@app.route('/')
def home():
    return "Bot is running!"

# Run bot in a separate thread
def run_bot():
    asyncio.run(bot_main())

if __name__ == '__main__':
    # Start bot in a thread
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    # Run Flask app for Koyeb
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8000)))

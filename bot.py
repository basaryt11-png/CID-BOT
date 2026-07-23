import os, sys, math, logging, re, subprocess, shutil, asyncio, time

# ✅ FIX: Railway/Docker এ stdout বাফার হয়ে থাকে, ফলে print() করা ডিবাগ লগ সাথে সাথে
# দেখা যাচ্ছিল না। এখানে line-buffering ফোর্স করা হলো যাতে প্রতিটা print সাথে সাথে
# Deploy Logs এ চলে আসে।
sys.stdout.reconfigure(line_buffering=True)

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import yt_dlp
import imageio_ffmpeg

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

API_ID = int(os.environ.get("API_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "")
TOKEN = os.environ.get("TOKEN", "")

if not API_ID or not API_HASH or not TOKEN:
    raise RuntimeError(
        "❌ TOKEN, API_ID এবং API_HASH — এই তিনটা Environment Variable Railway তে সেট করা আছে কিনা চেক করো!\n"
        "API_ID/API_HASH পাবে: https://my.telegram.org -> API Development Tools"
    )

DOWNLOAD_DIR = "downloads"
DEVELOPER = "BY : RH RATUL"
ADMIN_USERNAME = "@Ratul0070"
START_TIME = time.time()
PART_DURATION_SEC = 600  

logging.basicConfig(level=logging.ERROR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ✅ NEW: cookies আর GitHub রিপোতে ফাইল হিসেবে রাখা হচ্ছে না (এটা সিকিউরিটি রিস্ক ছিল)।
# এখন Railway এর YOUTUBE_COOKIES এনভায়রনমেন্ট ভ্যারিয়েবল থেকে কনটেন্ট পড়ে
# রানটাইমে একটা লোকাল cookies.txt বানানো হচ্ছে (এটা .gitignore এ রাখা, git এ কমিট হয় না)।
COOKIES_PATH = "cookies.txt"
_cookies_env = os.environ.get("YOUTUBE_COOKIES", "")
if _cookies_env.strip():
    with open(COOKIES_PATH, "w", encoding="utf-8") as _f:
        _f.write(_cookies_env)
    print("[DEBUG] YOUTUBE_COOKIES env var থেকে cookies.txt তৈরি হয়েছে")
elif os.path.exists(COOKIES_PATH):
    print("[DEBUG] YOUTUBE_COOKIES env var নেই, কিন্তু লোকাল cookies.txt পাওয়া গেছে (পুরনো ফাইল?)")
else:
    print("[DEBUG] কোনো cookies পাওয়া যায়নি — cookies ছাড়াই চেষ্টা করা হবে")

SESSIONS = {}

app = Client(
    "cid_bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TOKEN,
    in_memory=True,   
)


# ─────────────────────────────────────────────
#  yt-dlp helpers (Stable Bypass with Cookies)
# ─────────────────────────────────────────────

def _detect_js_runtime():
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            # ✅ FIX: yt-dlp আশা করে {runtime: {config-dict}}, শুধু পাথ স্ট্রিং না
            return {name: {"path": path}}
    return None


class _YDLLogger:
    """✅ NEW: quiet/no_warnings আসল কারণ (nsig solve failed ইত্যাদি) লুকিয়ে ফেলছিল।
    এই লগার দিয়ে yt-dlp এর সব internal warning/error Railway লগে দেখা যাবে।"""
    def debug(self, msg):
        if msg.startswith('[debug] '):
            return  # খুব বেশি ভার্বোজ, স্কিপ করা হলো
        print(f"[YTDLP] {msg}")

    def info(self, msg):
        print(f"[YTDLP] {msg}")

    def warning(self, msg):
        print(f"[YTDLP-WARNING] {msg}")

    def error(self, msg):
        print(f"[YTDLP-ERROR] {msg}")


def _base_ydl_opts():
    js_runtimes = _detect_js_runtime()
    print(f"[DEBUG] JS runtime detected: {js_runtimes}")
    opts = {
        "quiet": True,
        "no_warnings": False,   # ✅ FIX: warning লুকানো বন্ধ
        "logger": _YDLLogger(), # ✅ FIX: সব warning/error এখন প্রিন্ট হবে
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "nocheckcertificate": True,
        # ✅ FIX: android_vr ক্লায়েন্ট cookies-এর সাথে কাজ করে না (yt-dlp নিজেই স্কিপ করে
        # দেয়)। cookies ছাড়াই android_vr ব্যবহার করাটাই বেশি স্টেবল — এটাই PO Token এবং
        # bot-verification দুটোই এড়িয়ে যায়। android/tv_downgraded ফলব্যাক হিসেবে রাখা হলো।
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "android", "tv_downgraded"]
            }
        },
        "retries": 15,
        "fragment_retries": 15,
        "socket_timeout": 30,
        # ✅ FIX: এটা অবশ্যই একটা LIST হতে হবে, স্ট্রিং না — স্ট্রিং দিলে yt-dlp সেটাকে
        # অক্ষর-বাই-অক্ষর আলাদা কম্পোনেন্ট হিসেবে ধরে নেয় (এটাই আসল বাগ ছিল)
        "remote_components": ["ejs:github"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        }
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes

    # ✅ FIX: cookiefile ইচ্ছাকৃতভাবে yt-dlp তে পাস করা হচ্ছে না — cookies থাকলে yt-dlp
    # android_vr ক্লায়েন্ট বাদ দিয়ে দেয় (যেটা PO Token ছাড়াই কাজ করে)। cookies.txt ফাইল
    # ডিস্কে থেকে যাচ্ছে (ভবিষ্যতে দরকার হলে), কিন্তু এই মুহূর্তে ব্যবহার হচ্ছে না।
    if os.path.exists(COOKIES_PATH):
        age_min = (time.time() - os.path.getmtime(COOKIES_PATH)) / 60
        print(f"[DEBUG] cookies.txt আছে (বয়স: {age_min:.1f} মিনিট) কিন্তু android_vr এর জন্য ব্যবহার হচ্ছে না")
    else:
        print("[DEBUG] cookies.txt নেই")

    return opts


def get_available_qualities(url):
    ydl_opts = _base_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            # ✅ FIX: এরর আর নিঃশব্দে গেলা হবে না, Railway Deploy Logs এ পুরো এরর প্রিন্ট হবে
            print(f"[DEBUG] extract_info FAILED: {repr(e)}")
            return [], {}, "Video"

    if not info:
        print("[DEBUG] extract_info returned empty info (None)")
        return [], {}, "Video"

    print(f"[DEBUG] info keys: {list(info.keys())[:20]}")
    print(f"[DEBUG] info._type: {info.get('_type')}, availability: {info.get('availability')}")

    formats = info.get("formats", [])
    print(f"[DEBUG] Total formats returned: {len(formats)}")
    sample = [(f.get("format_id"), f.get("height"), f.get("vcodec"), f.get("format_note")) for f in formats]
    print(f"[DEBUG] Formats sample: {sample}")

    quality_map = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if h not in quality_map or (size and size > (quality_map[h] or 0)):
            quality_map[h] = size
    heights = sorted(quality_map.keys(), reverse=True)
    print(f"[DEBUG] Heights with video found: {heights}")
    return heights, quality_map, info.get("title", "Video")


def make_progress_hook(progress):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            progress["downloaded"] = downloaded
            progress["total"] = total
            if total:
                progress["percent"] = int(downloaded / total * 100)
            progress["speed"] = d.get("speed") or 0
            progress["eta"] = d.get("eta") or 0
        elif d.get("status") == "finished":
            progress["percent"] = 100
    return hook


def download_video(url, output_path, height=360, progress_hook=None):
    ydl_opts = _base_ydl_opts()
    ydl_opts.update({
        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best/bestaudio",
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "progress_hooks": [progress_hook] if progress_hook else [],
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        fname = ydl.prepare_filename(info)
        for ext in [".webm", ".mkv"]:
            if fname.endswith(ext):
                fname = fname[: -len(ext)] + ".mp4"
        return fname


# ─────────────────────────────────────────────
#  ffmpeg helpers
# ─────────────────────────────────────────────

def to_sec(t):
    t = t.strip()
    if ":" in t:
        p = t.split(":")
        return int(p[0]) * 60 + float(p[1])
    return float(t)


def get_duration(path):
    result = subprocess.run([FFMPEG, '-i', path], capture_output=True, text=True)
    m = re.search(r'Duration: (\d+):(\d+):(\d+\.?\d*)', result.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0


def prepare_promo(inp, out):
    cmd = [FFMPEG, '-i', inp, '-vf', 'scale=640:360',
           '-c:v', 'libx264', '-preset', 'ultrafast',
           '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-y', out]
    subprocess.run(cmd, capture_output=True)
    return out


def trim_video(inp, start, end, out):
    cmd = [FFMPEG, '-i', inp, '-ss', str(to_sec(start)), '-to', str(to_sec(end)),
           '-c', 'copy', '-y', out]
    subprocess.run(cmd, capture_output=True)
    return out


def merge_fast(video1, video2, out):
    list_file = out.replace('.mp4', '_list.txt')
    with open(list_file, 'w') as f:
        f.write(f"file '{os.path.abspath(video1)}'\n")
        f.write(f"file '{os.path.abspath(video2)}'\n")
    cmd = [FFMPEG, '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', '-y', out]
    subprocess.run(cmd, capture_output=True)
    try:
        os.remove(list_file)
    except:
        pass
    return out


def insert_promo_at_time(main, promo, insert_sec, out):
    uid = out.replace('.mp4', '')
    part1 = f"{uid}_p1.mp4"
    part2 = f"{uid}_p2.mp4"
    merged1 = f"{uid}_m1.mp4"
    dur = get_duration(main)
    if insert_sec >= dur:
        insert_sec = dur / 2
    subprocess.run([FFMPEG, '-i', main, '-t', str(insert_sec), '-c', 'copy', '-y', part1], capture_output=True)
    subprocess.run([FFMPEG, '-i', main, '-ss', str(insert_sec), '-c', 'copy', '-y', part2], capture_output=True)
    merge_fast(part1, promo, merged1)
    merge_fast(merged1, part2, out)
    for f in [part1, part2, merged1]:
        try:
            os.remove(f)
        except:
            pass
    return out


def add_promo_to_part(part, promo, promo_pos, promo_time, out):
    if promo_pos == "start":
        merge_fast(promo, part, out)
    elif promo_pos == "end":
        merge_fast(part, promo, out)
    elif promo_pos == "custom" and promo_time:
        insert_promo_at_time(part, promo, to_sec(promo_time), out)
    else:
        insert_promo_at_time(part, promo, get_duration(part) / 2, out)
    return out


def split_video(inp, part_seconds=PART_DURATION_SEC):
    total = get_duration(inp)
    if total <= part_seconds:
        return [inp]
    n = math.ceil(total / part_seconds)
    parts = []
    base = inp.replace('.mp4', '')
    for i in range(n):
        p = f"{base}_part{i + 1}.mp4"
        start = i * part_seconds
        subprocess.run([FFMPEG, '-i', inp, '-ss', str(start), '-t', str(part_seconds),
                        '-c', 'copy', '-y', p], capture_output=True)
        if os.path.exists(p):
            parts.append(p)
    return parts if parts else [inp]


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except:
            pass


# ─────────────────────────────────────────────
#  Progress bar helpers
# ─────────────────────────────────────────────

def make_bar(percent, length=18):
    filled = int(length * percent / 100)
    return "▓" * filled + "░" * (length - filled)


def fmt_mb(b):
    return f"{(b or 0) / 1024 / 1024:.1f}MB"


def fmt_size(b):
    if not b:
        return ""
    mb = b / 1024 / 1024
    return f" (~{mb:.0f}MB)"


def fmt_time(sec):
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


async def download_progress_watcher(message, progress, name):
    last = -1
    while not progress.get("done"):
        pct = progress.get("percent", 0)
        if pct != last:
            text = (
                f"📥 ডাউনলোড হচ্ছে...\n"
                f"👤 ইউজার: **{name}**\n\n"
                f"[{make_bar(pct)}] {pct}%\n"
                f"📦 {fmt_mb(progress.get('downloaded'))} / {fmt_mb(progress.get('total'))}\n"
                f"⚡ স্পিড: {fmt_mb(progress.get('speed'))}/s\n"
                f"⏳ বাকি সময়: {fmt_time(progress.get('eta'))}"
            )
            try:
                await message.edit_text(text)
            except Exception:
                pass
            last = pct
        await asyncio.sleep(2)
    try:
        await message.edit_text(f"✅ ডাউনলোড সম্পন্ন!\n👤 **{name}**")
    except Exception:
        pass


def make_upload_progress(message, name, part_i, total_parts):
    state = {"last_pct": -1, "last_time": 0.0}

    async def cb(current, total):
        now = time.time()
        pct = int(current * 100 / total) if total else 0
        if pct != state["last_pct"] and (now - state["last_time"] >= 2 or pct == 100):
            text = (
                f"📤 Part {part_i}/{total_parts} আপলোড হচ্ছে...\n"
                f"👤 ইউজার: **{name}**\n\n"
                f"[{make_bar(pct)}] {pct}%\n"
                f"📦 {fmt_mb(current)} / {fmt_mb(total)}"
            )
            try:
                await message.edit_text(text)
            except Exception:
                pass
            state["last_pct"] = pct
            state["last_time"] = now
    return cb


# ─────────────────────────────────────────────
#  Handlers
# ─────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    chat_id = message.chat.id
    user = message.from_user
    name = (user.first_name if user else None) or (f"@{user.username}" if user and user.username else "Friend")
    SESSIONS[chat_id] = {"state": "LINK", "user_name": name}

    elapsed = int(time.time() - START_TIME)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    text = (
        f"Hello👋 **{name}** I am one and only Downloader Bot on Telegram."
        f"You can use me to Download Any Youtube Videos Past Video links to Telegram ⤵️\n\n"
        f"Here I support Direct Downlode Video And Many Part If you found any issue please "
        f"contact Support {ADMIN_USERNAME}\n\n"
        f"📤 Bot Uptime: hours:{h:02d} minutes:{m:02d} and seconds:{s:02d} ago"
    )
    await message.reply_text(text)


@app.on_message(filters.command("cancel"))
async def cancel_handler(client, message):
    SESSIONS.pop(message.chat.id, None)
    await message.reply_text("❌ বাতিল!")


@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel"]))
async def text_router(client, message):
    chat_id = message.chat.id
    session = SESSIONS.get(chat_id)
    state = session.get("state") if session else None

    if state == "TRIM":
        await handle_trim_text(message, session)
    elif state == "PROMO_TIME":
        await handle_promo_time_text(client, message, session)
    else:
        await handle_link(message)


async def handle_link(message):
    url = message.text.strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        await message.reply_text("❌ সঠিক YouTube লিংক দাও!")
        return

    chat_id = message.chat.id
    user = message.from_user
    name = (user.first_name if user else None) or (f"@{user.username}" if user and user.username else "Friend")
    SESSIONS[chat_id] = {"state": "LINK", "url": url, "user_name": name}

    checking_msg = await message.reply_text("🔍 ভিডিওর কোয়ালিটি চেক করছি...")
    loop = asyncio.get_running_loop()
    try:
        heights, quality_map, title = await loop.run_in_executor(None, get_available_qualities, url)
    except Exception as e:
        await checking_msg.edit_text(f"❌ লিংক থেকে তথ্য আনা যায়নি!\n`{str(e)[:200]}`")
        return

    if not heights:
        await checking_msg.edit_text(
            "❌ **ইউটিউব আপনার বটকে ব্লক করেছে বা ভিডিওটি প্রটেক্টেড!**\n\n"
            "ভিডিওর কোনো কোয়ালিটি বা ফরম্যাট পাওয়া যায়নি।\n\n"
            "💡 **সমাধান:** একটি নতুন/ফ্রেশ `cookies.txt` ফাইল বটের ফোল্ডারে আপলোড করে রিস্টার্ট দিন।"
        )
        return

    SESSIONS[chat_id]["quality_map"] = quality_map
    SESSIONS[chat_id]["state"] = "QUALITY"

    kb = []
    for h in heights:
        size_text = fmt_size(quality_map.get(h))
        kb.append([InlineKeyboardButton(f"🎞️ {h}p {size_text}", callback_data=f"q_{h}")])

    await checking_msg.edit_text(
        f"🎬 **{title}**\n\nকোন কোয়ালিটিতে ডাউনলোড করতে চাও?",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def handle_trim_text(message, session):
    try:
        p = message.text.strip().split("-")
        s, e = p[0].strip(), p[1].strip()
        to_sec(s)
        to_sec(e)
        session["trim"] = (s, e)
    except Exception:
        await message.reply_text("❌ Format ঠিক নেই!\nউদাহরণ: `1:30 - 5:45`")
        return
    await ask_promo(message, session)


async def handle_promo_time_text(client, message, session):
    try:
        t = message.text.strip()
        to_sec(t)
        session["promo_time"] = t
    except Exception:
        await message.reply_text("❌ সঠিক সময় দাও!\nউদাহরণ: `2:30`")
        return
    await message.reply_text("⏳ Processing শুরু হচ্ছে...")
    await process_video(client, message, session)


async def ask_promo(message, session):
    session["state"] = "PROMO_CHOICE"
    kb = [
        [InlineKeyboardButton("📎 হ্যাঁ Promo যোগ করব", callback_data="promo_yes")],
        [InlineKeyboardButton("⏭️ না লাগবে না", callback_data="promo_no")]
    ]
    await message.reply_text("📎 Promo ক্লিপ যোগ করবে?", reply_markup=InlineKeyboardMarkup(kb))


@app.on_callback_query()
async def callback_router(client, cq):
    chat_id = cq.message.chat.id
    session = SESSIONS.get(chat_id)
    data = cq.data
    await cq.answer()

    if not session:
        await cq.edit_message_text("সেশনের মেয়াদ শেষ হয়ে গেছে! আবার /start দাও।")
        return

    if data.startswith("q_"):
        await quality_choice(cq, session)
    elif data.startswith("trim_"):
        await trim_choice(cq, session)
    elif data.startswith("promo_"):
        await promo_choice(client, cq, session)
    elif data.startswith("pos_"):
        await promo_position(client, cq, session)


async def quality_choice(cq, session):
    height = int(cq.data.split("_")[1])
    session["height"] = height
    session["state"] = "TRIM_ASK"
    kb = [
        [InlineKeyboardButton("✂️ হ্যাঁ Trim করব", callback_data="trim_yes")],
        [InlineKeyboardButton("⏭️ না পুরো ভিডিও", callback_data="trim_no")]
    ]
    await cq.edit_message_text(f"✅ {height}p সিলেক্ট করা হয়েছে!\n\nTrim করতে চাও?", reply_markup=InlineKeyboardMarkup(kb))


async def trim_choice(cq, session):
    if cq.data == "trim_yes":
        session["state"] = "TRIM"
        await cq.edit_message_text("✂️ সময় দাও:\n`শুরু - শেষ`\nউদাহরণ: `1:30 - 5:45`")
        return
    session["trim"] = None
    await ask_promo(cq.message, session)


async def promo_choice(client, cq, session):
    if cq.data == "promo_no":
        session["promo_path"] = None
        await cq.edit_message_text("⏳ Processing শুরু হচ্ছে...")
        await process_video(client, cq.message, session)
        return
    session["state"] = "PROMO_FILE"
    await cq.edit_message_text("📎 Promo ক্লিপ পাঠাও!\n⚠️ **File হিসেবে পাঠাও!**")


@app.on_message(filters.video | filters.document)
async def promo_file_handler(client, message):
    chat_id = message.chat.id
    session = SESSIONS.get(chat_id)
    if not session or session.get("state") != "PROMO_FILE":
        return  

    uid = str(chat_id)
    promo_raw = f"{DOWNLOAD_DIR}/{uid}_promo_raw"
    promo_path = f"{DOWNLOAD_DIR}/{uid}_promo.mp4"

    await message.download(file_name=promo_raw)
    await message.reply_text("⚙️ Promo prepare হচ্ছে...")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, prepare_promo, promo_raw, promo_path)
    cleanup(promo_raw)

    session["promo_path"] = promo_path
    session["state"] = "PROMO_POSITION"
    kb = [
        [InlineKeyboardButton("⏮️ শুরুতে", callback_data="pos_start")],
        [InlineKeyboardButton("⏭️ শেষে", callback_data="pos_end")],
        [InlineKeyboardButton("⏱️ নির্দিষ্ট সময়ে", callback_data="pos_custom")],
        [InlineKeyboardButton("🎯 মাঝখানে", callback_data="pos_middle")]
    ]
    await message.reply_text("✅ Promo ready!\nকোথায় যোগ করব?", reply_markup=InlineKeyboardMarkup(kb))


async def promo_position(client, cq, session):
    pos_map = {"pos_start": "start", "pos_end": "end", "pos_middle": "middle", "pos_custom": "custom"}
    session["promo_pos"] = pos_map[cq.data]
    if cq.data == "pos_custom":
        session["state"] = "PROMO_TIME"
        await cq.edit_message_text("⏱️ কত সময়ে?\nউদাহরণ: `2:30`")
        return
    await cq.edit_message_text("⏳ Processing শুরু হচ্ছে...")
    await process_video(client, cq.message, session)


# ─────────────────────────────────────────────
#  Core processing
# ─────────────────────────────────────────────

async def process_video(client, message, session):
    chat_id = message.chat.id
    url = session["url"]
    height = session.get("height", 360)
    trim = session.get("trim")
    promo_path = session.get("promo_path")
    promo_pos = session.get("promo_pos", "middle")
    promo_time = session.get("promo_time")
    name = session.get("user_name", "User")

    uid = str(chat_id)
    raw = f"{DOWNLOAD_DIR}/{uid}_raw.mp4"
    trimmed = f"{DOWNLOAD_DIR}/{uid}_trimmed.mp4"
    loop = asyncio.get_running_loop()

    try:
        progress_msg = await message.reply_text(f"📥 ডাউনলোড শুরু হচ্ছে...\n👤 ইউজার: **{name}**")
        progress = {"percent": 0, "done": False, "downloaded": 0, "total": 0, "speed": 0, "eta": 0}
        watcher_task = asyncio.create_task(download_progress_watcher(progress_msg, progress, name))

        hook = make_progress_hook(progress)
        try:
            current = await loop.run_in_executor(None, download_video, url, raw, height, hook)
        finally:
            progress["done"] = True
            await watcher_task

        if trim:
            await message.reply_text(f"✂️ Trimming: {trim[0]} → {trim[1]}")
            await loop.run_in_executor(None, trim_video, current, trim[0], trim[1], trimmed)
            current = trimmed

        await message.reply_text("📦 ১০ মিনিট করে ভাগ করছি...")
        parts = await loop.run_in_executor(None, split_video, current)
        total_parts = len(parts)

        for i, part in enumerate(parts, 1):
            send_path = part
            if promo_path and os.path.exists(promo_path):
                await message.reply_text(f"📎 Part {i}/{total_parts} এ Promo যোগ করছি...")
                promo_out = part.replace('.mp4', '_promo.mp4')
                await loop.run_in_executor(None, add_promo_to_part, part, promo_path, promo_pos, promo_time, promo_out)
                if os.path.exists(promo_out):
                    send_path = promo_out

            upload_msg = await message.reply_text(
                f"📤 Part {i}/{total_parts} আপলোড হচ্ছে...\n👤 ইউজার: **{name}**\n\n[{make_bar(0)}] 0%"
            )
            upload_cb = make_upload_progress(upload_msg, name, i, total_parts)

            await client.send_video(
                chat_id=chat_id,
                video=send_path,
                caption=f"🎬 Part {i}/{total_parts}\n👤 **{name}**\n\n_{DEVELOPER}_",
                progress=upload_cb,
            )
            try:
                await upload_msg.delete()
            except Exception:
                pass
            if send_path != part:
                cleanup(send_path)

        await message.reply_text(f"✅ Done! {total_parts}টা Part পাঠানো হয়েছে!\n👤 **{name}**\n\n_{DEVELOPER}_")

    except Exception as e:
        err = str(e)
        if "Requested format is not available" in err:
            await message.reply_text(
                "❌ Format Error!\nভিডিওটার format পাওয়া যাচ্ছে না। এটি সম্ভবত প্রিমিয়াম বা হাইলি-প্রটেক্টেড ভিডিও।\n"
                "কিছুক্ষণ পরে আবার চেষ্টা করো অথবা অন্য লিংক দাও।"
            )
        elif "Video unavailable" in err:
            await message.reply_text("❌ ভিডিওটা available নেই বা private!")
        else:
            await message.reply_text(f"❌ Error:\n`{err[:500]}`")

    finally:
        cleanup(raw, trimmed, promo_path or "")
        SESSIONS.pop(chat_id, None)


# ─────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────

print("✅ Bot চালু! BY : RH RATUL (Pyrogram / MTProto mode)")
app.run()

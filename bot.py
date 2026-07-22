import os, math, logging, re, subprocess, shutil, asyncio, time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ConversationHandler
import yt_dlp
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
TOKEN = os.environ.get("TOKEN")
MAX_FILE_MB = 35
DOWNLOAD_DIR = "downloads"
DEVELOPER = "BY : RH RATUL"
ADMIN_USERNAME = "@Ratul0070"
START_TIME = time.time()

(WAITING_LINK, WAITING_QUALITY, WAITING_TRIM, WAITING_PROMO_CHOICE,
 WAITING_PROMO_FILE, WAITING_PROMO_POSITION, WAITING_PROMO_TIME) = range(7)

logging.basicConfig(level=logging.ERROR)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _detect_js_runtime():
    """
    ✅ FIX: node/deno PATH-এ আছে কিনা চেক করে yt-dlp কে এক্সপ্লিসিটলি path দেওয়া হয়,
    কারণ কখনো কখনো yt-dlp নিজে PATH থেকে runtime auto-detect করতে ব্যর্থ হয়।
    """
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            return {name: path}
    return None


def _base_ydl_opts():
    """Common yt-dlp options shared between info-extraction and download."""
    js_runtimes = _detect_js_runtime()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "tv", "android", "mweb"],
            }
        },
        "remote_components": "ejs:github",
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    return opts


def get_available_qualities(url):
    """
    ✅ NEW: ভিডিওর সব উপলব্ধ কোয়ালিটি (height) এবং আনুমানিক সাইজ বের করে।
    রিটার্ন করে: (heights_sorted_desc, {height: approx_filesize_bytes}, title)
    """
    ydl_opts = _base_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    quality_map = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if h not in quality_map or (size and size > (quality_map[h] or 0)):
            quality_map[h] = size

    heights = sorted(quality_map.keys(), reverse=True)
    return heights, quality_map, info.get("title", "Video")


def make_progress_hook(progress):
    """
    ✅ NEW: yt-dlp progress hook — ডাউনলোডের লাইভ পার্সেন্টেজ ও স্পিড progress dict-এ লিখে রাখে।
    এই dict একটা আলাদা asyncio টাস্ক পোল করে টেলিগ্রাম মেসেজ এডিট করে।
    """
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress["percent"] = int(downloaded / total * 100)
            speed = d.get("speed")
            if speed:
                progress["speed_str"] = f"⚡ {speed / 1024 / 1024:.2f} MB/s"
            eta = d.get("eta")
            if eta is not None:
                progress["eta_str"] = f"⏳ বাকি: {eta}s"
        elif status == "finished":
            progress["percent"] = 100
    return hook


def download_video(url, output_path, height=360, progress_hook=None):
    """
    yt-dlp দিয়ে ভিডিও ডাউনলোড করে। ইউজারের সিলেক্ট করা height অনুযায়ী format তৈরি হয়।
    """
    ydl_opts = _base_ydl_opts()
    ydl_opts.update({
        "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
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


def split_video(inp, max_mb=MAX_FILE_MB):
    size_mb = os.path.getsize(inp) / (1024 * 1024)
    if size_mb <= max_mb:
        return [inp]
    total = get_duration(inp)
    n = math.ceil(size_mb / max_mb)
    part_dur = total / n
    parts = []
    base = inp.replace('.mp4', '')
    for i in range(n):
        p = f"{base}_part{i + 1}.mp4"
        subprocess.run([FFMPEG, '-i', inp, '-ss', str(i * part_dur), '-t', str(part_dur),
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


def fmt_size(b):
    if not b:
        return ""
    mb = b / 1024 / 1024
    return f" (~{mb:.0f}MB)"


# ─────────────────────────────────────────────
#  Telegram Handlers
# ─────────────────────────────────────────────

async def start(update, context):
    user = update.effective_user
    name = user.first_name or (f"@{user.username}" if user.username else "Friend")
    elapsed = int(time.time() - START_TIME)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    text = (
        f"Hello👋{name} I am one and only Downloader Bot on Telegram."
        f"You can use me to Download Any Youtube Videos Past Video links to Telegram ⤵️\n\n"
        f"Here I support Direct Downlode Video And Many Part If you found any issue please "
        f"contact Support {ADMIN_USERNAME}\n\n"
        f"📤 Bot Uptime: hours:{h:02d} minutes:{m:02d} and seconds:{s:02d} ago"
    )
    await update.message.reply_text(text)
    return WAITING_LINK


async def receive_link(update, context):
    url = update.message.text.strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text("❌ সঠিক YouTube লিংক দাও!")
        return WAITING_LINK

    context.user_data.update({"url": url, "chat_id": update.message.chat_id})
    checking_msg = await update.message.reply_text("🔍 ভিডিওর কোয়ালিটি চেক করছি...")

    loop = asyncio.get_running_loop()
    try:
        heights, quality_map, title = await loop.run_in_executor(None, get_available_qualities, url)
    except Exception as e:
        await checking_msg.edit_text(
            f"❌ লিংক থেকে তথ্য আনা যায়নি!\nকিছুক্ষণ পরে আবার চেষ্টা করো অথবা অন্য লিংক দাও।\n`{str(e)[:200]}`",
            parse_mode="Markdown"
        )
        return WAITING_LINK

    if not heights:
        heights = [360]
        quality_map = {360: None}

    context.user_data["quality_map"] = quality_map

    # সবচেয়ে বেশি ব্যবহৃত রেজোলিউশনগুলো, উপলব্ধগুলো থেকে সর্বোচ্চ ৬টা দেখানো হবে
    common = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    show = [h for h in common if h in heights][:6]
    if not show:
        show = heights[:6]

    kb = [[InlineKeyboardButton(f"🎞️ {h}p{fmt_size(quality_map.get(h))}", callback_data=f"q_{h}")] for h in show]

    await checking_msg.edit_text(
        f"🎬 *{title}*\n\nকোন কোয়ালিটিতে ডাউনলোড করতে চাও?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return WAITING_QUALITY


async def quality_choice(update, context):
    q = update.callback_query
    await q.answer()
    height = int(q.data.split("_")[1])
    context.user_data["height"] = height

    kb = [
        [InlineKeyboardButton("✂️ হ্যাঁ Trim করব", callback_data="trim_yes")],
        [InlineKeyboardButton("⏭️ না পুরো ভিডিও", callback_data="trim_no")]
    ]
    await q.edit_message_text(
        f"✅ {height}p সিলেক্ট করা হয়েছে!\n\nTrim করতে চাও?",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return WAITING_TRIM


async def trim_choice(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "trim_yes":
        await q.edit_message_text("✂️ সময় দাও:\n`শুরু - শেষ`\nউদাহরণ: `1:30 - 5:45`",
                                  parse_mode="Markdown")
        return WAITING_TRIM
    context.user_data["trim"] = None
    return await ask_promo(q.message, context)


async def receive_trim(update, context):
    try:
        p = update.message.text.strip().split("-")
        s, e = p[0].strip(), p[1].strip()
        to_sec(s)
        to_sec(e)
        context.user_data["trim"] = (s, e)
    except:
        await update.message.reply_text("❌ Format ঠিক নেই!\nউদাহরণ: `1:30 - 5:45`",
                                        parse_mode="Markdown")
        return WAITING_TRIM
    return await ask_promo(update.message, context)


async def ask_promo(message, context):
    kb = [
        [InlineKeyboardButton("📎 হ্যাঁ Promo যোগ করব", callback_data="promo_yes")],
        [InlineKeyboardButton("⏭️ না লাগবে না", callback_data="promo_no")]
    ]
    await message.reply_text("📎 Promo ক্লিপ যোগ করবে?",
                             reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_PROMO_CHOICE


async def promo_choice(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "promo_no":
        context.user_data["promo_path"] = None
        await q.edit_message_text("⏳ Processing শুরু হচ্ছে...")
        await process_video(q.message, context)
        return ConversationHandler.END
    await q.edit_message_text("📎 Promo ক্লিপ পাঠাও!\n⚠️ *File হিসেবে পাঠাও!*",
                              parse_mode="Markdown")
    return WAITING_PROMO_FILE


async def receive_promo_file(update, context):
    if not update.message.video and not update.message.document:
        await update.message.reply_text("❌ Video file পাঠাও!")
        return WAITING_PROMO_FILE
    uid = str(update.message.chat_id)
    promo_raw = f"{DOWNLOAD_DIR}/{uid}_promo_raw"
    promo_path = f"{DOWNLOAD_DIR}/{uid}_promo.mp4"
    file = update.message.video or update.message.document
    file_obj = await context.bot.get_file(file.file_id)
    await file_obj.download_to_drive(promo_raw)
    await update.message.reply_text("⚙️ Promo prepare হচ্ছে...")
    prepare_promo(promo_raw, promo_path)
    cleanup(promo_raw)
    context.user_data["promo_path"] = promo_path
    kb = [
        [InlineKeyboardButton("⏮️ শুরুতে", callback_data="pos_start")],
        [InlineKeyboardButton("⏭️ শেষে", callback_data="pos_end")],
        [InlineKeyboardButton("⏱️ নির্দিষ্ট সময়ে", callback_data="pos_custom")],
        [InlineKeyboardButton("🎯 মাঝখানে", callback_data="pos_middle")]
    ]
    await update.message.reply_text("✅ Promo ready!\nকোথায় যোগ করব?",
                                    reply_markup=InlineKeyboardMarkup(kb))
    return WAITING_PROMO_POSITION


async def promo_position(update, context):
    q = update.callback_query
    await q.answer()
    pos_map = {"pos_start": "start", "pos_end": "end",
               "pos_middle": "middle", "pos_custom": "custom"}
    context.user_data["promo_pos"] = pos_map[q.data]
    if q.data == "pos_custom":
        await q.edit_message_text("⏱️ কত সময়ে?\nউদাহরণ: `2:30`", parse_mode="Markdown")
        return WAITING_PROMO_TIME
    await q.edit_message_text("⏳ Processing শুরু হচ্ছে...")
    await process_video(q.message, context)
    return ConversationHandler.END


async def receive_promo_time(update, context):
    try:
        t = update.message.text.strip()
        to_sec(t)
        context.user_data["promo_time"] = t
    except:
        await update.message.reply_text("❌ সঠিক সময় দাও!\nউদাহরণ: `2:30`",
                                        parse_mode="Markdown")
        return WAITING_PROMO_TIME
    await update.message.reply_text("⏳ Processing শুরু হচ্ছে...")
    await process_video(update.message, context)
    return ConversationHandler.END


async def progress_watcher(message, progress):
    """
    ✅ NEW: প্রতি ২ সেকেন্ডে progress dict চেক করে ডাউনলোড % লাইভ আপডেট করে।
    """
    last = -1
    while not progress.get("done"):
        pct = progress.get("percent", 0)
        if pct != last:
            speed = progress.get("speed_str", "")
            eta = progress.get("eta_str", "")
            extra = ("\n" + speed) if speed else ""
            extra += ("\n" + eta) if eta else ""
            try:
                await message.edit_text(f"📥 ডাউনলোড হচ্ছে... {pct}%{extra}")
            except Exception:
                pass
            last = pct
        await asyncio.sleep(2)
    try:
        await message.edit_text("✅ ডাউনলোড সম্পন্ন! প্রসেসিং করছি...")
    except Exception:
        pass


async def process_video(message, context):
    url = context.user_data["url"]
    height = context.user_data.get("height", 360)
    trim = context.user_data.get("trim")
    promo_path = context.user_data.get("promo_path")
    promo_pos = context.user_data.get("promo_pos", "middle")
    promo_time = context.user_data.get("promo_time")
    chat_id = context.user_data["chat_id"]
    uid = str(chat_id)
    raw = f"{DOWNLOAD_DIR}/{uid}_raw.mp4"
    trimmed = f"{DOWNLOAD_DIR}/{uid}_trimmed.mp4"

    try:
        progress_msg = await message.reply_text("📥 ডাউনলোড শুরু হচ্ছে... 0%")
        progress = {"percent": 0, "done": False, "speed_str": "", "eta_str": ""}
        watcher_task = asyncio.create_task(progress_watcher(progress_msg, progress))

        loop = asyncio.get_running_loop()
        hook = make_progress_hook(progress)
        try:
            current = await loop.run_in_executor(None, download_video, url, raw, height, hook)
        finally:
            progress["done"] = True
            await watcher_task

        if trim:
            await message.reply_text(f"✂️ Trimming: {trim[0]} → {trim[1]}")
            trim_video(current, trim[0], trim[1], trimmed)
            current = trimmed

        await message.reply_text("📦 প্রস্তুত করছি...")
        parts = split_video(current)
        total = len(parts)

        for i, part in enumerate(parts, 1):
            send_path = part
            if promo_path and os.path.exists(promo_path):
                await message.reply_text(f"📎 Part {i}/{total} এ Promo যোগ করছি...")
                promo_out = part.replace('.mp4', '_promo.mp4')
                add_promo_to_part(part, promo_path, promo_pos, promo_time, promo_out)
                if os.path.exists(promo_out):
                    send_path = promo_out

            await message.reply_text(f"📤 পাঠাচ্ছি Part {i}/{total}...")
            with open(send_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"🎬 Part {i}/{total}\n\n_{DEVELOPER}_",
                    parse_mode="Markdown"
                )
            if send_path != part:
                cleanup(send_path)

        await message.reply_text(
            f"✅ Done! {total}টা Part!\n\n_{DEVELOPER}_",
            parse_mode="Markdown"
        )

    except Exception as e:
        err = str(e)
        if "Requested format is not available" in err:
            await message.reply_text(
                "❌ Format Error!\nভিডিওটার format পাওয়া যাচ্ছে না।\n"
                "কিছুক্ষণ পরে আবার চেষ্টা করো অথবা অন্য লিংক দাও।",
                parse_mode="Markdown"
            )
        elif "Video unavailable" in err:
            await message.reply_text("❌ ভিডিওটা available নেই বা private!")
        else:
            await message.reply_text(f"❌ Error:\n`{err[:500]}`", parse_mode="Markdown")

    finally:
        cleanup(raw, trimmed, promo_path or "")


async def cancel(update, context):
    await update.message.reply_text("❌ বাতিল!")
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  App Setup
# ─────────────────────────────────────────────

app = Application.builder().token(TOKEN).build()

conv = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)
    ],
    states={
        WAITING_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
        WAITING_QUALITY: [CallbackQueryHandler(quality_choice, pattern="^q_")],
        WAITING_TRIM: [
            CallbackQueryHandler(trim_choice, pattern="^trim_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_trim)
        ],
        WAITING_PROMO_CHOICE: [CallbackQueryHandler(promo_choice, pattern="^promo_")],
        WAITING_PROMO_FILE: [MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_promo_file)],
        WAITING_PROMO_POSITION: [CallbackQueryHandler(promo_position, pattern="^pos_")],
        WAITING_PROMO_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_promo_time)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

app.add_handler(conv)
print("✅ Bot চালু! BY : RH RATUL")
app.run_polling()

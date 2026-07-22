import os, math, logging, re, subprocess, shutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ConversationHandler
import yt_dlp
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
TOKEN = os.environ.get("TOKEN")
MAX_FILE_MB = 35
DOWNLOAD_DIR = "downloads"
DEVELOPER = "BY : RH RATUL"

(WAITING_LINK, WAITING_TRIM, WAITING_PROMO_CHOICE,
 WAITING_PROMO_FILE, WAITING_PROMO_POSITION, WAITING_PROMO_TIME) = range(6)

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


def download_video(url, output_path):
    """
    yt-dlp দিয়ে ভিডিও ডাউনলোড করে।
    Format error এড়াতে fallback chain + EJS (n-challenge) solver ব্যবহার করা হয়েছে।
    """
    js_runtimes = _detect_js_runtime()

    ydl_opts = {
        # ✅ FIX: hard [ext=mp4]/[ext=m4a] filter সরানো হলো, এটা মাঝেমধ্যে
        # ভ্যালিড format কেও reject করে ফেলছিল
        "format": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
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
                # ✅ FIX: কুকি থাকলে web client সবচেয়ে নির্ভরযোগ্য, বাকিগুলো fallback
                "player_client": ["web", "tv", "android", "mweb"],
            }
        },
        # ✅ FIX: n-challenge (nsig) সলভ করার জন্য EJS remote solver enable করা হলো
        # এটা ছাড়া node/deno থাকলেও শুধু storyboard format পাওয়া যায়
        "remote_components": "ejs:github",
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }

    if js_runtimes:
        ydl_opts["js_runtimes"] = js_runtimes

    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        fname = ydl.prepare_filename(info)
        # ✅ FIX: শুধু extension এর শেষে match করে replace করা হচ্ছে,
        # আগে path-এর মাঝে "webm"/"mkv" শব্দ থাকলেও ভুল replace হতো
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


# ─────────────────────────────────────────────
#  Telegram Handlers
# ─────────────────────────────────────────────

async def start(update, context):
    await update.message.reply_text(
        "🎬 *Video Downloader Bot*\n\nYouTube লিংক পাঠাও!\n\n"
        "✅ Best Quality\n✂️ Cut\n📎 Promo\n📦 Auto Split\n\n"
        f"_{DEVELOPER}_",
        parse_mode="Markdown"
    )
    return WAITING_LINK


async def receive_link(update, context):
    url = update.message.text.strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text("❌ সঠিক YouTube লিংক দাও!")
        return WAITING_LINK
    context.user_data.update({"url": url, "chat_id": update.message.chat_id})
    kb = [
        [InlineKeyboardButton("✂️ হ্যাঁ Trim করব", callback_data="trim_yes")],
        [InlineKeyboardButton("⏭️ না পুরো ভিডিও", callback_data="trim_no")]
    ]
    await update.message.reply_text("লিংক পেয়েছি!\nTrim করতে চাও?",
                                    reply_markup=InlineKeyboardMarkup(kb))
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


async def process_video(message, context):
    url = context.user_data["url"]
    trim = context.user_data.get("trim")
    promo_path = context.user_data.get("promo_path")
    promo_pos = context.user_data.get("promo_pos", "middle")
    promo_time = context.user_data.get("promo_time")
    chat_id = context.user_data["chat_id"]
    uid = str(chat_id)
    raw = f"{DOWNLOAD_DIR}/{uid}_raw.mp4"
    trimmed = f"{DOWNLOAD_DIR}/{uid}_trimmed.mp4"

    try:
        await message.reply_text("📥 ডাউনলোড হচ্ছে...")
        current = download_video(url, raw)

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
        # ✅ error message আরও helpful করা হয়েছে
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
            await message.reply_text(f"❌ Error:\n`{err}`", parse_mode="Markdown")

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

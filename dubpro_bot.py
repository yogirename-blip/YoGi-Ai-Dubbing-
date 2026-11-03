"""
Tamil Pro Dub Telegram Bot (laptop version: ElevenLabs voice clone)
===================================================================
Telegram-la video anuppunga -> bot Tamil-la dub panni thirumba anuppum.

Keys ellam .env file-la (.env.example-a copy panni .env nu peyar maathunga):
  GEMINI_API_KEY, ELEVENLABS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH
Optional: TELEGRAM_ALLOWED (unga Telegram id), TTS_WORKERS, TZ_OFFSET_MIN, SESSION_HOURS (0 = limit illa)

Run:  run.bat  (double-click)   illa   python dubpro_bot.py
"""
import os, sys, asyncio, shutil, time, traceback, subprocess, signal, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envload  # noqa  (.env padikkum)

try:
    import telethon  # noqa
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "telethon"])

from telethon import TelegramClient, events
from telethon.tl.custom import Button

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dubpro as tamil_dub  # noqa  (Gemini + ElevenLabs key-a ULLE edukkum)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED", "").replace(" ", "").split(",") if x}
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "0"))   # laptop-la 0 = limit illa
TZ_OFFSET = int(os.environ.get("TZ_OFFSET_MIN", "330"))

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs")
os.makedirs(BASE, exist_ok=True)
START = time.time()
USERS_FILE = os.path.join(BASE, "users.json")

client = TelegramClient(os.path.join(BASE, "bot_session"), API_ID, API_HASH)
queue: asyncio.Queue = asyncio.Queue()
JOBS = {}          # job_id -> Job
CURRENT = None     # ippo odittu irukkura Job
MODES = {"expressive": "eleven_v3", "stable": "eleven_multilingual_v2", "economy": "eleven_flash_v2_5"}
MODE_TXT = {"expressive": "நடிப்பு (v3)", "stable": "நிலையான (v2)", "economy": "சிக்கனம் (flash, credit பாதி)"}
SETTINGS = {}      # user_id -> {"fmt": "mp4|mkv", "send": "video|document", "mode": "expressive|stable|economy"}
STOPPING = False


# ----------------------------- helpers --------------------------------------
def now_str():
    t = datetime.datetime.utcnow() + datetime.timedelta(minutes=TZ_OFFSET)
    return t.strftime("%H:%M")


def bar(p):
    n = max(0, min(10, int(p // 10)))
    return "▓" * n + "░" * (10 - n)


def load_users():
    try:
        return set(json.load(open(USERS_FILE)))
    except Exception:
        return set()


def remember_user(uid):
    u = load_users()
    if uid not in u:
        u.add(uid)
        json.dump(sorted(u), open(USERS_FILE, "w"))


def owners():
    return set(ALLOWED) or load_users()


async def announce(text):
    for uid in owners():
        try:
            await client.send_message(uid, text)
        except Exception:
            pass


def prefs(uid):
    return SETTINGS.setdefault(uid, {"fmt": "mp4", "send": "video", "mode": "expressive"})


def describe(p):
    send = "document (file)" if p["send"] == "document" or p["fmt"] == "mkv" else "video"
    return f"Format: {p['fmt'].upper()} | அனுப்பும் முறை: {send} | குரல்: {MODE_TXT[p.get('mode', 'expressive')]}"


def is_video(msg):
    if msg.video:
        return True
    d = msg.document
    return bool(d and d.mime_type and d.mime_type.startswith("video/"))


class Job:
    def __init__(self, event, fmt, send, mode="expressive"):
        self.mode = mode
        self.event = event
        self.id = f"{event.sender_id}x{int(time.time() * 1000) % 10 ** 9}"
        self.fmt, self.send = fmt, send
        self.cancelled = False
        self.msg = None
        self.last_edit = 0.0
        self.last_text = ""

    def cancel_button(self):
        return [Button.inline("❌ Cancel", data=f"cancel:{self.id}".encode())]


async def update(job, pct, text, force=False, buttons=True):
    """Oru message-aye thirumba thirumba edit pannum (spam illa), % bar-oda."""
    t = f"🎬 {text}\n{bar(pct)} {int(pct)}%"
    now = time.time()
    if t == job.last_text or (not force and now - job.last_edit < 3 and pct < 100):
        return
    job.last_edit, job.last_text = now, t
    try:
        await job.msg.edit(t, buttons=job.cancel_button() if buttons else None)
    except Exception:
        pass


async def finish_msg(job, text):
    try:
        await job.msg.edit(text, buttons=None)
    except Exception:
        try:
            await job.event.reply(text)
        except Exception:
            pass


# ----------------------------- commands -------------------------------------
@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    remember_user(event.sender_id)
    await event.reply("வணக்கம்! 🎬 ஒரு வீடியோ அனுப்புங்க. ஒவ்வொரு கேரக்டரோட அசல் குரலையே clone செய்து, பேச்சுத் தமிழில் dub செய்து அனுப்புறேன்.\n"
                      "(நீளமான வீடியோவுக்கு நேரம் ஆகும். ஒரு நேரத்தில் ஒரு வீடியோ மட்டும்.)\n\n"
                      "வடிவம்: /mp4 /mkv  |  அனுப்பும் முறை: /video /document\n"
                      "குரல் நடை: /expressive (நடிப்பு, default)  /stable  /economy\n"
                      "நிலை: /status  |  நிறுத்த: /cancel  |  /settings")


@client.on(events.NewMessage(pattern="/id"))
async def my_id(event):
    await event.reply(f"உங்க ID: {event.sender_id}")


@client.on(events.NewMessage(pattern=r"^/(mp4|mkv|video|document|settings|expressive|stable|economy)"))
async def set_pref(event):
    cmd = event.raw_text.split()[0].lstrip("/").split("@")[0].lower()
    p = prefs(event.sender_id)
    if cmd in ("mp4", "mkv"):
        p["fmt"] = cmd
    elif cmd in ("video", "document"):
        p["send"] = cmd
    elif cmd in MODES:
        p["mode"] = cmd
    extra = ""
    if p["fmt"] == "mkv":
        extra = "\n(MKV: தமிழ் + அசல் ஆடியோ இரண்டு track இருக்கும். Telegram-ல் file-ஆகத்தான் போகும்.)"
    await event.reply("✅ " + describe(p) + extra + "\n\nமாற்ற: /mp4 /mkv /video /document  |  குரல்: /expressive /stable /economy")


@client.on(events.NewMessage(pattern=r"^/status"))
async def status(event):
    up = int((time.time() - START) / 60)
    cur = f"ஓடுது: {CURRENT.last_text.splitlines()[0]}" if CURRENT else "இப்போ வேலை எதுவும் இல்ல"
    await event.reply(f"🟢 Bot online (laptop)\nஓடிய நேரம்: {up} நிமிடம்\n"
                      f"வரிசையில்: {queue.qsize()}\n{cur}")


async def cancel_jobs(sender_id):
    n = 0
    for job in list(JOBS.values()):
        if job.event.sender_id == sender_id and not job.cancelled:
            job.cancelled = True
            n += 1
            if job is CURRENT:
                tamil_dub.CANCEL.set()
    return n


@client.on(events.NewMessage(pattern=r"^/cancel"))
async def cancel_cmd(event):
    n = await cancel_jobs(event.sender_id)
    await event.reply("❌ Cancel செய்யுது..." if n else "Cancel செய்ய job எதுவும் இல்ல.")


@client.on(events.CallbackQuery(pattern=rb"cancel:(.+)"))
async def cancel_btn(event):
    jid = event.pattern_match.group(1).decode()
    job = JOBS.get(jid)
    if not job:
        await event.answer("இந்த job ஏற்கனவே முடிந்தது.", alert=True)
        return
    if event.sender_id != job.event.sender_id and event.sender_id not in ALLOWED:
        await event.answer("இது உங்க job இல்ல.", alert=True)
        return
    job.cancelled = True
    if job is CURRENT:
        tamil_dub.CANCEL.set()
    await event.answer("Cancel செய்யுது...")


# ----------------------------- video handler --------------------------------
@client.on(events.NewMessage(func=lambda e: e.file is not None))
async def on_file(event):
    if ALLOWED and event.sender_id not in ALLOWED:
        await event.reply("மன்னிக்கவும், இந்த bot உங்களுக்கு அனுமதி இல்லை.")
        return
    if not is_video(event.message):
        await event.reply("வீடியோ file மட்டும் அனுப்புங்க.")
        return
    remember_user(event.sender_id)
    p = prefs(event.sender_id)
    cap = (event.raw_text or "").lower()        # caption-la 'mkv' / 'doc' ezhuthina adhu mudhal
    fmt = "mkv" if "mkv" in cap else ("mp4" if "mp4" in cap else p["fmt"])
    send = "document" if ("doc" in cap or "file" in cap) else p["send"]
    mode = next((m for m in MODES if m in cap), p.get("mode", "expressive"))
    job = Job(event, fmt, send, mode)
    JOBS[job.id] = job
    pos = queue.qsize() + (1 if CURRENT else 0)
    job.msg = await event.reply(
        f"வீடியோ கிடைத்தது ✅ வரிசையில் இருக்கு (முன்னாடி {pos})\n{describe({'fmt': fmt, 'send': send, 'mode': mode})}",
        buttons=job.cancel_button())
    await queue.put(job)


async def worker():
    global CURRENT
    loop = asyncio.get_running_loop()
    while True:
        job = await queue.get()
        if job.cancelled:
            await finish_msg(job, "❌ Cancel ஆனது.")
            JOBS.pop(job.id, None)
            queue.task_done()
            continue
        CURRENT = job
        tamil_dub.CANCEL.clear()
        event = job.event
        jobdir = os.path.join(BASE, job.id)
        os.makedirs(jobdir, exist_ok=True)
        try:
            await update(job, 1, "பதிவிறக்குது", force=True)

            def dl_cb(cur, tot):
                if tot:
                    asyncio.ensure_future(update(job, 1 + 3 * cur / tot, "பதிவிறக்குது"))

            src = await event.download_media(file=os.path.join(jobdir, "input"), progress_callback=dl_cb)
            if job.cancelled:
                raise tamil_dub.Cancelled()

            def progress(pct, text):
                asyncio.run_coroutine_threadsafe(update(job, pct, text), loop)

            def run_job():
                try:
                    return "ok", tamil_dub.dub(src, jobdir, progress, job.fmt, MODES[job.mode])
                except tamil_dub.Cancelled:
                    return "cancelled", None

            status_, out = await asyncio.to_thread(run_job)
            if status_ == "cancelled" or job.cancelled:
                await finish_msg(job, "❌ Cancel ஆனது.")
            else:
                await update(job, 95, "முடிந்தது! அனுப்புது", force=True)

                def up_cb(cur, tot):
                    if tot:
                        asyncio.ensure_future(update(job, 95 + 5 * cur / tot, "அனுப்புது"))

                as_doc = job.send == "document" or job.fmt == "mkv"
                await client.send_file(event.chat_id, out, caption="தமிழ் dub 🎧",
                                       force_document=as_doc, supports_streaming=not as_doc,
                                       reply_to=event.id, progress_callback=up_cb)
                await update(job, 100, "முடிந்தது ✅", force=True, buttons=False)
        except tamil_dub.Cancelled:
            await finish_msg(job, "❌ Cancel ஆனது.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa
            traceback.print_exc()
            await finish_msg(job, f"❌ பிழை: {str(e)[:300]}")
        finally:
            CURRENT = None
            JOBS.pop(job.id, None)
            shutil.rmtree(jobdir, ignore_errors=True)
            queue.task_done()


# ----------------------------- online / offline -----------------------------
async def heartbeat():
    """Online status message-a 5 nimishathuku oru murai edit pannum. Neram maaralana = bot nindruduchu."""
    msgs = {}
    for uid in owners():
        try:
            msgs[uid] = await client.send_message(
                uid, f"🟢 Bot online ({now_str()} முதல்)\nகடைசி உயிர்ப்பு: {now_str()}")
        except Exception:
            pass
    warned = False
    while True:
        await asyncio.sleep(300)
        up_h = (time.time() - START) / 3600
        if SESSION_HOURS > 0 and not warned and up_h >= SESSION_HOURS - 0.75:
            warned = True
            await announce("⚠️ session வரம்பு நெருங்குது (~45 நிமிடத்தில் நிற்கும்). "
                           "நின்றதும் மறுபடி Run செய்யுங்க.")
        for uid, m in list(msgs.items()):
            try:
                await m.edit(f"🟢 Bot online\nகடைசி உயிர்ப்பு: {now_str()}")
            except Exception:
                pass


async def shutdown(reason):
    global STOPPING
    if STOPPING:
        return
    STOPPING = True
    text = f"🔴 Bot OFFLINE ({reason}, {now_str()})\nLaptop-ல் run.bat மறுபடி ஓட்டி ஆன் பண்ணுங்க."
    if CURRENT:
        text += "\n⚠️ ஓடிக்கொண்டிருந்த வீடியோ பாதியில் நின்றது. ON ஆனதும் மறுபடி அனுப்புங்க."
        tamil_dub.CANCEL.set()
    try:
        await asyncio.wait_for(announce(text), timeout=8)
    except Exception:
        pass
    await client.disconnect()


def keep_awake():
    """Windows laptop sleep aagi bot nikkaama irukka (screen off aanaalum odum)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
        except Exception:
            pass


async def main():
    keep_awake()
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"Bot ready: @{me.username}  (Telegram-la poi /start anuppunga)")
    if not ALLOWED:
        print("WARNING: TELEGRAM_ALLOWED set pannala -> online/offline notice varaadhu, yaarum use pannalaam.")
    loop = asyncio.get_running_loop()
    for sig, name in ((signal.SIGTERM, "stop"), (signal.SIGINT, "stopped")):
        try:
            loop.add_signal_handler(sig, lambda n=name: asyncio.ensure_future(shutdown(n)))
        except NotImplementedError:
            pass
    asyncio.create_task(worker())
    asyncio.create_task(heartbeat())
    try:
        await client.run_until_disconnected()
    finally:
        if not STOPPING:
            await shutdown("crashed")


if __name__ == "__main__":
    asyncio.run(main())

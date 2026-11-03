"""
Tamil Pro Dubbing (ElevenLabs voice clone + Gemini script)
==========================================================
Video -> BGM/kural pirippu (demucs) -> Gemini: yaar enna pesuranga + movie-dub pechu Tamil
-> ovvoru character-oda ASAL kuralai ElevenLabs-la clone panni, adhe kural-la Tamil pesum
-> BGM + Tamil kural + video

Secrets: GEMINI_API_KEY, ELEVENLABS_API_KEY
"""

import os, sys, re, json, time, glob, shutil, subprocess, wave, uuid, threading, signal, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envload  # noqa  (.env padikkum, Windows UTF-8)
from types import SimpleNamespace as _NS
from concurrent.futures import ThreadPoolExecutor

# ----------------------------- CONFIG ---------------------------------------
VIDEO_PATH   = ""        # kaali na /kaggle/input-la irukkura mudhal video edukkum
OUT_DIR      = os.path.dirname(os.path.abspath(__file__))
WORK         = os.path.join(OUT_DIR, "work")
OUTPUT_VIDEO = os.path.join(OUT_DIR, "dubbed_tamil.mp4")

TEXT_MODEL   = "gemini-3.8-flash"            # video puriyum + Tamil mozhipeyarpu
TTS_MODEL    = "gemini-3.8-flash-tts"        # nalla quality; fail na lite-ku maarum
TTS_FALLBACK = "gemini-3.8-flash-lite-tts"

CHUNK_MIN    = 8         # video-va ivlo nimisham thundaa pirichu anuppum
PROXY_HEIGHT = 480       # Gemini-ku anuppa chinna video (upload vegam)
MAX_SPEEDUP  = 1.35      # Tamil vari neelamaa irundha max vegappaduthal
TTS_WORKERS  = int(os.environ.get("TTS_WORKERS", "4"))   # 429 niraya vandha .env-la TTS_WORKERS=2 (illa 1) vainga

# ---- ElevenLabs
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_v3")   # v3 = acting/emotion (audio tags). Stable: eleven_multilingual_v2
MAX_CLONES   = int(os.environ.get("MAX_CLONES", "8"))     # evlo character-ku kural clone (voice slot limit-ku)
KEEP_VOICES  = os.environ.get("KEEP_VOICES", "0") == "1"  # 1 na clone voices-a delete pannaadhu
MIN_SAMPLE_S = 6.0       # idhukku kuraivaana sample na clone panna maatom (premade voice use aagum)
VOICE_PREFIX = "dubbot_"
BGM_VOLUME   = 0.9
VOICE_VOLUME = 1.0
SR           = 24000     # Gemini TTS output sample rate
# ----------------------------------------------------------------------------


CANCEL = threading.Event()      # bot "Cancel" button idhai set pannum


class Cancelled(BaseException):
    """User cancel pannittaanga (BaseException: 'except Exception' idhai pidikkaadhu)."""


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


_PROG = None


def report(pct, text):
    """Bot-ku progress anuppum: pct (0-100) + text."""
    try:
        if _PROG:
            _PROG(int(pct), text)
    except Exception:
        pass


def _kill_tree(p):
    """Neelamaana process (demucs/ffmpeg) + adhoda child-a nirutthum (Windows + Linux)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass


def sh(cmd, check=True):
    check_cancel()
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
          else {"start_new_session": True})
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding="utf-8", errors="replace", **kw)
    while True:
        try:
            out, err = p.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if CANCEL.is_set():
                _kill_tree(p)
                p.communicate()
                raise Cancelled()
    r = _NS(returncode=p.returncode, stdout=out, stderr=err)
    if check and r.returncode != 0:
        raise RuntimeError(f"CMD FAILED: {cmd}\n{(err or '')[-1500:]}")
    return r


def ensure_deps():
    try:
        import google.genai  # noqa
    except ImportError:
        sh(f'"{sys.executable}" -m pip install -q google-genai')
    try:
        import demucs  # noqa
    except ImportError:
        sh(f'"{sys.executable}" -m pip install -q demucs', check=False)
    import numpy  # noqa
    try:
        import requests  # noqa
    except ImportError:
        sh(f'"{sys.executable}" -m pip install -q requests')


ensure_deps()
import numpy as np
from google import genai
from google.genai import types
import requests


def get_api_key():
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret("GEMINI_API_KEY")
    except Exception:
        k = os.environ.get("GEMINI_API_KEY")
        if not k:
            raise SystemExit("GEMINI_API_KEY illa. Kaggle Secrets-la add pannunga.")
        return k


client = genai.Client(api_key=get_api_key())
os.makedirs(WORK, exist_ok=True)
os.makedirs(os.path.join(WORK, "tts"), exist_ok=True)


# ----------------------------- helpers --------------------------------------
FATAL = ("quota_exceeded", "invalid_api_key", "needs_authorization", "voice_limit_reached",
         "payment_required", "detected_unusual_activity", "missing_permissions", "no_retry")


def retry(fn, tries=6, base=4.0, what="call"):
    last = ""
    for i in range(tries):
        check_cancel()
        try:
            return fn()
        except Exception as e:  # noqa
            msg = str(e)
            last = msg
            wait = base * (2 ** i)
            print(f"  [{what}] error: {msg[:220].replace(chr(10), ' ')}")
            if any(t in msg for t in FATAL):
                raise RuntimeError(f"{what}: {msg[:300]}")
            if "limit: 0" in msg:      # indha model-ku quota-ve illa, wait panni payan illa
                raise RuntimeError(f"{what}: quota 0 -> {msg[:250]}")
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg:
                if i < tries - 1:
                    print(f"  [{what}] {wait:.0f}s kaathirukkirom...")
                    CANCEL.wait(wait)
                    check_cancel()
                continue
            if i == tries - 1:
                raise
            time.sleep(2)
    raise RuntimeError(f"{what} failed: {last[:300]}")


def ts_to_sec(t):
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(p) for p in str(t).strip().split(":")]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def duration(path):
    r = sh(f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{path}"')
    return float(r.stdout.strip())


def find_video():
    if VIDEO_PATH and os.path.exists(VIDEO_PATH):
        return VIDEO_PATH
    for root in ("/kaggle/input", "/kaggle/working", "."):
        for ext in ("mp4", "mkv", "mov", "webm", "avi"):
            hits = glob.glob(os.path.join(root, "**", f"*.{ext}"), recursive=True)
            hits = [h for h in hits if "dubbed_" not in h and "/work/" not in h.replace("\\", "/")]
            if hits:
                return hits[0]
    raise SystemExit("Video kidaikala. VIDEO_PATH set pannunga.")


# ----------------------------- STEP 1: BGM ----------------------------------
def _device():
    """GPU irundha demucs-a adhula odum (10x vegam). Illa na cpu."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def step_bgm(video):
    bgm_path = os.path.join(WORK, "bgm.wav")
    if os.path.exists(bgm_path):
        print("[1] BGM cache irukku, skip")
        return bgm_path
    print("[1] Audio eduthu BGM pirikkirom (CPU-la konjam neram aagum)...")
    full = os.path.join(WORK, "full_audio.wav")
    sh(f'ffmpeg -y -i "{video}" -vn -ac 2 -ar 44100 "{full}"')
    extra = os.environ.get("DEMUCS_EXTRA", "")      # e.g. "-j 2" (experiment)
    r = sh(f'"{sys.executable}" -m demucs --two-stems=vocals -n htdemucs -d {_device()} --overlap 0.1 {extra} '
           f'-o "{WORK}/demucs" "{full}"', check=False)
    hits = glob.glob(os.path.join(WORK, "demucs", "**", "no_vocals.wav"), recursive=True)
    if r.returncode == 0 and hits:
        shutil.copy(hits[0], bgm_path)
        voc = glob.glob(os.path.join(WORK, "demucs", "**", "vocals.wav"), recursive=True)
        if voc:
            shutil.copy(voc[0], os.path.join(WORK, "vocals.wav"))
            try:
                os.remove(full)        # vocals.wav irukku -> full_audio thevai illa (disk migum)
            except Exception:
                pass
        shutil.rmtree(os.path.join(WORK, "demucs"), ignore_errors=True)
    else:
        print("  !! demucs fail aachu. Fallback: asal audio-va romba kuraichu BGM-aa "
              "use pannuvom (asal kural mella kekkum).")
        print("  ", r.stderr[-400:])
        sh(f'ffmpeg -y -i "{full}" -af volume=0.12 "{bgm_path}"')
    return bgm_path


class BgmJob:
    """demucs-a background thread-la odum; adhe neram Gemini script ezhuthum (laptop-la neram migum)."""

    def __init__(self, video):
        self.video, self.result, self.err = video, None, None
        self.t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.t.start()

    def _run(self):
        try:
            self.result = step_bgm(self.video)
        except BaseException as e:  # noqa  (Cancelled-um)
            self.err = e

    def wait(self):
        self.t.join()
        if self.err:
            raise self.err
        return self.result


# ----------------------------- STEP 2: Gemini script ------------------------
SCRIPT_PROMPT = """You are a professional dubbing scriptwriter for Tamil movies and web series. Watch and listen to this video clip.

Task: list EVERY spoken line in this clip, from the very first second to the very last second, and write the
Tamil dubbing line for each. Do NOT skip any dialogue, not even short words like "hey", "no", "what".

Tamil style (VERY IMPORTANT): write the casual SPOKEN Tamil of everyday Chennai conversation, exactly like
dialogue in Tamil-dubbed movies. NEVER literary/formal Tamil (sentamil).
Examples: "என்ன சொல்கிறாய்?" -> "என்ன சொல்ற?",  "நான் வருகிறேன்" -> "நான் வரேன்",
"அது இருக்கிறது" -> "அது இருக்கு",  "எனக்கு வேண்டும்" -> "எனக்கு வேணும்",  "ஆனால்" -> "ஆனா",
"இல்லை" -> "இல்ல",  "நீங்கள்" -> "நீங்க",  "அவர்கள்" -> "அவங்க",  "பிறகு" -> "அப்புறம்".
Never use words like மற்றும், ஏனெனில், எனவே. Use natural interjections (டா, சரி, ஆமா, அய்யோ) only where they fit.
Write in Tamil script. Common loanwords are fine (ஓகே, சாரி, போன்). No Hindi/English sentences.
Keep meaning, emotion and length close to the original so it fits the same time. Short, speakable sentences.

Speakers: use the video (who is on screen, who is speaking) and the voices to decide who speaks.
Use stable IDs like C1, C2, C3. The same person must ALWAYS get the same ID.
{roster}

Timestamps are relative to the START OF THIS CLIP in MM:SS.s format (e.g. 00:12.4).
Skip songs/singing/background noise. Include narrator voice-over as its own character.

Return ONLY JSON:
{{
 "characters": [ {{"id":"C1","gender":"male|female","age":"child|young|adult|elder",
                   "pitch":"low|mid|high","tone":"short English description of voice/personality",
                   "is_new": true}} ],
 "lines": [ {{"start":"00:01.2","end":"00:03.9","speaker":"C1",
              "original":"original words","tamil":"colloquial Tamil",
              "emotion":"neutral|angry|sad|happy|scared|whisper|shout|calm",
              "event":"none|laughs|sighs|gasps|cries|screams|breath"}} ]
}}
Only list characters that appear in this clip in "characters"."""


POLISH_PROMPT = """You are the director and dialogue writer of a professional Tamil dub (the kind you hear in
Tamil-dubbed Hollywood films, anime and web series). Below is a conversation, line by line, IN ORDER.
Rewrite each line so it sounds like a real person ACTING and TALKING in the moment, never like text read aloud.

How real dub dialogue sounds:
- Spoken Chennai Tamil with contractions: இருக்கிறது->இருக்கு, பண்றேன், சொல்ற, வரேன், போறேன், இல்ல, வேணும்,
  ஆனா, நீங்க, அவங்க, என்னோட, அப்புறம். Never literary Tamil. Never: மற்றும், ஏனெனில், எனவே, ஆகையால்.
- Natural speech, NOT complete written sentences: drop pronouns when obvious ("நீ வர்றியா?" -> "வர்றியா?"),
  open with reactions (ஏய், டேய், அட, ஐயோ, ச்சே, ம்ம், ஹா, என்னது?) and use endings like டா, ல, பா, மா, பாரு,
  சரி only where that character would say them.
- Copy the rhythm and emotion of the ORIGINAL line: short exclamations stay short, keep repeated words, stutters,
  interruptions ("—") and trailing off ("..."). Use ? ! ... and commas to place the pauses and pushes exactly where
  an actor would.
- Adapt like professional dubbing, not word-by-word: "Oh my god" -> "அய்யோ கடவுளே", "What the hell?" -> "என்னடா இது?",
  "Come on!" -> "வாடா சீக்கிரம்!", "Thank you" -> "தேங்க்ஸ்", "I'm sorry" -> "சாரி", "Are you kidding me?" -> "விளையாடுறியா?".
- Use the conversation to choose how people address each other (நீ/நீங்க, டா/டி, சார், அண்ணா, அக்கா, அம்மா...).
  Keep the same address style for the same pair of people throughout.
- HARD LIMIT: every line has max_chars (what fits the original timing). Stay at or below it.
- Audio tags: add at most two English tags in square brackets for the voice actor, only when the original really
  has it: [angry] [shouting] [whispers] [sad] [crying] [scared] [excited] [nervous] [sarcastic] [sighs] [laughs]
  [chuckles] [gasps] [breathes] [exhales]. A delivery tag goes at the start of the line, an event tag
  (laughs/sighs/gasps) where it happens. Use the given emotion and event. Plain neutral lines get no tag.
- Tamil script only (loanwords like ஓகே, சாரி are fine). Never explain anything.

Input JSON items: id, who, emotion, event, en (original words), ta (current Tamil), max_chars.
Return ONLY JSON: {{"lines":[{{"id":0,"tamil":"..."}}]}} with EVERY id exactly once.

INPUT:
{items}"""


TEXT_MODELS = []
for _m in [os.environ.get("GEMINI_TEXT_MODEL"), TEXT_MODEL,
           "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]:
    if _m and _m not in TEXT_MODELS:
        TEXT_MODELS.append(_m)


def gen(contents, config=None, what="gemini"):
    """Text/video model-ku call. Oru model fail aana adutha model-ku maarum."""
    last = None
    for m in TEXT_MODELS:
        try:
            r = retry(lambda: client.models.generate_content(
                model=m, contents=contents, config=config), tries=4, what=f"{what}:{m}")
            if m != TEXT_MODELS[0]:
                print(f"  ({m} use aachu)")
            return r
        except Exception as e:  # noqa
            last = e
            print(f"  {m} work aagala, adutha model try pannuren...")
    raise RuntimeError(f"ellaa models-um fail. Kadaisi error: {str(last)[:300]}")


def make_proxy(video):
    proxy = os.path.join(WORK, "proxy.mp4")
    if not os.path.exists(proxy):
        sh(f'ffmpeg -y -i "{video}" -vf "scale=-2:{PROXY_HEIGHT}" -r 12 '
           f'-c:v libx264 -preset veryfast -crf 30 -c:a aac -b:a 64k -ac 1 "{proxy}"')
    return proxy


def upload_wait(path):
    f = retry(lambda: client.files.upload(file=path), what="upload")
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        time.sleep(4)
        f = client.files.get(name=f.name)
    if getattr(f.state, "name", str(f.state)) != "ACTIVE":
        raise RuntimeError(f"Upload fail: {f.state}")
    return f


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return json.loads(re.sub(r"^```json|```$", "", text.strip(), flags=re.M))


def run_clip(clip, off, chars):
    """Oru video thundai Gemini-ku anuppi, varigalai (offset serthu) tharum."""
    f = upload_wait(clip)
    roster = ""
    if chars:
        roster = ("Characters already known from earlier clips (reuse these IDs for the same people, "
                  "only create a NEW id for a truly new person):\n" +
                  json.dumps(chars, ensure_ascii=False))
    prompt = SCRIPT_PROMPT.format(roster=roster)
    resp = gen([f, prompt],
               types.GenerateContentConfig(response_mime_type="application/json",
                                           temperature=0.4), what="script")
    try:
        client.files.delete(name=f.name)
    except Exception:
        pass
    data = parse_json(resp.text)
    for c in data.get("characters", []):
        if "id" in c:
            chars.setdefault(c["id"], {k: v for k, v in c.items() if k != "is_new"})
    out = []
    for ln in data.get("lines", []):
        try:
            s_, e_ = ts_to_sec(ln["start"]) + off, ts_to_sec(ln["end"]) + off
        except Exception:
            continue
        if not str(ln.get("tamil", "")).strip() or e_ <= s_:
            continue
        out.append({"start": round(s_, 2), "end": round(e_, 2),
                    "speaker": ln.get("speaker", "C1"),
                    "original": ln.get("original", ""),
                    "tamil": ln["tamil"].strip(),
                    "emotion": ln.get("emotion", "neutral"),
                    "event": ln.get("event", "none")})
    return out


def cut_clip(src, start, dur, dst):
    sh(f'ffmpeg -y -ss {start:.2f} -t {dur:.2f} -i "{src}" -c:v libx264 -preset ultrafast '
       f'-crf 32 -c:a aac -b:a 64k "{dst}"')


FRAME = 0.05


def speech_mask(vocals_path):
    """vocals.wav-la yengellam kural irukku-nu frame-wise (0.05s) mask."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", vocals_path, "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(len(x) // (FRAME * 16000))
    if n == 0:
        return np.zeros(0, dtype=bool)
    fr = x[: n * int(FRAME * 16000)].reshape(n, -1)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    loud = rms[rms > 0.003]
    thr = max(0.01, 0.15 * float(np.percentile(loud, 95))) if len(loud) else 0.01
    act = rms > thr
    dil = np.convolve(act.astype(int), np.ones(9), "same") > 0       # chinna idaiveli moodu
    return np.convolve(dil.astype(int), np.ones(9), "same") >= 9


def find_gaps(mask, lines, min_gap=0.7):
    """Kural kekkuthu aana script-la illatha idangal."""
    n = len(mask)
    covered = np.zeros(n, dtype=bool)
    for ln in lines:
        a = max(0, int((ln["start"] - 0.3) / FRAME))
        b = min(n, int((ln["end"] + 0.3) / FRAME) + 1)
        covered[a:b] = True
    miss = mask & ~covered
    gaps, i = [], 0
    while i < n:
        if miss[i]:
            j = i
            while j < n and (miss[j] or (j + int(1.0 / FRAME) < n and miss[j: j + int(1.0 / FRAME)].any())):
                j += 1
            if (j - i) * FRAME >= min_gap:
                gaps.append((i * FRAME, j * FRAME))
            i = j
        else:
            i += 1
    return gaps


def fill_gaps(proxy, lines, chars, total):
    voc = os.path.join(WORK, "vocals.wav")
    if not os.path.exists(voc):
        return lines
    try:
        gaps = find_gaps(speech_mask(voc), lines)
    except Exception as e:  # noqa
        print("  gap check fail:", str(e)[:150])
        return lines
    if not gaps:
        print("  koraivu illa: ellaa pechum cover aachu")
        return lines
    print(f"  {len(gaps)} idathula pechu miss aachu, marubadi kekkurom...")
    added = 0
    for k, (gs, ge) in enumerate(gaps):
        check_cancel()
        cs = max(0.0, gs - 0.7)
        ce = min(total, ge + 0.7)
        if ce - cs < 2.5:
            ce = min(total, cs + 2.5)
        clip = os.path.join(WORK, f"gap_{k}.mp4")
        cut_clip(proxy, cs, ce - cs, clip)
        try:
            new = run_clip(clip, cs, chars)
        except Exception as e:  # noqa
            print("  gap fill fail:", str(e)[:150])
            continue
        for nl in new:
            dur = nl["end"] - nl["start"]
            ov = sum(max(0.0, min(nl["end"], o["end"]) - max(nl["start"], o["start"])) for o in lines)
            if dur > 0 and ov / dur < 0.4:
                lines.append(nl)
                added += 1
    lines.sort(key=lambda x: x["start"])
    print(f"  {added} puthu varigal serthom")
    return lines


ALLOWED_TAGS = {"angry", "shouting", "whispers", "sad", "crying", "scared", "excited", "nervous",
                "sarcastic", "sighs", "laughs", "chuckles", "gasps", "breathes", "exhales"}


def strip_tags(t):
    return re.sub(r"\[[^\]]*\]\s*", "", t).strip()


def clean_tags(t):
    """Anumadhi illaadha tag-a neekki, max 3 tag mattum vaikkum."""
    count = [0]

    def f(m):
        name = m.group(1).strip().lower()
        if name in ALLOWED_TAGS and count[0] < 3:
            count[0] += 1
            return f"[{name}]"
        return ""
    return re.sub(r"\s+", " ", re.sub(r"\[([^\]]*)\]", f, t)).strip()


def polish(lines, chars):
    """Ellaa varigalaiyum 'original movie dub' pechu + acting-ku maatri ezhuthum (tag, pause, rhythm)."""
    print("  Dub director pass: original dub pechu nadaikku maathurom...")
    B = 30
    done = 0
    for i in range(0, len(lines), B):
        check_cancel()
        report(58 + 3 * i / max(1, len(lines)), "ஒரிஜினல் dub நடைக்கு மாத்துது")
        batch = lines[i: i + B]
        items = []
        for k, ln in enumerate(batch):
            c = chars.get(ln["speaker"], {})
            dur = ln["end"] - ln["start"]
            items.append({"id": k, "who": f"{c.get('age','adult')} {c.get('gender','male')}",
                          "emotion": ln.get("emotion", "neutral"), "event": ln.get("event", "none"),
                          "en": ln.get("original", ""), "ta": ln["tamil"],
                          "max_chars": max(8, int(dur * 13 * 1.15) + 4)})
        prompt = POLISH_PROMPT.format(items=json.dumps(items, ensure_ascii=False))
        try:
            resp = gen(prompt, types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.7), what="polish")
            data = parse_json(resp.text)
            for it in data.get("lines", []):
                k = int(it["id"])
                t = clean_tags(str(it.get("tamil", "")).strip())
                if 0 <= k < len(batch) and strip_tags(t) and \
                        len(strip_tags(t)) <= items[k]["max_chars"] * 1.4 + 6:
                    batch[k]["tamil_formal"] = batch[k]["tamil"]
                    batch[k]["tamil"] = t
                    done += 1
        except Exception as e:  # noqa
            print("  polish fail (original Tamil-ae use aagum):", str(e)[:150])
    print(f"  {done}/{len(lines)} varigal maathapattana")


def step_script(video, before_gaps=None):
    script_path = os.path.join(WORK, "script.json")
    if os.path.exists(script_path):
        print("[2] script.json cache irukku, skip")
        report(54, "✅ script ஏற்கனவே இருக்கு — மறுபடி எழுதல (வேகம்)")
        return json.load(open(script_path, encoding="utf-8"))

    print("[2] Gemini-ku video anuppi script eluthurom...")
    proxy = make_proxy(video)
    total = duration(proxy)
    step = CHUNK_MIN * 60
    chars, lines = {}, []
    n = int(np.ceil(total / step))
    for i in range(n):
        off = i * step
        clip = os.path.join(WORK, f"chunk_{i}.mp4")
        report(36 + 18 * i / n, f"Gemini பேச்சை எழுதுது ({i+1}/{n})")
        cs = max(0.0, off - 3.0)                       # 3s overlap: chunk nadula pechu vettu padaama
        cut_clip(proxy, cs, min(step, total - off) + (off - cs) + 3.0, clip)
        print(f"  chunk {i+1}/{n} ({off/60:.0f} min-la irundhu)")
        for nl in run_clip(clip, cs, chars):
            if off - 0.01 <= nl["start"] < off + step:  # start-a vaichu yaar-ku sondham-nu mudivu
                lines.append(nl)

    lines.sort(key=lambda x: x["start"])
    if before_gaps:
        report(54, "குரல்/BGM பிரிப்பு முடியக் காத்திருக்குது")
        before_gaps()          # gap check-ku vocals.wav venum
    report(55, "விடுபட்ட பேச்சு இருக்கான்னு பாக்குது")
    lines = fill_gaps(proxy, lines, chars, total)
    for sp in {l["speaker"] for l in lines}:
        chars.setdefault(sp, {"id": sp, "gender": "male", "age": "adult",
                              "pitch": "mid", "tone": "neutral"})
    polish(lines, chars)
    out = {"characters": chars, "lines": lines}
    json.dump(out, open(script_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for f_ in glob.glob(os.path.join(WORK, "chunk_*.mp4")) + glob.glob(os.path.join(WORK, "gap_*.mp4")):
        try:
            os.remove(f_)
        except Exception:
            pass
    print(f"  {len(lines)} varigal, {len(chars)} characters")
    return out


# ----------------------------- LANGUAGES ------------------------------------
LANGS = {
    "ta": {"name": "Tamil", "native": "தமிழ்", "iso3": "tam", "cps": 13.0, "block": (0x0B80, 0x0BFF),
           "style": "casual everyday spoken Chennai Tamil exactly like Tamil-dubbed movies, never literary Tamil"},
    "te": {"name": "Telugu", "native": "తెలుగు", "iso3": "tel", "cps": 13.0, "block": (0x0C00, 0x0C7F),
           "style": "casual everyday spoken Telugu (vyavaharika bhasha) exactly like Telugu-dubbed movies, never bookish or formal Telugu"},
    "ml": {"name": "Malayalam", "native": "മലയാളം", "iso3": "mal", "cps": 12.0, "block": (0x0D00, 0x0D7F),
           "style": "casual everyday spoken Malayalam exactly like Malayalam-dubbed movies, never bookish or formal Malayalam"},
    "kn": {"name": "Kannada", "native": "ಕನ್ನಡ", "iso3": "kan", "cps": 13.0, "block": (0x0C80, 0x0CFF),
           "style": "casual everyday spoken Kannada exactly like Kannada-dubbed movies, never bookish or formal Kannada"},
    "hi": {"name": "Hindi", "native": "हिन्दी", "iso3": "hin", "cps": 14.0, "block": (0x0900, 0x097F),
           "style": "casual everyday spoken Hindi (bolchaal ki Hindi) exactly like Hindi-dubbed movies, never shuddh/Sanskritised or formal Hindi"},
}
LANG_LONG = {"tamil": "ta", "தமிழ்": "ta", "telugu": "te", "தெலுங்கு": "te", "తెలుగు": "te",
             "malayalam": "ml", "மலையாளம்": "ml", "മലയാളം": "ml", "kannada": "kn", "கன்னடம்": "kn", "ಕನ್ನಡ": "kn",
             "hindi": "hi", "ஹிந்தி": "hi", "இந்தி": "hi", "हिन्दी": "hi", "हिंदी": "hi"}
LANG_WORDS = dict(LANG_LONG, **{"ta": "ta", "tam": "ta", "te": "te", "tel": "te", "ml": "ml", "mal": "ml",
                                "kn": "kn", "kan": "kn", "hi": "hi", "hin": "hi"})
ALL_WORDS = {"all", "alllang", "அனைத்தும்", "எல்லாம்"}
LANG_LONG_SET = set(LANG_LONG) | ALL_WORDS
NOTES = []          # user-ku sollanum-nu irukkura echcharikkaigal (bot caption-la kaattum)


def parse_langs(x):
    """'tamil telugu' / 'ta,te' / ['ta'] / 'all' -> ['ta','te',...'] (order maaraadhu). Therila na ['ta']."""
    if not x:
        return ["ta"]
    toks = re.split(r"[\s,+/|]+", x.lower()) if isinstance(x, str) else [str(t).lower() for t in x]
    out = []
    for t in toks:
        if t in ALL_WORDS:
            out += list(LANGS)
        elif t in LANG_WORDS:
            out.append(LANG_WORDS[t])
    seen = []
    for c in out:
        if c not in seen:
            seen.append(c)
    return seen or ["ta"]


def line_text(ln, lang):
    if lang == "ta":
        return ln.get("tamil", "")
    return (ln.get("t") or {}).get(lang, "")


def native_ok(t, lang):
    """Antha mozhi ezhuthu irukka? (English/Hindi/Tamil leak aagaama paarkka)"""
    lo, hi = LANGS[lang]["block"]
    return any(lo <= ord(ch) <= hi for ch in strip_tags(t))


TRANSLATE_PROMPT = """You are the director and dialogue writer of a professional {lang} dub (the kind heard in
{lang}-dubbed Hollywood films, anime and web series). Below is a conversation, line by line, IN ORDER.
Write every line in {lang} so it sounds like a real person ACTING and TALKING in the moment, never like text read aloud.

Language style (VERY IMPORTANT): {style}.
Write ONLY in {lang} ({native}) script. Common loanwords (OK, sorry, thanks, phone) may be written in {native} script.
No other language and no English sentences.
- Translate from the ORIGINAL words ("en"). The "ta" field is only a Tamil adaptation to help with meaning; never copy it.
- Natural speech with the contractions, reactions and interjections real {lang} speakers use. Copy the rhythm and emotion
  of the original: short exclamations stay short, keep repeated words, stutters, interruptions ("—") and trailing off ("...").
- Choose how people address each other (informal/respectful forms, honorifics) from the conversation and keep the same
  style for the same pair of people throughout.
- HARD LIMIT: every line has max_chars (what fits the original timing). Stay at or below it.
- Audio tags: at most two English tags in square brackets, only when the original really has it: [angry] [shouting]
  [whispers] [sad] [crying] [scared] [excited] [nervous] [sarcastic] [sighs] [laughs] [chuckles] [gasps] [breathes] [exhales].
  A delivery tag goes at the start of the line, an event tag where it happens. Plain neutral lines get no tag.
- Never explain anything.

Input JSON items: id, who, emotion, event, en (original words), ta (Tamil adaptation), max_chars.
Return ONLY JSON: {{"lines":[{{"id":0,"text":"..."}}]}} with EVERY id exactly once.

INPUT:
{items}"""


def step_translate(script, langs, base_pct=58, span=8):
    """Tamil-ai thavira matra mozhigalukku (te/ml/kn/hi) asal pechilirundhu dub-style varigal ezhuthum.
    Mozhigal ellaam SERNDHU (parallel) odum. script.json-la save aagum -> adutha murai thirumba ezhutha vendaam."""
    chars, lines = script["characters"], script["lines"]
    todo = [l for l in langs if l != "ta"]
    if not todo:
        return
    lock = threading.Lock()
    done_n = [0]

    def missing(lang):
        return [k for k, ln in enumerate(lines) if not (ln.get("t") or {}).get(lang)]

    def one(lang):
        info = LANGS[lang]
        if not missing(lang):
            print(f"  {info['native']} cache irukku, skip")
            return False
        print(f"  {info['name']} varigal ezhuthurom ({len(missing(lang))} varigal)...")
        fails = 0
        for rnd in range(4):          # ovvoru round-um neelam vidhi-ya konjam thalarthum -> oru vari kooda vidupadaadhu
            need = missing(lang)
            if not need or fails >= 3:
                break
            slack = (1.4, 2.0, 3.0, 99.0)[rnd]
            B = 30 if rnd == 0 else 10
            for i in range(0, len(need), B):
                check_cancel()
                idxs = need[i: i + B]
                items = []
                for j, k in enumerate(idxs):
                    ln = lines[k]
                    c = chars.get(ln["speaker"], {})
                    dur = ln["end"] - ln["start"]
                    items.append({"id": j, "who": f"{c.get('age', 'adult')} {c.get('gender', 'male')}",
                                  "emotion": ln.get("emotion", "neutral"), "event": ln.get("event", "none"),
                                  "en": ln.get("original", ""), "ta": ln["tamil"],
                                  "max_chars": max(8, int(dur * info["cps"] * 1.15) + 4)})
                prompt = TRANSLATE_PROMPT.format(lang=info["name"], native=info["native"], style=info["style"],
                                                 items=json.dumps(items, ensure_ascii=False))
                try:
                    resp = gen(prompt, types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=0.6), what=f"translate-{lang}")
                    for it in parse_json(resp.text).get("lines", []):
                        j = int(it["id"])
                        t = clean_tags(str(it.get("text", "")).strip())
                        if 0 <= j < len(idxs) and native_ok(t, lang) and \
                                len(strip_tags(t)) <= items[j]["max_chars"] * slack + 6:
                            with lock:
                                lines[idxs[j]].setdefault("t", {})[lang] = t
                except Exception as e:  # noqa
                    fails += 1
                    print(f"  {lang} translate fail: {str(e)[:150]}")
                    if fails >= 3:
                        break
        left = missing(lang)
        if left:
            NOTES.append(f"{info['native']}: {len(left)}/{len(lines)} வரி எழுத முடியல (அந்த இடங்கள் அமைதியா இருக்கும்)")
        with lock:
            done_n[0] += 1
            report(base_pct + span * done_n[0] / len(todo), f"{info['native']} வரிகள் தயார்")
        return True

    report(base_pct, "மொழி வரிகள் எழுதுது (எல்லா மொழியும் ஒரே நேரத்தில்)")
    with ThreadPoolExecutor(max_workers=min(3, len(todo))) as ex:
        changed = any(ex.map(one, todo))
    if changed:
        try:
            json.dump(script, open(os.path.join(WORK, "script.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:  # noqa
            print("  script save fail:", str(e)[:100])


# ----------------------------- ElevenLabs -----------------------------------
def get_eleven_key():
    k = os.environ.get("ELEVENLABS_API_KEY")
    if k:
        return k
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret("ELEVENLABS_API_KEY")
    except Exception:
        raise SystemExit("ELEVENLABS_API_KEY illa. Kaggle Secrets-la add pannunga.")


EL_KEY = get_eleven_key()
EL_URL = "https://api.elevenlabs.io"
EL_STATE = {"dead": False, "fmt": "pcm_24000", "voices": None, "model": ELEVEN_MODEL}
RATE = {k: v["cps"] for k, v in LANGS.items()}   # oru nodiyil ivlo ezhuthu (dub-la irundhu thaanaa kathukkum)


def el_req(method, path, what, **kw):
    def go():
        r = requests.request(method, EL_URL + path, headers={"xi-api-key": EL_KEY},
                             timeout=180, **kw)
        if r.status_code >= 400:
            tag = " no_retry" if r.status_code in (400, 401, 402, 403, 404, 422) else ""
            raise RuntimeError(f"ElevenLabs {r.status_code}{tag}: {r.text[:300]}")
        return r
    return retry(go, tries=5, base=5.0, what=what)


def credit_mult(lang="ta"):
    m = EL_STATE["model"]
    return 0.5 if (lang in ("ta", "hi") and ("flash" in m or "turbo" in m)) else 1.0


def el_credits():
    """(meedham, motha) credits. Therila na (None, None)."""
    try:
        d = el_req("GET", "/v1/user/subscription", "credits").json()
        lim, used = int(d["character_limit"]), int(d["character_count"])
        return lim - used, lim
    except Exception as e:  # noqa
        print("  credits check mudiyala:", str(e)[:150])
        return None, None


def el_voices():
    if EL_STATE["voices"] is None:
        out = []
        for path in ("/v1/voices", "/v2/voices?page_size=100"):
            try:
                out = el_req("GET", path, "voices").json().get("voices", [])
                if out:
                    break
            except Exception as e:  # noqa
                print("  voices list fail:", str(e)[:120])
        EL_STATE["voices"] = out
    return EL_STATE["voices"]


def delete_voice(vid):
    try:   # retry/cancel check illaama (cancel pannaalum clean-up nadakkanum)
        requests.delete(f"{EL_URL}/v1/voices/{vid}", headers={"xi-api-key": EL_KEY}, timeout=30)
    except Exception:
        pass


def cleanup_old_voices():
    """Munnaadi nindru ponna job-oda meendha 'dubbot_' clone-gal voice slot-a thinnaama delete."""
    for v in el_voices():
        if str(v.get("name", "")).startswith(VOICE_PREFIX):
            print("  pazhaya clone delete:", v.get("name"))
            delete_voice(v.get("voice_id"))
    EL_STATE["voices"] = None


def cut_pcm(src, start, dur):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, start):.2f}", "-t", f"{dur:.2f}",
                        "-i", src, "-ac", "1", "-ar", "44100", "-f", "s16le", "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.int16)


def load_pcm(src):
    """Audio-va oru thadavai mattum decode (44.1k mono). Appuram numpy-la thundu edukkalaam (ffmpeg per line illa)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", "44100", "-f", "s16le", "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.int16)


def build_sample(cid, lines, pcm, out_path):
    """Oru character-oda thelivaana pechu thundugalai serthu clone sample-aakkum. Seconds-a thirumbum."""
    mine = [l for l in lines if l["speaker"] == cid]
    others = [l for l in lines if l["speaker"] != cid]

    def pick(strict):
        cand = []
        for l in mine:
            d = l["end"] - l["start"]
            if d < (1.2 if strict else 0.9) or d > 20:
                continue
            if strict:
                a, b = l["start"] - 0.2, l["end"] + 0.2
                if any(min(b, o["end"]) - max(a, o["start"]) > 0 for o in others):
                    continue
            cand.append((d, l))
        cand.sort(key=lambda x: -x[0])
        chosen, total = [], 0.0
        for d, l in cand:
            if total >= 70:
                break
            chosen.append(l)
            total += d
        return chosen, total

    chosen, total = pick(True)
    if total < MIN_SAMPLE_S:
        chosen, total = pick(False)
    if total < MIN_SAMPLE_S:
        return 0.0
    gap = np.zeros(int(0.25 * 44100), dtype=np.int16)
    parts = []
    for l in sorted(chosen, key=lambda x: x["start"]):
        a0 = int(max(0.0, l["start"] + 0.03) * 44100)
        x = pcm[a0: a0 + int(((l["end"] - l["start"]) - 0.06) * 44100)]
        if len(x) > 4410:
            parts += [x, gap]
    if not parts:
        return 0.0
    audio = np.concatenate(parts)
    wav = out_path.replace(".mp3", ".wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(audio.tobytes())
    r = sh(f'ffmpeg -y -i "{wav}" -codec:a libmp3lame -b:a 128k "{out_path}"', check=False)
    if r.returncode != 0 or not os.path.exists(out_path):
        shutil.copy(wav, out_path.replace(".mp3", ".wavsample"))
        out_path = wav
    return len(audio) / 44100


def clone_voice(cid, sample_path, remove_bg):
    ext = os.path.splitext(sample_path)[1].lower()
    mime = "audio/mpeg" if ext == ".mp3" else "audio/wav"
    data = open(sample_path, "rb").read()
    name = f"{VOICE_PREFIX}{cid}_{int(time.time()) % 100000}"
    r = el_req("POST", "/v1/voices/add", f"clone {cid}",
               data={"name": name, "description": "dubbing character voice",
                     "remove_background_noise": "true" if remove_bg else "false"},
               files=[("files", (os.path.basename(sample_path), data, mime))])
    return r.json()["voice_id"]


def premade_voice(c, used):
    g = c.get("gender", "male")
    want = {"child": "young", "young": "young", "adult": "middle_aged", "elder": "old"}.get(
        c.get("age", "adult"), "middle_aged")
    pool = [v for v in el_voices() if v.get("category") == "premade"
            and (v.get("labels") or {}).get("gender") == g]
    pool.sort(key=lambda v: ((v.get("labels") or {}).get("age") != want, v.get("voice_id") in used))
    if pool:
        return pool[0]["voice_id"]
    return "JBFqnCBsd6RMkjVDRZzb" if g == "male" else "21m00Tcm4TlvDq8ikWAM"


def step_voices(script):
    """Ovvoru character-ku asal kural clone (illa na premade). Clone-gal SERNDHU (parallel). (voices, info, created) tharum."""
    chars, lines = script["characters"], script["lines"]
    voc = os.path.join(WORK, "vocals.wav")
    has_voc = os.path.exists(voc)
    src = voc if has_voc else os.path.join(WORK, "full_audio.wav")
    talk = {cid: sum(l["end"] - l["start"] for l in lines if l["speaker"] == cid) for cid in chars}
    order = sorted(chars, key=lambda c: -talk.get(c, 0))
    pcm = load_pcm(src) if os.path.exists(src) else None
    info, created, lock, n_clone = {}, [], threading.Lock(), [0]
    got = {}

    def do(cid):
        check_cancel()
        if pcm is None or talk.get(cid, 0) < 1.0:
            return
        with lock:
            if n_clone[0] >= MAX_CLONES:
                return
            n_clone[0] += 1                       # idam othukkiduvom
        ok = False
        sample = os.path.join(WORK, f"sample_{cid}.mp3")
        try:
            dur = build_sample(cid, lines, pcm, sample)
            if dur >= MIN_SAMPLE_S:
                sp = sample if os.path.exists(sample) else sample.replace(".mp3", ".wav")
                vid = clone_voice(cid, sp, remove_bg=not has_voc)
                with lock:
                    created.append(vid)
                    got[cid] = vid
                    info[cid] = f"clone ({dur:.0f}s)"
                ok = True
            else:
                info[cid] = f"sample kammi ({dur:.0f}s)"
        except Exception as e:  # noqa
            print(f"  {cid} clone fail: {str(e)[:200]}")
            info[cid] = "clone fail"
        if not ok:
            with lock:
                n_clone[0] -= 1                   # fail aana idam thirumba kidaikkum

    report(61, f"கேரக்டர் குரல்கள் clone ஆகுது (0/{len(order)})")
    with ThreadPoolExecutor(max_workers=3) as ex:
        for k, _ in enumerate(ex.map(do, order)):
            report(61 + 5 * (k + 1) / max(1, len(order)), f"கேரக்டர் குரல் clone ({k+1}/{len(order)})")
    voices, used = {}, set()
    for cid in order:
        vid = got.get(cid)
        if not vid:
            vid = premade_voice(chars[cid], used)
            info[cid] = info.get(cid, "") + " -> premade"
        used.add(vid)
        voices[cid] = vid
    print("[3] Kural:", info)
    return voices, info, created


def decode_audio(b):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "-"], input=b, capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def is_expressive(m):
    return m.startswith("eleven_v3") or m.startswith("eleven_v4")


def el_tts(text, voice_id, emotion, prev_text=None, next_text=None, lang="ta"):
    m = EL_STATE["model"]
    if lang in ("te", "ml", "kn") and not is_expressive(m):
        m = "eleven_v3"          # Telugu / Malayalam / Kannada-ku v3 thaan venum (Tamil, Hindi-ku ellaa model-um okay)
    expr = is_expressive(m)
    if expr:     # v3/v4: audio tag-a nadippaanga; stability = Creative(0) / Natural(0.5)
        hot = emotion in ("angry", "shout", "scared", "happy")
        vs = {"stability": 0.0 if hot else 0.5, "similarity_boost": 0.85}
    else:        # v2/flash: tag-a vaasikkum, athanaala neekkanum
        text = strip_tags(text)
        prev_text = strip_tags(prev_text) if prev_text else None
        next_text = strip_tags(next_text) if next_text else None
        stab, style = {"angry": (0.30, 0.55), "shout": (0.30, 0.60), "scared": (0.35, 0.45),
                       "happy": (0.40, 0.40), "sad": (0.55, 0.30), "whisper": (0.60, 0.20),
                       "calm": (0.60, 0.20)}.get(emotion, (0.45, 0.30))
        vs = {"stability": stab, "similarity_boost": 0.85, "style": style, "use_speaker_boost": True}
    body = {"text": text, "model_id": m, "voice_settings": vs}
    if not expr:
        if prev_text:
            body["previous_text"] = prev_text[:400]
        if next_text:
            body["next_text"] = next_text[:400]
    if "flash" in m or "turbo" in m:
        body["language_code"] = lang

    def call(fmt):
        return el_req("POST", f"/v1/text-to-speech/{voice_id}?output_format={fmt}", "eleven-tts", json=body)

    try:
        try:
            r = call(EL_STATE["fmt"])
        except RuntimeError as e:
            if "output_format" in str(e) and EL_STATE["fmt"] != "mp3_44100_128":
                EL_STATE["fmt"] = "mp3_44100_128"        # pcm_24000 ungal plan-la illa na mp3
                r = call(EL_STATE["fmt"])
            else:
                raise
    except RuntimeError as e:
        msg = str(e)
        if expr and lang == "ta" and EL_STATE["model"] == m and "model" in msg.lower() and "quota_exceeded" not in msg:
            print(f"  {m} work aagala -> eleven_multilingual_v2-ku maaruren")
            EL_STATE["model"] = "eleven_multilingual_v2"
            return el_tts(text, voice_id, emotion, prev_text, next_text, lang)
        raise
    if EL_STATE["fmt"].startswith("pcm_"):
        return pcm_to_np(r.content)
    return decode_audio(r.content)


# ----------------------------- STEP 3: voices -------------------------------
# name: (gender, age, pitch)
VOICE_POOL = {
    "Charon": ("male", "adult", "low"), "Orus": ("male", "adult", "low"),
    "Alnilam": ("male", "adult", "low"), "Enceladus": ("male", "adult", "low"),
    "Algenib": ("male", "elder", "low"), "Rasalgethi": ("male", "elder", "mid"),
    "Iapetus": ("male", "adult", "mid"), "Umbriel": ("male", "adult", "mid"),
    "Algieba": ("male", "adult", "mid"), "Schedar": ("male", "adult", "mid"),
    "Zubenelgenubi": ("male", "adult", "mid"), "Sadaltager": ("male", "adult", "mid"),
    "Fenrir": ("male", "young", "mid"), "Achird": ("male", "young", "mid"),
    "Puck": ("male", "young", "high"), "Sadachbia": ("male", "young", "high"),
    "Kore": ("female", "adult", "mid"), "Callirrhoe": ("female", "adult", "mid"),
    "Despina": ("female", "adult", "mid"), "Erinome": ("female", "adult", "mid"),
    "Sulafat": ("female", "adult", "mid"), "Autonoe": ("female", "young", "mid"),
    "Aoede": ("female", "young", "mid"), "Pulcherrima": ("female", "young", "mid"),
    "Zephyr": ("female", "young", "high"), "Leda": ("female", "young", "high"),
    "Laomedeia": ("female", "young", "high"), "Achernar": ("female", "young", "high"),
    "Gacrux": ("female", "elder", "low"), "Vindemiatrix": ("female", "elder", "mid"),
}


def assign_voices(chars):
    used, mapping = set(), {}
    # adhigam pesuravanga mudhalla (nalla voice kidaikkum)
    for cid, c in chars.items():
        g = c.get("gender", "male")
        age = c.get("age", "adult")
        pitch = c.get("pitch", "mid")
        if age == "child":
            age, pitch = "young", "high"
        best, best_score = None, -1
        for name, (vg, va, vp) in VOICE_POOL.items():
            if vg != g:
                continue
            score = (3 if va == age else 0) + (2 if vp == pitch else 0)
            if name not in used:
                score += 4
            if score > best_score:
                best, best_score = name, score
        mapping[cid] = best or "Kore"
        used.add(mapping[cid])
    return mapping


def style_text(c, emotion, lang="ta"):
    age = c.get("age", "adult")
    g = c.get("gender", "male")
    who = "a child" if age == "child" else f"a {age} {g}"
    info = LANGS[lang]
    return (f"You are the {info['name']} dubbing voice actor for {who} in a professional {info['name']} movie dub. "
            f"Voice character: {c.get('tone','natural')}. Current emotion: {emotion}. "
            f"Speak casual, everyday spoken {info['name']} exactly as written, with a relaxed conversational rhythm "
            f"and real acting emotion, like dialogue in a {info['name']} film. Not like a news reader and not formal. "
            f"Read only the user's text, never read these instructions.")


# ----------------------------- STEP 4: TTS ----------------------------------
def pcm_to_np(data):
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def trim_silence(x, thr=0.01, pad=int(0.04 * SR)):
    idx = np.where(np.abs(x) > thr)[0]
    if len(idx) == 0:
        return x
    return x[max(0, idx[0] - pad): min(len(x), idx[-1] + pad)]


def drop_tail_burst(x):
    """Pechu mudinjapuram varra 'kirich' maadhiri chinna burst irundha vettidum."""
    W = int(0.02 * SR)
    n = len(x) // W
    if n < 6:
        return x
    rms = np.sqrt((x[: n * W].reshape(n, W) ** 2).mean(axis=1))
    thr = max(0.008, 0.06 * float(rms.max()))
    act = rms > thr
    segs, i = [], 0
    while i < n:
        if act[i]:
            j = i
            while j < n and act[j]:
                j += 1
            segs.append((i, j))
            i = j
        else:
            i += 1
    if len(segs) >= 2:
        last, prev = segs[-1], segs[-2]
        if (last[1] - last[0]) * 0.02 < 0.2 and (last[0] - prev[1]) * 0.02 >= 0.06:
            x = x[: (prev[1] + 2) * W]
    return x


def declick(x):
    """Clip-oda arambam/mudivu click-ku: tail burst vettu, DC neekki, fade, loudness samam."""
    if len(x) < int(0.2 * SR):
        return x
    x = drop_tail_burst(x)
    x = x - float(np.mean(x[:int(0.01 * SR)]))
    pk = float(np.abs(x).max())
    if pk > 1e-4:
        x = x * (0.8 / pk)
    fi, fo = int(0.015 * SR), int(0.09 * SR)
    x = x.copy()
    x[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)
    x[-fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)
    return x


def tts_once(text, voice, system, model, use_system=True):
    cfg = dict(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    if use_system:
        cfg["system_instruction"] = system
    resp = client.models.generate_content(
        model=model, contents=text, config=types.GenerateContentConfig(**cfg))
    part = resp.candidates[0].content.parts[0]
    return pcm_to_np(part.inline_data.data)


_state = {"model": TTS_MODEL, "use_system": True}


def tts(text, voice, system):
    def go():
        try:
            return tts_once(text, voice, system, _state["model"], _state["use_system"])
        except Exception as e:  # noqa
            m = str(e)
            if _state["use_system"] and ("system_instruction" in m or "INVALID_ARGUMENT" in m):
                print("  system_instruction support illa pola, adhu illama try pannuren")
                _state["use_system"] = False
                return tts_once(text, voice, system, _state["model"], False)
            if "404" in m or "NOT_FOUND" in m:
                if _state["model"] != TTS_FALLBACK:
                    print(f"  {_state['model']} kidaikkala, {TTS_FALLBACK}-ku maaruren")
                    _state["model"] = TTS_FALLBACK
                    return tts_once(text, voice, system, _state["model"], _state["use_system"])
            raise
    return trim_silence(retry(go, tries=3, base=3.0, what="tts"))


def shorten(text, ratio, lang="ta"):
    nm = LANGS[lang]["name"]
    prompt = (f"Shorten this colloquial {nm} dubbing line to about {int(ratio*100)}% of its length. "
              f"Keep meaning and tone, keep it spoken {nm}, in {LANGS[lang]['native']} script only. "
              f"Return ONLY the {nm} text.\n\n{text}")
    r = gen(prompt, what="shorten")
    return r.text.strip().strip('"')


def speed_np(x, factor):
    """ffmpeg atempo (pitch maaraamal vegam)."""
    if factor <= 1.01:
        return x
    uid = uuid.uuid4().hex[:10]       # thread-ku thani file (munnadi ore file-a share panni mix aachu)
    tmp_in, tmp_out = os.path.join(WORK, f"_s_{uid}_in.wav"), os.path.join(WORK, f"_s_{uid}_out.wav")
    write_wav(tmp_in, x)
    try:
        sh(f'ffmpeg -y -i "{tmp_in}" -filter:a "atempo={factor:.3f}" "{tmp_out}"')
        return read_wav(tmp_out)
    finally:
        for t in (tmp_in, tmp_out):
            if os.path.exists(t):
                os.remove(t)


def write_wav(path, x, sr=SR):
    x16 = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x16.tobytes())


def read_wav(path):
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    return pcm_to_np(data)


STATS = {"tts_failed": 0, "fallback": 0, "gem_dead": False, "last_err": ""}


def step_tts(script, voices, lang="ta", base_pct=67, span=25):
    """Oru mozhikku ellaa varigalukkum kural. (audios, fail count) tharum."""
    chars, lines = script["characters"], script["lines"]
    info = LANGS[lang]
    gem_voices = assign_voices(chars)      # ElevenLabs fail aana line-ku Gemini kural (backup)
    STATS["fallback"] = 0
    tdir = os.path.join(WORK, "tts", lang)
    os.makedirs(tdir, exist_ok=True)
    print(f"[4] {len(lines)} varigalukku {info['name']} kural ({EL_STATE['model']})...")
    silent = lambda: np.zeros(int(0.1 * SR), dtype=np.float32)  # noqa

    def work(i):
        check_cancel()
        ln = lines[i]
        path = os.path.join(tdir, f"{i:05d}.wav")
        if os.path.exists(path):
            return i, read_wav(path)
        text = line_text(ln, lang).strip()
        if not strip_tags(text):                       # indha mozhi varigal illa -> amaidhi
            write_wav(path, silent())
            return i, silent()
        sp = ln["speaker"]
        c = chars.get(sp, {})
        emotion = ln.get("emotion", "neutral")
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else ln["end"] + 1.5
        orig_len = ln["end"] - ln["start"]
        allowed = max(orig_len, min(nxt - ln["start"] - 0.05, orig_len * 1.6 + 0.5))
        # credits veenaagaama: munnaadiye neelam kooduna varigalai surukkum
        est = len(strip_tags(text)) / RATE[lang]
        if est > allowed * 1.25 and len(strip_tags(text)) > 12:
            try:
                text = shorten(text, max(0.5, allowed * 1.1 / est), lang)
            except Exception as e:  # noqa
                print(f"  line {i} shorten fail: {str(e)[:100]}")
        audio = None
        if not EL_STATE["dead"]:
            prev_t = line_text(lines[i - 1], lang) if i > 0 else None
            next_t = line_text(lines[i + 1], lang) if i + 1 < len(lines) else None
            try:
                audio = trim_silence(el_tts(text, voices[sp], emotion, prev_t, next_t, lang))
            except Exception as e:  # noqa
                m = str(e)
                print(f"  line {i} ElevenLabs fail: {m[:160]}")
                STATS["last_err"] = m[:200]
                if any(k in m for k in ("voice_access_denied", "detected_captcha_voice", "requires verification")):
                    # indha clone kural-ku ElevenLabs verification ketkudhu -> premade kural-ku maarrom
                    pv = premade_voice(c, set())
                    print(f"  {sp}: clone kural verification ketkudhu -> premade kural use pannuren")
                    voices[sp] = pv
                    try:
                        audio = trim_silence(el_tts(text, pv, emotion, prev_t, next_t, lang))
                    except Exception as e2:  # noqa
                        m = str(e2)
                        STATS["last_err"] = m[:200]
                        print(f"  line {i} premade kural-um fail: {m[:160]}")
                if "quota_exceeded" in m:
                    EL_STATE["dead"] = True
                    report(70, "⚠️ ElevenLabs credits முடிந்தது. மீதி வரிகள் Gemini குரலில்")
        if audio is None or len(audio) < int(0.15 * SR):
            if STATS.get("gem_dead"):
                return i, silent()
            try:
                audio = tts(strip_tags(text), gem_voices.get(sp, "Kore"), style_text(c, emotion, lang))
                STATS["fallback"] += 1
            except Exception as e:  # noqa
                m = str(e)
                print(f"  line {i} Gemini TTS-um fail: {m[:120]}")
                STATS["last_err"] = STATS.get("last_err") or m[:200]
                if "429" in m or "RESOURCE_EXHAUSTED" in m:
                    STATS["gem_dead"] = True      # quota mudinjadhu, ovvoru variyum kaathirukka vendaam
                return i, silent()
        dur = len(audio) / SR
        if dur > 0.6 and len(strip_tags(text)) > 6:
            RATE[lang] = 0.7 * RATE[lang] + 0.3 * (len(strip_tags(text)) / dur)
        if dur > allowed:
            audio = speed_np(audio, min(dur / allowed, MAX_SPEEDUP))
        audio = declick(audio)
        write_wav(path, audio)
        return i, audio

    results = {}
    with ThreadPoolExecutor(max_workers=TTS_WORKERS) as ex:
        for k, (i, a_) in enumerate(ex.map(work, range(len(lines)))):
            results[i] = a_
            report(base_pct + span * 0.93 * (k + 1) / len(lines), f"{info['native']} குரல் உருவாக்குது ({k+1}/{len(lines)})")
            if k % 10 == 0:
                print(f"  {k+1}/{len(lines)}")

    def missing():
        return [i for i in range(len(lines)) if not os.path.exists(os.path.join(tdir, f"{i:05d}.wav"))]

    for rnd in range(3):
        failed = missing()
        if not failed or STATS.get("gem_dead") and EL_STATE["dead"]:
            break
        print(f"  {len(failed)} varigal fail aachu, marubadi try (round {rnd+1})...")
        report(base_pct + span * 0.95, f"{len(failed)} வரி மறுமுயற்சி")
        CANCEL.wait(10)
        check_cancel()
        for i in failed:
            _, results[i] = work(i)
    failed = missing()
    STATS["tts_failed"] = len(failed)
    return [results[i] for i in range(len(lines))], len(failed)


# ----------------------------- STEP 5: mix ----------------------------------
def step_mix(video, bgm, lines, audios_by_lang, langs):
    """Ovvoru mozhikkum oru audio track (BGM + antha mozhi dub). Palamozhi na MKV-la ellaa track-um."""
    print("[5] Ellathaiyum serkkirom...")
    total = duration(video)
    tracks = []
    for lg in langs:
        track = np.zeros(int((total + 5) * SR), dtype=np.float32)
        for ln, a in zip(lines, audios_by_lang[lg]):
            s = int(ln["start"] * SR)
            e = min(len(track), s + len(a))
            if s < len(track):
                track[s:e] += a[: e - s]
        peak = np.abs(track).max()
        if peak > 1.0:
            track /= peak
        dub_wav = os.path.join(WORK, f"dub_track_{lg}.wav")
        write_wav(dub_wav, track)
        tracks.append(dub_wav)
        del track
    n = len(tracks)
    is_mkv = OUTPUT_VIDEO.lower().endswith(".mkv")

    def build(norm):
        nn = ":normalize=0" if norm else ""
        parts = []
        if n == 1:
            parts.append(f"[1:a]volume={BGM_VOLUME}[b0]")
        else:
            parts.append(f"[1:a]volume={BGM_VOLUME},asplit={n}" + "".join(f"[b{k}]" for k in range(n)))
        for k in range(n):
            parts.append(f"[{k + 2}:a]aresample=44100,volume={VOICE_VOLUME}[d{k}]")
            parts.append(f"[b{k}][d{k}]amix=inputs=2:duration=first:dropout_transition=0{nn}[m{k}]")
            parts.append(f"[m{k}]alimiter=limit=0.95[a{k}]")
        fc = ";".join(parts)
        cmd = (f'ffmpeg -y -i "{video}" -i "{bgm}" ' + "".join(f'-i "{t}" ' for t in tracks) +
               f'-filter_complex "{fc}" -map 0:v ' + "".join(f'-map "[a{k}]" ' for k in range(n)))
        meta = "".join(f'-metadata:s:a:{k} language={LANGS[lg]["iso3"]} -metadata:s:a:{k} title="{LANGS[lg]["name"]} Dub" '
                       for k, lg in enumerate(langs))
        if is_mkv:
            # MKV: dub track-gal mudhalla (mudhal mozhi default) + asal audio kadaisi track
            cmd += ('-map 0:a:0? -c:v copy -c:a aac -b:a 192k ' + meta +
                    f'-metadata:s:a:{n} language=und -metadata:s:a:{n} title="Original" '
                    '-disposition:a:0 default ' + "".join(f"-disposition:a:{k} 0 " for k in range(1, n + 1)))
        else:
            cmd += '-c:v copy -c:a aac -b:a 192k -movflags +faststart ' + meta
        return cmd + f'-shortest "{OUTPUT_VIDEO}"'

    r = sh(build(True), check=False)
    if r.returncode != 0:  # pazhaya ffmpeg-la normalize illa
        sh(build(False))
    for t in tracks:                       # periya temp wav-gal neekku (cache-la thevai illa)
        try:
            os.remove(t)
        except Exception:
            pass
    print(f"\nMudinjadhu -> {OUTPUT_VIDEO}")


# ----------------------------- CACHE ----------------------------------------
def cache_key(path):
    """Video-oda adaiyaalam (size + 3 idathula 2MB). Adhe video thirumba vandha script/BGM/kural cache payanpadum."""
    h = hashlib.sha1(str(os.path.getsize(path)).encode())
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        for pos in (0, max(0, size // 2 - 1_000_000), max(0, size - 2_000_000)):
            f.seek(pos)
            h.update(f.read(2_000_000))
    return h.hexdigest()[:16]


def prune_cache(root, keep=3):
    """Kadaisi 'keep' videos-oda cache mattum vachukkum (disk nirambaama)."""
    try:
        ds = sorted((os.path.join(root, d) for d in os.listdir(root)), key=os.path.getmtime, reverse=True)
        for d in ds[keep:]:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# ----------------------------- MAIN -----------------------------------------
def dub(video, job_dir, progress=None, fmt="mp4", model=None, langs=None, cache_dir=None):
    """Video-va (asal kural clone-oda) dub panni output path-a tharum.
    langs: ["ta","te","ml","kn"] (ethaiyum serkkalam). Onnukku mela na MKV-la multi-audio."""
    global WORK, OUTPUT_VIDEO, _PROG
    _PROG = progress or (lambda p, t: print(f"[{p}%] {t}"))
    langs = parse_langs(langs)
    NOTES.clear()
    EL_STATE["model"] = model or ELEVEN_MODEL
    EL_STATE["dead"] = False
    STATS["gem_dead"], STATS["last_err"] = False, ""
    WORK = cache_dir or os.path.join(job_dir, "work")     # cache_dir = script/BGM/kural thangum (adutha mozhikku reuse)
    fmt = "mkv" if (str(fmt).lower() == "mkv" or len(langs) > 1) else "mp4"
    OUTPUT_VIDEO = os.path.join(job_dir, f"dubbed_{'_'.join(langs)}.{fmt}")
    os.makedirs(os.path.join(WORK, "tts"), exist_ok=True)
    try:
        os.utime(WORK, None)
    except Exception:
        pass
    if any(l in ("te", "ml", "kn") for l in langs) and not is_expressive(EL_STATE["model"]):
        NOTES.append("தெலுங்கு/மலையாளம்/கன்னடத்துக்கு ElevenLabs v3 மட்டும்தான் வேலை செய்யும், அதனால் அவற்றுக்கு v3 பயன்படுத்தினேன்")
    created = []
    bgm_job = None
    try:
        report(5, f"வீடியோ {duration(video)/60:.1f} நிமிடம்: BGM பிரிப்பும் Gemini-யும் ஒரே நேரத்தில் ஓடுது")
        bgm_job = BgmJob(video)
        bgm_job.start()
        report(10, "Gemini பார்த்து பேச்சை எழுதுது")
        script = step_script(video, before_gaps=bgm_job.wait)
        bgm = bgm_job.wait()
        ls = script["lines"]
        if not ls:
            raise RuntimeError("பேச்சு எதுவும் கண்டுபிடிக்கல. வேற வீடியோ try பண்ணுங்க.")
        step_translate(script, langs)
        usable = []
        for lg in langs:
            if lg == "ta" or any(line_text(x, lg) for x in ls):
                usable.append(lg)
            else:
                NOTES.append(f"{LANGS[lg]['native']}: வரிகளே எழுத முடியல, அந்த track சேர்க்கல")
        if not usable:
            raise RuntimeError("எந்த மொழிக்கும் வரிகள் உருவாகல. Gemini quota/error பாருங்க.")
        span = f" ({ls[0]['start']:.0f}s-{ls[-1]['end']:.0f}s)"
        need = int(sum(len(strip_tags(line_text(l, lg))) * credit_mult(lg) for lg in usable for l in ls))
        remaining, limit = el_credits()
        info = f"~{need} credits தேவை" + (f", இருப்பு {remaining}" if remaining is not None else "")
        report(60, f"{len(ls)} வரிகள்{span}, {len(script['characters'])} கேரக்டர், {len(usable)} மொழி | {info}")
        if remaining is not None and need > remaining:
            raise RuntimeError(f"ElevenLabs credits போதாது ({info}). குறைவான மொழி / சின்ன வீடியோ அனுப்புங்க.")
        cleanup_old_voices()
        voices, vinfo, created = step_voices(script)
        n_clone = sum(1 for v in vinfo.values() if v.startswith("clone"))
        report(66, f"{n_clone}/{len(vinfo)} கேரக்டருக்கு அசல் குரல் clone ஆனது")
        audios = {}
        for k, lg in enumerate(usable):
            check_cancel()
            a_, nf = step_tts(script, voices, lg, 67 + 25 * k / len(usable), 25 / len(usable))
            if nf > max(3, 0.3 * len(ls)):
                NOTES.append(f"{LANGS[lg]['native']}: {nf}/{len(ls)} வரிக்கு குரல் வரல "
                             f"({STATS['last_err'][:120]}), அதனால் அந்த track சேர்க்கல")
                continue
            if nf:
                NOTES.append(f"{LANGS[lg]['native']}: {nf} வரிக்கு குரல் வரல")
            audios[lg] = a_
        if not audios:
            raise RuntimeError("எந்த மொழிக்கும் குரல் உருவாகல. காரணம்: " + (STATS["last_err"][:200] or "தெரியல"))
        final = [lg for lg in usable if lg in audios]
        report(93, "BGM + குரல் + வீடியோ சேர்க்குது")
        step_mix(video, bgm, ls, audios, final)
        left, _ = el_credits()
        if left is not None and remaining is not None:
            print(f"  ElevenLabs credits: {remaining} -> {left} (செலவு {remaining - left})")
        report(95, "dub தயார்")
        return OUTPUT_VIDEO
    except BaseException:
        if bgm_job and bgm_job.t.is_alive():      # error vandha demucs-um nirkkanum
            CANCEL.set()
            bgm_job.t.join(30)
        raise
    finally:
        if created and not KEEP_VOICES:
            for vid in created:
                delete_voice(vid)
            print(f"  {len(created)} clone voices delete (slot free)")


if __name__ == "__main__":
    video = find_video()
    dub(video, OUT_DIR, langs=os.environ.get("DUB_LANGS", "ta"))

import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image
import telebot
from telebot import types
from google import genai
from google.genai import types as genai_types


# ================= LOGGING & HEADLESS STDOUT SETUP =================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot_runtime.log")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "bot_state.json")

_log_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=1_500_000, backupCount=1, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger = logging.getLogger("MobinManhwaBot")
logger.setLevel(logging.INFO)
logger.addHandler(_log_handler)
if sys.stdout is not None:
    _stream_handler = logging.StreamHandler(sys.stdout)
    _stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_stream_handler)
else:
    class _NullStream:
        def write(self, msg):
            if msg and msg.strip():
                logger.info(msg.strip())

        def flush(self):
            pass

    sys.stdout = _NullStream()
    sys.stderr = _NullStream()


# ================= CONFIGURATION =================

TELEGRAM_BOT_MAX_BYTES = 20 * 1024 * 1024
FAST_MODEL = "gemini-3.5-flash-lite"
ACCURATE_MODEL = "gemini-3.5-flash"
BOT_START_TIME = time.time()


def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("MODEL_NAME", "").strip()

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not token:
                token = str(cfg.get("telegram_bot_token", "")).strip()
            if not gemini_key:
                gemini_key = str(cfg.get("gemini_api_key", "")).strip()
            if not model:
                model = str(cfg.get("model_name", FAST_MODEL)).strip()
        except Exception as e:
            logger.warning(f"Warning reading config.json: {e}")

    if not model:
        model = FAST_MODEL
    return token, gemini_key, model


BOT_TOKEN, GEMINI_KEY, DEFAULT_CONFIG_MODEL = load_config()

if not BOT_TOKEN or not GEMINI_KEY:
    logger.error("TELEGRAM_BOT_TOKEN or GEMINI_API_KEY is missing!")
    sys.exit(1)

genai_client = genai.Client(api_key=GEMINI_KEY)

SAFETY_SETTINGS = [
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
]


# ================= PERSISTENT STATE =================

STATE_LOCK = threading.Lock()
CHAT_LOCKS = {}
CHAT_LOCKS_GUARD = threading.Lock()


def get_chat_lock(chat_id):
    with CHAT_LOCKS_GUARD:
        if chat_id not in CHAT_LOCKS:
            CHAT_LOCKS[chat_id] = threading.Lock()
        return CHAT_LOCKS[chat_id]


def _default_chat_state():
    return {
        "page": 1,
        "mode": "english",
        "model_name": DEFAULT_CONFIG_MODEL,
        "recent_context": [],
    }


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"Failed loading bot_state.json: {e}")
    return {}


USER_STATE = _load_state()


def save_state():
    with STATE_LOCK:
        tmp_path = STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(USER_STATE, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATE_PATH)
        except Exception as e:
            logger.warning(f"Failed saving state: {e}")


def get_user_state(chat_id):
    key = str(chat_id)
    with STATE_LOCK:
        if key not in USER_STATE or not isinstance(USER_STATE[key], dict):
            USER_STATE[key] = _default_chat_state()
        st = USER_STATE[key]
        for k, v in _default_chat_state().items():
            st.setdefault(k, v)
        return st


# ================= HELPERS & TYPOGRAPHY =================

def esc(val):
    return html.escape(str(val or ""), quote=False)


def send_html(chat_id, html_text, reply_markup=None, reply_to_message_id=None):
    return bot.send_message(
        chat_id,
        html_text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
    )


def reply_html(message, html_text, reply_markup=None):
    return bot.reply_to(message, html_text, parse_mode="HTML", reply_markup=reply_markup)


def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


ZWNJ = "\u200c"


def fix_persian_typography(text):
    if not text:
        return text

    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())

    raw_lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    fixed_lines = []
    persian_letters = r"آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"

    def _join_silent_he_ey(m):
        word = m.group(1)
        if word in ("که", "به", "نه", "چه", "سه", "ده", "مه", "یه"):
            return m.group(0)
        return word + ZWNJ + m.group(2)

    for l in raw_lines:
        if re.match(r"^\s*تصویر\s+[0-9۰-۹]+", l):
            fixed_lines.append(re.sub(r"\s+", " ", l.strip()))
            continue

        # Convert Latin comma/question-mark near Persian letters first (preserve numbers like 1,000)
        l = re.sub(rf"(?<=[{persian_letters}])\s*,", "،", l)
        l = re.sub(rf",(?=\s*[{persian_letters}])", "،", l)
        l = re.sub(rf"(?<=[{persian_letters}])\s*\?", "؟", l)

        # Clean ZWNJ prefixes/suffixes without harming standalone words like "ای کاش"
        l = re.sub(rf"\b(ن?می)\s+(?=[{persian_letters}])", r"\1" + ZWNJ, l)
        l = re.sub(
            rf"(?<=[{persian_letters}])\s+(ها(?:ی|یی|یت|یم|یش|یتان|یمان|یشان)?)(?=\s|[،؛!؟:.\)]|$)",
            ZWNJ + r"\1",
            l,
        )
        l = re.sub(
            rf"(?<=[{persian_letters}]{{2}})\s+(تر(?:ین)?)(?=\s|[،؛!؟:.\)]|$)",
            ZWNJ + r"\1",
            l,
        )
        l = re.sub(
            rf"\b([{persian_letters}]+ه)\s+(ای)(?=\s|[،؛!؟:.\)]|$)",
            _join_silent_he_ey,
            l,
        )

        l = re.sub(r"\s+([،؛!؟:»])", r"\1", l)
        l = re.sub(r"([«])\s+", r"\1", l)
        l = re.sub(r"[ \t]+", " ", l)
        fixed_lines.append(l)

    return "\n\n".join(fixed_lines)


def split_text_cleanly(text, max_len=3900):
    if len(text) <= max_len:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut < max_len // 3:
            cut = remaining.rfind("\n", 0, max_len)
        if cut < max_len // 3:
            cut = remaining.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


def split_and_send(chat_id, text, reply_to_message_id=None):
    chunks = split_text_cleanly(text)
    for idx, chunk in enumerate(chunks):
        rto = reply_to_message_id if idx == 0 else None
        bot.send_message(chat_id, chunk, reply_to_message_id=rto)


def send_txt_file(chat_id, content, filename="chapter_translation.txt", caption=None):
    content = fix_persian_typography(content)
    bio = io.BytesIO((content + "\n").encode("utf-8"))
    bio.name = filename
    bot.send_document(chat_id, bio, caption=caption)


def compress_to_jpeg_bytes(img, quality=92):
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def prepare_image_parts(raw_bytes):
    """Slices tall vertical webtoon strips into overlapping tiles so tiny text remains sharp."""
    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if w > 2200:
        img = img.resize((2200, int(h * (2200 / w))), Image.Resampling.LANCZOS)
        w, h = img.size

    if h <= 4000:
        return [genai_types.Part.from_bytes(data=compress_to_jpeg_bytes(img, 92), mime_type="image/jpeg")]

    tile_h = 3400
    overlap = 260
    max_tiles = 6
    step = tile_h - overlap
    parts = []
    y = 0
    tile_idx = 0
    while y < h and tile_idx < max_tiles:
        y_end = h if tile_idx == max_tiles - 1 else min(h, y + tile_h)
        crop = img.crop((0, y, w, y_end))
        parts.append(genai_types.Part.from_bytes(data=compress_to_jpeg_bytes(crop, 90), mime_type="image/jpeg"))
        if y_end >= h:
            break
        y += step
        tile_idx += 1
    return parts


# ================= KEYBOARD =================

def get_main_keyboard(st):
    lang_btn = "🌐 مبدأ: کره‌ای خام" if st.get("mode") == "korean" else "🌐 مبدأ: انگلیسی"
    acc_btn = "🎯 دقت: بالا (Flash)" if st.get("model_name") == ACCURATE_MODEL else "⚡️ دقت: سریع (Lite)"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🔄 شروع چپتر جدید (صفحه ۱)"), types.KeyboardButton(lang_btn))
    kb.row(types.KeyboardButton(acc_btn), types.KeyboardButton("❓ راهنما"))
    return kb


# ================= GEMINI ENGINE =================

def generate_with_retry(model_name, contents, retries=3):
    config = genai_types.GenerateContentConfig(
        safety_settings=SAFETY_SETTINGS,
        temperature=0.3,
    )
    last_err = None
    for attempt in range(retries):
        try:
            resp = genai_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            return (getattr(resp, "text", None) or "").strip()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_retryable = any(
                k in err_str
                for k in ("429", "500", "502", "503", "504", "timeout", "timed out", "connection", "resource exhausted")
            )
            if attempt < retries - 1 and is_retryable:
                wait_sec = 2 * (attempt + 1) + (2 if "429" in err_str else 0)
                logger.warning(f"Gemini retry {attempt + 1}/{retries} after {wait_sec}s: {str(e)[:150]}")
                time.sleep(wait_sec)
                continue
            raise last_err
    raise last_err


def build_prompt(st, page_num, extra_note="", is_multi_slice=False):
    source_lang = st.get("mode", "english")
    source_instruction = (
        "KOREAN RAW -> IRANIAN PERSIAN (FARSI). Translate directly from original Korean manhwa panels into Persian."
        if source_lang == "korean"
        else "ENGLISH -> IRANIAN PERSIAN (FARSI). Translate directly from English manhwa panels into Persian."
    )

    recent_ctx = st.get("recent_context", [])
    context_block = ""
    if recent_ctx:
        context_block = (
            "\nPREVIOUS PAGE DIALOGUE CONTEXT (for pronoun & scene continuity only — DO NOT re-translate):\n"
            + "\n---\n".join(recent_ctx[-2:])
        )

    slice_note = (
        f"\nNOTE: Attached images are consecutive top-to-bottom vertical slices (with slight overlap) of THIS SAME SINGLE PAGE. Read seamlessly top-to-bottom, avoid repeating overlap text, and output ONE block starting with `تصویر {page_num}`."
        if is_multi_slice
        else ""
    )

    prompt = f"""ULTIMATE PROFESSIONAL MANHWA TRANSLATION ENGINE (Mobin Studio Standard)

1. YOUR PROFESSIONAL ROLE
You are an elite professional manhwa translator, OCR analyst, dialogue editor, and typesetting assistant for Mobin Studio.
Direction: {source_instruction}

2. TRANSLATION QUALITY & STYLE
- Use natural, fluent, conversational Iranian Persian (عامیانه تهرانی روان).
- Avoid robotic, literal, or stiff translations. Keep sentences concise enough to fit inside speech bubbles.
- Preserve exact meaning, character personality, slang, idioms, and emotional tone.
- MOANS, SIGHS & VOCAL SOUNDS: Convert moans, sighs, gasps, groans, and panting (e.g. "haa..", "ah..", "uh..", "mmh..", "ngh..") into natural Persian equivalents (هاااه...، آااه...، اوووم...، ن‌ن‌نه...، هاننگ...، پوف...). NEVER leave them in Latin letters.
- Localize environmental signs and handwritten text inside art to natural Persian.

3. ADULT CONTENT POLICY (18+)
This work includes mature and fictional adult-rated manhwa (18+). Act purely as a translator:
- Do NOT refuse, censor, sanitize, or soften explicit dialogue, profanity, insults, or intimate scenes.
- Translate adult terminology and profanity accurately with equivalent Persian intensity.

4. IMAGE ANALYSIS & OCR
- Inspect panels in natural top-to-bottom reading order.
- Keep each bubble/box separate. Never merge dialogue from different bubbles.
- Omit tiny decorative squiggles or illegible background noise so the script stays clean.
- ONLY if an actual speech bubble or important text is genuinely obscured/illegible, put `[ناخوانا]` (never use English placeholder tags).{slice_note}

5. PROFESSIONAL 21-FONT CLASSIFICATION SYSTEM
Put the exact Persian font name in parentheses next to every element label:
1. Regular dialogue: کودک
2. Rectangular narration/explanation boxes: الهام
3. Thoughts inside square boxes: یکان
4. Thoughts inside manhwa/manhua illustrations: نگار / لاله‌زار
5. Thought bubbles: مروارید
6. Cute or decorative bubbles: رم الله
7. Manhwa sun-shaped bubbles: مهر
8. Manhua/manga sun-shaped bubbles: کودک با استایل
9. Old-style sun-shaped bubbles: حیات
10. System messages/boxes: فانوس / اصفهان / فرناز
11. Text written inside illustrations & whispering: دست نویس
12. Scared, trembling, crying, or shaking dialogue: موج
13. Soft shouting: کروش
14. Loud shouting/angry dialogue in manhwa: افسانه
15. Loud shouting/angry dialogue in manga/manhua: عارفی
16. Dark dialogue bubbles: زنگار
17. Screaming and monster sounds: کردی
18. Radio, TV, phone-call dialogue: اکبر / آسمان / مثلث
19. Letters, documents, scrolls: آندالوس / فورات
20. Thoughts inside manga illustrations: سپهبد
21. Cloud-shaped dialogue bubbles: جودی

6. MANDATORY EXACT MOBIN STUDIO FORMAT
Start with `تصویر {page_num}`, followed by one blank line, and strictly one single-line entry per element separated by one blank line:

تصویر {page_num}

بالن اول (کودک): وای گه... خیلی خیسه!!

بالن دوم (کودک): و گرمه!

افکت صوتی اول (موج): هاننگ...

بالن افکار اول (مروارید): من تنها کسی‌ام که تازه فهمیده این چقدر اعصاب‌خردکنه...!

نوشته داخل تصویر اول (دست نویس): قصر سرمایه‌دار

افکت صوتی دوم (افسانه): کیاا!!

STRICT RULES:
- Translated text MUST be on the EXACT SAME LINE directly after `: `.
- One blank line between every line.
- If a page has no readable text, output ONLY `تصویر {page_num}`.
- Output ONLY the structured script lines (no intro, no notes, no markdown blocks).{context_block}"""

    if extra_note:
        prompt += f"\n\nنکته کاربر درباره این صفحه: {extra_note}"
    return prompt


def update_context(st, translated_text):
    lines = [
        ln.strip()
        for ln in translated_text.splitlines()
        if ln.strip() and not ln.strip().startswith("تصویر ")
    ]
    if not lines:
        return
    ctx = st.setdefault("recent_context", [])
    ctx.append(" | ".join(lines[:6])[:320])
    if len(ctx) > 2:
        del ctx[:-2]


# ================= BOT SETUP =================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)


@bot.message_handler(commands=["start"])
def handle_start(message):
    st = get_user_state(message.chat.id)
    lang_disp = "کره‌ای (خام) → فارسی" if st["mode"] == "korean" else "انگلیسی → فارسی"
    acc_disp = "دقت بالا (Flash)" if st.get("model_name") == ACCURATE_MODEL else "سریع (Flash Lite)"
    text = (
        f"👑 <b>مترجم مانهوا استودیو مبین</b>\n\n"
        f"📊 <b>صفحه بعدی:</b> تصویر {st['page']}\n"
        f"🌐 <b>زبان مبدأ:</b> {lang_disp}\n"
        f"🧠 <b>حالت مدل:</b> {acc_disp}\n\n"
        f"📥 عکس، آلبوم عکس یا فایل <code>.zip</code> / <code>.cbz</code> چپتر رو بفرست تا ترجمه بشه!"
    )
    send_html(message.chat.id, text, reply_markup=get_main_keyboard(st))


@bot.message_handler(commands=["new_chapter", "new"])
def cmd_new_chapter(message):
    st = get_user_state(message.chat.id)
    st["page"] = 1
    st["recent_context"] = []
    save_state()
    reply_html(message, "🔄 شمارنده ریست شد! صفحه بعدی <b>تصویر 1</b> خواهد بود.", reply_markup=get_main_keyboard(st))


@bot.message_handler(commands=["page"])
def cmd_page(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        reply_html(message, "مثال: <code>/page 12</code>")
        return
    st = get_user_state(message.chat.id)
    st["page"] = max(1, int(parts[1].strip()))
    save_state()
    reply_html(message, f"📌 صفحه بعدی روی <b>تصویر {st['page']}</b> تنظیم شد.")


@bot.message_handler(commands=["mode"])
def cmd_mode(message):
    st = get_user_state(message.chat.id)
    st["mode"] = "korean" if st["mode"] == "english" else "english"
    save_state()
    name = "کره‌ای (خام) به فارسی" if st["mode"] == "korean" else "انگلیسی به فارسی"
    reply_html(message, f"🌐 زبان مبدأ تغییر کرد به: <b>{name}</b>", reply_markup=get_main_keyboard(st))


@bot.message_handler(commands=["model"])
def cmd_toggle_model(message):
    st = get_user_state(message.chat.id)
    st["model_name"] = ACCURATE_MODEL if st.get("model_name") != ACCURATE_MODEL else FAST_MODEL
    save_state()
    label = "🎯 دقت بالا (gemini-3.5-flash)" if st["model_name"] == ACCURATE_MODEL else "⚡️ سریع و اقتصادی (gemini-3.5-flash-lite)"
    reply_html(message, f"🧠 مدل مترجم تغییر کرد به: <b>{label}</b>", reply_markup=get_main_keyboard(st))


@bot.message_handler(commands=["help"])
def cmd_help(message):
    help_text = (
        "📖 <b>راهنمای سریع مترجم:</b>\n\n"
        "• <b>عکس تکی یا چندتایی:</b> مستقیم بفرست تا با فرمت استودیو مبین ترجمه بشه (صفحات خیلی بلند هم خودکار برش می‌خورن).\n"
        "• <b>فایل ZIP / CBZ:</b> تا سقف ۲۰ مگابایت بفرست تا کل چپتر توی یک فایل <code>.txt</code> تحویل داده بشه.\n"
        "• <b>متن تکی:</b> دیالوگ انگلیسی/کره‌ای بفرستی مستقیم ترجمه می‌کنه.\n"
        "• <code>/page 15</code> — پرش به شماره صفحه دلخواه\n"
        "• <code>/new_chapter</code> — ریست به تصویر ۱"
    )
    reply_html(message, help_text)


# ================= KEYBOARD BUTTONS =================

@bot.message_handler(func=lambda m: m.text == "🔄 شروع چپتر جدید (صفحه ۱)")
def btn_new_chapter(message):
    cmd_new_chapter(message)


@bot.message_handler(func=lambda m: bool(m.text) and (m.text.startswith("🌐 مبدأ:") or m.text == "🌐 تغییر زبان مبدأ"))
def btn_change_mode(message):
    cmd_mode(message)


@bot.message_handler(func=lambda m: bool(m.text) and (m.text.startswith("⚡️ دقت:") or m.text.startswith("🎯 دقت:")))
def btn_toggle_model(message):
    cmd_toggle_model(message)


@bot.message_handler(func=lambda m: m.text == "❓ راهنما")
def btn_help(message):
    cmd_help(message)


# ================= IMAGE TRANSLATION HANDLERS =================

def _translate_single_image(message, file_id, caption=""):
    chat_id = message.chat.id
    lock = get_chat_lock(chat_id)
    with lock:
        st = get_user_state(chat_id)
        cur_page = st["page"]
        st["page"] = cur_page + 1
        msg_wait = reply_html(message, f"⏳ در حال ترجمه <b>تصویر {cur_page}</b>...")
        try:
            bot.send_chat_action(chat_id, "typing")
            file_info = bot.get_file(file_id)
            file_bytes = bot.download_file(file_info.file_path)
            parts = prepare_image_parts(file_bytes)
            prompt = build_prompt(st, cur_page, extra_note=caption, is_multi_slice=(len(parts) > 1))
            model_to_use = st.get("model_name", FAST_MODEL)

            raw_out = generate_with_retry(model_to_use, [prompt, *parts])
            fixed = fix_persian_typography(raw_out) if raw_out else f"تصویر {cur_page}"
            update_context(st, fixed)
            save_state()

            try:
                bot.delete_message(chat_id, msg_wait.message_id)
            except Exception:
                pass
            split_and_send(chat_id, fixed, reply_to_message_id=message.message_id)
        except Exception as e:
            save_state()
            try:
                bot.delete_message(chat_id, msg_wait.message_id)
            except Exception:
                pass
            reply_html(message, f"❌ خطا در پردازش تصویر {cur_page}:\n<code>{esc(e)}</code>")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    _translate_single_image(message, message.photo[-1].file_id, message.caption or "")


@bot.message_handler(content_types=["document"])
def handle_document(message):
    doc = message.document
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    fsize = doc.file_size or 0

    if fsize > TELEGRAM_BOT_MAX_BYTES:
        mb = fsize / (1024 * 1024)
        reply_html(
            message,
            f"⚠️ حجم فایل <b>{esc(doc.file_name)}</b> برابر <b>{mb:.1f} MB</b> است (سقف تلگرام برای ربات ۲۰ مگابایت است).\n"
            f"فایل را به ۲ زیپ زیر ۲۰ مگ تقسیم کن یا عکس‌ها را مستقیماً بفرست.",
        )
        return

    if fname.endswith((".zip", ".cbz")) or "zip" in mime:
        handle_zip_chapter(message)
        return

    if mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp")):
        _translate_single_image(message, doc.file_id, message.caption or "")
        return

    reply_html(message, "⚠️ لطفاً عکس مانهوا یا فایل <code>.zip</code> / <code>.cbz</code> چپتر را بفرست.")


def handle_zip_chapter(message):
    chat_id = message.chat.id
    doc = message.document
    lock = get_chat_lock(chat_id)

    with lock:
        st = get_user_state(chat_id)
        model_to_use = st.get("model_name", FAST_MODEL)
        msg_prog = reply_html(message, "📦 فایل آرشیو دریافت شد. در حال استخراج صفحات...")
        try:
            file_info = bot.get_file(doc.file_id)
            file_bytes = bot.download_file(file_info.file_path)

            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                all_files = [
                    f for f in z.namelist() if not f.startswith("__MACOSX") and not f.endswith("/")
                ]
                img_files = [
                    f for f in all_files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                ]
                if not img_files:
                    bot.edit_message_text(
                        "❌ هیچ عکس معتبری (JPG/PNG/WEBP) در این فایل پیدا نشد.",
                        chat_id,
                        msg_prog.message_id,
                    )
                    return

                img_files.sort(key=natural_sort_key)
                total = len(img_files)
                try:
                    bot.edit_message_text(
                        f"📖 <b>{total} صفحه</b> شناسایی شد.\n⏳ شروع ترجمه خط به خط...",
                        chat_id,
                        msg_prog.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                chapter_results = []
                st["recent_context"] = []
                for idx, img_name in enumerate(img_files, start=1):
                    if idx == 1 or idx % 2 == 0 or idx == total:
                        try:
                            bot.send_chat_action(chat_id, "typing")
                            bot.edit_message_text(
                                f"⏳ در حال ترجمه صفحه {idx} از {total}...\n<code>{esc(os.path.basename(img_name))}</code>",
                                chat_id,
                                msg_prog.message_id,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                    try:
                        raw_data = z.read(img_name)
                        parts = prepare_image_parts(raw_data)
                        prompt = build_prompt(st, idx, "", is_multi_slice=(len(parts) > 1))
                        res_text = generate_with_retry(model_to_use, [prompt, *parts])
                        res_text = fix_persian_typography(res_text) if res_text else f"تصویر {idx}"
                        update_context(st, res_text)
                        chapter_results.append(res_text)
                        time.sleep(0.8)
                    except Exception as ex:
                        chapter_results.append(f"تصویر {idx}\n\n[خطا در صفحه {idx}: {str(ex)}]")

                save_state()
                try:
                    bot.edit_message_text(
                        f"✅ ترجمه تمام <b>{total} صفحه</b> با موفقیت کامل شد!",
                        chat_id,
                        msg_prog.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                full_script = "\n\n".join(chapter_results)
                zip_base = os.path.splitext(doc.file_name or "chapter")[0]
                send_txt_file(
                    chat_id,
                    full_script,
                    filename=f"{zip_base}_MobinStudio.txt",
                    caption=f"📄 فایل کامل چپتر مخصوص Mobin Studio ({total} صفحه)",
                )
        except Exception as e:
            reply_html(message, f"❌ خطا در پردازش فایل آرشیو:\n<code>{esc(e)}</code>")


# ================= PLAIN TEXT TRANSLATION =================

@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()
    if not text:
        return
    st = get_user_state(message.chat.id)
    try:
        bot.send_chat_action(message.chat.id, "typing")
        prompt = build_prompt(st, st["page"]) + f"\n\nمتن زیر را ترجمه کن:\n{text}"
        res = generate_with_retry(st.get("model_name", FAST_MODEL), prompt)
        if res:
            split_and_send(
                message.chat.id,
                fix_persian_typography(res),
                reply_to_message_id=message.message_id,
            )
        else:
            reply_html(message, "⚠️ پاسخی تولید نشد.")
    except Exception as e:
        reply_html(message, f"❌ خطا:\n<code>{esc(e)}</code>")


# ================= CLOUD HEALTH CHECK SERVER =================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        data = {
            "status": "online",
            "bot": "@RmftMobinBot",
            "service": "MobinStudio Manhwa Translator",
            "default_model": DEFAULT_CONFIG_MODEL,
            "uptime_sec": int(time.time() - BOT_START_TIME),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def start_health_check_server(host, port):
    try:
        if ":" in host:
            class V6Server(HTTPServer):
                address_family = socket.AF_INET6

            server = V6Server((host, port), HealthCheckHandler)
        else:
            server = HTTPServer((host, port), HealthCheckHandler)
        logger.info(f"🌐 Cloud Health Check server listening on [{host}]:{port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")


if __name__ == "__main__":
    cloud_port = os.environ.get("PORT")
    cloud_host = os.environ.get("IP", "::" if cloud_port else "0.0.0.0")
    if cloud_port:
        try:
            p = int(cloud_port)
            threading.Thread(
                target=start_health_check_server, args=(cloud_host, p), daemon=True
            ).start()
        except Exception as e:
            logger.error(f"Could not parse PORT: {e}")

    logger.info("=" * 60)
    logger.info("🚀 ربات مترجم استودیو مبین فعال شد...")
    logger.info("=" * 60)
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(3)

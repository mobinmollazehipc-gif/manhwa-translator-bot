import html
import io
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image
import requests
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
    # Ensure print/telebot errors never crash under headless pythonw.exe
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
MAX_URL_ZIP_BYTES = 80 * 1024 * 1024

MODELS = {
    "gemini-3.5-flash-lite": "سریع و اقتصادی (Flash Lite)",
    "gemini-3.5-flash": "دقت و کیفیت بالاتر (Flash)",
    "gemini-2.5-pro": "حرفه‌ای برای کره‌ای سخت (Pro)",
}
DEFAULT_MODEL = "gemini-3.5-flash-lite"

TONE_LABELS = {
    "default": "عادی (تهرانی روان)",
    "short": "کوتاه و جمع‌وجور (مناسب حباب کوچک)",
    "street": "لاتی و خیابونی",
    "romantic": "رمانتیک و احساسی",
    "dark": "دارک و خشن",
    "funny": "طنز و کنایه‌دار",
}

TONE_INSTRUCTIONS = {
    "default": "محاوره‌ای تهرانی روان، طبیعی و زنده متناسب با فضای صحنه.",
    "short": "خیلی کوتاه، خلاصه و موجز تا راحت توی بالن‌های کوچک جا بشه، بدون حذف مفهوم اصلی.",
    "street": "لحن تند، لاتی، خیابونی و بی‌پرده با اصطلاحات روزمره محاوره‌ای.",
    "romantic": "لحن احساسی، لطیف، عاشقانه و صمیمی.",
    "dark": "لحن سرد، خشن، تاریک و تهدیدآمیز.",
    "funny": "لحن طنزآمیز، شوخ و کنایه‌دار.",
}


def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("MODEL_NAME", "").strip()
    env_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
    allowed_ids = []
    owner_pin = os.environ.get("OWNER_PIN", "").strip()

    if env_allowed:
        for part in env_allowed.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                allowed_ids.append(int(part))

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not token:
                token = str(cfg.get("telegram_bot_token", "")).strip()
            if not gemini_key:
                gemini_key = str(cfg.get("gemini_api_key", "")).strip()
            if not model:
                model = str(cfg.get("model_name", DEFAULT_MODEL)).strip()
            if not allowed_ids and isinstance(cfg.get("allowed_user_ids"), list):
                for uid in cfg["allowed_user_ids"]:
                    if str(uid).lstrip("-").isdigit():
                        allowed_ids.append(int(uid))
            if not owner_pin:
                owner_pin = str(cfg.get("owner_pin", "")).strip()
        except Exception as e:
            logger.warning(f"Warning reading config.json: {e}")

    if not model:
        model = DEFAULT_MODEL
    if not owner_pin and token:
        owner_pin = token[-6:]

    return token, gemini_key, model, allowed_ids, owner_pin


BOT_TOKEN, GEMINI_KEY, DEFAULT_CONFIG_MODEL, CONFIG_ALLOWED_IDS, OWNER_PIN = load_config()

if not BOT_TOKEN or not GEMINI_KEY:
    logger.error("TELEGRAM_BOT_TOKEN or GEMINI_API_KEY is missing!")
    sys.exit(1)

if DEFAULT_CONFIG_MODEL not in MODELS:
    MODELS[DEFAULT_CONFIG_MODEL] = DEFAULT_CONFIG_MODEL

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

ASSISTANT_SYSTEM_PROMPT = """تو «دستیار استودیو مبین» هستی؛ یک هوش مصنوعی باهوش، صمیمی، دقیق و متخصص ترجمه، بازنویسی و ادیت دیالوگ مانهوا، مانگا و کمیک.
مخاطب تو «مبین» (مترجم و تایپیست حرفه‌ای مانهوا و سازنده Mobin Studio) است.

قابلیت‌ها و قواعد اصلی تو:
1. تسلط کامل به اصطلاحات عامیانه (Slang & Idioms) انگلیسی، کره‌ای و فارسی تهرانی روزمره.
2. بازنویسی دیالوگ در لحن‌های مختلف (تهرانی روان، لاتی/خیابونی، دارک/تهدیدآمیز، طنز/کنایه‌دار، رمانتیک، تاریخی/سلطنتی) و کوتاه‌کردن متن برای حباب‌های کوچک.
3. آشنایی کامل با القاب و فرهنگ کره‌ای (هیونگ، نونا، اوپا، آجوما، سونبه، په‌ها، ماکنه، چوبول و...) و روش درست بومی‌سازی آن‌ها.
4. آشنایی کامل با سیستم ۲۱ فونتی استودیو مبین:
   کودک، الهام، یکان، نگار/لاله‌زار، مروارید، رم الله، مهر، کودک با استایل، حیات، فانوس/اصفهان/فرناز، دست نویس، موج، کروش، افسانه، عارفی، زنگار، کردی، اکبر/آسمان/مثلث، آندالوس/فورات، سپهبد، جودی.
5. آثار بزرگسال (18+): راحت، دقیق و کاملاً بدون سانسور در ترجمه فحش‌ها، مکالمات صمیمی و عبارات بزرگسالانه عمل کن.
6. پاسخگویی شفاف، سریع و کاربردی بدون پرگویی اضافی."""


# ================= PERSISTENT STATE & CACHES =================

STATE_LOCK = threading.Lock()
CHAT_LOCKS = {}
CHAT_LOCKS_GUARD = threading.Lock()
PAGE_CACHE = {}  # chat_id -> {cache_key: {...}}
CACHE_ORDER = {}  # chat_id -> [cache_key, ...]
MAX_CACHED_ITEMS_PER_CHAT = 8
MAX_CACHED_BYTES_TOTAL = 18 * 1024 * 1024
BOT_START_TIME = time.time()


def get_chat_lock(chat_id):
    with CHAT_LOCKS_GUARD:
        if chat_id not in CHAT_LOCKS:
            CHAT_LOCKS[chat_id] = threading.Lock()
        return CHAT_LOCKS[chat_id]


def _default_project():
    return {"page": 1, "glossary": {}, "recent_context": []}


def _default_chat_state():
    return {
        "mode": "english",
        "app_mode": "manhwa",
        "model_name": DEFAULT_CONFIG_MODEL,
        "tone_style": "default",
        "numbering": "ordinal",
        "active_project": "عمومی",
        "projects": {"عمومی": _default_project()},
        "batch_active": False,
        "batch_pages": [],
        "assistant_history": [],
    }


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("owner_ids", [])
                data.setdefault("chats", {})
                return data
        except Exception as e:
            logger.warning(f"Failed loading bot_state.json: {e}")
    return {"owner_ids": [], "chats": {}}


APP_STATE = _load_state()


def save_state():
    with STATE_LOCK:
        tmp_path = STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(APP_STATE, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATE_PATH)
        except Exception as e:
            logger.warning(f"Failed saving state: {e}")


def get_user_state(chat_id):
    key = str(chat_id)
    with STATE_LOCK:
        if key not in APP_STATE["chats"]:
            APP_STATE["chats"][key] = _default_chat_state()
        st = APP_STATE["chats"][key]
        defaults = _default_chat_state()
        for k, v in defaults.items():
            st.setdefault(k, v)
        proj_name = st.get("active_project") or "عمومی"
        st.setdefault("projects", {})
        if proj_name not in st["projects"]:
            st["projects"][proj_name] = _default_project()
        return st


def get_active_project(st):
    name = st.get("active_project") or "عمومی"
    st.setdefault("projects", {})
    if name not in st["projects"]:
        st["projects"][name] = _default_project()
    proj = st["projects"][name]
    proj.setdefault("page", 1)
    proj.setdefault("glossary", {})
    proj.setdefault("recent_context", [])
    return name, proj


def get_allowed_ids():
    ids = set(CONFIG_ALLOWED_IDS)
    for uid in APP_STATE.get("owner_ids", []):
        if str(uid).lstrip("-").isdigit():
            ids.add(int(uid))
    return ids


def is_authorized(message_or_call):
    allowed = get_allowed_ids()
    if not allowed:
        return True
    uid = message_or_call.from_user.id if message_or_call.from_user else None
    cid = (
        message_or_call.chat.id
        if hasattr(message_or_call, "chat")
        else message_or_call.message.chat.id
    )
    return (uid in allowed) or (cid in allowed)


def cache_page_result(chat_id, payload):
    """Stores translated page data in memory for instant tone rewrites / re-translate."""
    cid = str(chat_id)
    key = f"{int(time.time() * 1000) % 10_000_000}_{payload.get('page_num', 0)}"
    with STATE_LOCK:
        bucket = PAGE_CACHE.setdefault(cid, {})
        order = CACHE_ORDER.setdefault(cid, [])
        bucket[key] = payload
        order.append(key)
        while len(order) > MAX_CACHED_ITEMS_PER_CHAT:
            old_key = order.pop(0)
            bucket.pop(old_key, None)

        # Also enforce total memory budget for cached images across all chats
        total_bytes = sum(
            len(v.get("img_bytes") or b"")
            for bmap in PAGE_CACHE.values()
            for v in bmap.values()
        )
        while total_bytes > MAX_CACHED_BYTES_TOTAL and order:
            old_key = order.pop(0)
            removed = bucket.pop(old_key, None)
            if removed:
                total_bytes -= len(removed.get("img_bytes") or b"")
    return key


# ================= HELPER UTILITIES =================

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
    return bot.reply_to(
        message,
        html_text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


ZWNJ = "\u200c"


def fix_persian_typography(text):
    if not text:
        return text

    # Strip accidental markdown code fences
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())

    raw_lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    fixed_lines = []
    for l in raw_lines:
        if re.match(r"^\s*تصویر\s+[0-9۰-۹]+", l):
            fixed_lines.append(re.sub(r"\s+", " ", l.strip()))
            continue

        persian_letters = r"آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"

        # Convert Latin comma/question-mark near Persian letters first (preserve 1,000 and pure English)
        l = re.sub(rf"(?<=[{persian_letters}])\s*,", "،", l)
        l = re.sub(rf",(?=\s*[{persian_letters}])", "،", l)
        l = re.sub(rf"(?<=[{persian_letters}])\s*\?", "؟", l)

        # Proper ZWNJ on prefixes and suffixes without harming standalone words like "ای کاش"
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

        # Join "ای" ONLY after nouns ending in silent ه (never after که/به/نه/سه/چه/ده/یه)
        def _join_silent_he_ey(m):
            word = m.group(1)
            if word in ("که", "به", "نه", "چه", "سه", "ده", "مه", "یه"):
                return m.group(0)
            return word + ZWNJ + m.group(2)

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


def format_assistant_html(text):
    safe = esc(text)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)
    return safe


def split_and_send(chat_id, text, reply_to_message_id=None, reply_markup=None, as_html=False):
    chunks = split_text_cleanly(text, max_len=3600 if as_html else 3900)
    last_msg = None
    for idx, chunk in enumerate(chunks):
        kb = reply_markup if idx == len(chunks) - 1 else None
        rto = reply_to_message_id if idx == 0 else None
        if as_html:
            try:
                last_msg = send_html(
                    chat_id, format_assistant_html(chunk), reply_markup=kb, reply_to_message_id=rto
                )
                continue
            except Exception:
                pass
        last_msg = bot.send_message(
            chat_id, chunk, reply_to_message_id=rto, reply_markup=kb
        )
    return last_msg


def send_txt_file(chat_id, content, filename="chapter_translation.txt", caption=None):
    content = fix_persian_typography(content)
    bio = io.BytesIO((content + "\n").encode("utf-8"))
    bio.name = filename
    bot.send_document(chat_id, bio, caption=caption)


def compress_to_jpeg_bytes(img, quality=90):
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def prepare_image_parts(raw_bytes):
    """Slices tall vertical webtoon strips into crisp overlapping tiles so tiny text stays legible."""
    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    # Resize excessively wide images gently while keeping text crisp
    if w > 2200:
        new_h = int(h * (2200 / w))
        img = img.resize((2200, new_h), Image.Resampling.LANCZOS)
        w, h = img.size

    tile_h = 3400
    overlap = 260
    max_tiles = 6

    if h <= 4000:
        jpg_bytes = compress_to_jpeg_bytes(img, quality=92)
        return [genai_types.Part.from_bytes(data=jpg_bytes, mime_type="image/jpeg")], jpg_bytes

    parts = []
    step = tile_h - overlap
    y = 0
    tile_idx = 0
    while y < h and tile_idx < max_tiles:
        y_end = min(h, y + tile_h)
        if tile_idx == max_tiles - 1:
            y_end = h
        crop = img.crop((0, y, w, y_end))
        c_bytes = compress_to_jpeg_bytes(crop, quality=90)
        parts.append(genai_types.Part.from_bytes(data=c_bytes, mime_type="image/jpeg"))
        if y_end >= h:
            break
        y += step
        tile_idx += 1

    cache_preview = raw_bytes if len(raw_bytes) <= 4 * 1024 * 1024 else None
    return parts, cache_preview


# ================= KEYBOARDS =================

def get_manhwa_keyboard(st):
    mode_txt = "🌐 مبدأ: کره‌ای" if st.get("mode") == "korean" else "🌐 مبدأ: انگلیسی"
    batch_txt = "📚 چپتر دسته‌ای (روشن ✅)" if st.get("batch_active") else "📚 چپتر دسته‌ای (خاموش)"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("💬 دستیار هوشمند"), types.KeyboardButton(batch_txt))
    kb.row(types.KeyboardButton("📥 تحویل TXT دسته‌ای"), types.KeyboardButton("🔄 شروع چپتر جدید (صفحه ۱)"))
    kb.row(
        types.KeyboardButton(mode_txt),
        types.KeyboardButton("⚙️ مدل و تنظیمات"),
        types.KeyboardButton("📖 واژه‌نامه و راهنما"),
    )
    return kb


def get_assistant_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🎨 بازگشت به مترجم مانهوا"), types.KeyboardButton("🗑 پاک کردن حافظه چت"))
    kb.row(types.KeyboardButton("🎭 راهنمای بازنویسی لحن"), types.KeyboardButton("📖 واژه‌نامه و راهنما"))
    return kb


def get_translation_inline_kb(cache_key):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("✂️ کوتاه‌تر", callback_data=f"tone:short:{cache_key}"),
        types.InlineKeyboardButton("🔥 خیابونی", callback_data=f"tone:street:{cache_key}"),
        types.InlineKeyboardButton("🎭 رمانتیک", callback_data=f"tone:romantic:{cache_key}"),
    )
    kb.add(
        types.InlineKeyboardButton("😈 دارک", callback_data=f"tone:dark:{cache_key}"),
        types.InlineKeyboardButton("🔄 دقت بالا", callback_data=f"retrans:{cache_key}"),
        types.InlineKeyboardButton("📄 دریافت TXT", callback_data=f"totxt:{cache_key}"),
    )
    return kb


def get_settings_inline_kb(st):
    cur_model = st.get("model_name", DEFAULT_CONFIG_MODEL)
    cur_tone = st.get("tone_style", "default")
    cur_num = st.get("numbering", "ordinal")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for m_id, m_label in MODELS.items():
        mark = "✅ " if cur_model == m_id else ""
        kb.add(types.InlineKeyboardButton(f"{mark}مدل: {m_label}", callback_data=f"setmodel:{m_id}"))

    tone_btns = []
    for t_id, t_name in [
        ("default", "عادی"),
        ("short", "کوتاه"),
        ("street", "لاتی"),
        ("romantic", "رمانتیک"),
        ("dark", "دارک"),
        ("funny", "طنز"),
    ]:
        mark = "✅ " if cur_tone == t_id else ""
        tone_btns.append(types.InlineKeyboardButton(f"{mark}لحن {t_name}", callback_data=f"settone:{t_id}"))
    kb.row(*tone_btns[:3])
    kb.row(*tone_btns[3:])

    num_label = "ترتیبی (بالن اول ✅)" if cur_num == "ordinal" else "عددی (بالن 1 ✅)"
    kb.add(types.InlineKeyboardButton(f"شماره‌گذاری: {num_label}", callback_data="togglenum"))
    return kb


# ================= GEMINI CALLS WITH RETRY =================

def generate_with_retry(model_name, contents, system_instruction=None, temperature=0.35, retries=3):
    cfg_kwargs = {
        "safety_settings": SAFETY_SETTINGS,
        "temperature": temperature,
    }
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction

    config = genai_types.GenerateContentConfig(**cfg_kwargs)
    last_err = None
    for attempt in range(retries):
        try:
            resp = genai_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            text = getattr(resp, "text", None) or ""
            return text.strip()
        except Exception as e:
            last_err = e
            err_str = str(e)
            is_retryable = any(
                code in err_str.lower()
                for code in ("429", "500", "502", "503", "504", "timeout", "timed out", "temporarily", "connection", "resource exhausted")
            )
            if attempt < retries - 1 and is_retryable:
                wait_sec = 2 * (attempt + 1) + (2 if "429" in err_str else 0)
                logger.warning(f"Gemini retry {attempt + 1}/{retries} after {wait_sec}s: {err_str[:160]}")
                time.sleep(wait_sec)
                continue
            raise last_err
    raise last_err


# ================= TRANSLATION PROMPTS =================

def build_prompt(st, page_num, extra_note="", tone_override=None, is_multi_slice=False):
    source_lang = st.get("mode", "english")
    tone_key = tone_override or st.get("tone_style", "default")
    tone_desc = TONE_INSTRUCTIONS.get(tone_key, TONE_INSTRUCTIONS["default"])
    numbering = st.get("numbering", "ordinal")
    proj_name, proj = get_active_project(st)

    source_instruction = (
        "ENGLISH -> IRANIAN PERSIAN (FARSI). The manhwa is translated into English. Translate directly from English to Persian."
    )
    if source_lang == "korean":
        source_instruction = (
            "KOREAN RAW -> IRANIAN PERSIAN (FARSI). Translate directly from original Korean manhwa panels into Persian."
        )

    numbering_rule = (
        "Use Persian ordinal numbering per element type inside the page (e.g. بالن اول، بالن دوم، کادر مستطیلی اول، افکت صوتی اول)."
        if numbering == "ordinal"
        else "Use numeric numbering per element type inside the page (e.g. بالن 1، بالن 2، کادر مستطیلی 1، افکت صوتی 1)."
    )

    sample_block = (
        f"تصویر {page_num}\n\n"
        f"بالن اول (کودک): وای گه... خیلی خیسه!!\n\n"
        f"بالن دوم (کودک): و گرمه!\n\n"
        f"افکت صوتی اول (موج): هاننگ...\n\n"
        f"بالن افکار اول (مروارید): من تنها کسی‌ام که تازه فهمیده این چقدر اعصاب‌خردکنه...!\n\n"
        f"نوشته داخل تصویر اول (دست نویس): قصر سرمایه‌دار\n\n"
        f"افکت صوتی دوم (افسانه): کیاا!!"
        if numbering == "ordinal"
        else
        f"تصویر {page_num}\n\n"
        f"بالن 1 (کودک): وای گه... خیلی خیسه!!\n\n"
        f"بالن 2 (کودک): و گرمه!\n\n"
        f"افکت صوتی 1 (موج): هاننگ...\n\n"
        f"بالن افکار 1 (مروارید): من تنها کسی‌ام که تازه فهمیده این چقدر اعصاب‌خردکنه...!\n\n"
        f"نوشته داخل تصویر 1 (دست نویس): قصر سرمایه‌دار\n\n"
        f"افکت صوتی 2 (افسانه): کیاا!!"
    )

    glossary = proj.get("glossary", {})
    glossary_block = ""
    if glossary:
        lines = [f"- {k} -> {v}" for k, v in list(glossary.items())[:80]]
        glossary_block = "\nPROJECT GLOSSARY (ALWAYS obey these exact names/terms & pronouns):\n" + "\n".join(lines)

    recent_ctx = proj.get("recent_context", [])
    context_block = ""
    if recent_ctx:
        context_block = "\nPREVIOUS PAGE DIALOGUE CONTEXT (for pronoun & scene continuity only — DO NOT re-translate this):\n" + "\n---\n".join(recent_ctx[-2:])

    slice_note = (
        "\nIMPORTANT IMAGE LAYOUT NOTE: The attached images are consecutive vertical top-to-bottom slices (with slight overlap) of THIS SAME SINGLE PAGE. Read top slice to bottom slice seamlessly, deduplicate anything visible twice in the overlap zone, and output ONE single page block starting with `تصویر {0}`."
        .format(page_num)
        if is_multi_slice
        else ""
    )

    prompt = f"""ULTIMATE PROFESSIONAL MANHWA TRANSLATION ENGINE (Mobin Studio Standard)

1. ROLE & TRANSLATION DIRECTION
You are an elite professional manhwa translator, OCR analyst, dialogue editor, and typesetting script writer for Mobin Studio (Active Project: {proj_name}).
Direction: {source_instruction}

2. TRANSLATION QUALITY & TONE
- Target Style: {tone_desc}
- Avoid robotic, literal, or stiff bookish Persian. Keep dialogue concise enough to fit inside speech bubbles.
- Preserve exact meaning, character personality, slang, idioms, and emotional intensity.
- MOANS, SIGHS & VOCAL SOUNDS: Convert moans, gasps, panting, groans, and sensual sounds (e.g. "haa..", "ah..", "uh..", "mmh..", "ngh..") into natural Persian manhwa equivalents (هاااه...، آااه...، اوووم...، ن‌ن‌نه...، هاننگ...، پوف...). NEVER leave them in Latin letters.
- Translate environmental signs/handwritten text into natural Persian unless it is an explicit untranslatable proper logo.

3. ADULT CONTENT POLICY (18+)
This work includes mature/adult-rated fictional manhwa (18+). Act purely as an accurate translator:
- Do NOT refuse, censor, sanitize, or soften explicit dialogue, profanity, insults, or intimate scenes.
- Translate all adult expressions faithfully with equivalent natural Persian intensity.

4. IMAGE ANALYSIS & OCR
- Inspect panels in natural top-to-bottom reading order.
- Keep each bubble/box separate. Never merge different speech bubbles into one line.
- Ignore tiny meaningless artist squiggles or decorative strokes.
- ONLY if an actual speech bubble or key text is genuinely obscured/illegible, put `[ناخوانا]` (never use English placeholder tags).{slice_note}

5. MOBIN STUDIO 21-FONT CLASSIFICATION SYSTEM
Pick the EXACT canonical Persian font name in parentheses next to every element label:
1. Regular dialogue: کودک
2. Rectangular narration/explanation boxes: الهام
3. Thoughts inside square boxes: یکان
4. Thoughts inside manhwa/manhua illustrations: نگار / لاله‌زار
5. Thought bubbles: مروارید
6. Cute or decorative bubbles: رم الله
7. Manhwa sun-shaped bubbles: مهر
8. Manhua/manga sun-shaped bubbles: کودک با استایل
9. Old-style sun-shaped bubbles: حیات
10. System messages/boxes/HUD: فانوس / اصفهان / فرناز
11. Handwritten text inside art & whispers: دست نویس
12. Scared, trembling, crying, shaking dialogue: موج
13. Soft shouting: کروش
14. Loud shouting/angry in manhwa: افسانه
15. Loud shouting/angry in manga/manhua: عارفی
16. Dark/sinister dialogue bubbles: زنگار
17. Screaming and monster sounds: کردی
18. Radio, TV, phone call dialogue: اکبر / آسمان / مثلث
19. Letters, documents, scrolls: آندالوس / فورات
20. Thoughts inside manga illustrations: سپهبد
21. Cloud-shaped dialogue bubbles: جودی
(Note: Use one exact single font name inside parentheses for categories with options, e.g. `(فانوس)` or `(دست نویس)` or `(لاله‌زار)`.)

6. MANDATORY MOBIN STUDIO OUTPUT FORMAT
- {numbering_rule}
- First line MUST be `تصویر {page_num}`. If the page has zero text/dialogue, output ONLY `تصویر {page_num}`.
- SINGLE LINE PER ELEMENT: Translated text MUST follow `: ` on the exact same line.
- ONE BLANK LINE between every line.
- Output ONLY the script lines — NO intro, NO notes, NO markdown code blocks.

Example Output:
{sample_block}"""

    if glossary_block:
        prompt += "\n" + glossary_block
    if context_block:
        prompt += "\n" + context_block
    if extra_note:
        prompt += f"\n\nUSER SPECIAL NOTE FOR THIS PAGE: {extra_note}"

    return prompt


def update_project_context(st, translated_text):
    _, proj = get_active_project(st)
    lines = [
        ln.strip()
        for ln in translated_text.splitlines()
        if ln.strip() and not ln.strip().startswith("تصویر ")
    ]
    if not lines:
        return
    compact = " | ".join(lines[:6])[:320]
    ctx = proj.setdefault("recent_context", [])
    ctx.append(compact)
    if len(ctx) > 2:
        del ctx[:-2]


# ================= NETWORK URL SAFETY =================

def is_safe_public_url(url):
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        host = parsed.hostname
        if not host or host.lower() in ("localhost", "localhost.localdomain"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            # Domain name — resolve check
            try:
                resolved = socket.gethostbyname(host)
                ip = ipaddress.ip_address(resolved)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False
            except Exception:
                return False
        return True
    except Exception:
        return False


def download_zip_from_url(url):
    if not is_safe_public_url(url):
        raise ValueError("لینک نامعتبر یا مسدود است (فقط لینک‌های عمومی http/https مجازند).")
    resp = requests.get(url, stream=True, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    if not is_safe_public_url(resp.url):
        raise ValueError("تغییر مسیر (Redirect) به آدرس غیرمجاز مسدود شد.")
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > MAX_URL_ZIP_BYTES:
        raise ValueError(f"حجم فایل ({int(cl) / (1024*1024):.1f} MB) از سقف مجاز سرور (80 MB) بیشتر است.")

    buf = io.BytesIO()
    total = 0
    for chunk in resp.iter_content(chunk_size=128 * 1024):
        if chunk:
            total += len(chunk)
            if total > MAX_URL_ZIP_BYTES:
                raise ValueError("حجم فایل در حال دانلود از 80 مگابایت فراتر رفت و متوقف شد.")
            buf.write(chunk)
    return buf.getvalue()


# ================= CORE IMAGE / ZIP TRANSLATORS =================

def translate_image_bytes(chat_id, raw_bytes, caption="", override_page=None, model_override=None, tone_override=None):
    st = get_user_state(chat_id)
    _, proj = get_active_project(st)

    if override_page is None:
        cur_page = proj["page"]
        proj["page"] = cur_page + 1
    else:
        cur_page = override_page

    parts, cache_img = prepare_image_parts(raw_bytes)
    is_multi = len(parts) > 1
    prompt = build_prompt(
        st, cur_page, extra_note=caption, tone_override=tone_override, is_multi_slice=is_multi
    )
    model_to_use = model_override or st.get("model_name", DEFAULT_CONFIG_MODEL)

    raw_out = generate_with_retry(model_to_use, [prompt, *parts], temperature=0.32)
    fixed = fix_persian_typography(raw_out) if raw_out else f"تصویر {cur_page}"

    update_project_context(st, fixed)
    if st.get("batch_active") and override_page is None:
        st.setdefault("batch_pages", []).append({"page": cur_page, "text": fixed})
    save_state()

    cache_key = cache_page_result(
        chat_id,
        {
            "page_num": cur_page,
            "img_bytes": cache_img,
            "source_text": caption,
            "translation": fixed,
            "mode": st.get("mode", "english"),
            "caption": caption,
            "project": st.get("active_project", "عمومی"),
        },
    )
    return cur_page, fixed, cache_key, len(parts)


def process_zip_bytes(message, file_bytes, orig_filename):
    chat_id = message.chat.id
    st = get_user_state(chat_id)
    proj_name, proj = get_active_project(st)
    model_to_use = st.get("model_name", DEFAULT_CONFIG_MODEL)

    msg_prog = reply_html(
        message,
        f"📦 فایل <b>{esc(orig_filename)}</b> دریافت شد. در حال استخراج صفحات...",
    )

    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        all_files = [
            f for f in z.namelist() if not f.startswith("__MACOSX") and not f.endswith("/")
        ]
        img_files = [
            f for f in all_files if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
        if not img_files:
            bot.edit_message_text(
                "❌ هیچ عکس معتبری (JPG/PNG/WEBP) در این آرشیو پیدا نشد.",
                chat_id,
                msg_prog.message_id,
            )
            return

        img_files.sort(key=natural_sort_key)
        total = len(img_files)
        start_page = proj["page"] if st.get("batch_active") else 1

        try:
            bot.edit_message_text(
                f"📖 <b>{total} صفحه</b> شناسایی شد (پروژه: {esc(proj_name)} | مدل: {esc(model_to_use)}).\n"
                f"⏳ شروع ترجمه از <b>تصویر {start_page}</b>...",
                chat_id,
                msg_prog.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

        chapter_results = []
        failed_pages = []
        cur_page = start_page

        for idx, img_name in enumerate(img_files, start=1):
            if idx == 1 or idx % 2 == 0 or idx == total:
                try:
                    bot.send_chat_action(chat_id, "typing")
                    bot.edit_message_text(
                        f"⏳ ترجمه صفحه {idx} از {total} (تصویر {cur_page})...\n"
                        f"📄 <code>{esc(os.path.basename(img_name))}</code>",
                        chat_id,
                        msg_prog.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            try:
                raw_data = z.read(img_name)
                parts, _ = prepare_image_parts(raw_data)
                prompt = build_prompt(st, cur_page, "", is_multi_slice=(len(parts) > 1))
                res_text = generate_with_retry(model_to_use, [prompt, *parts], temperature=0.32)
                res_text = fix_persian_typography(res_text) if res_text else f"تصویر {cur_page}"
                update_project_context(st, res_text)
                chapter_results.append(res_text)
                if st.get("batch_active"):
                    st.setdefault("batch_pages", []).append({"page": cur_page, "text": res_text})
                time.sleep(0.8)
            except Exception as ex:
                failed_pages.append(cur_page)
                chapter_results.append(f"تصویر {cur_page}\n\n[خطا در صفحه {cur_page}: {str(ex)}]")
            cur_page += 1

        if st.get("batch_active"):
            proj["page"] = cur_page
        save_state()

        status_line = f"✅ ترجمه <b>{total} صفحه</b> کامل شد!"
        if failed_pages:
            status_line += f"\n⚠️ صفحات دارای خطا: {', '.join(map(str, failed_pages))}"
        try:
            bot.edit_message_text(
                status_line, chat_id, msg_prog.message_id, parse_mode="HTML"
            )
        except Exception:
            pass

        full_script = "\n\n".join(chapter_results)
        zip_base = os.path.splitext(orig_filename or "chapter")[0]
        txt_name = f"{zip_base}_MobinStudio.txt"
        caption = f"📄 فایل متنی کامل مخصوص Mobin Studio ({total} صفحه | پروژه: {proj_name})"
        if st.get("batch_active"):
            caption += "\n📚 در صف دسته‌ای هم ذخیره شد (برای فایل تجمیعی، «📥 تحویل TXT دسته‌ای» را بزن)."
        send_txt_file(chat_id, full_script, filename=txt_name, caption=caption)


# ================= TELEGRAM BOT INIT =================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)


def reject_unauthorized(obj):
    try:
        if hasattr(obj, "chat"):
            bot.reply_to(obj, "🔒 این ربات خصوصی است و فقط برای مالک آن فعال می‌باشد.")
        else:
            bot.answer_callback_query(obj.id, "🔒 دسترسی مجاز نیست.", show_alert=True)
    except Exception:
        pass


# ================= COMMAND HANDLERS =================

@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id if message.from_user else message.chat.id
    reply_html(
        message,
        f"🆔 آیدی عددی تلگرام شما: <code>{uid}</code>\n"
        f"💬 آیدی چت: <code>{message.chat.id}</code>",
    )


@bot.message_handler(commands=["lock"])
def cmd_lock(message):
    if get_allowed_ids() and not is_authorized(message):
        reject_unauthorized(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != OWNER_PIN:
        reply_html(
            message,
            "❌ کد قفل اشتباه است.\nاستفاده: <code>/lock کد_تایید</code>",
        )
        return
    uid = message.from_user.id if message.from_user else message.chat.id
    with STATE_LOCK:
        owners = APP_STATE.setdefault("owner_ids", [])
        if uid not in owners:
            owners.append(uid)
        if message.chat.id not in owners:
            owners.append(message.chat.id)
    save_state()
    reply_html(
        message,
        f"🔒 <b>ربات با موفقیت قفل شد!</b>\nاز این لحظه فقط آیدی شما (<code>{uid}</code>) به ربات دسترسی دارد.",
    )


@bot.message_handler(commands=["unlock"])
def cmd_unlock(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() != OWNER_PIN:
        reply_html(message, "❌ کد تایید اشتباه است.\nاستفاده: <code>/unlock کد_تایید</code>")
        return
    with STATE_LOCK:
        APP_STATE["owner_ids"] = []
    save_state()
    reply_html(message, "🔓 قفل دسترسی برداشته شد.")


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    lang_display = "انگلیسی → فارسی" if st["mode"] == "english" else "کره‌ای (خام) → فارسی"
    model_disp = MODELS.get(st.get("model_name", DEFAULT_CONFIG_MODEL), st.get("model_name"))
    tone_disp = TONE_LABELS.get(st.get("tone_style", "default"), "عادی")
    batch_disp = f"روشن ✅ ({len(st.get('batch_pages', []))} صفحه آماده)" if st.get("batch_active") else "خاموش"
    lock_disp = "فعال 🔒" if get_allowed_ids() else "باز 🔓 (برای قفل /lock را بزنید)"

    text = (
        f"👑 <b>سلام مبین جان! استودیو هوشمندت آماده به کاره</b>\n\n"
        f"📁 <b>پروژه فعال:</b> {esc(proj_name)}\n"
        f"📊 <b>صفحه بعدی:</b> تصویر {proj['page']}\n"
        f"🌐 <b>زبان مبدأ:</b> {lang_display}\n"
        f"🧠 <b>مدل فعال:</b> {esc(model_disp)}\n"
        f"🎭 <b>لحن پیش‌فرض:</b> {esc(tone_disp)}\n"
        f"📚 <b>چپتر دسته‌ای:</b> {esc(batch_disp)}\n"
        f"🛡 <b>امنیت:</b> {esc(lock_disp)}\n\n"
        f"✨ <b>امکانات:</b>\n"
        f"• ارسال عکس تکی یا آلبومی (صفحات خیلی بلند هم برش هوشمند می‌خورن)\n"
        f"• دکمه‌های <b>تغییر لحن، دقت بالا و خروجی TXT</b> زیر هر ترجمه\n"
        f"• ارسال <code>.zip</code> یا <code>.cbz</code> (تا ۲۰ مگابایت) یا <b>لینک مستقیم دانلود ZIP</b> تا ۸۰ مگابایت!\n"
        f"• حالت <b>چپتر دسته‌ای</b> برای دور زدن محدودیت ۲۰ مگابایت با عکس یا ZIPهای تکه‌شده\n"
        f"• پروژه و واژه‌نامه اختصاصی (<code>/project</code> و <code>/addterm</code>)\n"
        f"• پشتیبانی از ویس (Voice) در هر دو حالت\n\n"
        f"📥 عکس، فایل ZIP یا ویست رو بفرست!"
    )
    kb = get_manhwa_keyboard(st) if st["app_mode"] == "manhwa" else get_assistant_keyboard()
    send_html(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["chat"])
def cmd_switch_chat(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    st["app_mode"] = "assistant"
    save_state()
    msg = (
        "💬 <b>حالت دستیار چت فعال شد!</b>\n\n"
        "حافظه گفتگوی ما ماندگاره (حتی بعد از ری‌استارت سرور) و واژه‌نامه پروژه‌ت رو هم بلدم.\n\n"
        "🔹 متن یا <b>ویس</b> بفرست تا دیالوگ رو بازنویسی یا معادل‌یابی کنم.\n"
        "🔹 هر وقت عکس یا ZIP بفرستی فوراً ترجمه می‌شه.\n"
        "🔹 برای برگشت دکمه «🎨 بازگشت به مترجم مانهوا» رو بزن."
    )
    send_html(message.chat.id, msg, reply_markup=get_assistant_keyboard())


@bot.message_handler(commands=["manhwa"])
def cmd_switch_manhwa(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    st["app_mode"] = "manhwa"
    save_state()
    send_html(
        message.chat.id,
        "🎨 <b>حالت مترجم مانهوا فعال شد!</b>\nعکس، آلبوم، فایل ZIP/CBZ یا لینک مستقیم بفرست.",
        reply_markup=get_manhwa_keyboard(st),
    )


@bot.message_handler(commands=["clear_chat"])
def cmd_clear_chat(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    st["assistant_history"] = []
    save_state()
    reply_html(message, "🗑 <b>حافظه چت دستیار پاک شد!</b> مکالمه تازه‌ای شروع شد.")


@bot.message_handler(commands=["new_chapter"])
def cmd_new_chapter(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    proj["page"] = 1
    proj["recent_context"] = []
    save_state()
    reply_html(
        message,
        f"🔄 شمارنده پروژه <b>{esc(proj_name)}</b> ریست شد! صفحه بعدی <b>تصویر 1</b> خواهد بود.",
    )


@bot.message_handler(commands=["page"])
def cmd_set_page(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        reply_html(message, "استفاده: <code>/page 15</code>")
        return
    num = max(1, int(parts[1].strip()))
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    proj["page"] = num
    save_state()
    reply_html(message, f"📌 صفحه بعدی پروژه <b>{esc(proj_name)}</b> روی <b>تصویر {num}</b> تنظیم شد.")


@bot.message_handler(commands=["mode"])
def cmd_mode(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    new_mode = "korean" if st["mode"] == "english" else "english"
    st["mode"] = new_mode
    save_state()
    name = "کره‌ای (خام) به فارسی" if new_mode == "korean" else "انگلیسی به فارسی"
    kb = get_manhwa_keyboard(st) if st["app_mode"] == "manhwa" else get_assistant_keyboard()
    reply_html(message, f"🌐 زبان مبدأ تغییر کرد به: <b>{name}</b>", reply_markup=kb)


@bot.message_handler(commands=["model", "tone", "settings"])
def cmd_settings(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    reply_html(
        message,
        "⚙️ <b>تنظیمات مترجم استودیو مبین:</b>\nمدل هوش مصنوعی، لحن پیش‌فرض یا نوع شماره‌گذاری رو انتخاب کن:",
        reply_markup=get_settings_inline_kb(st),
    )


@bot.message_handler(commands=["project"])
def cmd_project(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        cur_name, _ = get_active_project(st)
        all_projs = [
            f"• {'<b>' + esc(k) + ' (فعال)</b>' if k == cur_name else esc(k)} — صفحه {v.get('page', 1)} | {len(v.get('glossary', {}))} واژه"
            for k, v in st.get("projects", {}).items()
        ]
        reply_html(
            message,
            "📁 <b>پروژه‌های شما:</b>\n"
            + "\n".join(all_projs)
            + "\n\nبرای تغییر یا ساخت پروژه جدید:\n<code>/project نام_مانهوا</code>",
        )
        return

    new_name = parts[1].strip()[:60]
    st["active_project"] = new_name
    _, proj = get_active_project(st)
    save_state()
    reply_html(
        message,
        f"📁 پروژه فعال تغییر کرد به: <b>{esc(new_name)}</b>\n"
        f"📊 صفحه بعدی: <b>تصویر {proj['page']}</b> | 📖 واژه‌نامه: <b>{len(proj.get('glossary', {}))}</b> مورد",
    )


@bot.message_handler(commands=["glossary"])
def cmd_glossary(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    g = proj.get("glossary", {})
    if not g:
        reply_html(
            message,
            f"📖 واژه‌نامه پروژه <b>{esc(proj_name)}</b> فعلاً خالیه.\n\n"
            "برای اضافه کردن نام شخصیت یا اصطلاح:\n"
            "<code>/addterm Kim Chul-soo = کیم چول‌سو (مرد)</code>",
        )
        return
    items = [f"• <code>{esc(k)}</code> ← <b>{esc(v)}</b>" for k, v in g.items()]
    reply_html(
        message,
        f"📖 <b>واژه‌نامه پروژه {esc(proj_name)}:</b>\n\n"
        + "\n".join(items[:60])
        + "\n\nافزودن: <code>/addterm مبدأ = فارسی</code>\nحذف: <code>/delterm مبدأ</code>",
    )


@bot.message_handler(commands=["addterm"])
def cmd_addterm(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2 or "=" not in body[1]:
        reply_html(
            message,
            "فرمت صحیح:\n<code>/addterm Han Yura = هان یورا (زن)</code>",
        )
        return
    src, dst = body[1].split("=", 1)
    src, dst = src.strip(), dst.strip()
    if not src or not dst:
        reply_html(message, "❌ هر دو طرف <code>=</code> باید پر باشند.")
        return
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    proj.setdefault("glossary", {})[src] = dst
    save_state()
    reply_html(
        message,
        f"✅ به واژه‌نامه <b>{esc(proj_name)}</b> اضافه شد:\n<code>{esc(src)}</code> ← <b>{esc(dst)}</b>",
    )


@bot.message_handler(commands=["delterm"])
def cmd_delterm(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        reply_html(message, "استفاده: <code>/delterm نام_اصطلاح</code>")
        return
    term = body[1].strip()
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    if term in proj.get("glossary", {}):
        del proj["glossary"][term]
        save_state()
        reply_html(message, f"🗑 <code>{esc(term)}</code> از واژه‌نامه <b>{esc(proj_name)}</b> حذف شد.")
    else:
        reply_html(message, f"⚠️ مورد <code>{esc(term)}</code> پیدا نشد.")


@bot.message_handler(commands=["batch"])
def cmd_toggle_batch(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    st["batch_active"] = not st.get("batch_active", False)
    save_state()
    kb = get_manhwa_keyboard(st)
    if st["batch_active"]:
        reply_html(
            message,
            "📚 <b>حالت چپتر دسته‌ای روشن شد!</b>\n\n"
            "از الان هر عکس (تکی یا آلبومی) یا ZIP کوچک بفرستی، هم اینجا جواب می‌گیری و هم توی صف ذخیره می‌شه.\n"
            "آخر کار روی <b>«📥 تحویل TXT دسته‌ای»</b> (یا <code>/export</code>) کلیک کن تا یک فایل کامل بگیری!",
            reply_markup=kb,
        )
    else:
        count = len(st.get("batch_pages", []))
        reply_html(
            message,
            f"📚 <b>حالت چپتر دسته‌ای خاموش شد.</b> ({count} صفحه هنوز توی صف آماده دریافت هست؛ با <code>/export</code> قابل دریافته.)",
            reply_markup=kb,
        )


@bot.message_handler(commands=["export"])
def cmd_export_batch(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    pages = st.get("batch_pages", [])
    if not pages:
        reply_html(
            message,
            "📭 صف دسته‌ای خالیه!\nاول «📚 چپتر دسته‌ای» رو روشن کن و صفحات رو بفرست.",
        )
        return
    pages_sorted = sorted(pages, key=lambda x: x.get("page", 0))
    full_script = "\n\n".join(p["text"] for p in pages_sorted)
    proj_name, _ = get_active_project(st)
    safe_proj = re.sub(r'[\\/:*?"<>|\s]+', "_", proj_name).strip("_") or "chapter"
    fname = f"{safe_proj}_Batch_MobinStudio.txt"
    send_txt_file(
        message.chat.id,
        full_script,
        filename=fname,
        caption=f"📄 فایل کامل چپتر دسته‌ای ({len(pages_sorted)} صفحه | پروژه: {proj_name})",
    )
    st["batch_pages"] = []
    st["batch_active"] = False
    save_state()
    send_html(
        message.chat.id,
        "✅ فایل نهایی تحویل داده شد و صف دسته‌ای ریست شد.",
        reply_markup=get_manhwa_keyboard(st),
    )


@bot.message_handler(commands=["clear_batch"])
def cmd_clear_batch(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    st["batch_pages"] = []
    save_state()
    reply_html(message, "🗑 صف چپتر دسته‌ای خالی شد.")


def _process_zip_url(message, url):
    wait_msg = reply_html(message, "🌐 در حال دانلود فایل آرشیو از لینک...")
    lock = get_chat_lock(message.chat.id)
    with lock:
        try:
            zbytes = download_zip_from_url(url)
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            fname = os.path.basename(urllib.parse.urlparse(url).path) or "chapter.zip"
            if not fname.lower().endswith((".zip", ".cbz")):
                fname += ".zip"
            process_zip_bytes(message, zbytes, fname)
        except Exception as e:
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            reply_html(message, f"❌ خطا در دانلود یا بازکردن لینک:\n<code>{esc(e)}</code>")


@bot.message_handler(commands=["zip_url"])
def cmd_zip_url(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not URL_RE.match(parts[1].strip()):
        reply_html(message, "استفاده:\n<code>/zip_url https://example.com/chapter1.zip</code>")
        return
    _process_zip_url(message, parts[1].strip())


# ================= REPLY KEYBOARD BUTTON HANDLERS =================

@bot.message_handler(func=lambda m: m.text in ("💬 دستیار هوشمند", "💬 دستیار هوشمند (چت)"))
def btn_to_assistant(message):
    cmd_switch_chat(message)


@bot.message_handler(func=lambda m: m.text == "🎨 بازگشت به مترجم مانهوا")
def btn_to_manhwa(message):
    cmd_switch_manhwa(message)


@bot.message_handler(func=lambda m: m.text == "🗑 پاک کردن حافظه چت")
def btn_clear_memory(message):
    cmd_clear_chat(message)


@bot.message_handler(func=lambda m: m.text == "🎭 راهنمای بازنویسی لحن")
def btn_tone_guide(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    guide = (
        "🎭 <b>راهنمای بازنویسی لحن دیالوگ‌ها:</b>\n\n"
        "فقط جمله یا عکس رو بفرست و بگو چه لحنی می‌خوای، مثلاً:\n"
        "• «این جمله رو لاتی و خیابانی کن: Get out of my face»\n"
        "• «این رو کوتاه‌تر کن برای حباب کوچک»\n"
        "• «این رو دارک و تهدیدآمیز بازنویسی کن»\n\n"
        "همچنین زیر هر ترجمه صفحه، دکمه‌های شیشه‌ای آماده تغییر لحن داری!"
    )
    reply_html(message, guide)


@bot.message_handler(func=lambda m: m.text == "🔄 شروع چپتر جدید (صفحه ۱)")
def btn_new_chapter(message):
    cmd_new_chapter(message)


@bot.message_handler(func=lambda m: bool(m.text) and (m.text.startswith("🌐 مبدأ:") or m.text == "🌐 تغییر زبان مبدأ"))
def btn_change_mode(message):
    cmd_mode(message)


@bot.message_handler(func=lambda m: bool(m.text) and m.text.startswith("📚 چپتر دسته‌ای"))
def btn_batch_toggle(message):
    cmd_toggle_batch(message)


@bot.message_handler(func=lambda m: m.text == "📥 تحویل TXT دسته‌ای")
def btn_export_batch(message):
    cmd_export_batch(message)


@bot.message_handler(func=lambda m: m.text == "⚙️ مدل و تنظیمات")
def btn_settings(message):
    cmd_settings(message)


@bot.message_handler(func=lambda m: m.text in ("📖 واژه‌نامه و راهنما", "❓ راهنما"))
def btn_help_glossary(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    st = get_user_state(message.chat.id)
    proj_name, proj = get_active_project(st)
    g_count = len(proj.get("glossary", {}))
    help_text = (
        "📖 <b>راهنمای سریع ربات استودیو مبین (نسخه ۲):</b>\n\n"
        "۱. <b>ارسال عکس یا آلبوم:</b> برش هوشمند وب‌تون + ترجمه ۲۱ فونتی + دکمه‌های تغییر لحن.\n"
        "۲. <b>فایل‌های بالای ۲۰ مگابایت:</b>\n"
        "   • از <b>«📚 چپتر دسته‌ای»</b> استفاده کن (عکس‌ها یا چند ZIP تکه‌شده بفرست و با «📥 تحویل TXT دسته‌ای» خروجی یکپارچه بگیر).\n"
        "   • یا <b>لینک مستقیم</b> فایل <code>.zip</code> / <code>.cbz</code> رو بفرست تا سرور مستقیم دانلود کنه!\n"
        f"۳. <b>پروژه و واژه‌نامه:</b> پروژه فعلی: <b>{esc(proj_name)}</b> ({g_count} واژه).\n"
        "   • <code>/project نام</code> — تغییر/ساخت پروژه\n"
        "   • <code>/addterm اصلی = فارسی</code> — ثبت اسم شخصیت\n"
        "   • <code>/glossary</code> و <code>/delterm</code>\n"
        "۴. <b>صفحه و مدل:</b> <code>/page 10</code> | <code>/model</code> | <code>/tone</code>\n"
        "۵. <b>امنیت:</b> <code>/myid</code> | <code>/lock کد</code>"
    )
    reply_html(message, help_text)


# ================= INLINE CALLBACK HANDLERS =================

@bot.callback_query_handler(func=lambda call: True)
def handle_inline_callbacks(call):
    if not is_authorized(call):
        reject_unauthorized(call)
        return

    chat_id = call.message.chat.id
    data = call.data or ""
    st = get_user_state(chat_id)

    # 1. Settings callbacks
    if data.startswith("setmodel:"):
        m_id = data.split(":", 1)[1]
        if m_id in MODELS:
            st["model_name"] = m_id
            save_state()
            bot.answer_callback_query(call.id, f"✅ مدل تغییر کرد به {MODELS[m_id]}")
            try:
                bot.edit_message_reply_markup(
                    chat_id, call.message.message_id, reply_markup=get_settings_inline_kb(st)
                )
            except Exception:
                pass
        return

    if data.startswith("settone:"):
        t_id = data.split(":", 1)[1]
        if t_id in TONE_LABELS:
            st["tone_style"] = t_id
            save_state()
            bot.answer_callback_query(call.id, f"🎭 لحن پیش‌فرض: {TONE_LABELS[t_id]}")
            try:
                bot.edit_message_reply_markup(
                    chat_id, call.message.message_id, reply_markup=get_settings_inline_kb(st)
                )
            except Exception:
                pass
        return

    if data == "togglenum":
        st["numbering"] = "numeric" if st.get("numbering", "ordinal") == "ordinal" else "ordinal"
        save_state()
        lbl = "ترتیبی (بالن اول)" if st["numbering"] == "ordinal" else "عددی (بالن 1)"
        bot.answer_callback_query(call.id, f"🔢 شماره‌گذاری: {lbl}")
        try:
            bot.edit_message_reply_markup(
                chat_id, call.message.message_id, reply_markup=get_settings_inline_kb(st)
            )
        except Exception:
            pass
        return

    # 2. Cached page actions
    bucket = PAGE_CACHE.get(str(chat_id), {})

    if data.startswith("totxt:"):
        ckey = data.split(":", 1)[1]
        item = bucket.get(ckey)
        if not item:
            bot.answer_callback_query(call.id, "⚠️ حافظه این صفحه منقضی شده.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "📄 فایل TXT ارسال شد!")
        send_txt_file(
            chat_id,
            item["translation"],
            filename=f"Page_{item['page_num']}_MobinStudio.txt",
            caption=f"📄 فایل TXT آماده استودیو مبین (تصویر {item['page_num']})",
        )
        return

    if data.startswith("tone:"):
        _, tone_key, ckey = data.split(":", 2)
        item = bucket.get(ckey)
        if not item:
            bot.answer_callback_query(call.id, "⚠️ حافظه این صفحه منقضی شده.", show_alert=True)
            return
        bot.answer_callback_query(call.id, f"⏳ بازنویسی با لحن {TONE_LABELS.get(tone_key, tone_key)}...")
        try:
            bot.send_chat_action(chat_id, "typing")
            tone_rule = TONE_INSTRUCTIONS.get(tone_key, TONE_INSTRUCTIONS["default"])
            model_to_use = st.get("model_name", DEFAULT_CONFIG_MODEL)
            rewrite_prompt = (
                f"بازنویسی متن ترجمه‌شده زیر به استایل: {tone_rule}\n\n"
                f"قوانین اجباری:\n"
                f"- ساختار دقیق Mobin Studio (`تصویر N` و نام فونت‌ها داخل پرانتز) را کاملاً حفظ کن و تغییر نده.\n"
                f"- بدون سانسور و بدون توضیح اضافه فقط متن بازنویسی‌شده را بده.\n\n"
                f"{item['translation']}"
            )
            if item.get("img_bytes"):
                part = genai_types.Part.from_bytes(data=item["img_bytes"], mime_type="image/jpeg")
                res = generate_with_retry(model_to_use, [rewrite_prompt, part], temperature=0.45)
            else:
                res = generate_with_retry(model_to_use, rewrite_prompt, temperature=0.45)

            if res:
                fixed = fix_persian_typography(res)
                new_key = cache_page_result(
                    chat_id,
                    {**item, "translation": fixed},
                )
                split_and_send(
                    chat_id,
                    fixed,
                    reply_to_message_id=call.message.message_id,
                    reply_markup=get_translation_inline_kb(new_key),
                )
        except Exception as e:
            reply_html(call.message, f"❌ خطا در تغییر لحن:\n<code>{esc(e)}</code>")
        return

    if data.startswith("retrans:"):
        ckey = data.split(":", 1)[1]
        item = bucket.get(ckey)
        if not item or not item.get("img_bytes"):
            bot.answer_callback_query(call.id, "⚠️ تصویر این صفحه در کش موجود نیست؛ دوباره عکس رو بفرست.", show_alert=True)
            return
        cur_m = st.get("model_name", DEFAULT_CONFIG_MODEL)
        better_model = "gemini-3.5-flash" if cur_m == "gemini-3.5-flash-lite" else "gemini-2.5-pro"
        bot.answer_callback_query(call.id, f"🔄 ترجمه مجدد با مدل {better_model}...")
        try:
            bot.send_chat_action(chat_id, "typing")
            _, fixed, new_key, _ = translate_image_bytes(
                chat_id,
                item["img_bytes"],
                caption=item.get("caption", ""),
                override_page=item["page_num"],
                model_override=better_model,
            )
            split_and_send(
                chat_id,
                fixed,
                reply_to_message_id=call.message.message_id,
                reply_markup=get_translation_inline_kb(new_key),
            )
        except Exception as e:
            reply_html(call.message, f"❌ خطا در ترجمه مجدد:\n<code>{esc(e)}</code>")
        return


# ================= PHOTO & MEDIA HANDLERS =================

def _translate_and_reply_image(message, file_id, wait_label="تصویر"):
    chat_id = message.chat.id
    lock = get_chat_lock(chat_id)
    with lock:
        st = get_user_state(chat_id)
        _, proj = get_active_project(st)
        preview_page = proj["page"]
        msg_wait = reply_html(message, f"⏳ در حال ترجمه {wait_label} <b>{preview_page}</b>...")
        try:
            bot.send_chat_action(chat_id, "typing")
            file_info = bot.get_file(file_id)
            file_bytes = bot.download_file(file_info.file_path)

            _, fixed, ckey, _ = translate_image_bytes(
                chat_id, file_bytes, caption=message.caption or ""
            )
            try:
                bot.delete_message(chat_id, msg_wait.message_id)
            except Exception:
                pass
            split_and_send(
                chat_id,
                fixed,
                reply_to_message_id=message.message_id,
                reply_markup=get_translation_inline_kb(ckey),
            )
        except Exception as e:
            try:
                bot.delete_message(chat_id, msg_wait.message_id)
            except Exception:
                pass
            reply_html(message, f"❌ خطا در پردازش تصویر {preview_page}:\n<code>{esc(e)}</code>")


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return
    _translate_and_reply_image(message, message.photo[-1].file_id, wait_label="تصویر")


# ================= DOCUMENT & ZIP/CBZ HANDLERS =================

@bot.message_handler(content_types=["document"])
def handle_document(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return

    doc = message.document
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    fsize = doc.file_size or 0

    if fsize > TELEGRAM_BOT_MAX_BYTES:
        mb = fsize / (1024 * 1024)
        reply_html(
            message,
            f"⚠️ فایل <b>{esc(doc.file_name)}</b> حجمی برابر <b>{mb:.1f} MB</b> دارد و از سقف ۲۰ مگابایتی ربات تلگرام بیشتر است.\n\n"
            f"<b>۳ راه حل سریع همینجا:</b>\n"
            f"۱. دکمه <b>«📚 چپتر دسته‌ای»</b> رو بزن، عکس‌ها یا دو فایل ZIP زیر ۲۰ مگ بفرست و آخرش <b>«📥 تحویل TXT دسته‌ای»</b> رو بزن.\n"
            f"۲. فایل رو جایی آپلود کن و <b>لینک دانلود مستقیم</b> (<code>http/https</code>) رو بفرست تا سرور خودش مستقیم دانلود کنه!\n"
            f"۳. کیفیت JPEG رو کمی کمتر (مثلاً ۸۵٪) ذخیره کن تا زیر ۲۰ مگ بیاد.",
        )
        return

    is_archive = fname.endswith((".zip", ".cbz")) or "zip" in mime
    is_image = mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp"))

    if is_archive:
        lock = get_chat_lock(message.chat.id)
        with lock:
            try:
                file_info = bot.get_file(doc.file_id)
                file_bytes = bot.download_file(file_info.file_path)
                process_zip_bytes(message, file_bytes, doc.file_name or "chapter.zip")
            except Exception as e:
                reply_html(message, f"❌ خطا در پردازش آرشیو:\n<code>{esc(e)}</code>")
        return

    if is_image:
        _translate_and_reply_image(message, doc.file_id, wait_label="فایل تصویر")
        return

    reply_html(
        message,
        "⚠️ لطفاً عکس مانهوا، فایل <code>.zip</code> / <code>.cbz</code> یا لینک دانلود مستقیم بفرست.",
    )


# ================= VOICE & AUDIO HANDLER =================

@bot.message_handler(content_types=["voice", "audio"])
def handle_voice(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return

    media = message.voice or message.audio
    if not media:
        return
    if (media.file_size or 0) > TELEGRAM_BOT_MAX_BYTES:
        reply_html(message, "⚠️ حجم فایل صوتی بیش از ۲۰ مگابایت است.")
        return

    chat_id = message.chat.id
    st = get_user_state(chat_id)
    model_to_use = st.get("model_name", DEFAULT_CONFIG_MODEL)
    proj_name, proj = get_active_project(st)

    try:
        bot.send_chat_action(chat_id, "typing")
        finfo = bot.get_file(media.file_id)
        audio_bytes = bot.download_file(finfo.file_path)
        mime_type = getattr(media, "mime_type", None) or "audio/ogg"
        audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

        if st.get("app_mode") == "assistant":
            g = proj.get("glossary", {})
            g_note = "\nواژه‌نامه پروژه فعال:\n" + "\n".join(f"- {k}: {v}" for k, v in g.items()) if g else ""
            res = generate_with_retry(
                model_to_use,
                ["به این ویس مبین با لحن دستیار حرفه‌ای و صمیمی جواب بده.", audio_part],
                system_instruction=ASSISTANT_SYSTEM_PROMPT + g_note,
                temperature=0.5,
            )
            if res:
                split_and_send(chat_id, res, reply_to_message_id=message.message_id, as_html=True)
        else:
            prompt = (
                "کاربر مبین یک پیام صوتی درباره دیالوگ یا ترجمه مانهوا فرستاده است.\n"
                "اگر در ویس دیالوگ‌ها را می‌گوید، آن‌ها را به فارسی تهرانی طبیعی و دقیقاً در قالب استودیو مبین "
                f"(شروع با `تصویر {proj['page']}` و برچسب فونت مثل `بالن اول (کودک): متن`) بنویس.\n"
                "اگر سؤال یا دستور ویرایشی است، مستقیماً و خلاصه انجامش بده."
            )
            res = generate_with_retry(model_to_use, [prompt, audio_part], temperature=0.35)
            if res:
                fixed = fix_persian_typography(res)
                split_and_send(chat_id, fixed, reply_to_message_id=message.message_id)
    except Exception as e:
        reply_html(message, f"❌ خطا در پردازش صوت:\n<code>{esc(e)}</code>")


# ================= TEXT, URL & ASSISTANT CHAT HANDLER =================

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def run_assistant_chat(message):
    chat_id = message.chat.id
    st = get_user_state(chat_id)
    _, proj = get_active_project(st)
    model_to_use = st.get("model_name", DEFAULT_CONFIG_MODEL)

    hist = list(st.get("assistant_history", []))
    hist.append({"role": "user", "text": message.text})
    if len(hist) > 16:
        hist = hist[-16:]

    contents = [
        genai_types.Content(
            role=turn["role"],
            parts=[genai_types.Part.from_text(text=turn["text"])],
        )
        for turn in hist
    ]

    g = proj.get("glossary", {})
    sys_prompt = ASSISTANT_SYSTEM_PROMPT
    if g:
        sys_prompt += "\n\nواژه‌نامه پروژه فعلی:\n" + "\n".join(f"- {k} -> {v}" for k, v in list(g.items())[:50])

    reply = generate_with_retry(model_to_use, contents, system_instruction=sys_prompt, temperature=0.6)
    if reply:
        hist.append({"role": "model", "text": reply})
        st["assistant_history"] = hist[-16:]
        save_state()
        split_and_send(chat_id, reply, reply_to_message_id=message.message_id, as_html=True)
    else:
        reply_html(message, "⚠️ پاسخی دریافت نشد.")


@bot.message_handler(content_types=["text"])
def handle_text(message):
    if not is_authorized(message):
        reject_unauthorized(message)
        return

    text = (message.text or "").strip()
    if not text:
        return

    # 1. Direct ZIP/CBZ URL support (only when URL path points to .zip/.cbz)
    if URL_RE.match(text):
        url_path = urllib.parse.urlparse(text).path.lower()
        if url_path.endswith((".zip", ".cbz")):
            _process_zip_url(message, text)
            return

    st = get_user_state(message.chat.id)

    # 2. Assistant mode
    if st.get("app_mode") == "assistant":
        try:
            bot.send_chat_action(message.chat.id, "typing")
            run_assistant_chat(message)
        except Exception as e:
            reply_html(message, f"❌ خطای دستیار چت:\n<code>{esc(e)}</code>")
        return

    # 3. Manhwa mode with plain text: smartly translate dialogue OR reply naturally to Persian prompts
    has_latin_or_kr = bool(re.search(r"[A-Za-z\uAC00-\uD7AF]", text))
    try:
        bot.send_chat_action(message.chat.id, "typing")
        if not has_latin_or_kr and len(text.split()) >= 2:
            # Persian text sent while in manhwa mode (likely a rewrite/editing note)
            run_assistant_chat(message)
            return

        _, proj = get_active_project(st)
        prompt = build_prompt(st, proj["page"]) + f"\n\nمتن زیر را با ساختار استاندارد ترجمه کن:\n{text}"
        model_to_use = st.get("model_name", DEFAULT_CONFIG_MODEL)
        res = generate_with_retry(model_to_use, prompt, temperature=0.35)
        if res:
            fixed = fix_persian_typography(res)
            ckey = cache_page_result(
                message.chat.id,
                {
                    "page_num": proj["page"],
                    "img_bytes": None,
                    "source_text": text,
                    "translation": fixed,
                    "mode": st.get("mode", "english"),
                    "caption": "",
                    "project": st.get("active_project", "عمومی"),
                },
            )
            split_and_send(
                message.chat.id,
                fixed,
                reply_to_message_id=message.message_id,
                reply_markup=get_translation_inline_kb(ckey),
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
            "service": "MobinStudio Manhwa Translator v2",
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
    logger.info("🚀 ربات استودیو مبین (نسخه ۲ - ارتقایافته) فعال شد...")
    logger.info("=" * 60)
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(3)

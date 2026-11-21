import os
import sys
import json
import io
import time
import re
import zipfile

# Ensure stdout/stderr exist when running headlessly via pythonw
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_runtime.log")
if sys.stdout is None:
    try:
        sys.stdout = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        sys.stdout = io.StringIO()
if sys.stderr is None:
    try:
        sys.stderr = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        sys.stderr = io.StringIO()
from PIL import Image
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    # 1. First check environment variables (ideal for Cloud/Render/Koyeb hosting)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("MODEL_NAME", "").strip()

    # 2. Fallback to config.json if available locally
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if not token:
                    token = cfg.get("telegram_bot_token", "").strip()
                if not gemini_key:
                    gemini_key = cfg.get("gemini_api_key", "").strip()
                if not model:
                    model = cfg.get("model_name", "gemini-3.5-flash-lite").strip()
        except Exception as e:
            print(f"Warning reading config.json: {e}")

    if not model:
        model = "gemini-3.5-flash-lite"

    return token, gemini_key, model

BOT_TOKEN, GEMINI_KEY, MODEL_NAME = load_config()

if not BOT_TOKEN or not GEMINI_KEY:
    print("❌ Error: TELEGRAM_BOT_TOKEN or GEMINI_API_KEY is missing!")
    print("Please set them as Environment Variables or in config.json.")
    sys.exit(1)

genai.configure(api_key=GEMINI_KEY)

SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# 1. Manhwa Vision Translation Model
translation_model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    safety_settings=SAFETY_SETTINGS
)

# 2. Conversational Assistant Model
ASSISTANT_SYSTEM_PROMPT = """تو «دستیار استودیو مبین» هستی؛ یک هوش مصنوعی بسیار باهوش، صمیمی، خوش‌برخورد و متخصص حوزه ترجمه، بازنویسی و ادیت دیالوگ مانهوا، مانگا و کمیک.
مخاطب تو «مبین» (مترجم و تایپیست حرفه‌ای مانهوا) است.

ویژگی‌ها و قابلیت‌های اصلی تو:
1. تسلط کامل به اصطلاحات عامیانه (Slang & Idioms) در زبان انگلیسی، کره‌ای و فارسی روزمره.
2. توانایی بازنویسی یک جمله یا دیالوگ در لحن‌های مختلف به درخواست مبین:
   - عامیانه تهرانی روان
   - لاتی، خیابانی و داش‌مشتی
   - دارک، خشن و تهدیدآمیز
   - طنز، کنایه‌آمیز و خنده‌دار
   - احساسی، عاشقانه و رمانتیک
   - تاریخی، سلطنتی و فاخر
3. آشنایی کامل با اصطلاحات، القاب و فرهنگ کره‌ای (مانند هیونگ، نونا، اوپا، آجوما، سونبه، په‌ها، ماکنه، چوبول، و...) و نحوه بومی‌سازی آنها.
4. آشنایی با سیستم ۲۱ فونتی استودیو مبین (کودک، الهام، یکان، مروارید، رم الله، مهر، کودک استایل، حیات، فانوس، دست‌نویس، موج، کروش، افسانه، عارفی، زنگار، کردی، اکبر، آندالوس، سپهبد، جودی).
5. زمینه مانهواهای بزرگسال (18+): راحت و بدون سانسور در ترجمه جملات صمیمی، فحش‌ها و مکالمات بزرگسالانه عمل کن.
6. پاسخگویی به هر سوال عمومی و داشتن گپ و گفت دوستانه و پرانرژی با مبین."""

assistant_model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    system_instruction=ASSISTANT_SYSTEM_PROMPT,
    safety_settings=SAFETY_SETTINGS
)

# User state & chat memory
user_state = {}

def get_user_state(chat_id):
    if chat_id not in user_state:
        user_state[chat_id] = {
            'page': 1,
            'mode': 'english',       # 'english' or 'korean'
            'app_mode': 'manhwa',    # 'manhwa' or 'assistant'
            'chat_session': None
        }
    if user_state[chat_id].get('chat_session') is None:
        user_state[chat_id]['chat_session'] = assistant_model.start_chat(history=[])
    return user_state[chat_id]

def get_manhwa_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("💬 دستیار هوشمند (چت)"), types.KeyboardButton("🔄 شروع چپتر جدید (صفحه ۱)"))
    kb.row(types.KeyboardButton("🌐 تغییر زبان مبدأ"), types.KeyboardButton("❓ راهنما"))
    return kb

def get_assistant_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🎨 بازگشت به مترجم مانهوا"), types.KeyboardButton("🗑 پاک کردن حافظه چت"))
    kb.row(types.KeyboardButton("🎭 راهنمای بازنویسی لحن"), types.KeyboardButton("❓ راهنما"))
    return kb

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

ZWNJ = '\u200c'

def fix_persian_typography(text):
    if not text:
        return text
    raw_lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    fixed_lines = []
    for l in raw_lines:
        if re.match(r'^\s*تصویر\s+\d+', l):
            fixed_lines.append(l.strip())
            continue

        l = re.sub(r'\b(ن?می)\s+([آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی])', r'\1' + ZWNJ + r'\2', l)
        l = re.sub(r'([آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی])\s+(ها(?:ی|یی|یت|یم|یش|یتان|یمان|یشان)?)\b', r'\1' + ZWNJ + r'\2', l)
        l = re.sub(r'([آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی])\s+(تر(?:ین)?)\b', r'\1' + ZWNJ + r'\2', l)
        l = re.sub(r'([آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی])\s+(ای)\b', r'\1' + ZWNJ + r'\2', l)
        l = re.sub(r'\s+([،؛!؟:.,])', r'\1', l)
        l = l.replace('?', '؟').replace(',', '،')
        l = re.sub(r'[ \t]+', ' ', l)
        fixed_lines.append(l)

    return '\n\n'.join(fixed_lines)

def split_and_send(chat_id, text, reply_to_message_id=None):
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        bot.send_message(chat_id, chunk, reply_to_message_id=reply_to_message_id)

def send_txt_file(chat_id, content, filename="chapter_translation.txt", caption=None):
    content = fix_persian_typography(content)
    bio = io.BytesIO(content.encode('utf-8'))
    bio.name = filename
    bot.send_document(chat_id, bio, caption=caption)

def build_prompt(source_lang, page_num, extra_note=""):
    source_instruction = "ENGLISH -> IRANIAN PERSIAN (FARSI). The manhwa is translated into English. Translate directly from English to Persian."
    if source_lang == 'korean':
        source_instruction = "KOREAN RAW -> IRANIAN PERSIAN (FARSI). Translate directly from original Korean manhwa panels into Persian."

    prompt = f"""ULTIMATE PROFESSIONAL MANHWA TRANSLATION ENGINE

1. YOUR PROFESSIONAL ROLE
You are an elite professional manhwa translator, literary localization specialist, OCR analyst, dialogue editor, and typesetting assistant for Mobin Studio.
YOUR TRANSLATION DIRECTION: {source_instruction}

2. TRANSLATION QUALITY AND STYLE
- Use natural, fluent, conversational Iranian Persian (عامیانه و روان).
- Avoid robotic, literal, and machine-translated sentences.
- Preserve the exact meaning, character personality, idioms, slang, and emotional tone.
- Keep sentences concise enough to fit inside speech bubbles.
- MOANS, SIGHS & SOUNDS RULE: Moans, sighs, gasps, groans, panting, and sensual sounds (e.g. "haa..", "ah..", "ahh...", "uh..", "mmh...", "ngh...", "sigh..") MUST BE CONVERTED INTO NATURAL FARSI MANHWA EQUIVALENTS (e.g. هاااه...، آه...!، آااه...، اوووم...، ن‌ن‌نه...، پوف...). NEVER leave them in English letters!

3. ADULT CONTENT AND TRANSLATION POLICY
This project includes mature and fictional adult-rated manhwa (18+). You are acting as a translator, not a content critic.
- An adult rating alone is not a reason to refuse a translation.
- Do not unnecessarily censor, sanitize, or soften permitted source material.
- Translate vulgar expressions, profanity, sexual terminology, intimate dialogue, and adult vocabulary accurately without hesitation.

4. IMAGE ANALYSIS AND OCR
- Inspect the image carefully. Read the text directly from the image panels.
- Identify every speech bubble and text element in natural reading order (top-to-bottom, panel flow).
- Keep each text element separate. Never merge dialogue from different bubbles.
- SOUND EFFECTS & [UNREADABLE] RULE: Only include distinct, meaningful, and prominent sound effects that can be clearly read. DO NOT spam or output multiple [UNREADABLE] lines for vague background scribbles, ambient artist marks, or minor unreadable drawings. If a background sound effect is completely illegible or just a tiny decorative squiggle, OMIT IT entirely so the typesetting script stays clean and usable. Only use [UNREADABLE] for actual dialogue speech bubbles that are genuinely obscured.

5. PROFESSIONAL FONT IDENTIFICATION SYSTEM
You MUST identify the correct Persian font for each text element according to its visual appearance and function:
1. Regular dialogue: کودک
2. Rectangular narration and explanation boxes: الهام
3. Thoughts inside square boxes: یکان
4. Thoughts written directly inside manhwa/manhua illustrations: نگار / لاله زار
5. Thought bubbles: مروارید
6. Cute or decorative bubbles: رم الله
7. Manhwa sun-shaped bubbles: مهر
8. Manhua and manga sun-shaped bubbles: کودک با استایل
9. Old-style sun-shaped bubbles: حیات
10. System messages and system boxes: فانوس / اصفهان / فرناز / الهام همراه با استایل
11. Text written directly inside illustrations and whispering: دست نویس
12. Scared, trembling, crying, or shaking dialogue: موج
13. Soft shouting: کروش
14. Loud shouting and angry dialogue in manhwa: افسانه
15. Loud shouting and angry dialogue in manga and manhua: عارفی
16. Dark dialogue bubbles: زنگار
17. Screaming and monster sounds: کردی
18. Radio, television, and phone-call dialogue: اکبر / آسمان / مثلث
19. Letters, documents, and scrolls: آندالوس / فورات
20. Thoughts written inside manga illustrations: سپهبد
21. Cloud-shaped dialogue bubbles: جودی

The font name must ALWAYS appear in parentheses next to its corresponding element label.

6. MANDATORY EXACT TXT STRUCTURE (IDENTICAL TO CHAPTER SCRIPT)
The output for this image MUST strictly follow this exact single-line per element format, separated by exactly one blank line:

تصویر {page_num}

بالن اول (کودک): وای گه... خیلی خیسه!!

بالن دوم (کودک): و گرمه!

افکت صوتی اول (موج): هاننگ...

بالن افکار اول (مروارید): من تنها کسی‌ام که تازه فهمیده این چقدر اعصاب‌خردکنه...!

نوشته داخل تصویر اول (دست نویس): CAPITALIST HAREM

افکت صوتی دوم (افسانه): کیاا!!

STRICT FORMATTING RULES:
- SINGLE LINE PER ELEMENT: The translated text MUST be on the EXACT SAME LINE directly after the colon (": "). Never break the line immediately after the colon!
- EXACTLY ONE BLANK LINE between every entry.
- NO [UNREADABLE] TAGS: Never output "[UNREADABLE]". Translate all moans, gasps, and vocal sound effects to natural Persian sounds.
- Output ONLY the structured translation lines. No conversational filler or notes."""

    if extra_note:
        prompt += f"\n\nنکته اختصاصی کاربر درباره این صفحه: {extra_note}"

    return prompt

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ================= COMMAND HANDLERS =================

@bot.message_handler(commands=['start'])
def handle_start(message):
    st = get_user_state(message.chat.id)
    lang_display = "انگلیسی به فارسی" if st['mode'] == 'english' else "کره‌ای (خام) به فارسی"

    text = (
        f"👑 **سلام مبین جان! خوش اومدی به استودیو هوشمندت**\n\n"
        f"📊 شماره صفحه بعدی: **تصویر {st['page']}**\n"
        f"🌐 زبان فعلی ترجمه: **{lang_display}**\n\n"
        f"✨ **امکانات فعال:**\n"
        f"• **ترجمه تک عکس مانهوا:** ارسال تصویر و تحویل اسکریپت دقیق خط به خط با تطبیق ۲۱ فونت\n"
        f"• **ترجمه کل چپتر با ZIP:** ارسال فایل زیپ و تحویل فایل نهایی `.txt` مخصوص استودیو مبین\n"
        f"• **دستیار هوشمند (چت):** بازنویسی لحن دیالوگ‌ها (لاتی، دارک، طنز)، معادل اصطلاحات و چت با حافظه مکالمه\n"
        f"• **بدون سانسور:** ترجمه دقیق عبارات و صحنه‌های بزرگسال (18+)\n\n"
        f"📥 عکس مانهوا یا فایل ZIP چپترت رو بفرست تا شروع کنیم!"
    )
    kb = get_manhwa_keyboard() if st['app_mode'] == 'manhwa' else get_assistant_keyboard()
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=['chat'])
def cmd_switch_chat(message):
    st = get_user_state(message.chat.id)
    st['app_mode'] = 'assistant'
    msg = (
        "💬 **حالت دستیار چت فعال شد!**\n\n"
        "من دستیار تخصصی تو برای ترجمه و ادیت مانهوا هستم. حافظه گفتگوی ما فعاله و هر سوالی بپرسی در یادم می‌مونه.\n\n"
        "🔹 هر متنی بفرستی باهات گفتگو می‌کنم و می‌تونم دیالوگ‌ها رو با هر لحنی که بخوای بازنویسی کنم.\n"
        "🔹 اگر عکس یا فایل ZIP بفرستی، در هر حالتی فوراً ترجمه می‌شه!\n"
        "🔹 برای برگشت به پنل مترجم، دکمه «🎨 بازگشت به مترجم مانهوا» رو بزن."
    )
    bot.send_message(message.chat.id, msg, reply_markup=get_assistant_keyboard())

@bot.message_handler(commands=['manhwa'])
def cmd_switch_manhwa(message):
    st = get_user_state(message.chat.id)
    st['app_mode'] = 'manhwa'
    bot.send_message(message.chat.id, "🎨 **حالت مترجم مانهوا فعال شد!**\nعکس‌ها یا فایل ZIP چپترت رو بفرست.", reply_markup=get_manhwa_keyboard())

@bot.message_handler(commands=['clear_chat'])
def cmd_clear_chat(message):
    st = get_user_state(message.chat.id)
    st['chat_session'] = assistant_model.start_chat(history=[])
    bot.reply_to(message, "🗑 **حافظه چت پاک شد!** مکالمه جدیدی با دستیار شروع شد.")

@bot.message_handler(commands=['new_chapter'])
def cmd_new_chapter(message):
    st = get_user_state(message.chat.id)
    st['page'] = 1
    bot.reply_to(message, "🔄 شمارنده ریست شد! صفحه بعدی **تصویر 1** خواهد بود.")

@bot.message_handler(commands=['mode'])
def cmd_mode(message):
    st = get_user_state(message.chat.id)
    new_mode = 'korean' if st['mode'] == 'english' else 'english'
    st['mode'] = new_mode
    name = "کره‌ای (خام) به فارسی" if new_mode == 'korean' else "انگلیسی به فارسی"
    bot.reply_to(message, f"🌐 زبان مبدأ تغییر کرد به: **{name}**")

# ================= BUTTON HANDLERS =================

@bot.message_handler(func=lambda m: m.text == "💬 دستیار هوشمند (چت)")
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
    guide = (
        "🎭 **راهنمای بازنویسی لحن دیالوگ‌ها:**\n\n"
        "کافیه دیالوگ یا جمله‌ات رو بفرستی و بگی با چه لحنی می‌خوای، مثلاً:\n"
        "• «این جمله رو لاتی و خیابانی کن: Get out of my face»\n"
        "• «این رو دارک و تهدیدآمیز بازنویسی کن»\n"
        "• «معادل تهرانی اصطلاح Piece of cake چیه؟»\n\n"
        "دستیار برات چندین گزینه جذاب با استایل مانهوا پیشنهاد میده!"
    )
    bot.reply_to(message, guide)

@bot.message_handler(func=lambda m: m.text == "🔄 شروع چپتر جدید (صفحه ۱)")
def btn_new_chapter(message):
    cmd_new_chapter(message)

@bot.message_handler(func=lambda m: m.text == "🌐 تغییر زبان مبدأ")
def btn_change_mode(message):
    cmd_mode(message)

@bot.message_handler(func=lambda m: m.text == "❓ راهنما")
def btn_help(message):
    help_text = (
        "📖 **راهنمای کامل ربات استودیو مبین:**\n\n"
        "۱. **ترجمه تک عکس مانهوا:** هر عکسی بفرستی فوراً طبق قالب چپتر ۸ استودیو (تصویر، بالن، فونت) ترجمه می‌کنه.\n"
        "۲. **ترجمه کل چپتر با ZIP:** فایل زیپ عکس‌ها رو بفرست، همه صفحات به نوبت ترجمه و در قالب فایل آماده `.txt` تحویلت داده می‌شه.\n"
        "۳. **دستیار هوشمند (چت):** دکمه '💬 دستیار هوشمند' رو بزن و هر سوال ترجمه‌ای، دیالوگ‌نویسی یا اصطلاح داری بپرس.\n"
        "۴. **ریست شمارنده:** دکمه 'شروع چپتر جدید' رو بزن تا صفحه بعدی تصویر ۱ بشه.\n"
        "۵. **تغییر زبان:** بین انگلیسی به فارسی و کره‌ای خام به فارسی جابجا شو."
    )
    bot.reply_to(message, help_text)

# ================= PHOTO HANDLERS =================

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    st = get_user_state(message.chat.id)
    cur_page = st['page']
    st['page'] += 1

    try:
        bot.send_chat_action(message.chat.id, 'typing')
        msg_wait = bot.reply_to(message, f"⏳ در حال ترجمه تصویر {cur_page}...")

        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_bytes = bot.download_file(file_info.file_path)

        image = Image.open(io.BytesIO(file_bytes))
        prompt = build_prompt(st['mode'], cur_page, message.caption or "")
        response = translation_model.generate_content([prompt, image])

        bot.delete_message(message.chat.id, msg_wait.message_id)

        if response.text:
            fixed_text = fix_persian_typography(response.text)
            split_and_send(message.chat.id, fixed_text, reply_to_message_id=message.message_id)
        else:
            bot.reply_to(message, f"⚠️ متنی در تصویر {cur_page} شناسایی نشد.")

    except Exception as e:
        bot.reply_to(message, f"❌ خطا در پردازش تصویر {cur_page}:\n{str(e)}")

# ================= DOCUMENT & ZIP HANDLERS =================

@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    fname = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    # Case A: ZIP File of entire chapter
    if fname.endswith('.zip') or 'zip' in mime:
        handle_zip_chapter(message)
        return

    # Case B: Single uncompressed image document
    if mime.startswith('image/') or fname.endswith(('.jpg', '.jpeg', '.png', '.webp')):
        st = get_user_state(message.chat.id)
        cur_page = st['page']
        st['page'] += 1

        try:
            bot.send_chat_action(message.chat.id, 'typing')
            msg_wait = bot.reply_to(message, f"⏳ در حال پردازش فایل تصویر {cur_page}...")

            file_info = bot.get_file(doc.file_id)
            file_bytes = bot.download_file(file_info.file_path)
            image = Image.open(io.BytesIO(file_bytes))

            prompt = build_prompt(st['mode'], cur_page, message.caption or "")
            response = translation_model.generate_content([prompt, image])

            bot.delete_message(message.chat.id, msg_wait.message_id)

            if response.text:
                fixed_text = fix_persian_typography(response.text)
                split_and_send(message.chat.id, fixed_text, reply_to_message_id=message.message_id)
            else:
                bot.reply_to(message, "⚠️ پاسخی تولید نشد.")
        except Exception as e:
            bot.reply_to(message, f"❌ خطا:\n{str(e)}")
        return

    bot.reply_to(message, "⚠️ لطفاً عکس مانهوا یا فایل ZIP حاوی صفحات چپتر را ارسال کنید.")

def handle_zip_chapter(message):
    st = get_user_state(message.chat.id)
    doc = message.document

    try:
        msg_prog = bot.reply_to(message, "📦 فایل ZIP دریافت شد. در حال باز کردن و استخراج صفحات...")
        file_info = bot.get_file(doc.file_id)
        file_bytes = bot.download_file(file_info.file_path)

        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            all_files = [f for f in z.namelist() if not f.startswith('__MACOSX') and not f.endswith('/')]
            img_files = [f for f in all_files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]

            if not img_files:
                bot.edit_message_text("❌ هیچ عکس معتبری (JPG/PNG/WEBP) در این فایل ZIP پیدا نشد.", message.chat.id, msg_prog.message_id)
                return

            img_files.sort(key=natural_sort_key)
            total = len(img_files)

            bot.edit_message_text(f"📖 فصل شامل {total} صفحه شناسایی شد.\n⏳ شروع ترجمه و استخراج خط به خط...", message.chat.id, msg_prog.message_id)

            chapter_results = []

            for idx, img_name in enumerate(img_files, start=1):
                try:
                    bot.send_chat_action(message.chat.id, 'typing')
                    bot.edit_message_text(f"⏳ در حال ترجمه صفحه {idx} از {total}...\n({os.path.basename(img_name)})", message.chat.id, msg_prog.message_id)
                except Exception:
                    pass

                try:
                    raw_data = z.read(img_name)
                    image = Image.open(io.BytesIO(raw_data))

                    prompt = build_prompt(st['mode'], idx, "")
                    response = translation_model.generate_content([prompt, image])

                    res_text = (response.text or "").strip()
                    if not res_text:
                        res_text = f"تصویر {idx}\n[بدون متن یا دیالوگ]"

                    chapter_results.append(res_text)
                    time.sleep(1)
                except Exception as ex:
                    chapter_results.append(f"تصویر {idx}\n[خطا در صفحه {idx}: {str(ex)}]")

            full_script = "\n\n" + "\n\n".join(chapter_results) + "\n"
            bot.edit_message_text(f"✅ ترجمه تمام {total} صفحه با موفقیت کامل شد!", message.chat.id, msg_prog.message_id)

            zip_base = os.path.splitext(doc.file_name or "chapter")[0]
            txt_name = f"{zip_base}_MobinStudio.txt"
            send_txt_file(message.chat.id, full_script, filename=txt_name, caption=f"📄 فایل متنی کامل چپتر مخصوص استودیو مبین ({total} صفحه)")

    except Exception as e:
        bot.reply_to(message, f"❌ خطا در پردازش فایل ZIP:\n{str(e)}")

# ================= TEXT & CHAT ASSISTANT HANDLER =================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    st = get_user_state(message.chat.id)

    if st['app_mode'] == 'assistant':
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            chat_session = st['chat_session']
            response = chat_session.send_message(message.text)
            if response.text:
                split_and_send(message.chat.id, response.text, reply_to_message_id=message.message_id)
            else:
                bot.reply_to(message, "⚠️ پاسخی دریافت نشد.")
        except Exception as e:
            bot.reply_to(message, f"❌ خطای دستیار چت:\n{str(e)}")
        return

    try:
        bot.send_chat_action(message.chat.id, 'typing')
        prompt = build_prompt(st['mode'], st['page']) + f"\n\nمتن زیر را ترجمه کن:\n{message.text}"
        response = translation_model.generate_content(prompt)
        if response.text:
            fixed_text = fix_persian_typography(response.text)
            split_and_send(message.chat.id, fixed_text, reply_to_message_id=message.message_id)
        else:
            bot.reply_to(message, "⚠️ پاسخی تولید نشد.")
    except Exception as e:
        bot.reply_to(message, f"❌ خطا:\n{str(e)}")

import socket

# --- Cloud Health Check Server (Alwaysdata / Render / Koyeb Web Service) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        data = {
            "status": "online",
            "bot": "@RmftMobinBot",
            "service": "MobinStudio Manhwa Translator",
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        # Mute normal health ping logs
        pass

def start_health_check_server(host, port):
    try:
        if ":" in host:
            class V6Server(HTTPServer):
                address_family = socket.AF_INET6
            server = V6Server((host, port), HealthCheckHandler)
        else:
            server = HTTPServer((host, port), HealthCheckHandler)
        print(f"🌐 Cloud Health Check server listening on [{host}]:{port}")
        server.serve_forever()
    except Exception as e:
        print(f"Health server error: {e}")

if __name__ == '__main__':
    # If running on cloud platform (Alwaysdata sets IP/PORT, Render sets PORT)
    cloud_port = os.environ.get("PORT")
    cloud_host = os.environ.get("IP", "::" if cloud_port else "0.0.0.0")
    if cloud_port:
        try:
            p = int(cloud_port)
            threading.Thread(target=start_health_check_server, args=(cloud_host, p), daemon=True).start()
        except Exception as e:
            print(f"Could not parse PORT: {e}")

    print("=" * 60)
    print("🚀 ربات استودیو مبین (مترجم حرفه‌ای مانهوا + دستیار چت) فعال شد...")
    print("=" * 60)
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(3)

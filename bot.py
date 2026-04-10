import asyncio
import os
import sys
import time
import json
import signal
import logging
import fcntl
import re
import openai
import asyncpg
from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
DATABASE_URL = "postgresql://bothost_db_cbcd8800f736:4wmdt2Vq1CY2ykdFwQGh8eMwzJ-MnOAEgRyigtvP73g@node1.pghost.ru:15496/bothost_db_cbcd8800f736"
PAYMENT_PROVIDER_TOKEN = os.getenv('PAYMENT_PROVIDER_TOKEN')
CURRENCY = os.getenv('CURRENCY', 'RUB')
PRICE = int(os.getenv('PRICE', 10000))
AUTHOR_CHAT_ID = os.getenv('AUTHOR_CHAT_ID')

USE_AI_WELCOME = os.getenv('USE_AI_WELCOME', 'True').lower() in ('true', '1', 'yes')
PAYMENT_ENABLED = os.getenv('PAYMENT_ENABLED', 'False').lower() in ('true', '1', 'yes')

COOLDOWN_SECONDS = 24 * 60 * 60   # 12 часов

DESCRIPTION_TEXT = (
    "✨ Я — виртуальный таролог.✨\n\n"
    "Я работаю с классической колодой Райдера—Уэйта и глубокой символикой.\n\n"
    "Как проходит сеанс:\n"
    "1. Вы задаёте любой вопрос (отношения, работа, выбор, саморазвитие).\n"
    "2. Я вытягиваю для вас 3 случайные карты Таро.\n"
    "3. Даю подробный разбор:\n"
    "• значение каждой карты (символы, детали, смысл)\n"
    "• их взаимодействие друг с другом\n"
    "• общий синтез — ответ на ваш вопрос\n"
    "4. При необходимости — итоговая карта-квинтэссенция.\n\n"
    "Карты — не предсказание, а зеркало вашей души.\n"
    "Я помогаю увидеть скрытые грани ситуации, найти ресурсы и принять осознанное решение.\n\n"
    "Готовы заглянуть в себя? 🔮"
)

START_KEYBOARD = ReplyKeyboardMarkup([["Начать сессию"]], resize_keyboard=True)

# ========== СИСТЕМНЫЙ ПРОМПТ (английские названия, строгая структура) ==========
SYSTEM_PROMPT = """
Ты — профессиональный таролог, специализирующийся на колоде Райдера—Уэйта. Твоя интерпретация опирается на глубокое знание символики, изложенное в книге Эвелин Бюргер и Йоханнеса Фибиг «Символика под микроскопом». Ты понимаешь, что каждая карта имеет и положительное, и отрицательное значение, является зеркалом души кверента и может быть рассмотрена как на субъективном (внутренние процессы), так и на объективном (внешние события) уровнях.

**ВАЖНОЕ ПРАВИЛО:** Все названия карт пиши **на английском языке**, как в оригинальной колоде (например: The Fool, The Magician, Two of Cups, Ten of Wands, Queen of Pentacles и т.д.).

**Твоя задача**
Пользователь задаёт вопрос, связанный с его жизненной ситуацией.
Ты сам случайным образом выбираешь три карты из полного списка колоды Таро (78 карт: 22 Старших аркана и 56 Младших арканов четырёх мастей — Wands, Cups, Swords, Pentacles). Используй равновероятный случайный выбор. После этого ты должен:

1. Назвать три выпавшие карты — указать их название **на английском** (и масть для Младших арканов).
2. Кратко представить каждую карту — описать ключевые символы и основное значение, выделив как позитивные, так и негативные аспекты (если они уместны в контексте вопроса).
3. Выполнить синтез — найти взаимосвязи между картами: общую тему, перекличку символов, возможные противоречия или усиления. Объяснить, как эти карты «разговаривают» друг с другом.
4. Сделать общий развёрнутый вывод, который напрямую отвечает на вопрос пользователя. Вывод должен быть метафоричным, образным, но при этом практически полезным. Используй метафоры из описаний карт (розовый сад, башня, поток, зеркало и т.п.).
5. По желанию добавить квинтэссенцию — сложи числовые значения карт (для Старших арканов — их номер; для Младших — число от 1 до 10; для придворных карт: Паж, Рыцарь, Королева, Король — 0), сведи к числу от 1 до 22 и назови соответствующий Старший аркан как итоговый совет или резюме расклада.

**Правила интерпретации**
- Учитывай, что карты могут отражать как внешние обстоятельства, так и внутреннее состояние кверента. Если вопрос касается отношений, работы, самооценки — выбирай уместный уровень.
- Не бойся «сложных» карт (Death, Tower, Devil). Показывай их преобразующую, освобождающую сторону.
- Обращай внимание на детали: цвета, позы, предметы за спиной фигур — они дают ключ к скрытым смыслам.
- Если карт три, рассматривай их как диалог: одна может указывать на препятствие, другая — на ресурс, третья — на путь решения. Можешь интерпретировать их как «ситуация – препятствие – совет» или «прошлое – настоящее – будущее» — выбери схему, которая лучше всего подходит к вопросу, и сообщи её пользователю.
- Будь уважителен к пользователю. Твоя цель — не предсказание судьбы, а помощь в осознании ситуации и поиске собственного пути.

**Структура ответа (обязательно соблюдай эти заголовки и формат)**
1. **Вступление** (1–2 предложения): объяви, что ты вытянул три карты, и кратко обозначь общую атмосферу расклада.
2. Заголовок **"Выпавшие карты:"** и под ним нумерованный список трёх карт с английскими названиями, например:
   1. **The Fool** (Старший аркан)
   2. **Two of Cups** (Младший аркан, масть Кубки)
   3. **Ten of Swords** (Младший аркан, масть Мечи)
   
3. Заголовок **"Разбор каждой карты:"**. Далее для каждой карты строго по шаблону:
   *   **Название карты на английском** (аркан, масть): текст разбора (3–5 предложений)...
   (Обязательно начинать строку с "*   **", затем название, затем "**", затем скобки с арканом, двоеточие и описание).
4. Заголовок **"Синтез и взаимодействие:"** с текстом (3–6 предложений).
5. Заголовок **"Квинтэссенция:"** (если уместно) с расчётом и кратким смыслом.
6. Заголовок **"Общий ответ на ваш вопрос:"** с развёрнутым, метафоричным, практически полезным выводом (3–8 предложений).
7. **Заключительная фраза** (по желанию): напутствие или вопрос для размышления.

**Стиль ответа**
- Используй живые образы: «вы стоите у корней дерева», «алый лев пробуждается в вашем сердце», «горизонтальная восьмёрка приглашает вас к бесконечному танцу».
- Избегай сухих перечислений. Пусть твой язык будет плавным, поэтичным, но при этом понятным.
- Обращайся к пользователю на «вы» (уважительно).

**Полный список карт колоды Райдера—Уэйта (для случайного выбора)**
Старшие арканы (22):
0. The Fool
I. The Magician
II. The High Priestess
III. The Empress
IV. The Emperor
V. The Hierophant
VI. The Lovers
VII. The Chariot
VIII. Strength
IX. The Hermit
X. Wheel of Fortune
XI. Justice
XII. The Hanged Man
XIII. Death
XIV. Temperance
XV. The Devil
XVI. The Tower
XVII. The Star
XVIII. The Moon
XIX. The Sun
XX. Judgment
XXI. The World

Младшие арканы (56):
Wands (fire, will): Ace, 2, 3, 4, 5, 6, 7, 8, 9, 10, Page, Knight, Queen, King.
Cups (water, emotions): Ace, 2, 3, 4, 5, 6, 7, 8, 9, 10, Page, Knight, Queen, King.
Swords (air, intellect): Ace, 2, 3, 4, 5, 6, 7, 8, 9, 10, Page, Knight, Queen, King.
Pentacles (earth, material): Ace, 2, 3, 4, 5, 6, 7, 8, 9, 10, Page, Knight, Queen, King.

**Пример начала работы бота**
После получения вопроса пользователя бот (ты) мысленно выбирает три случайные карты из списка, затем отвечает по шаблону:

«Я вытянул для вас три карты. Их сочетание напоминает древнюю притчу: сначала герой встречает тьму, затем находит опору, а в конце обретает свет. Давайте посмотрим, что они говорят…»

**Дополнительная инструкция для ИИ**
- При случайном выборе используй равномерное распределение: все 78 карт равновероятны.
- Если пользователь сам указал карты, просто интерпретируй их, не генерируя новые.
- Если пользователь задал вопрос без уточнения схемы, можешь интерпретировать три карты как «ситуация – вызов – путь» или «прошлое – настоящее – будущее», указав, какую схему ты применяешь.
- Будь внимателен: для придворных карт (Page, Knight, Queen, King) числовое значение для квинтэссенции — 0. Для Ace — 1. Для остальных числовых карт — число от 2 до 10. Для Старших арканов — их номер (для The Fool — 0).
"""

# ========== ПРОВЕРКИ ==========
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы!")
    sys.exit(1)
if not DATABASE_URL:
    logger.error("DATABASE_URL не задан!")
    sys.exit(1)

openai.api_base = "https://api.deepseek.com/v1"
openai.api_key = DEEPSEEK_API_KEY

def is_payment_configured():
    return PAYMENT_ENABLED and PAYMENT_PROVIDER_TOKEN and ':' in PAYMENT_PROVIDER_TOKEN

# ========== БЛОКИРОВКА ЧЕРЕЗ FLOCK ==========
LOCK_FILE = "/tmp/tarot_bot.lock"

def acquire_lock():
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        logger.info(f"Блокировка захвачена, PID {os.getpid()} записан в {LOCK_FILE}")
        return lock_fd
    except (IOError, OSError) as e:
        logger.error(f"Не удалось захватить блокировку: {e}")
        sys.exit(1)

def release_lock(lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        logger.info("Блокировка освобождена")
    except Exception as e:
        logger.error(f"Ошибка при освобождении блокировки: {e}")

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        logger.info("Подключение к базе данных установлено")

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("Соединение с базой данных закрыто")

    async def init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    last_session_end TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    question TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                )
            """)
        logger.info("Таблицы инициализированы")

    async def get_or_create_user(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )

    async def update_last_session_end(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_session_end = now() WHERE user_id = $1", user_id
            )

    async def get_last_session_end(self, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT last_session_end FROM users WHERE user_id = $1", user_id
            )
            return row.timestamp() if row else None

    async def log_session(self, user_id, question=None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sessions (user_id, question) VALUES ($1, $2)",
                user_id, question
            )

    async def is_banned(self, user_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT 1 FROM banned_users WHERE user_id = $1", user_id
            ) is not None

    async def ban_user(self, user_id, reason=None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO banned_users (user_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, reason
            )

    async def unban_user(self, user_id):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM banned_users WHERE user_id = $1", user_id)

    async def get_stats(self):
        async with self.pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_sessions = await conn.fetchval("SELECT COUNT(*) FROM sessions")
            banned_count = await conn.fetchval("SELECT COUNT(*) FROM banned_users")
            return {
                "total_users": total_users,
                "total_sessions": total_sessions,
                "banned_users": banned_count
            }

    async def get_all_users(self):
        async with self.pool.acquire() as conn:
            return [row["user_id"] async for row in conn.fetch("SELECT user_id FROM users")]

    async def reset_database(self):
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS users CASCADE")
            await self.init_tables()
        logger.info("База данных сброшена")

# ========== AI ФУНКЦИИ ==========
async def generate_welcome_message():
    welcome_prompt = "Ты — виртуальный таролог. Предложи пользователю задать вопрос. Объясни, что чем подробнее и детальнее он опишет свой вопрос, тем точнее будет расклад. Не используй Markdown."
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=[{"role": "user", "content": welcome_prompt}],
                max_tokens=500,
                temperature=1
            ),
            timeout=30
        )
        return response.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        logger.error("Таймаут генерации приветствия")
        return get_default_welcome()
    except Exception as e:
        logger.error(f"Ошибка генерации приветствия: {e}")
        return get_default_welcome()

def get_default_welcome():
    return ("Добро пожаловать. Я — таролог, работающий с мудростью колоды Таро. "
            "Я готов выслушать ваш вопрос и обратиться к картам.\n\n"
            "Чтобы образы и символы заговорили с вами максимально ясно, пожалуйста, опишите вашу ситуацию или вопрос как можно подробнее. "
            "Чем больше деталей вы предоставите, тем глубже и точнее будет наше совместное путешествие к пониманию. Я жду вашего вопроса.")

async def ask_ai(question, history):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        messages.append(msg)
    messages.append({"role": "user", "content": question})
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                openai.ChatCompletion.create,
                model="deepseek-chat",
                messages=messages,
                max_tokens=2000,
                temperature=1
            ),
            timeout=60
        )
        return response.choices[0].message.content
    except asyncio.TimeoutError:
        logger.error("Таймаут AI запроса")
        return "Извините, запрос к серверу занял слишком много времени. Попробуйте позже."
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        return "Извините, произошла ошибка. Попробуйте позже."

# ========== ПЛАТЕЖИ ==========
async def send_invoice(chat_id, context, payload="cooldown_bypass"):
    if not is_payment_configured():
        return
    prices = [LabeledPrice(label="Расклад Таро", amount=PRICE)]
    provider_data = json.dumps({
        "receipt": {
            "items": [{
                "description": "Расклад Таро",
                "quantity": "1.00",
                "amount": {"value": f"{PRICE/100:.2f}", "currency": CURRENCY},
                "vat_code": 1
            }]
        }
    })
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title="Оплата расклада",
            description="Получить расклад сейчас.",
            payload=payload,
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
            provider_data=provider_data,
            need_email=True,
            send_email_to_provider=True
        )
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса: {e}")
        await context.bot.send_message(
            chat_id, "Платёжная система временно недоступна. Попробуйте позже."
        )

# ========== ОТЗЫВЫ ==========
async def ask_feedback(chat_id, context):
    keyboard = [
        [InlineKeyboardButton("📝 Оставить отзыв", callback_data="feedback_yes")],
        [InlineKeyboardButton("❌ Пропустить", callback_data="feedback_no")]
    ]
    await context.bot.send_message(
        chat_id,
        "Вы можете поделиться впечатлениями о прошедшей сессии если захотите.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========== ЗАГРУЗКА ДАННЫХ ==========
CARD_IMAGES_FILE = "card_images.json"
ENG_TO_RUS_FILE = "eng_to_rus.json"

def load_card_images():
    try:
        with open(CARD_IMAGES_FILE, 'r', encoding='utf-8') as f:
            images = json.load(f)
        logger.info(f"Загружено {len(images)} изображений карт")
        return images
    except Exception as e:
        logger.error(f"Ошибка загрузки {CARD_IMAGES_FILE}: {e}")
        return {}

def load_translations():
    try:
        with open(ENG_TO_RUS_FILE, 'r', encoding='utf-8') as f:
            translations = json.load(f)
        logger.info(f"Загружено {len(translations)} переводов")
        return translations
    except Exception as e:
        logger.error(f"Ошибка загрузки переводов {ENG_TO_RUS_FILE}: {e}")
        return {}

def translate_card_names(text: str, translations: dict) -> str:
    if not translations:
        return text
    for eng_name in sorted(translations.keys(), key=len, reverse=True):
        rus_name = translations[eng_name]
        text = text.replace(eng_name, rus_name)
    return text

# ========== ПАРСИНГ ОТВЕТА AI ==========
def parse_ai_response(answer: str):
    header_cards = "**Выпавшие карты:**"
    header_analysis = "**Разбор каждой карты:**"
    header_synthesis = "**Синтез и взаимодействие:**"

    pos_cards = answer.find(header_cards)
    pos_analysis = answer.find(header_analysis)

    if pos_cards == -1 or pos_analysis == -1:
        return None, None, None, answer

    intro = answer[:pos_cards].strip()
    cards_section = answer[pos_cards + len(header_cards):pos_analysis].strip()
    rest_full = answer[pos_analysis + len(header_analysis):]

    pos_synthesis = rest_full.find(header_synthesis)
    if pos_synthesis == -1:
        analysis_part = rest_full
        rest = ""
    else:
        analysis_part = rest_full[:pos_synthesis]
        rest = rest_full[pos_synthesis:]

    card_details = []
    lines = analysis_part.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('*   **'):
            name_start = line.find('**') + 2
            name_end = line.find('**', name_start)
            name = line[name_start:name_end].strip()
            colon_pos = line.find(':')
            if colon_pos != -1:
                first_line = line[colon_pos+1:].strip()
            else:
                first_line = line.split('**', 2)[-1].strip()
            desc_lines = [first_line] if first_line else []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('*   **'):
                if lines[i].strip():
                    desc_lines.append(lines[i].strip())
                i += 1
            description = ' '.join(desc_lines).strip()
            if description:
                card_details.append((name, description))
        else:
            i += 1

    return intro, cards_section, card_details, rest

def split_rest_sections(rest: str):
    sections = {
        'synthesis': None,
        'quintessence': None,
        'general_answer': None,
        'extra': None
    }
    headers = {
        'synthesis': '**Синтез и взаимодействие:**',
        'quintessence': '**Квинтэссенция:**',
        'general_answer': '**Общий ответ на ваш вопрос:**'
    }
    
    pos_synth = rest.find(headers['synthesis'])
    pos_quint = rest.find(headers['quintessence'])
    pos_answer = rest.find(headers['general_answer'])
    
    if pos_synth == -1:
        sections['extra'] = rest
        return sections
    
    next_pos = len(rest)
    if pos_quint != -1 and pos_quint > pos_synth:
        next_pos = pos_quint
    elif pos_answer != -1 and pos_answer > pos_synth:
        next_pos = pos_answer
    sections['synthesis'] = rest[pos_synth:next_pos].strip()
    
    if pos_quint != -1:
        next_pos = len(rest)
        if pos_answer != -1 and pos_answer > pos_quint:
            next_pos = pos_answer
        sections['quintessence'] = rest[pos_quint:next_pos].strip()
    
    if pos_answer != -1:
        sections['general_answer'] = rest[pos_answer:].strip()
    else:
        if sections['synthesis']:
            remaining = rest[pos_synth + len(headers['synthesis']):].strip()
            if remaining and not sections['quintessence'] and not sections['general_answer']:
                sections['extra'] = remaining
    
    return sections

def extract_quintessence_card_name(quintessence_text: str, translations: dict) -> str:
    if not quintessence_text or not translations:
        return None

    patterns = [
        r'соответствует аркану\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)',
        r'аркан\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)',
        r'аркану\s+[**]?([А-Яа-яё\s]+?)[**]?\s*(?:[.,;]|$)'
    ]
    for pattern in patterns:
        match = re.search(pattern, quintessence_text, re.IGNORECASE)
        if match:
            card_name_rus = match.group(1).strip()
            for eng_name, rus_name in translations.items():
                if rus_name == card_name_rus:
                    return eng_name
            for eng_name, rus_name in translations.items():
                if card_name_rus in rus_name or rus_name in card_name_rus:
                    return eng_name
            return None

    found = []
    for eng_name in translations.keys():
        if eng_name in quintessence_text:
            pos = quintessence_text.rfind(eng_name)
            found.append((pos, eng_name))
    if found:
        found.sort(key=lambda x: x[0], reverse=True)
        return found[0][1]
    return None

# ========== ОТПРАВКА ОТВЕТА AI ==========
async def send_ai_response(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str):
    intro, cards_section, card_details, rest = parse_ai_response(answer)
    translations = context.bot_data.get('translations', {})
    card_images = context.bot_data.get('card_images', {})

    if intro is None:
        if len(answer) > 4096:
            for i in range(0, len(answer), 4096):
                await update.message.reply_text(answer[i:i+4096])
        else:
            await update.message.reply_text(answer)
        return

    first_part = translate_card_names(intro, translations)
    if cards_section:
        first_part += f"\n\n**Выпавшие карты:**\n{translate_card_names(cards_section, translations)}"
    await update.message.reply_text(first_part)

    for eng_name, description in card_details:
        rus_name = translations.get(eng_name, eng_name)
        file_id = card_images.get(eng_name)
        if file_id:
            try:
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=rus_name)
            except Exception as e:
                logger.error(f"Ошибка отправки фото для {eng_name}: {e}")
        else:
            logger.warning(f"Нет изображения для карты: {eng_name}")

        if description:
            desc_translated = translate_card_names(description, translations)
            if len(desc_translated) > 4096:
                for i in range(0, len(desc_translated), 4096):
                    await update.message.reply_text(desc_translated[i:i+4096])
            else:
                await update.message.reply_text(desc_translated)

    if not rest:
        return

    sections = split_rest_sections(rest)

    if sections['synthesis']:
        synth_translated = translate_card_names(sections['synthesis'], translations)
        if len(synth_translated) > 4096:
            for i in range(0, len(synth_translated), 4096):
                await update.message.reply_text(synth_translated[i:i+4096])
        else:
            await update.message.reply_text(synth_translated)

    if sections['quintessence']:
        quint_text = sections['quintessence']
        quint_translated = translate_card_names(quint_text, translations)
        if len(quint_translated) > 4096:
            for i in range(0, len(quint_translated), 4096):
                await update.message.reply_text(quint_translated[i:i+4096])
        else:
            await update.message.reply_text(quint_translated)

        card_name = extract_quintessence_card_name(quint_text, translations)
        if card_name:
            file_id = card_images.get(card_name)
            if file_id:
                rus_name = translations.get(card_name, card_name)
                try:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=rus_name)
                except Exception as e:
                    logger.error(f"Ошибка отправки фото для квинтэссенции {card_name}: {e}")
            else:
                logger.warning(f"Нет изображения для карты квинтэссенции: {card_name}")

    if sections['general_answer']:
        answer_translated = translate_card_names(sections['general_answer'], translations)
        if len(answer_translated) > 4096:
            for i in range(0, len(answer_translated), 4096):
                await update.message.reply_text(answer_translated[i:i+4096])
        else:
            await update.message.reply_text(answer_translated)

    if sections['extra']:
        extra_translated = translate_card_names(sections['extra'], translations)
        if len(extra_translated) > 4096:
            for i in range(0, len(extra_translated), 4096):
                await update.message.reply_text(extra_translated[i:i+4096])
        else:
            await update.message.reply_text(extra_translated)

# ========== АНИМАЦИЯ ПРИ ГЕНЕРАЦИИ ==========
async def show_animation(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    """Показывает анимацию "Тасую колоду...", "Вытягиваю карты...", "Читаю символы...", пока генерируется ответ."""
    chat_id = update.effective_chat.id
    # Отправляем первое сообщение
    message = await context.bot.send_message(chat_id, "Тасую колоду...")
    await asyncio.sleep(13)
    await message.edit_text("Вытягиваю карты...")
    await asyncio.sleep(13)
    await message.edit_text("Читаю символы...")
    return message  # вернём объект сообщения, чтобы потом удалить

# ========== ЦЕНТРАЛИЗОВАННАЯ ПРОВЕРКА ==========
async def can_start_session(user_id, db, context):
    if await db.is_banned(user_id):
        return False, "banned", "Ваш доступ к боту ограничен. Если вы считаете это ошибкой, свяжитесь с администратором."

    if context.user_data.get('state') == 'awaiting_question':
        return False, "active", "У вас уже есть активная сессия. Завершите её или задайте вопрос."

    last_end = await db.get_last_session_end(user_id)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return False, "cooldown", remaining, f"🌙 Следующий расклад будет доступен через {hours} ч {minutes} мин."

    return True, None, None, None

# ========== ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добро пожаловать! Нажмите «Начать сессию», чтобы получить расклад.",
        reply_markup=START_KEYBOARD
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = AUTHOR_CHAT_ID and user_id == int(AUTHOR_CHAT_ID) if AUTHOR_CHAT_ID else False

    if is_admin:
        help_text = """
🤖 **Доступные команды:**

/start - Начать работу с ботом
/help - Показать это сообщение

**Для пользователей:**
Начать сессию - кнопка или команда /start

**Административные команды:**
/test [вопрос] - Тестовый расклад (если вопрос не указан, бот запросит его)
/stats - Показать статистику
/broadcast <текст> - Рассылка всем пользователям
/set_cooldown <часы> - Установить время кулдауна
/ban <user_id> [причина] - Забанить пользователя
/unban <user_id> - Разбанить пользователя
/resetdb - Сброс базы данных (требует подтверждения)
        """
    else:
        help_text = """
🔮 **Как пользоваться ботом:**

1. Нажмите кнопку «Начать сессию» или используйте /start
2. Если перерыв активен, нужно подождать или оплатить снятие ограничения.
3. После окончания перерыва (или если его нет) нажмите «Начать расклад» и задайте вопрос.
4. Получите расклад из трёх карт с подробным разбором.
5. После расклада включается перерыв на 24 часа.

**Команды:**
/start - Начать сессию или сбросить текущую
/help - Показать эту справку

Если у вас возникли вопросы или проблемы, свяжитесь с администратором.
        """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    await db.get_or_create_user(user_id)

    can, reason, remaining, msg = await can_start_session(user_id, db, context)
    if not can:
        if reason == "cooldown" and is_payment_configured():
            # Отправляем сообщение с оставшимся временем и инвойс
            await update.message.reply_text(
                f"{msg}\n\nВы можете сделать расклад без ожидания за {PRICE/100} {CURRENCY}."
            )
            await send_invoice(chat_id, context, payload="cooldown_bypass")
        else:
            # Другая причина (бан, активная сессия) – просто сообщаем
            await update.message.reply_text(msg, reply_markup=START_KEYBOARD)
        return

    # Кулдаун не активен – показываем описание и кнопку "Начать расклад"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Начать расклад", callback_data="start_tarot")]
    ])
    await update.message.reply_text(DESCRIPTION_TEXT, parse_mode='Markdown', reply_markup=keyboard)

async def start_tarot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    db: Database = context.bot_data['db']

    # Повторно проверим кулдаун
    can, reason, remaining, msg = await can_start_session(user_id, db, context)
    if not can:
        await query.edit_message_text(msg)
        return

    # Удаляем сообщение с описанием и кнопкой
    await query.delete_message()

    # Отправляем "Начинаем расклад..." и сразу удаляем через 2 секунды
    start_msg = await context.bot.send_message(chat_id, "Начинаем расклад...")
    await asyncio.sleep(2)
    await start_msg.delete()

    # Генерируем приветствие от AI
    if USE_AI_WELCOME:
        welcome = await generate_welcome_message()
    else:
        welcome = get_default_welcome()

    # Отправляем приветствие
    await context.bot.send_message(chat_id, welcome)

    # Устанавливаем состояние ожидания вопроса
    context.user_data['state'] = 'awaiting_question'
    context.user_data['user_id'] = user_id
    context.user_data['chat_id'] = chat_id
    context.user_data['history'] = []

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    # Проверка бана
    if await db.is_banned(user_id):
        await update.message.reply_text("Ваш доступ к боту ограничен. Если вы считаете это ошибкой, свяжитесь с администратором.")
        return

    # Обработка тестового вопроса
    if context.user_data.get('awaiting_test_question'):
        question = user_message
        context.user_data.pop('awaiting_test_question', None)
        await perform_test_spread(update, context, question)
        return

    # Обработка отзыва
    if context.user_data.get('state') == 'awaiting_feedback':
        feedback = user_message
        if AUTHOR_CHAT_ID:
            try:
                await context.bot.send_message(
                    int(AUTHOR_CHAT_ID),
                    f"📬 Новый отзыв\n\n{feedback}"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить отзыв: {e}")
        await update.message.reply_text("Спасибо за ваш отзыв!")
        context.user_data['state'] = 'idle'
        return

    # Завершение сессии
    if user_message == "Завершить сессию":
        if context.user_data.get('state') == 'awaiting_question':
            context.user_data.clear()
            context.user_data['state'] = 'idle'
            await update.message.reply_text("✨", reply_markup=START_KEYBOARD)
        else:
            await update.message.reply_text(
                "Активной сессии нет. Нажмите «Начать сессию».",
                reply_markup=START_KEYBOARD
            )
        return

    # Обработка вопроса во время сессии
    if context.user_data.get('state') == 'awaiting_question':
        if context.user_data.get('user_id') != user_id:
            await update.message.reply_text(
                "Сейчас идёт сессия другого пользователя. Подождите.",
                reply_markup=START_KEYBOARD
            )
            return

        # Запускаем анимацию и получаем объект сообщения
        animation_msg = await show_animation(update, context, user_message)

        # Запрашиваем AI
        history = context.user_data.get('history', [])
        answer = await ask_ai(user_message, history)

        # Удаляем анимацию
        try:
            await animation_msg.delete()
        except Exception as e:
            logger.error(f"Ошибка удаления анимации: {e}")

        # Сохраняем историю
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": answer})
        if len(history) > 10:
            history = history[-10:]
        context.user_data['history'] = history

        # Отправляем ответ
        await send_ai_response(update, context, answer)

        # Обновляем данные сессии
        try:
            await db.update_last_session_end(user_id)
            await db.log_session(user_id, user_message)
        except Exception as e:
            logger.error(f"Ошибка обновления данных сессии: {e}")

        # Завершаем сессию
        context.user_data.clear()
        context.user_data['state'] = 'idle'
        await update.message.reply_text("✨", reply_markup=START_KEYBOARD)
        await ask_feedback(chat_id, context)
        return

    # Если ничего не подошло
    await update.message.reply_text(
        "Сейчас нет активной сессии. Нажмите «Начать сессию».",
        reply_markup=START_KEYBOARD
    )

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "feedback_yes":
        context.user_data['state'] = 'awaiting_feedback'
        await query.edit_message_text("Пожалуйста, напишите ваш отзыв одним сообщением.⤵️")
    else:
        await query.edit_message_text("Спасибо! Если захотите оставить отзыв позже, используйте /feedback.")

# ========== ПЛАТЁЖНЫЕ ОБРАБОТЧИКИ ==========
async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_payment_configured():
        await update.pre_checkout_query.answer(ok=True)
    else:
        await update.pre_checkout_query.answer(ok=False, error_message="Платежи временно недоступны.")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    await db.get_or_create_user(user_id)

    # После оплаты сразу запускаем сессию (кулдаун игнорируется)
    # Отправляем сообщение об успехе
    await update.message.reply_text(
        "✅ Оплата прошла успешно! Начинаем сеанс.",
        reply_markup=ReplyKeyboardMarkup([["Завершить сессию"]], resize_keyboard=True)
    )

    # Удаляем старую клавиатуру? Не обязательно.
    # Далее повторяем логику start_tarot_callback: удаляем предыдущее сообщение (если есть),
    # но здесь сообщение уже новое, поэтому просто запускаем приветствие.
    start_msg = await context.bot.send_message(chat_id, "Начинаем расклад...")
    await asyncio.sleep(2)
    await start_msg.delete()

    if USE_AI_WELCOME:
        welcome = await generate_welcome_message()
    else:
        welcome = get_default_welcome()

    await context.bot.send_message(chat_id, welcome)
    context.user_data['state'] = 'awaiting_question'
    context.user_data['user_id'] = user_id
    context.user_data['chat_id'] = chat_id
    context.user_data['history'] = []
    # Клавиатура "Завершить сессию" уже отправлена

# ========== АДМИНИСТРАТИВНЫЕ КОМАНДЫ ==========
async def test_spread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if context.args:
        question = " ".join(context.args)
        await perform_test_spread(update, context, question)
    else:
        context.user_data['awaiting_test_question'] = True
        await update.message.reply_text("🧪 Введите ваш вопрос для тестового расклада:")

async def perform_test_spread(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    await update.message.reply_text(f"🧪 Тестовый расклад для вопроса:\n{question}\n\nГенерирую...")
    history = []
    answer = await ask_ai(question, history)
    await send_ai_response(update, context, answer)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    db: Database = context.bot_data['db']
    stats_data = await db.get_stats()
    text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {stats_data['total_users']}\n"
        f"🔮 Всего сессий: {stats_data['total_sessions']}\n"
        f"🚫 Забаненных: {stats_data['banned_users']}\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /broadcast <текст сообщения>")
        return

    message_text = " ".join(context.args)
    db: Database = context.bot_data['db']
    users = await db.get_all_users()

    await update.message.reply_text(f"📢 Начинаю рассылку {len(users)} пользователям...")
    success = 0
    fail = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, message_text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            fail += 1
            logger.error(f"Ошибка отправки пользователю {uid}: {e}")
    await update.message.reply_text(f"✅ Рассылка завершена. Успешно: {success}, ошибок: {fail}")

async def set_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Использование: /set_cooldown <часы>")
        return

    try:
        hours = float(context.args[0])
        global COOLDOWN_SECONDS
        COOLDOWN_SECONDS = int(hours * 3600)
        await update.message.reply_text(f"✅ Кулдаун установлен на {hours} часов ({COOLDOWN_SECONDS} секунд).")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите число часов.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /ban <user_id> [причина]")
        return

    target_id = int(context.args[0])
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
    db: Database = context.bot_data['db']
    await db.ban_user(target_id, reason)
    await update.message.reply_text(f"🚫 Пользователь {target_id} забанен.\nПричина: {reason or 'не указана'}")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHOR_CHAT_ID and user_id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /unban <user_id>")
        return

    target_id = int(context.args[0])
    db: Database = context.bot_data['db']
    await db.unban_user(target_id)
    await update.message.reply_text(f"✅ Пользователь {target_id} разбанен.")

async def resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHOR_CHAT_ID and update.effective_user.id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав для выполнения этой команды.")
        return

    context.user_data['confirm_reset'] = True
    await update.message.reply_text(
        "⚠️ ВНИМАНИЕ! Сброс базы данных удалит всех пользователей и историю.\n"
        "Для подтверждения отправьте команду /resetdb_confirm\n"
        "Для отмены ничего не делайте."
    )

async def resetdb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if AUTHOR_CHAT_ID and update.effective_user.id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return

    if not context.user_data.get('confirm_reset'):
        await update.message.reply_text("Нет запроса на сброс. Используйте /resetdb для начала.")
        return

    db: Database = context.bot_data['db']
    await update.message.reply_text("⚠️ Сброс базы данных...")
    try:
        await db.reset_database()
        await update.message.reply_text("✅ База данных успешно сброшена.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при сбросе базы: {e}")
    finally:
        context.user_data.pop('confirm_reset', None)

# ========== ЗАПУСК ==========
async def main():
    lock_fd = acquire_lock()
    try:
        logger.info("🚀 Запуск бота...")
        db = Database(DATABASE_URL)
        await db.connect()
        await db.init_tables()

        card_images = load_card_images()
        translations = load_translations()

        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.bot_data['db'] = db
        app.bot_data['card_images'] = card_images
        app.bot_data['translations'] = translations

        await app.bot.delete_webhook()
        await asyncio.sleep(1)
        webhook_info = await app.bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(f"Вебхук всё ещё установлен: {webhook_info.url}. Повторная попытка удаления...")
            await app.bot.delete_webhook()
            await asyncio.sleep(1)

        # Регистрация обработчиков
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("test", test_spread))
        app.add_handler(CommandHandler("stats", stats))
        app.add_handler(CommandHandler("broadcast", broadcast))
        app.add_handler(CommandHandler("set_cooldown", set_cooldown))
        app.add_handler(CommandHandler("ban", ban))
        app.add_handler(CommandHandler("unban", unban))
        app.add_handler(CommandHandler("resetdb", resetdb))
        app.add_handler(CommandHandler("resetdb_confirm", resetdb_confirm))
        app.add_handler(MessageHandler(filters.Regex("^Начать сессию$"), start_session))
        app.add_handler(CallbackQueryHandler(start_tarot_callback, pattern="^start_tarot$"))
        app.add_handler(PreCheckoutQueryHandler(pre_checkout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
        app.add_handler(CallbackQueryHandler(feedback_callback, pattern="^feedback_"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("✅ Бот запущен, polling активен")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()

        logger.info("🛑 Остановка...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await db.close()
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
    finally:
        release_lock(lock_fd)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

import asyncio
import time
import openai
import sys
import os
import json
import signal
from datetime import datetime
from dotenv import load_dotenv
import asyncpg
from telegram import Update, ReplyKeyboardMarkup, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)

load_dotenv()

# ===== НАСТРОЙКИ =====
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
PAYMENT_PROVIDER_TOKEN = os.getenv('PAYMENT_PROVIDER_TOKEN')
CURRENCY = os.getenv('CURRENCY', 'RUB')
PRICE = int(os.getenv('PRICE', 15000))          # цена в копейках
AUTHOR_CHAT_ID = os.getenv('AUTHOR_CHAT_ID')    # Telegram ID администратора для отзывов

# Флаги AI
USE_AI_WELCOME = os.getenv('USE_AI_WELCOME', 'True').lower() in ('true', '1', 'yes')
USE_AI_END = os.getenv('USE_AI_END', 'True').lower() in ('true', '1', 'yes')

# Включены ли платежи
PAYMENT_ENABLED = os.getenv('PAYMENT_ENABLED', 'False').lower() in ('true', '1', 'yes')

# Бесплатная первая консультация
FREE_CONSULTATION_ENABLED = os.getenv('FREE_CONSULTATION_ENABLED', 'True').lower() in ('true', '1', 'yes')

# Текст для бесплатной консультации
FREE_CONSULTATION_TEXT = (
    "✨ Я — виртуальный таролог.\n"
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
    "Вашпервый расклад — **бесплатный!**.\n\n"
    "Готовы заглянуть в себя? 🔮"
)

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ Ошибка: TELEGRAM_TOKEN или DEEPSEEK_API_KEY не найдены!")
    sys.exit(1)

if not DATABASE_URL:
    print("❌ Ошибка: DATABASE_URL не задан!")
    sys.exit(1)

# Проверка токена платежей
def is_payment_configured() -> bool:
    if not PAYMENT_ENABLED or not PAYMENT_PROVIDER_TOKEN:
        return False
    if ':' not in PAYMENT_PROVIDER_TOKEN:
        return False
    return True

if PAYMENT_ENABLED and not is_payment_configured():
    print("⚠️ PAYMENT_ENABLED = True, но PAYMENT_PROVIDER_TOKEN не задан или невалиден. Платежи будут недоступны.")
    PAYMENT_ENABLED = False

openai.api_base = "https://api.deepseek.com/v1"
openai.api_key = DEEPSEEK_API_KEY
# =====================

# ===== КОНСТАНТЫ =====
MAX_HISTORY = 30
SESSION_DURATION = 45 * 60          # 45 минут (не используется, оставлено для совместимости)
COOLDOWN_SECONDS = 12 * 60 * 60     # 24 часа

END_MESSAGE = (
    "🕊️ Сеанс завершён. Чтобы начать новый, нажмите «Начать сессию»."
)

DEFAULT_WELCOME = (
    "Здравствуйте! Задайте свой вопрос, и я вытяну для вас три карты Таро."
)

START_KEYBOARD = ReplyKeyboardMarkup([["Начать сессию"]], resize_keyboard=True)
END_KEYBOARD = ReplyKeyboardMarkup([["Завершить сессию"]], resize_keyboard=True)

# Inline-кнопка для бесплатной консультации
FREE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎁 Сделать бесплатний расклад", callback_data="free_consultation")]
])

# ===== КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ =====
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def init_tables(self):
        """Создаёт таблицы, если их нет."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    free_used BOOLEAN DEFAULT false,
                    last_session_end TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    start_time TIMESTAMPTZ NOT NULL,
                    expiry_time TIMESTAMPTZ NOT NULL,
                    status TEXT DEFAULT 'active'
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    session_id INTEGER REFERENCES sessions(session_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)

    async def get_or_create_user(self, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )

    async def is_free_used(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT free_used FROM users WHERE user_id = $1",
                user_id
            )
            return row if row is not None else False

    async def set_free_used(self, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET free_used = true WHERE user_id = $1",
                user_id
            )

    async def update_last_session_end(self, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_session_end = now() WHERE user_id = $1",
                user_id
            )

    async def get_last_session_end(self, user_id: int) -> float | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT last_session_end FROM users WHERE user_id = $1",
                user_id
            )
            if row:
                return row.timestamp()
            return None

    async def create_session(self, user_id: int, start_time: float, expiry_time: float) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO sessions (user_id, start_time, expiry_time) VALUES ($1, to_timestamp($2), to_timestamp($3)) RETURNING session_id",
                user_id, start_time, expiry_time
            )
            return row['session_id']

    async def get_active_session(self, user_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT session_id, start_time, expiry_time FROM sessions WHERE user_id = $1 AND status = 'active' AND expiry_time > now()",
                user_id
            )
            return dict(row) if row else None

    async def add_message(self, session_id: int, role: str, content: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)",
                session_id, role, content
            )

    async def get_session_history(self, session_id: int, limit: int = 30) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE session_id = $1 ORDER BY created_at ASC LIMIT $2",
                session_id, limit
            )
            return [dict(row) for row in rows]

    async def delete_session(self, session_id: int) -> None:
        """Удаляет сессию и все её сообщения (каскадно)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1",
                session_id
            )

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def split_long_message(text: str, max_length: int = 4096) -> list[str]:
    if len(text) <= max_length:
        return [text]
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        split_index = text.rfind(' ', 0, max_length)
        if split_index == -1:
            split_index = max_length
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()
    return parts

# ===== AI-ФУНКЦИИ =====
SYSTEM_PROMPT = """
Ты — профессиональный таролог, специализирующийся на колоде Райдера—Уэйта. Твоя интерпретация опирается на глубокое знание символики, изложенное в книге Эвелин Бюргер и Йоханнеса Фибиг «Символика под микроскопом». Ты понимаешь, что каждая карта имеет и положительное, и отрицательное значение, является зеркалом души кверента и может быть рассмотрена как на субъективном (внутренние процессы), так и на объективном (внешние события) уровнях.

Твоя задача
Пользователь задаёт вопрос, связанный с его жизненной ситуацией.
Ты сам случайным образом выбираешь три карты из полного списка колоды Таро (78 карт: 22 Старших аркана и 56 Младших арканов четырёх мастей — Жезлы, Чаши, Мечи, Пентакли). Используй равновероятный случайный выбор. После этого ты должен:

Назвать три выпавшие карты — указать их название (и масть для Младших арканов).

Кратко представить каждую карту — описать ключевые символы и основное значение, выделив как позитивные, так и негативные аспекты (если они уместны в контексте вопроса).

Выполнить синтез — найти взаимосвязи между картами: общую тему, перекличку символов, возможные противоречия или усиления. Объяснить, как эти карты «разговаривают» друг с другом.

Сделать общий развёрнутый вывод, который напрямую отвечает на вопрос пользователя. Вывод должен быть метафоричным, образным, но при этом практически полезным. Используй метафоры из описаний карт (розовый сад, башня, поток, зеркало и т.п.).

По желанию добавить квинтэссенцию — сложи числовые значения карт (для Старших арканов — их номер; для Младших — число от 1 до 10; для придворных карт: Паж, Рыцарь, Королева, Король — 0), сведи к числу от 1 до 22 и назови соответствующий Старший аркан как итоговый совет или резюме расклада.

Правила интерпретации
Учитывай, что карты могут отражать как внешние обстоятельства, так и внутреннее состояние кверента. Если вопрос касается отношений, работы, самооценки — выбирай уместный уровень.

Не бойся «сложных» карт (Смерть, Башня, Дьявол). Показывай их преобразующую, освобождающую сторону.

Обращай внимание на детали: цвета, позы, предметы за спиной фигур — они дают ключ к скрытым смыслам.

Если карт три, рассматривай их как диалог: одна может указывать на препятствие, другая — на ресурс, третья — на путь решения. Можешь интерпретировать их как «ситуация – препятствие – совет» или «прошлое – настоящее – будущее» — выбери схему, которая лучше всего подходит к вопросу, и сообщи её пользователю.

Будь уважителен к пользователю. Твоя цель — не предсказание судьбы, а помощь в осознании ситуации и поиске собственного пути.

Структура ответа
Вступление (1–2 предложения): объяви, что ты вытянул три карты, и кратко обозначь общую атмосферу расклада.

Перечисление выпавших карт (чётко назови их).

Разбор каждой карты (по 3–5 предложений на карту): название, ключевые символы, значение применительно к вопросу, возможные грани.

Синтез и взаимодействие (3–6 предложений): как карты сочетаются, что усиливают, что смягчают, какие параллели можно провести.

Квинтэссенция (если уместно): суммарный аркан и его краткий смысл.

Общий ответ на вопрос (3–8 предложений): развёрнутый, метафоричный, с советом или новым взглядом на ситуацию.

Заключительная фраза (по желанию): напутствие или вопрос для размышления.

Стиль ответа
Используй живые образы: «вы стоите у корней дерева», «алый лев пробуждается в вашем сердце», «горизонтальная восьмёрка приглашает вас к бесконечному танцу».

Избегай сухих перечислений. Пусть твой язык будет плавным, поэтичным, но при этом понятным.

Обращайся к пользователю на «вы» (уважительно).

Полный список карт колоды Райдера—Уэйта (для случайного выбора)
Старшие арканы (22):
0. Шут / Дурак
I. Маг
II. Верховная Жрица
III. Императрица
IV. Император
V. Иерофант
VI. Влюблённые
VII. Колесница
VIII. Сила
IX. Отшельник
X. Колесо Фортуны
XI. Правосудие
XII. Повешенный
XIII. Смерть
XIV. Умеренность
XV. Дьявол
XVI. Башня
XVII. Звезда
XVIII. Луна
XIX. Солнце
XX. Суд
XXI. Мир

Младшие арканы (56):
Жезлы (огонь, воля): Туз, 2, 3, 4, 5, 6, 7, 8, 9, 10, Паж, Рыцарь, Королева, Король.
Чаши (вода, чувства): Туз, 2, 3, 4, 5, 6, 7, 8, 9, 10, Паж, Рыцарь, Королева, Король.
Мечи (воздух, разум): Туз, 2, 3, 4, 5, 6, 7, 8, 9, 10, Паж, Рыцарь, Королева, Король.
Пентакли (земля, материя): Туз, 2, 3, 4, 5, 6, 7, 8, 9, 10, Паж, Рыцарь, Королева, Король.

Пример начала работы бота
После получения вопроса пользователя бот (ты) мысленно выбирает три случайные карты из списка, затем отвечает по шаблону:

«Я вытянул для вас три карты. Их сочетание напоминает древнюю притчу: сначала герой встречает тьму, затем находит опору, а в конце обретает свет. Давайте посмотрим, что они говорят…
Выпали: …»

Дополнительная инструкция для ИИ
При случайном выборе используй равномерное распределение: все 78 карт равновероятны.

Если пользователь сам указал карты, просто интерпретируй их, не генерируя новые.

Если пользователь задал вопрос без уточнения схемы, можешь интерпретировать три карты как «ситуация – вызов – путь» или «прошлое – настоящее – будущее», указав, какую схему ты применяешь.

Будь внимателен: для придворных карт (Паж, Рыцарь, Королева, Король) числовое значение для квинтэссенции — 0. Для Тузов — 1. Для остальных числовых карт — число от 2 до 10. Для Старших арканов — их номер (для Шута — 0).

Этот промт можно использовать в диалоге с ИИ, который поддерживает выполнение программной логики (например, ChatGPT с код-интерпретатором или API с вызовом функции random). Если же ИИ не может программно генерировать случайные числа, можно вручную указать, что он должен «вообразить» случайный выбор, опираясь на описание процесса. В текущей версии я добавил чёткий алгоритм и список всех карт, чтобы ИИ мог осмысленно произвести выбор.
"""

async def generate_welcome_message() -> str:
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Пользователь готов начать разговор. Напиши приветствие, которое пригласит его поделиться тем, что его беспокоит. Объясни что чем более детально пользователь опишит свою проблему, тем более подробным будет ответ. Сохрани свой обычный тон. Не используй Markdown, просто текст."}
        ]
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="deepseek-chat",
            messages=messages,
            max_tokens=800,
            temperature=1
        )
        welcome = response.choices[0].message.content.strip()
        return welcome
    except Exception as e:
        print(f"❌ Ошибка при генерации приветствия: {e}")
        return None

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ СТАТУСА НАБОРА =====
async def send_typing_periodically(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass

async def stop_typing(typing_task: asyncio.Task):
    if typing_task and not typing_task.done():
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

# ===== ОСНОВНЫЕ ФУНКЦИИ СЕССИИ =====
async def finish_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE, send_end_message: bool = True):
    """Завершает сессию, обновляет last_session_end, удаляет историю из БД."""
    db: Database = context.bot_data['db']
    session_id = context.user_data.get('session_id')
    user_id = context.user_data.get('user_id')
    if not session_id or not user_id:
        return

    # Обновляем время последней сессии
    await db.update_last_session_end(user_id)

    # Удаляем сессию и все сообщения
    await db.delete_session(session_id)

    # Очистка временных данных
    context.user_data.pop('session_id', None)
    context.user_data.pop('session_start_time', None)
    context.user_data.pop('user_id', None)

    # Если нужно, отправляем сообщение о завершении
    if send_end_message:
        await context.bot.send_message(chat_id, END_MESSAGE, reply_markup=START_KEYBOARD)

    # Предлагаем отзыв
    await ask_feedback(chat_id, context)

async def start_session_core(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, is_free: bool = False):
    """Запускает сессию (общая логика)."""
    db: Database = context.bot_data['db']
    # Проверяем активную сессию
    if await db.get_active_session(user_id):
        await context.bot.send_message(chat_id, "У вас уже есть активная сессия.", reply_markup=END_KEYBOARD)
        return

    start_time = time.time()
    expiry_time = start_time + (45 * 60)   # 45 минут (для совместимости)
    session_id = await db.create_session(user_id, start_time, expiry_time)

    context.user_data['session_id'] = session_id
    context.user_data['user_id'] = user_id
    context.user_data['session_start_time'] = start_time

    # Отправка приветствия
    typing_task = asyncio.create_task(send_typing_periodically(chat_id, context))
    try:
        if USE_AI_WELCOME:
            welcome_text = await generate_welcome_message()
            if not welcome_text:
                welcome_text = DEFAULT_WELCOME
        else:
            welcome_text = DEFAULT_WELCOME
    finally:
        await stop_typing(typing_task)

    await context.bot.send_message(chat_id, welcome_text, reply_markup=END_KEYBOARD)
    print(f"✅ Сессия начата для {user_id} (бесплатная: {is_free})")

async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start и кнопки «Начать сессию»."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    # Создаём пользователя, если его нет
    await db.get_or_create_user(user_id)

    # Проверка активной сессии
    if context.user_data.get('session_id'):
        await update.message.reply_text("У вас уже есть активная сессия.", reply_markup=END_KEYBOARD)
        return

    # Проверка кулдауна
    last_end = await db.get_last_session_end(user_id)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await update.message.reply_text(
            f"🌙 Картам нужно время, чтобы их образы улеглись в душе. "
            f"Следующий расклад будет доступен через {hours_left} ч {minutes_left} мин. "
            f"Приходите позже — мудрость не терпит спешки. ",
            reply_markup=START_KEYBOARD
        )
        return

    # Бесплатная консультация
    if FREE_CONSULTATION_ENABLED:
        free_used = await db.is_free_used(user_id)
        if not free_used:
            # Показываем описание бесплатной консультации и кнопку
            await update.message.reply_text(FREE_CONSULTATION_TEXT, parse_mode='Markdown', reply_markup=FREE_KEYBOARD)
            return

    # Если бесплатная уже использована или отключена – переходим к оплате
    if is_payment_configured():
        # Отправляем инвойс
        service_text = (
            "✨ Я — виртуальный таролог.\n"
            "Я работаю с классической колодой Райдера—Уэйта и глубокой символикой.\n\n"
            "Как проходит сеанс:\n"
            "1. Вы задаёте любой вопрос (отношения, работа, выбор, саморазвитие).\n"
            "2. Я вытягиваю для вас 3 случайные карты Таро.\n"
            "3. Показываю изображения каждой карты.\n"
            "4. Даю подробный разбор:\n"
            "• значение каждой карты (символы, детали, смысл)\n"
            "• их взаимодействие друг с другом\n"
            "• общий синтез — ответ на ваш вопрос\n"
            "5. При необходимости — итоговая карта-квинтэссенция.\n\n"
            "Карты — не предсказание, а зеркало вашей души.\n"
            "Я помогаю увидеть скрытые грани ситуации, найти ресурсы и принять осознанное решение.\n\n"
            f"💰 Стоимость одного расклада — {PRICE/100} {CURRENCY}\n\n"
            "Сразу после оплаты вы сможете задать свой вопрос.\n\n"
            "Готовы заглянуть в себя? 🔮"
        )
        await update.message.reply_text(service_text, parse_mode='Markdown')
        await send_invoice(chat_id, context)
    else:
        # Платежи отключены или токен не задан – стартуем сессию сразу
        await start_session_core(chat_id, user_id, context, is_free=False)

async def reset_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает базу данных (удаляет все таблицы и создаёт заново). Доступно только администратору."""
    if AUTHOR_CHAT_ID and update.effective_user.id != int(AUTHOR_CHAT_ID):
        await update.message.reply_text("⛔ Недостаточно прав для выполнения этой команды.")
        return

    db: Database = context.bot_data['db']
    await update.message.reply_text("⚠️ Сброс базы данных... Это удалит все данные пользователей, сессий и сообщений.")

    try:
        async with db.pool.acquire() as conn:
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
            await conn.execute("GRANT ALL ON SCHEMA public TO public")
        await db.init_tables()
        await update.message.reply_text("✅ База данных успешно сброшена и таблицы пересозданы.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при сбросе базы: {e}")

async def free_consultation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на кнопку бесплатной консультации."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    db: Database = context.bot_data['db']

    free_used = await db.is_free_used(user_id)
    if free_used:
        await query.edit_message_text("Вы уже использовали бесплатную консультацию. Следующие сессии – платные.")
        return

    if context.user_data.get('session_id'):
        await query.edit_message_text("У вас уже есть активная сессия. Завершите её перед началом новой.")
        return

    last_end = await db.get_last_session_end(user_id)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await query.edit_message_text(
            f"🌙 Картам нужно время, чтобы их образы улеглись в душе. "
            f"Следующий расклад будет доступен через {hours_left} ч {minutes_left} мин. "
            f"Приходите позже — мудрость не терпит спешки. "
        )
        return

    await db.set_free_used(user_id)
    await query.edit_message_text("Начинаем бесплатную консультацию...")
    await start_session_core(chat_id, user_id, context, is_free=True)

# ===== ПЛАТЕЖИ =====
async def send_invoice(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_payment_configured():
        return
    prices = [LabeledPrice(label="Сессия (45 мин)", amount=PRICE)]
    provider_data = json.dumps({
        "receipt": {
            "items": [{
                "description": "Консультация (45 минут)",
                "quantity": "1.00",
                "amount": {"value": f"{PRICE/100:.2f}", "currency": CURRENCY},
                "vat_code": 1
            }]
        }
    })
    try:
        invoice_message = await context.bot.send_invoice(
            chat_id=chat_id,
            title="Оплата сессии",
            description="Одна консультация (45 минут).",
            payload="session_payment",
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
            provider_data=provider_data,
            need_email=True,
            send_email_to_provider=True
        )
        return invoice_message
    except Exception as e:
        print(f"❌ Ошибка при отправке инвойса: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Извините, платёжная система временно недоступна. Попробуйте позже или обратитесь к администратору."
        )
        return None

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_payment_configured():
        await update.message.reply_text("Платёжные функции отключены администратором.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    if FREE_CONSULTATION_ENABLED:
        free_used = await db.is_free_used(user_id)
        if not free_used:
            await update.message.reply_text(FREE_CONSULTATION_TEXT, parse_mode='Markdown', reply_markup=FREE_KEYBOARD)
            return

    if context.user_data.get('session_id'):
        await update.message.reply_text("У вас уже есть активная сессия.", reply_markup=END_KEYBOARD)
        return

    last_end = await db.get_last_session_end(user_id)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await update.message.reply_text(
            f"🌙 Картам нужно время, чтобы их образы улеглись в душе. "
            f"Следующий расклад будет доступен через {hours_left} ч {minutes_left} мин. "
            f"Приходите позже — мудрость не терпит спешки. "
        )
        return

    await send_invoice(chat_id, context)

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_payment_configured():
        await update.pre_checkout_query.answer(ok=True)
    else:
        await update.pre_checkout_query.answer(ok=False, error_message="Платежи временно недоступны.")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_payment_configured():
        await update.message.reply_text("Платежи отключены, сессия не может быть начата.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db: Database = context.bot_data['db']

    service_msg_id = context.user_data.get('service_message_id')
    if service_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=service_msg_id)
        except:
            pass
        context.user_data.pop('service_message_id', None)
    invoice_msg_id = context.user_data.get('invoice_message_id')
    if invoice_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=invoice_msg_id)
        except:
            pass
        context.user_data.pop('invoice_message_id', None)

    await update.message.reply_text("✅ Оплата прошла успешно! Сейчас начнём сессию.", reply_markup=END_KEYBOARD)
    await start_session_core(chat_id, user_id, context, is_free=False)

# ===== ОТЗЫВЫ =====
async def ask_feedback(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 Оставить отзыв", callback_data="feedback_yes")],
        [InlineKeyboardButton("❌ Пропустить", callback_data="feedback_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Вы можете оставить отзыв о прошедшей сессии, если захотите.",
        reply_markup=reply_markup
    )

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "feedback_yes":
        context.user_data['awaiting_feedback'] = True
        await query.edit_message_text("Пожалуйста, напишите Ваш отзыв одним сообщением. ⤵️")
    else:
        await query.edit_message_text("Если захотите оставить отзыв позже, просто напишите /feedback.")

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_feedback(update.effective_chat.id, context)

async def view_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Просмотр отзывов отключён (хранение не ведётся).")

# ===== ОБРАБОТЧИК СООБЩЕНИЙ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    db: Database = context.bot_data['db']
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Проверка на ожидание отзыва
    if context.user_data.get('awaiting_feedback'):
        feedback_text = user_message
        username = update.effective_user.username or "без имени"
        if AUTHOR_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(AUTHOR_CHAT_ID),
                    text=f"📬 Новый отзыв\n\n{feedback_text}"
                )
            except Exception as e:
                print(f"Не удалось отправить отзыв автору: {e}")
        await update.message.reply_text("Спасибо за ваш отзыв!", reply_markup=START_KEYBOARD)
        context.user_data['awaiting_feedback'] = False
        return

    # Обработка кнопок
    if user_message == "Начать сессию":
        await start_session(update, context)
        return
    if user_message == "Завершить сессию":
        await end(update, context)
        return

    # Проверка активной сессии
    session_id = context.user_data.get('session_id')
    if not session_id:
        await update.message.reply_text("Сейчас нет активной сессии. Нажмите «Начать сессию».", reply_markup=START_KEYBOARD)
        return

    # Добавляем сообщение пользователя в БД
    await db.add_message(session_id, 'user', user_message)

    # Получаем историю из БД
    history = await db.get_session_history(session_id, limit=MAX_HISTORY*2)

    # Формируем запрос к AI
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        messages.append({"role": msg['role'], "content": msg['content']})

    typing_task = asyncio.create_task(send_typing_periodically(chat_id, context))
    context.user_data['typing_task'] = typing_task
    try:
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="deepseek-chat",
            messages=messages,
            max_tokens=2500,
            temperature=1
        )
        clean_reply = response.choices[0].message.content
        # Сохраняем ответ
        await db.add_message(session_id, 'assistant', clean_reply)
        # Отправляем пользователю
        parts = split_long_message(clean_reply)
        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, reply_markup=END_KEYBOARD)
            else:
                await update.message.reply_text(part)
    except Exception as e:
        print(f"❌ Ошибка при запросе к DeepSeek: {e}")
        await update.message.reply_text("Извините, произошла техническая ошибка. Попробуйте позже.", reply_markup=END_KEYBOARD)
    finally:
        await stop_typing(typing_task)
        context.user_data.pop('typing_task', None)

    # Завершаем сессию после ответа бота (без дополнительного сообщения о завершении,
    # потому что finish_session уже отправит END_MESSAGE)
    await finish_session(chat_id, context, send_end_message=True)

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await finish_session(chat_id, context, send_end_message=True)

# ===== ЗАПУСК =====
async def main():
    print("🚀 Запуск бота...")
    db = Database(DATABASE_URL)
    await db.connect()
    await db.init_tables()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data['db'] = db

    app.add_handler(CommandHandler("start", start_session))
    app.add_handler(CommandHandler("end", end))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("view_feedback", view_feedback))
    app.add_handler(CommandHandler("resetdb", reset_db))
    if PAYMENT_ENABLED:
        app.add_handler(CommandHandler("buy", buy))
        app.add_handler(PreCheckoutQueryHandler(pre_checkout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    app.add_handler(CallbackQueryHandler(free_consultation_callback, pattern="^free_consultation$"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern="^feedback_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Обработчики добавлены")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("✅ Бот запущен и ожидает сообщения")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    print("🛑 Остановка бота...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await db.close()
    print("✅ Бот остановлен")

if __name__ == "__main__":
    import asyncio
    import signal

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        asyncio.run(main())
    else:
        loop.create_task(main())

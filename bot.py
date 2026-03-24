import asyncio
import time
import openai
import sys
import os
import json
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)

# Загружаем переменные окружения
load_dotenv()

# ===== НАСТРОЙКИ =====
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
PAYMENT_PROVIDER_TOKEN = os.getenv('PAYMENT_PROVIDER_TOKEN')
CURRENCY = os.getenv('CURRENCY', 'RUB')
PRICE = int(os.getenv('PRICE', 15000))  # цена в копейках
AUTHOR_CHAT_ID = os.getenv('AUTHOR_CHAT_ID')  # Telegram ID администратора для отзывов

# Флаг: использовать AI для генерации приветствия (True) или стандартный текст (False)
USE_AI_WELCOME = os.getenv('USE_AI_WELCOME', 'True').lower() in ('true', '1', 'yes')

# Флаг: включить платёжные функции (True/False)
PAYMENT_ENABLED = os.getenv('PAYMENT_ENABLED', 'False').lower() in ('true', '1', 'yes')

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ Ошибка: TELEGRAM_TOKEN или DEEPSEEK_API_KEY не найдены!")
    sys.exit(1)
else:
    print("✅ Переменные окружения загружены.")

if PAYMENT_ENABLED and not PAYMENT_PROVIDER_TOKEN:
    print("⚠️ PAYMENT_ENABLED = True, но PAYMENT_PROVIDER_TOKEN не задан. Платежи будут недоступны.")
    PAYMENT_ENABLED = False

openai.api_base = "https://api.deepseek.com/v1"
openai.api_key = DEEPSEEK_API_KEY
# =====================

# ===== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ =====
DB_PATH = "bot_data.db"

def init_db():
    """Создаёт таблицы users и feedback."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Таблица users
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_session_end REAL DEFAULT 0,
                free_session_used INTEGER DEFAULT 0
            )
        ''')
        try:
            c.execute("ALTER TABLE users ADD COLUMN free_session_used INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже существует

        # Таблица feedback
        c.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                timestamp REAL
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")

init_db()

def get_last_session_end(user_id: int) -> float:
    """Возвращает время последней сессии пользователя."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT last_session_end FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"❌ Ошибка чтения из БД для user {user_id}: {e}")
        return 0

def save_last_session_end(user_id: int, last_session_end: float):
    """Сохраняет время последней сессии пользователя."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            INSERT INTO users (user_id, last_session_end)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_session_end = excluded.last_session_end
        ''', (user_id, last_session_end))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Ошибка записи в БД для user {user_id}: {e}")

def get_free_session_used(user_id: int) -> bool:
    """Возвращает True, если бесплатная сессия уже использована (только если PAYMENT_ENABLED)."""
    if not PAYMENT_ENABLED:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT free_session_used FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] == 1 if row else False
    except Exception as e:
        print(f"❌ Ошибка чтения free_session_used для user {user_id}: {e}")
        return False

def set_free_session_used(user_id: int, used: bool = True):
    """Устанавливает флаг использования бесплатной сессии (только если PAYMENT_ENABLED)."""
    if not PAYMENT_ENABLED:
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            INSERT INTO users (user_id, free_session_used)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                free_session_used = excluded.free_session_used
        ''', (user_id, 1 if used else 0))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Ошибка записи free_session_used для user {user_id}: {e}")

def save_feedback(user_id: int, username: str, text: str):
    """Сохраняет отзыв в БД."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute('''
            INSERT INTO feedback (user_id, username, text, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, text, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Ошибка сохранения отзыва: {e}")

def get_feedbacks(limit: int = 10) -> list:
    """Возвращает последние limit отзывов."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            SELECT user_id, username, text, timestamp
            FROM feedback
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"❌ Ошибка чтения отзывов: {e}")
        return []

async def ensure_user_data(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Загружает last_session_end из БД в context.user_data, если его там нет."""
    if 'last_session_end' not in context.user_data:
        context.user_data['last_session_end'] = get_last_session_end(user_id)
# ======================================

# ===== ЗАГРУЗКА ПРОМПТА =====
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

MAX_HISTORY = 30
COOLDOWN_SECONDS = 1 * 60  # 1 минута (для теста; можно увеличить до 15*60)

END_MESSAGE = (
    "🕊️"
)

DEFAULT_WELCOME = (
)

START_KEYBOARD = ReplyKeyboardMarkup([["Начать сессию"]], resize_keyboard=True)
END_KEYBOARD = ReplyKeyboardMarkup([["Завершить сессию"]], resize_keyboard=True)


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


async def generate_welcome_message() -> str:
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Пользователь готов задать свой вопрос. Напиши приветствие, которое пригласит его задать свой вопрос. Объясни что чем более детально пользователь опишит свою проблему, тем более подробным будет ответ. Сохрани свой обычный тон. Не используй Markdown, просто текст."}
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


async def send_typing_periodically(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except:
                break
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

async def cleanup_session(context: ContextTypes.DEFAULT_TYPE, clear_history: bool = True, chat_id: int = None):
    typing_task = context.user_data.get('typing_task')
    if typing_task and not typing_task.done():
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
    if clear_history:
        context.user_data['history'] = []
    context.user_data.pop('typing_task', None)
    context.user_data.pop('session_start_time', None)

async def finish_session(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, send_end_message: bool = True):
    """Завершает сессию: очищает данные, сохраняет время окончания, отправляет прощальное сообщение и спрашивает отзыв."""
    if 'session_start_time' not in context.user_data:
        # Сессия уже завершена
        return

    await cleanup_session(context, clear_history=True, chat_id=chat_id)

    now = time.time()
    context.user_data['last_session_end'] = now
    save_last_session_end(user_id, now)

    if send_end_message:
        await context.bot.send_message(chat_id, END_MESSAGE, reply_markup=START_KEYBOARD)

    await ask_feedback(chat_id, context)


# Функции для отзывов
async def ask_feedback(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 Оставить отзыв", callback_data="feedback_yes")],
        [InlineKeyboardButton("❌ Пропустить", callback_data="feedback_no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Спасибо Вам за разговор. Вы можете оставить отзыв о прошедшей сессии если захотите.",
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
    user_id = update.effective_user.id
    ADMIN_ID = 928589977  # Замените на свой
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для просмотра отзывов.")
        return
    feedbacks = get_feedbacks(limit=10)
    if not feedbacks:
        await update.message.reply_text("Пока нет отзывов.")
        return
    message_lines = ["📋 **Последние 10 отзывов**:\n"]
    for fb in feedbacks:
        user_id, username, text, ts = fb
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        message_lines.append(f"⏱️ {dt}\n💬 {text}\n")
        message_lines.append("-" * 30)
    full_message = "\n".join(message_lines)
    parts = split_long_message(full_message)
    for part in parts:
        await update.message.reply_text(part, parse_mode='Markdown')


# Платёжные функции (только если PAYMENT_ENABLED)
async def send_invoice(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if not PAYMENT_ENABLED or not PAYMENT_PROVIDER_TOKEN:
        return None
    prices = [LabeledPrice(label="Сессия (40 мин)", amount=PRICE)]
    provider_data = json.dumps({
        "receipt": {
            "items": [{
                "description": "Консультация (40 минут)",
                "quantity": "1.00",
                "amount": {"value": f"{PRICE/100:.2f}", "currency": CURRENCY},
                "vat_code": 1
            }]
        }
    })
    invoice_message = await context.bot.send_invoice(
        chat_id=chat_id,
        title="Оплата сессии",
        description="Одна консультация (40 минут).",
        payload="session_payment",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=prices,
        provider_data=provider_data,
        need_email=True,
        send_email_to_provider=True
    )
    return invoice_message

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PAYMENT_ENABLED:
        await update.message.reply_text("Платёжные функции отключены администратором.")
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await ensure_user_data(context, user_id)
    if 'session_start_time' in context.user_data:
        await context.bot.send_message(chat_id, "У вас уже есть активная сессия.", reply_markup=END_KEYBOARD)
        return
    last_end = context.user_data.get('last_session_end', 0)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await context.bot.send_message(
            chat_id,
            f"🌙 Картам нужно время, чтобы их образы улеглись в душе. "
            f"Следующий расклад будет доступен через {hours_left} ч {minutes_left} мин. "
            f"Приходите позже — мудрость не терпит спешки. ",
            reply_markup=START_KEYBOARD
        )
        return
    invoice_message = await send_invoice(chat_id, context)
    if invoice_message:
        context.user_data['invoice_message_id'] = invoice_message.message_id

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if PAYMENT_ENABLED:
        await update.pre_checkout_query.answer(ok=True)
    else:
        await update.pre_checkout_query.answer(ok=False, error_message="Платежи временно недоступны.")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PAYMENT_ENABLED:
        await update.message.reply_text("Платежи отключены, сессия не может быть начата.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
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
    await ensure_user_data(context, user_id)
    if 'session_start_time' in context.user_data:
        await update.message.reply_text("У вас уже есть активная сессия.")
        return
    last_end = context.user_data.get('last_session_end', 0)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await update.message.reply_text(
            f"К сожалению, кулдаун ещё не прошёл. Подождите {hours_left} ч {minutes_left} мин.",
            reply_markup=START_KEYBOARD
        )
        return
    await start_session_core(chat_id, user_id, context)


# Функция запуска сессии (общая)
async def start_session_core(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_data(context, user_id)
    context.user_data['user_id'] = user_id
    context.user_data['history'] = []
    context.user_data['session_start_time'] = time.time()

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
    print(f"✅ Сессия начата.")


# Основная функция старта сессии
async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🟢 Запуск start_session")
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await ensure_user_data(context, user_id)

    # Если уже есть активная сессия
    if 'session_start_time' in context.user_data:
        await update.message.reply_text(
            "У вас уже есть активная сессия. Завершите её командой /end или кнопкой.",
            reply_markup=END_KEYBOARD
        )
        return

    # Проверка кулдауна
    last_end = context.user_data.get('last_session_end', 0)
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

    # Если платежи отключены — запускаем сессию сразу
    if not PAYMENT_ENABLED:
        await start_session_core(chat_id, user_id, context)
        return

    # Иначе — логика с бесплатной/платной сессией
    free_available = not get_free_session_used(user_id)
    if free_available:
        keyboard = [[InlineKeyboardButton("🎁 Начать бесплатную сессию", callback_data="free_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
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
            "Вашпервый расклад — **бесплатный!**.\n\n"
            "Готовы заглянуть в себя? 🔮",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
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
            "💰 Стоимость одного расклада — 200 рублей.\n"
            "Сразу после оплаты вы сможете задать свой вопрос.\n\n"
            "Готовы заглянуть в себя? 🔮"
        )
        service_msg = await update.message.reply_text(service_text, parse_mode='Markdown')
        context.user_data['service_message_id'] = service_msg.message_id
        invoice_message = await send_invoice(chat_id, context)
        if invoice_message:
            context.user_data['invoice_message_id'] = invoice_message.message_id
        else:
            await update.message.reply_text("Платёжный сервис временно недоступен. Попробуйте позже.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("📨 Получена команда /start")
    await start_session(update, context)


async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🔚 Получена команда /end")
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await ensure_user_data(context, user_id)
    if 'session_start_time' not in context.user_data:
        await update.message.reply_text("Сейчас нет активной сессии.", reply_markup=START_KEYBOARD)
        return

    await finish_session(chat_id, user_id, context, send_end_message=True)


async def free_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Начать бесплатную сессию' (используется только при PAYMENT_ENABLED)."""
    if not PAYMENT_ENABLED:
        # Если платежи отключены, эта кнопка не показывается, но на всякий случай
        await update.callback_query.answer("Функция недоступна.")
        return
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    if get_free_session_used(user_id):
        await query.edit_message_text("Бесплатная сессия уже была использована.")
        return
    if 'session_start_time' in context.user_data:
        await query.edit_message_text("У вас уже есть активная сессия.")
        return
    last_end = get_last_session_end(user_id)
    if last_end and (time.time() - last_end) < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (time.time() - last_end)
        hours_left = int(remaining // 3600)
        minutes_left = int((remaining % 3600) // 60)
        await query.edit_message_text(
            f"К сожалению, кулдаун ещё не прошёл. Подождите {hours_left} ч {minutes_left} мин."
        )
        return
    await query.delete_message()
    set_free_session_used(user_id, True)
    await start_session_core(chat_id, user_id, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text

    if context.user_data.get('awaiting_feedback'):
        feedback_text = user_message
        user_id = update.effective_user.id
        username = update.effective_user.username or "без имени"
        save_feedback(user_id, username, feedback_text)
        if AUTHOR_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=int(AUTHOR_CHAT_ID), text=f"📬 Новый отзыв:\n\n{feedback_text}")
            except Exception as e:
                print(f"Не удалось отправить отзыв автору: {e}")
        await update.message.reply_text("Спасибо за ваш отзыв!", reply_markup=START_KEYBOARD)
        context.user_data['awaiting_feedback'] = False
        return

    if user_message == "Начать сессию":
        await start_session(update, context)
        return

    if user_message == "Завершить сессию":
        await end(update, context)
        return

    if 'session_start_time' not in context.user_data:
        await update.message.reply_text("Сейчас нет активной сессии. Нажмите «Начать сессию».", reply_markup=START_KEYBOARD)
        return

    typing_task = asyncio.create_task(send_typing_periodically(update.effective_chat.id, context))
    context.user_data['typing_task'] = typing_task

    if 'history' not in context.user_data:
        context.user_data['history'] = []
    context.user_data['history'].append({"role": "user", "content": user_message})
    if len(context.user_data['history']) > MAX_HISTORY * 2:
        context.user_data['history'] = context.user_data['history'][-MAX_HISTORY*2:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + context.user_data['history']

    try:
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="deepseek-chat",
            messages=messages,
            max_tokens=2000,
            temperature=1
        )
        clean_reply = response.choices[0].message.content
        context.user_data['history'].append({"role": "assistant", "content": clean_reply})
        if len(context.user_data['history']) > MAX_HISTORY * 2:
            context.user_data['history'] = context.user_data['history'][-MAX_HISTORY*2:]

        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        context.user_data.pop('typing_task', None)

        parts = split_long_message(clean_reply)
        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, reply_markup=END_KEYBOARD)
            else:
                await update.message.reply_text(part)

        # Завершаем сессию после ответа на первый вопрос
        await finish_session(update.effective_chat.id, update.effective_user.id, context, send_end_message=True)

    except Exception as e:
        print(f"❌ Ошибка при запросе к DeepSeek: {e}")
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        context.user_data.pop('typing_task', None)
        error_message = "Извините, произошла техническая ошибка. Пожалуйста, попробуйте позже."
        await update.message.reply_text(error_message, reply_markup=END_KEYBOARD)
        # Даже при ошибке завершаем сессию, чтобы пользователь мог начать новую
        await finish_session(update.effective_chat.id, update.effective_user.id, context, send_end_message=False)


def main():
    print("🚀 Запуск бота...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("end", end))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("view_feedback", view_feedback))

    if PAYMENT_ENABLED:
        app.add_handler(CommandHandler("buy", buy))
        app.add_handler(PreCheckoutQueryHandler(pre_checkout))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Обработчики inline-кнопок
    app.add_handler(CallbackQueryHandler(free_start_callback, pattern="^free_start$"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern="^feedback_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Обработчики добавлены")
    app.run_polling(timeout=50, drop_pending_updates=True)


if __name__ == "__main__":
    main()

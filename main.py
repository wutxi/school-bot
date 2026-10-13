import os
import asyncio
import datetime
import sqlite3
import json
import base64
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= НАСТРОЙКИ (ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ / БАЗЫ ДАННЫХ) =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
USER_ID = int(os.getenv("USER_ID", "0"))

LATITUDE = float(os.getenv("LATITUDE", "55.83"))
LONGITUDE = float(os.getenv("LONGITUDE", "37.33"))
CITY_NAME = os.getenv("CITY_NAME", "Красногорск")

MORNING_HOUR = 7
MORNING_MINUTE = 15

# Резервное статическое расписание
BACKUP_SCHEDULE = {
    0: {  # Понедельник
        "lessons_count": 6,
        "start": "09:25",
        "end": "14:55",
        "lessons": [
            "1. 09:25–10:05: Иностранный (английский) язык (каб. 406)",
            "2. 10:20–11:00: Химия (каб. 413)",
            "3. 11:15–11:55: Русский язык (каб. 312)",
            "4. 12:15–12:55: География (каб. 403)",
            "5. 13:15–13:55: Алгебра (каб. 205)",
            "6. 14:15–14:55: Литература (каб. 312)"
        ]
    },
    1: {  # Вторник
        "lessons_count": 6,
        "start": "09:25",
        "end": "14:55",
        "lessons": [
            "2. 09:25–10:05: Труд (технология) (каб. 104)",
            "3. 10:20–11:00: Русский язык (каб. 312)",
            "4. 11:15–11:55: Алгебра (каб. 205)",
            "5. 12:15–12:55: Физика (каб. 102)",
            "6. 13:15–13:55: История (каб. 109)",
            "7. 14:15–14:55: Россия - мои горизонты (каб. т1)"
        ]
    },
    2: {  # Среда
        "lessons_count": 7,
        "start": "08:30",
        "end": "14:55",
        "lessons": [
            "4. 11:15–11:55: Физика (каб. 102)",
            "5. 12:15–12:55: Химия (каб. 413)",
            "6. 13:15–13:55: Русский язык (каб. 312)",
            "7. 14:15–14:55: Литература (каб. 312)"
        ]
    },
    3: {  # Четверг
        "lessons_count": 8,
        "start": "08:30",
        "end": "15:45",
        "lessons": [
            "1. 08:30–09:10: История (каб. 109)",
            "2. 09:25–10:05: Физкультура (каб. т1)",
            "3. 10:20–11:00: Обществознание (каб. 103)",
            "4. 11:15–11:55: География (каб. 403)",
            "5. 12:15–12:55: Геометрия (каб. 205)",
            "6. 13:15–13:55: Английский язык (каб. 406)",
            "7. 14:15–14:55: Литература (каб. 312)",
            "8. 15:05–15:45: Россия - мои горизонты (каб. т1)"
        ]
    },
    4: {  # Пятница
        "lessons_count": 7,
        "start": "08:30",
        "end": "14:55",
        "lessons": ["Расписание уточняется"]
    }
}

# ================= БАЗА ДАННЫХ =================
conn = sqlite3.connect("todos.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    task TEXT,
    done INTEGER DEFAULT 0
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS known_marks (
    mark_id INTEGER PRIMARY KEY,
    created_at TEXT
)
""")
conn.commit()


def get_config(key: str, default: str = "") -> str:
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    return default


def set_config(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value.strip()))
    conn.commit()


def get_saved_user_id() -> int:
    val = get_config("user_id")
    if val and val.isdigit():
        return int(val)
    return USER_ID


def save_user_id(uid: int):
    if uid:
        set_config("user_id", str(uid))


def get_token() -> str:
    return get_config("mosreg_token", os.getenv("MOSREG_TOKEN", ""))


def get_student_id() -> str:
    return get_config("student_id", os.getenv("MOSREG_STUDENT_ID", "3542105"))


def is_mark_known(mark_id: int) -> bool:
    cursor.execute("SELECT 1 FROM known_marks WHERE mark_id = ?", (mark_id,))
    return cursor.fetchone() is not None


def add_known_mark(mark_id: int, created_at: str = ""):
    cursor.execute("INSERT OR IGNORE INTO known_marks (mark_id, created_at) VALUES (?, ?)", (mark_id, created_at))
    conn.commit()


def get_token_exp_datetime(token_str: str):
    try:
        parts = token_str.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            if "exp" in decoded:
                return datetime.datetime.fromtimestamp(decoded["exp"])
    except Exception as e:
        print("Error parsing JWT:", e)
    return None


def get_todos(user_id):
    cursor.execute("SELECT id, task FROM todos WHERE user_id = ? AND done = 0", (user_id,))
    return cursor.fetchall()


def add_todo(user_id, task):
    cursor.execute("INSERT INTO todos (user_id, task) VALUES (?, ?)", (user_id, task))
    conn.commit()


def mark_done(todo_id):
    cursor.execute("UPDATE todos SET done = 1 WHERE id = ?", (todo_id,))
    conn.commit()


# ================= ПОЛУЧЕНИЕ ДАННЫХ ИЗ МОСРЕГ =================
async def check_mosreg_health():
    token = get_token()
    student_id = get_student_id()
    if not token:
        return False, "Токен Мосрег ещё не задан! Отправь его в чат."
        
    today = datetime.date.today()
    url = f"https://authedu.mosreg.ru/api/family/web/v1/schedule?student_id={student_id}&date={today.isoformat()}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token}",
        "profile-id": student_id,
        "profile-type": "student",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-mes-roleid": "1",
        "x-mes-subsystem": "familyweb"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return True, "OK"
                elif resp.status in (401, 403):
                    return False, f"Токен истёк или недействителен (HTTP {resp.status})"
                else:
                    return False, f"Ошибка сервера Мосрег (HTTP {resp.status})"
    except Exception as e:
        return False, f"Ошибка соединения: {e}"


async def fetch_mosreg_schedule(target_date: datetime.date):
    token = get_token()
    student_id = get_student_id()
    
    if token:
        url = f"https://authedu.mosreg.ru/api/family/web/v1/schedule?student_id={student_id}&date={target_date.isoformat()}"
        headers = {
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {token}",
            "profile-id": student_id,
            "profile-type": "student",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "x-mes-roleid": "1",
            "x-mes-subsystem": "familyweb"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        activities = data.get("activities", [])
                        lessons = []
                        start_time = None
                        end_time = None
                        
                        for act in activities:
                            if act.get("type") == "LESSON":
                                lesson = act.get("lesson") or {}
                                sub = lesson.get("subject_name", "Урок")
                                info = act.get("info", "")
                                room = act.get("room_number")
                                b_time = act.get("begin_time", "")
                                e_time = act.get("end_time", "")
                                
                                if not start_time and b_time:
                                    start_time = b_time
                                if e_time:
                                    end_time = e_time
                                    
                                room_str = f" (каб. {room})" if room else ""
                                line = f"{info}: {sub}{room_str}" if info else f"{sub}{room_str}"
                                if b_time and e_time:
                                    line = f"{b_time}–{e_time} | {line}"
                                
                                hw = lesson.get("homework")
                                if hw:
                                    line += f"\n     📌 <i>ДЗ: {hw}</i>"
                                    
                                lessons.append(line)
                                
                        if lessons:
                            return {
                                "lessons_count": len(lessons),
                                "start": start_time or "—",
                                "end": end_time or "—",
                                "lessons": lessons,
                                "is_online": True
                            }
        except Exception as e:
            print(f"Mosreg fetch failed: {e}")
        
    # Фоллбек на статическое расписание
    weekday = target_date.weekday()
    if weekday in BACKUP_SCHEDULE:
        res = dict(BACKUP_SCHEDULE[weekday])
        res["is_online"] = False
        return res
    return None


async def fetch_subjects_map(session, headers, student_id):
    """Словарь subject_id -> subject_name из subject_marks"""
    try:
        url = f"https://authedu.mosreg.ru/api/family/web/v1/subject_marks?student_id={student_id}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                mapping = {}
                for item in data.get("payload", []):
                    sid = item.get("subject_id")
                    sname = item.get("subject_name")
                    if sid and sname:
                        mapping[sid] = sname
                return mapping
    except Exception as e:
        print("Error fetching subjects map:", e)
    return {}


async def check_new_marks_job(bot_instance: Bot):
    """Фоновая проверка новых оценок в Мосрег раз в 5 минут"""
    token = get_token()
    student_id = get_student_id()
    target_uid = get_saved_user_id()
    if not token or not target_uid:
        return

    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token}",
        "profile-id": student_id,
        "profile-type": "student",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-mes-roleid": "1",
        "x-mes-subsystem": "familyweb"
    }
    url = f"https://authedu.mosreg.ru/api/ej/core/family/v1/marks?student_profile_id={student_id}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    marks = await resp.json()
                    if not isinstance(marks, list):
                        return
                    
                    # Проверяем, есть ли уже записи в known_marks
                    cursor.execute("SELECT count(*) FROM known_marks")
                    count = cursor.fetchone()[0]
                    if count == 0:
                        # Первый запуск — просто запоминаем существующие оценки, чтобы не спамить
                        for m in marks:
                            add_known_mark(m.get("id"), m.get("created_at", ""))
                        return

                    subjects_map = None

                    # Ищем новые оценки
                    for m in marks:
                        mid = m.get("id")
                        if mid and not is_mark_known(mid):
                            if subjects_map is None:
                                subjects_map = await fetch_subjects_map(session, headers, student_id)
                            
                            val = m.get("name") or "Оценка"
                            weight = m.get("weight")
                            sub_id = m.get("subject_id")
                            sub_name = subjects_map.get(sub_id, "Предмет")
                            control = m.get("control_form_name") or ""
                            
                            # Формирование степени (если weight > 1)
                            weight_str = f" в степени {weight}" if (weight and weight > 1) else ""
                            control_str = f" за {control}" if control else ""
                            
                            # «вам поставили оценку n по предмету в степени (если без степени не пиши в степени просто пропуск) за ( и то за что поставлена оценка »
                            msg_text = (
                                f"🔔 <b>Новая оценка!</b>\n\n"
                                f"Вам поставили оценку <b>{val}</b> по <b>{sub_name}</b>{weight_str}{control_str}."
                            )
                            
                            await bot_instance.send_message(target_uid, msg_text, parse_mode="HTML")
                            add_known_mark(mid, m.get("created_at", ""))
    except Exception as e:
        print("Marks checker error:", e)


# ================= ПОГОДА =================
WEATHER_CODES = {
    0: "Ясно ☀️",
    1: "Преимущественно ясно 🌤", 2: "Переменная облачность ⛅️", 3: "Пасмурно ☁️",
    45: "Туман 🌫", 48: "Иней/туман 🌫",
    51: "Легкая морось 🌦", 53: "Морось 🌦", 55: "Плотная морось 🌧",
    61: "Небольшой дождь 🌧", 63: "Дождь 🌧", 65: "Сильный дождь ⛈",
    71: "Небольшой снег 🌨", 73: "Снег 🌨", 75: "Сильный снегопад ❄️",
    80: "Ливень 🌧", 95: "Гроза ⚡️"
}


async def get_weather(target_date: datetime.date):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=temperature_2m,weathercode&daily=weathercode,temperature_2m_max,temperature_2m_min&timezone=auto"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                
                target_str = target_date.isoformat()
                daily_dates = data["daily"]["time"]
                
                day_idx = 0
                if target_str in daily_dates:
                    day_idx = daily_dates.index(target_str)
                
                target_hour = f"{target_str}T08:00"
                times = data["hourly"]["time"]
                temps = data["hourly"]["temperature_2m"]
                
                morning_temp = None
                for t, temp in zip(times, temps):
                    if t == target_hour:
                        morning_temp = round(temp)
                        break
                if morning_temp is None and temps:
                    morning_temp = round(temps[8 if day_idx == 0 else 32])
                    
                t_min = round(data["daily"]["temperature_2m_min"][day_idx])
                t_max = round(data["daily"]["temperature_2m_max"][day_idx])
                code = data["daily"]["weathercode"][day_idx]
                cond = WEATHER_CODES.get(code, "Облачно 🌥")
                
                return {
                    "morning": morning_temp,
                    "min": t_min,
                    "max": t_max,
                    "cond": cond
                }
    except Exception as e:
        print("Ошибка погоды:", e)
        return {"morning": "—", "min": "—", "max": "—", "cond": "Нет данных"}


# ================= ФОРМИРОВАНИЕ СВОДКИ =================
async def build_brief_message(user_id, target_date: datetime.date, is_tomorrow: bool = False):
    weekday = target_date.weekday()
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    
    # 1. Расписание
    sched = await fetch_mosreg_schedule(target_date)
    if sched:
        source_note = "<i>(онлайн из Мосрег)</i>" if sched.get("is_online") else "<i>(из базы)</i>"
        school_text = (
            f"• Первый урок: <b>{sched['start']}</b>\n"
            f"• Всего уроков: <b>{sched['lessons_count']}</b>\n"
            f"• Закончишь в: <b>{sched['end']}</b> {source_note}"
        )
        if sched.get("lessons") and sched["lessons"][0] != "Расписание уточняется":
            lessons_list = "\n".join([f"▫️ {item}" for item in sched["lessons"]])
            school_text += f"\n\n<b>Список уроков:</b>\n{lessons_list}"
    else:
        school_text = "🎉 <b>Выходной день!</b> Уроков в школе нет."

    # 2. Погода
    w = await get_weather(target_date)
    weather_text = (
        f"• В момент выхода (~8:00): <b>{w['morning']}°C</b>\n"
        f"• В среднем за день: от <b>{w['min']}°C</b> до <b>{w['max']}°C</b>\n"
        f"• На улице: <b>{w['cond']}</b>"
    )

    # 3. Дела
    todos = get_todos(user_id)
    if todos:
        todo_lines = [f"▫️ {t[1]}" for t in todos]
        todo_text = "\n".join(todo_lines)
    else:
        todo_text = "<i>Все дела сделаны или список пуст!</i>"

    header = "🔮 <b>СВОДКА НА ЗАВТРА</b>" if is_tomorrow else "🌅 <b>СВОДКА НА СЕГОДНЯ</b>"
    msg = (
        f"{header}\n"
        f"📅 <b>{day_names[weekday]}</b>, {target_date.strftime('%d.%m.%Y')}\n\n"
        f"📚 <b>ШКОЛА:</b>\n"
        f"{school_text}\n\n"
        f"🌤 <b>ПОГОДА ({CITY_NAME}):</b>\n"
        f"{weather_text}\n\n"
        f"📝 <b>СПИСОК ДЕЛ:</b>\n"
        f"{todo_text}\n"
    )
    return msg


# ================= TELEGRAM BOT =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

TOKEN_INSTRUCTION_TEXT = (
    "<b>Авторизация в Моя школа: Московская область</b>\n\n"
    "Для привязки аккаунта нужно:\n"
    "1. Перейти по <a href=\"https://authedu.mosreg.ru/\">ссылке</a> и войти в аккаунт\n"
    "2. Перейти по <a href=\"https://authedu.mosreg.ru/v2/token/refresh?roleId=1&subsystem=2\">ссылке</a>, скопировать весь текст и отправить сюда\n"
    "<i>(там будет токен начинающийся с eyJhbGciOiJSUzI1NiJ9. )</i>\n\n"
    "⚠️ <i>Для входа в Моя школа обычно требуется отключить VPN</i>"
)


@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_start(message: types.Message):
    if message.from_user:
        save_user_id(message.from_user.id)
    await message.answer(
        "👋 Привет, Алексей! Я твой школьный ассистент с интеграцией Мосрег.\n\n"
        "Команды:\n"
        "• /today — расписание уроков (с ДЗ), погода и дела на сегодня\n"
        "• /tomorrow (или /tomorow) — расписание уроков и погода на завтра\n"
        "• /todo — список задач с кнопками (нажми, чтобы вычеркнуть)\n"
        "• /add [текст] — добавить дело (например: <code>/add сдать литературу</code>)\n"
        "• /status — проверить статус токена Мосрег и срок его действия\n"
        "• /token — инструкция по получению токена\n\n"
        "⏰ Автоматика:\n"
        "• Мгновенные уведомления о новых оценках 🔔\n"
        "• 07:15 — утренняя сводка\n"
        "• 00:00 (12:00 AM) — ежедневная проверка токена Мосрег и напоминание за 1 день до истечения."
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    token = get_token()
    exp_dt = get_token_exp_datetime(token)
    now = datetime.datetime.now()
    
    ok, diag = await check_mosreg_health()
    status_str = "🟢 <b>Работает отлично</b>" if ok else f"🔴 <b>Ошибка:</b> {diag}"
    
    if exp_dt:
        days_left = (exp_dt - now).total_seconds() / 86400.0
        exp_str = f"{exp_dt.strftime('%d.%m.%Y %H:%M')} (осталось ~{round(days_left, 1)} дн.)"
    else:
        exp_str = "Токен ещё не добавлен"
        
    await message.answer(
        f"🔍 <b>Статус интеграции с Мосрег:</b>\n\n"
        f"• Состояние API: {status_str}\n"
        f"• Истекает: <b>{exp_str}</b>\n\n"
        f"<i>Чтобы обновить токен, просто отправь его сообщением прямо в этот чат или напиши /token.</i>",
        parse_mode="HTML"
    )


@dp.message(Command("today"))
async def cmd_today(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    today = datetime.date.today()
    text = await build_brief_message(message.from_user.id, today, is_tomorrow=False)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("tomorrow"))
@dp.message(Command("tomorow"))
async def cmd_tomorrow(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    text = await build_brief_message(message.from_user.id, tomorrow, is_tomorrow=True)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("token"))
async def cmd_token(message: types.Message):
    text = message.text.replace("/token", "").strip()
    if text.startswith("eyJ") and len(text) > 50:
        await process_token(message, text)
    else:
        await message.answer(TOKEN_INSTRUCTION_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    task = message.text.replace("/add", "").strip()
    if not task:
        await message.answer("⚠️ Напиши задачу после команды, например:\n<code>/add выучить слова</code>", parse_mode="HTML")
        return
    add_todo(message.from_user.id, task)
    await message.answer(f"✅ Задача добавлена:\n▫️ {task}")


@dp.message(Command("todo"))
async def cmd_todo(message: types.Message):
    todos = get_todos(message.from_user.id)
    if not todos:
        await message.answer("📝 <b>Список задач пуст!</b>\nДобавь новую задачу через команду:\n<code>/add выучить слова</code>", parse_mode="HTML")
        return

    kb = []
    for tid, ttext in todos:
        kb.append([InlineKeyboardButton(text=f"✅ {ttext}", callback_data=f"done_{tid}")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    
    await message.answer(
        "📝 <b>Твои текущие задачи:</b>\n<i>(Нажми на кнопку задачи, чтобы отметить её выполненной)</i>",
        reply_markup=markup,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("done_"))
async def cb_done(call: types.CallbackQuery):
    todo_id = int(call.data.replace("done_", ""))
    mark_done(todo_id)
    await call.answer("Выполнено!")
    
    todos = get_todos(call.from_user.id)
    if not todos:
        await call.message.edit_text("🎉 Все задачи на сегодня выполнены!")
    else:
        kb = [[InlineKeyboardButton(text=f"✅ {ttext}", callback_data=f"done_{tid}")] for tid, ttext in todos]
        await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


# Обработка входящего токена прямо в чат (без команды)
@dp.message(F.text.startswith("eyJ"))
async def handle_direct_token_input(message: types.Message):
    await process_token(message, message.text.strip())


async def process_token(message: types.Message, raw_token: str):
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    set_config("mosreg_token", raw_token)
    exp_dt = get_token_exp_datetime(raw_token)
    now = datetime.datetime.now()
    
    exp_info = ""
    if exp_dt:
        days = round((exp_dt - now).total_seconds() / 86400.0, 1)
        exp_info = f"\n⏳ Действует до: <b>{exp_dt.strftime('%d.%m.%Y %H:%M')}</b> (~{days} дн.)"
        
    ok, diag = await check_mosreg_health()
    if ok:
        await message.answer(f"✅ <b>Новый токен Мосрег сохранён и проверен!</b>{exp_info}\nРасписание и оценки теперь работают онлайн.", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ Токен сохранён, но при проверке возникла ошибка:\n{diag}", parse_mode="HTML")


# Проверка токена каждый день в полночь (00:00 / 12:00 AM)
async def midnight_health_check_job():
    target_uid = get_saved_user_id()
    if not target_uid:
        return
        
    token = get_token()
    if not token:
        return
        
    exp_dt = get_token_exp_datetime(token)
    now = datetime.datetime.now()
    ok, diag = await check_mosreg_health()
    
    # 1. Если токен перестал работать
    if not ok:
        await bot.send_message(
            target_uid,
            f"⚠️ <b>Внимание: токен Мосрег перестал работать!</b>\nПричина: {diag}\n\nНапиши /token чтобы обновить его.",
            parse_mode="HTML"
        )
        return

    # 2. Если до конца срока действия остался 1 день (или меньше)
    if exp_dt:
        hours_left = (exp_dt - now).total_seconds() / 3600.0
        if 0 < hours_left <= 28:
            await bot.send_message(
                target_uid,
                f"⏰ <b>Напоминание: токен Мосрег истекает менее чем через сутки!</b>\n"
                f"Дата истечения: <b>{exp_dt.strftime('%d.%m.%Y %H:%M')}</b>.\n\n"
                f"Чтобы расписание продолжало обновляться, просто пришли новый токен в этот чат (или напиши /token).",
                parse_mode="HTML"
            )


# Автоматическая утренняя рассылка в 07:15
async def morning_job():
    target_uid = get_saved_user_id()
    if target_uid:
        try:
            today = datetime.date.today()
            text = await build_brief_message(target_uid, today, is_tomorrow=False)
            await bot.send_message(target_uid, text, parse_mode="HTML")
            print("Утренняя сводка успешно отправлена!")
        except Exception as e:
            print("Ошибка при отправке утренней сводки:", e)


async def start_web_server():
    app = web.Application()
    async def handle_ping(request):
        return web.Response(text="Bot is running!")
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web healthcheck server started on port {port}")


async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # Утренняя рассылка в 07:15
    scheduler.add_job(morning_job, "cron", hour=MORNING_HOUR, minute=MORNING_MINUTE)
    # Проверка токена в 00:00 (12:00 AM)
    scheduler.add_job(midnight_health_check_job, "cron", hour=0, minute=0)
    # Проверка новых оценок каждые 5 минут
    scheduler.add_job(check_new_marks_job, "interval", minutes=5, args=[bot])
    scheduler.start()
    
    # Запуск веб-сервера для Render (Port binding)
    await start_web_server()
    
    print("Школьный бот запущен! Расписания: 07:15 (утро), 00:00 (проверка), интервал 5 мин (оценки).")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import logging
import os
import re
import json
import random
import time
import csv
import hmac
import hashlib
import io
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from collections import OrderedDict
from io import StringIO

from dotenv import load_dotenv
load_dotenv()

# ---- FastAPI и сопутствующие компоненты ----
from fastapi import (
    FastAPI, Request, Depends, HTTPException, status,
    Form, Query, File, UploadFile
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# ---- База данных (SQLAlchemy) ----
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Float, BigInteger, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

# ---- Хеширование паролей ----
from passlib.context import CryptContext

# ---- Генерация Excel ----
from openpyxl import Workbook
import openpyxl

# ---- Внешние API и парсинг ----
from google import genai
from google.genai import types as genai_types
import feedparser
import aiohttp
import httpx

# ---- Aiogram (телеграм-бот) ----
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

# ---- Pydantic для валидации ----
from pydantic import BaseModel

# ---- Uvicorn (сервер) ----
import uvicorn

# ---- SSE ----
from sse_starlette.sse import EventSourceResponse

# ---------- Переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

if not all([BOT_TOKEN, GEMINI_API_KEY, API_FOOTBALL_KEY]):
    print("⚠️ Предупреждение: не все основные переменные окружения заданы. Бот может работать некорректно.")

# ---------- Хеширование паролей ----------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------- Gemini ----------
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash"

# ---------- Кэш ----------
team_stats_cache = OrderedDict()
CACHE_TTL = 3600
news_cache = {"data": [], "last_update": 0}
NEWS_CACHE_TTL = 1800

# ---------- База данных ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bot_database.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=True, index=True)
    bet_id = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    attempts_left = Column(Integer, default=0)
    is_active = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    is_premium = Column(Boolean, default=False)
    last_activity = Column(DateTime, nullable=True)
    source = Column(String, nullable=True)  # сырой хвост (например fb_cpc_promo)
    
    # --- Поля для трекера (Этап 3) ---
    ip_address = Column(String, nullable=True)
    country = Column(String, nullable=True)
    os_device = Column(String, nullable=True)
    browser = Column(String, nullable=True)

class PredictionLog(Base):
    __tablename__ = "prediction_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    match_description = Column(String, nullable=False)
    winner = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    prediction_text = Column(String, nullable=False)
    additional_predictions = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="logs")

User.logs = relationship("PredictionLog", order_by=PredictionLog.created_at.desc())

class Staff(Base):
    __tablename__ = "staff"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="manager")  # admin / manager
    is_active = Column(Boolean, default=True)
    session_token = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

class StaffLog(Base):
    __tablename__ = "staff_logs"
    id = Column(Integer, primary_key=True, index=True)
    staff_id = Column(Integer, ForeignKey("staff.id"), nullable=False)
    action = Column(String, nullable=False)
    target_user_id = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    staff = relationship("Staff")

Base.metadata.create_all(bind=engine)

# --- АВТО-МИГРАЦИЯ ДЛЯ POSTGRESQL (Этап 3) ---
# Добавляем новые колонки в существующую таблицу без потери данных
try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS ip_address VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS country VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS os_device VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS browser VARCHAR;"))
        print("✅ Миграция базы данных успешно выполнена (колонки проверены/добавлены)!")
except Exception as e:
    print(f"⚠️ Миграция пропущена: {e}")
# ---------------------------------------------

# Создаём администратора по умолчанию, если нет ни одного сотрудника
with SessionLocal() as db:
    if not db.query(Staff).first():
        admin = Staff(
            username="admin",
            password_hash=pwd_context.hash("admin2025"),
            role="admin",
            is_active=True
        )
        db.add(admin)
        db.commit()
        print("✅ Создан администратор: admin / admin2025")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Логирование действий сотрудников ----------
def log_staff_action(db: Session, staff_id: int, action: str, target_user_id: int = None):
    log = StaffLog(staff_id=staff_id, action=action, target_user_id=target_user_id)
    db.add(log)
    db.commit()
    print(f"[STAFF LOG] staff_id={staff_id}, action={action}, target={target_user_id}")

# ---------- Вспомогательные функции для аутентификации и ролей ----------
async def get_current_staff(request: Request, db: Session = Depends(get_db)):
    session_token = request.cookies.get("staff_session")
    if not session_token:
        return None
    staff = db.query(Staff).filter(Staff.session_token == session_token, Staff.is_active == True).first()
    return staff

async def require_admin(staff: Staff = Depends(get_current_staff)):
    if not staff or staff.role != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return staff

# ---------- Валидация Telegram WebApp ----------
def validate_telegram_data(init_data: str, bot_token: str) -> dict:
    try:
        parsed = dict(parse_qsl(init_data))
        hash_check = parsed.pop('hash', None)
        if not hash_check:
            raise ValueError("No hash provided")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(key=b"WebAppData", msg=bot_token.encode(), digestmod=hashlib.sha256).digest()
        calculated_hash = hmac.new(key=secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()
        if calculated_hash != hash_check:
            raise ValueError("Invalid hash")
        return parsed
    except Exception as e:
        print(f"[ERROR] Telegram data validation failed: {e}")
        raise ValueError("Invalid Telegram data")

# ---------- Telegram бот ----------
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class RegistrationForm(StatesGroup):
    waiting_for_bet_id = State()

class AnalysisState(StatesGroup):
    waiting_for_match_info = State()

def get_main_keyboard(attempts: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎲 AI Анализ")], [KeyboardButton(text="📰 Новости")]],
        resize_keyboard=True
    )

# Функцию download_photo мы удалили, она больше не нужна

async def extract_match_from_image(file_id: str) -> dict:
    file = await bot.get_file(file_id)
    
    # Скачиваем прямо в оперативную память (BytesIO)
    photo_bytes = io.BytesIO()
    await bot.download_file(file.file_path, photo_bytes)
    
    image_part = genai_types.Part.from_bytes(
        data=photo_bytes.getvalue(),
        mime_type="image/jpeg"
    )
    
    max_retries = 2
    for attempt in range(max_retries):
        try:
            prompt = """
You are an expert at extracting football match information from ANY screenshot, regardless of orientation (horizontal/vertical), cropping, or layout.
Look at the image carefully. Find the two team names. They can be:
- Near flags or logos (left/right)
- In the center, sometimes with a "vs" or dash between them
- In a table or list
- Even if partially cut off, guess the most likely name
Ignore ALL numbers, percentages, timers, odds, standings, ads, and other text that are NOT team names or tournament names.
Return ONLY valid JSON in this format:
{"team1": "First Team Name (as written)", "team2": "Second Team Name", "tournament": "Tournament or league (if visible, else 'Unknown')"}
If you are absolutely unsure, use "Unknown" for a team name. But try your best.
"""
            # Вызываем Gemini асинхронно, передавая байты напрямую
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=[image_part, prompt],
                config=genai_types.GenerateContentConfig(temperature=0.1)
            )
            text = response.text.strip()
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "team1": data.get("team1", "Unknown").strip(),
                    "team2": data.get("team2", "Unknown").strip(),
                    "tournament": data.get("tournament", "Unknown").strip()
                }
        except Exception as e:
            print(f"Error in extract_match_from_image (attempt {attempt+1}): {e}")
            await asyncio.sleep(1)
            
    return {"team1": "Unknown", "team2": "Unknown", "tournament": "Unknown"}
    
def _fallback_stats():
    return {"last_5": [0.5, 0.5, 0.5, 0.5, 0.5], "injuries": [], "home_advantage": 0.1}

async def get_team_stats(team_name: str) -> dict:
    # Заглушка (в будущем замените на API-Football)
    import random
    last_5 = []
    for _ in range(5):
        r = random.random()
        if r < 0.4:
            last_5.append(1)
        elif r < 0.7:
            last_5.append(0.5)
        else:
            last_5.append(0)
    return {
        "last_5": last_5,
        "injuries": [],
        "home_advantage": random.uniform(-0.1, 0.2)
    }

def calculate_prediction(stats1: dict, stats2: dict) -> dict:
    wins1 = sum(1 for r in stats1['last_5'] if r == 1)
    wins2 = sum(1 for r in stats2['last_5'] if r == 1)
    draws1 = sum(1 for r in stats1['last_5'] if r == 0.5)
    draws2 = sum(1 for r in stats2['last_5'] if r == 0.5)
    diff = wins1 - wins2
    confidence = 50 + diff * 8
    if draws1 > draws2:
        confidence -= 2
    elif draws2 > draws1:
        confidence += 2
    confidence += stats1['home_advantage'] * 10
    confidence += random.uniform(-3, 3)
    confidence = max(30, min(95, confidence))
    confidence = round(confidence, 2)
    if diff > 0.5:
        winner = "team1"
    elif diff < -0.5:
        winner = "team2"
    else:
        winner = "draw"
    return {"winner": winner, "confidence": confidence}

async def generate_prediction_text(team1, team2, stats1, stats2, winner, confidence):
    injuries1 = ', '.join(stats1['injuries']) if stats1['injuries'] else 'нет'
    injuries2 = ', '.join(stats2['injuries']) if stats2['injuries'] else 'нет'
    prompt = f"""
Ты спортивный аналитик. На основе статистики:
Команда {team1}: последние 5 матчей {stats1['last_5']}, травмы: {injuries1}
Команда {team2}: последние 5 матчей {stats2['last_5']}, травмы: {injuries2}
Твой вердикт уже вынесен: победа {winner}.
ЗАДАЧА: Напиши ТОЛЬКО краткое аналитическое обоснование (2-3 предложения).
СТРОГОЕ ПРАВИЛО: НЕ пиши проценты уверенности, НЕ пиши итоговый счет, НЕ дублируй вердикт (не пиши "Прогноз: победа"). Только логика и рассуждения на русском языке.
"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            print(f"Gemini error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                break
            await asyncio.sleep(2 ** attempt)
    return "Сервис аналитики временно перегружен. Попробуйте позже."

async def save_prediction_log(user_id: int, match_desc: str, winner: str, confidence: float, full_text: str, additional: str = None):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            log = PredictionLog(
                user_id=user.id,
                match_description=match_desc,
                winner=winner,
                confidence=confidence,
                prediction_text=full_text,
                additional_predictions=additional
            )
            db.add(log)
            db.commit()
            user.last_activity = datetime.utcnow()
            db.commit()
    except Exception as e:
        print(f"Log save error: {e}")
    finally:
        db.close()

async def generate_and_send_prediction(message: types.Message, team1: str, team2: str):
    await message.answer("📊 Получаю статистику и анализирую...")
    stats1 = await get_team_stats(team1)
    stats2 = await get_team_stats(team2)
    pred = calculate_prediction(stats1, stats2)
    winner = pred["winner"]
    confidence = pred["confidence"]
    analysis_text = await generate_prediction_text(team1, team2, stats1, stats2, winner, confidence)
    winner_name = team1 if winner == "team1" else (team2 if winner == "team2" else "Ничья")
    total_over_conf = random.randint(55, 75)
    corners_over_conf = random.randint(55, 75)
    additional = f"• Тотал голов (2.5): OVER (уверенность {total_over_conf}%)\n• Тотал угловых (9.5): OVER (уверенность {corners_over_conf}%)"
    result_text = (
        f"🏆 *Прогноз AI*\n"
        f"Победитель: *{winner_name}*\n"
        f"Уверенность: *{confidence}%*\n\n"
        f"{analysis_text}\n\n"
        f"📊 *Дополнительные исходы:*\n{additional}"
    )
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый анализ", callback_data="new_analysis"),
         InlineKeyboardButton(text="📊 Мой лимит", callback_data="my_limit"),
         InlineKeyboardButton(text="📰 Новости", callback_data="news")]
    ])
    await message.answer(result_text, parse_mode="Markdown", reply_markup=inline_kb)

    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
    if user:
        await save_prediction_log(user.id, f"{team1} - {team2}", winner, confidence, result_text, additional)
        user.attempts_left -= 1
        user.last_activity = datetime.utcnow()
        db.commit()
    db.close()

# ---------- Обработчики бота ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
    db.close()
    if user and user.is_active and not user.is_banned:
        await message.answer(f"С возвращением! У вас осталось прогнозов: {user.attempts_left}", reply_markup=get_main_keyboard(user.attempts_left))
    elif user and user.is_banned:
        await message.answer("❌ Ваш аккаунт заблокирован.")
    else:
        await state.set_state(RegistrationForm.waiting_for_bet_id)
        await message.answer("Привет! 👋\n\nЯ нейросеть для анализа спортивных событий.\nДля использования мне нужен ваш ID 1xBet.\n\nВведите ID (только цифры):", reply_markup=ReplyKeyboardRemove())

@dp.message(RegistrationForm.waiting_for_bet_id)
async def process_bet_id(message: types.Message, state: FSMContext):
    bet_id = message.text.strip()
    if not bet_id.isdigit():
        await message.answer("❌ ID должен состоять только из цифр. Попробуйте еще раз.")
        return
    db = SessionLocal()
    user = db.query(User).filter(User.bet_id == bet_id).first()
    if user:
        await message.answer("❌ Этот ID уже зарегистрирован.")
        db.close()
        await state.clear()
        return
    new_user = User(
        telegram_id=message.from_user.id,
        bet_id=bet_id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        attempts_left=0,
        is_active=False,
        last_activity=datetime.utcnow()
    )
    db.add(new_user)
    db.commit()
    db.close()
    await message.answer("✅ Ваш ID отправлен на проверку менеджеру.\nДождитесь подтверждения, я сообщу вам.")
    await state.clear()

@dp.message(F.text == "🎲 AI Анализ")
async def ai_analysis_start(message: types.Message, state: FSMContext):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
    db.close()
    if not user or not user.is_active:
        await message.answer("❌ Аккаунт не активирован. /start")
        return
    if user.is_banned:
        await message.answer("❌ Аккаунт заблокирован.")
        return
    if user.attempts_left <= 0:
        support_menu = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Написать менеджеру", url=f"tg://resolve?domain=YOUR_MANAGER_USERNAME&text=Мой ID: {user.bet_id} Хочу обновить лимиты")]
        ])
        await message.answer("❌ Лимит прогнозов исчерпан.", reply_markup=support_menu)
        return
    await state.set_state(AnalysisState.waiting_for_match_info)
    await message.answer("📸 Отправьте скриншот матча из 1xBet или напишите текстом: `Команда А - Команда Б`", parse_mode="Markdown")

@dp.message(AnalysisState.waiting_for_match_info, F.photo)
async def process_match_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    await message.answer("🔍 Анализирую скриншот...")
    match_data = await extract_match_from_image(photo.file_id)
    team1 = match_data.get("team1", "Unknown")
    team2 = match_data.get("team2", "Unknown")
    if team1 == "Unknown" or team2 == "Unknown":
        await message.answer("❌ Не удалось распознать команды.\nПожалуйста, напишите текстом: `Команда А - Команда Б`", parse_mode="Markdown")
        return
    await generate_and_send_prediction(message, team1, team2)
    await state.clear()

@dp.message(AnalysisState.waiting_for_match_info, F.text)
async def process_match_text(message: types.Message, state: FSMContext):
    text = message.text.strip()
    parts = re.split(r'[-–—]', text)
    if len(parts) >= 2:
        team1 = parts[0].strip()
        team2 = parts[1].strip()
    else:
        prompt = f"Extract team1 and team2 from '{text}'. Return JSON: {{'team1': '', 'team2': ''}}"
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=prompt)
            data = json.loads(response.text)
            team1 = data.get("team1", "Unknown")
            team2 = data.get("team2", "Unknown")
        except:
            await message.answer("❌ Не удалось распознать команды. Напишите в формате: `Команда А - Команда Б`")
            return
    await generate_and_send_prediction(message, team1, team2)
    await state.clear()

@dp.message(F.text == "📰 Новости")
async def news(message: types.Message):
    feed = feedparser.parse("https://news.sportbox.ru/rss")
    if not feed.entries:
        await message.answer("Новости временно недоступны.")
        return
    news_list = [f"🔹 {entry.title}\n{entry.link}" for entry in feed.entries[:10]]
    await message.answer("\n\n".join(news_list), disable_web_page_preview=True)

@dp.callback_query(lambda c: c.data == "new_analysis")
async def new_analysis_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("📸 Отправьте скриншот или текст матча...")
    await state.set_state(AnalysisState.waiting_for_match_info)

@dp.callback_query(lambda c: c.data == "my_limit")
async def my_limit_callback(callback: types.CallbackQuery):
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == callback.from_user.id).first()
    db.close()
    if user:
        await callback.answer(f"Осталось прогнозов: {user.attempts_left}", show_alert=True)
    else:
        await callback.answer("Ошибка", show_alert=True)

@dp.callback_query(lambda c: c.data == "news")
async def news_callback(callback: types.CallbackQuery):
    await callback.answer()
    await news(callback.message)

# -------------------------------------------------------------------
# Конец блока 1. Далее идёт блок 2 (FastAPI эндпоинты)
# -------------------------------------------------------------------

# ---------- FastAPI приложение ----------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://venbetapp-production.up.railway.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# ---------- Страница логина ----------
@app.get("/admin/login", response_class=HTMLResponse)
async def staff_login_page():
    return templates.TemplateResponse("admin_login.html", {"request": {}})

@app.post("/admin/login")
async def staff_login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    staff = db.query(Staff).filter(Staff.username == username, Staff.is_active == True).first()
    if not staff or not pwd_context.verify(password, staff.password_hash):
        return HTMLResponse("<h3>Неверные учётные данные</h3><a href='/admin/login'>Попробовать снова</a>", status_code=401)
    import uuid
    session_token = str(uuid.uuid4())
    staff.session_token = session_token
    staff.last_login = datetime.utcnow()
    db.commit()
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(key="staff_session", value=session_token, httponly=True, max_age=86400*7)
    return response

@app.get("/admin/logout")
async def staff_logout(request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if staff:
        staff.session_token = None
        db.commit()
    response = RedirectResponse(url="/admin/login")
    response.delete_cookie("staff_session")
    return response

# ---------- Эндпоинт для обновления last_activity ----------
@app.post("/update_activity")
async def update_activity(user_id: str = Form(...)):
    db = SessionLocal()
    user = db.query(User).filter(User.bet_id == user_id).first()
    if user:
        user.last_activity = datetime.utcnow()
        db.commit()
    db.close()
    return {"status": "ok"}

# ---------- Экспорт в Excel ----------
@app.get("/export_users_excel")
async def export_users_excel(
    request: Request,
    search: str = Query(None),
    status: str = Query(None),
    limit_min: int = Query(None),
    limit_max: int = Query(None),
    date_filter: str = Query(None),
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")
    query = db.query(User)
    if search:
        query = query.filter(
            (User.telegram_id.cast(String).contains(search)) |
            (User.bet_id.contains(search)) |
            (User.username.contains(search))
        )
    if status == "active":
        query = query.filter(User.is_active == True, User.is_banned == False)
    elif status == "banned":
        query = query.filter(User.is_banned == True)
    elif status == "premium":
        query = query.filter(User.is_premium == True)
    if limit_min is not None:
        query = query.filter(User.attempts_left >= limit_min)
    if limit_max is not None:
        query = query.filter(User.attempts_left <= limit_max)
    now = datetime.utcnow()
    if date_filter == "today":
        start_date = now.replace(hour=0, minute=0, second=0)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "week":
        start_date = now - timedelta(days=7)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "month":
        start_date = now - timedelta(days=30)
        query = query.filter(User.created_at >= start_date)
    users = query.order_by(User.created_at.desc()).all()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Telegram ID", "1xBet ID", "Username", "Лимит", "Активен", "Забанен", "Premium", "Дата регистрации", "Последняя активность"])
    for u in users:
        ws.append([u.id, u.telegram_id or "", u.bet_id, u.username or "", u.attempts_left, u.is_active, u.is_banned, u.is_premium, u.created_at, u.last_activity])
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=users.xlsx"})

# ---------- Экспорт в CSV ----------
@app.get("/export_users_csv")
async def export_users_csv(
    request: Request,
    search: str = Query(None),
    status: str = Query(None),
    limit_min: int = Query(None),
    limit_max: int = Query(None),
    date_filter: str = Query(None),
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")
    query = db.query(User)
    if search:
        query = query.filter(
            (User.telegram_id.cast(String).contains(search)) |
            (User.bet_id.contains(search)) |
            (User.username.contains(search))
        )
    if status == "active":
        query = query.filter(User.is_active == True, User.is_banned == False)
    elif status == "banned":
        query = query.filter(User.is_banned == True)
    elif status == "premium":
        query = query.filter(User.is_premium == True)
    if limit_min is not None:
        query = query.filter(User.attempts_left >= limit_min)
    if limit_max is not None:
        query = query.filter(User.attempts_left <= limit_max)
    now = datetime.utcnow()
    if date_filter == "today":
        start_date = now.replace(hour=0, minute=0, second=0)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "week":
        start_date = now - timedelta(days=7)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "month":
        start_date = now - timedelta(days=30)
        query = query.filter(User.created_at >= start_date)
    users = query.order_by(User.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Telegram ID", "1xBet ID", "Username", "Лимит", "Активен", "Забанен", "Premium", "Дата регистрации"])
    for u in users:
        writer.writerow([u.id, u.telegram_id, u.bet_id, u.username or "", u.attempts_left, u.is_active, u.is_banned, u.is_premium, u.created_at])
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=users_export.csv"
    return response

# ---------- Главная страница админки (дашборд) ----------
@app.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    search: str = Query(None),
    status: str = Query(None),
    limit_min: int = Query(None),
    limit_max: int = Query(None),
    date_filter: str = Query(None),
    page: int = Query(1),
    per_page: int = Query(20),
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")

    query = db.query(User)
    if search:
        query = query.filter(
            (User.telegram_id.cast(String).contains(search)) |
            (User.bet_id.contains(search)) |
            (User.username.contains(search))
        )
    if status == "active":
        query = query.filter(User.is_active == True, User.is_banned == False)
    elif status == "banned":
        query = query.filter(User.is_banned == True)
    elif status == "premium":
        query = query.filter(User.is_premium == True)
    elif status == "pending":
        query = query.filter(User.is_active == False, User.is_banned == False)

    if limit_min is not None:
        query = query.filter(User.attempts_left >= limit_min)
    if limit_max is not None:
        query = query.filter(User.attempts_left <= limit_max)

    now = datetime.utcnow()
    if date_filter == "today":
        start_date = now.replace(hour=0, minute=0, second=0)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "week":
        start_date = now - timedelta(days=7)
        query = query.filter(User.created_at >= start_date)
    elif date_filter == "month":
        start_date = now - timedelta(days=30)
        query = query.filter(User.created_at >= start_date)

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    users = query.order_by(User.created_at.desc()).offset(offset).limit(per_page).all()

    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.is_active == True, User.is_banned == False).count()
    premium_users = db.query(User).filter(User.is_premium == True).count()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    predictions_today = db.query(PredictionLog).filter(PredictionLog.created_at >= today_start).count()
    pending_count = db.query(User).filter(User.is_active == False, User.is_banned == False).count()

    return templates.TemplateResponse("users.html", {
        "request": request,
        "users": users,
        "total_users": total_users,
        "active_users": active_users,
        "premium_users": premium_users,
        "predictions_today": predictions_today,
        "pending_count": pending_count,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
        "search_query": search or "",
        "status_filter": status or "",
        "limit_min": limit_min,
        "limit_max": limit_max,
        "date_filter": date_filter or "",
        "staff_role": staff.role
    })

# ---------- Управление пользователями (approve, ban, premium) ----------
# ---------- Управление пользователями (approve, ban, premium) ----------
@app.post("/approve")
async def approve_user(
    request: Request,
    user_id: int = Form(...),
    attempts: int = Form(...),
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_active = True
        user.is_banned = False
        user.attempts_left = attempts
        user.confirmed_at = datetime.utcnow()
        db.commit()
        log_staff_action(db, staff.id, f"approved user {user_id} with {attempts} attempts", target_user_id=user_id)
        if user.telegram_id:
            try:
                await bot.send_message(user.telegram_id, f"✅ Ваш аккаунт активирован! У вас {attempts} прогнозов.")
            except:
                pass
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/ban")
async def ban_user(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_banned = True
        user.is_active = False
        db.commit()
        log_staff_action(db, staff.id, f"banned user {user_id}", target_user_id=user_id)
        if user.telegram_id:
            try:
                await bot.send_message(user.telegram_id, "❌ Ваш аккаунт заблокирован.")
            except:
                pass
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/premium")
async def set_premium(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_premium = True
        db.commit()
        log_staff_action(db, staff.id, f"set premium to user {user_id}", target_user_id=user_id)
        if user.telegram_id:
            try:
                await bot.send_message(user.telegram_id, "⭐ Вам выдан премиум-статус!")
            except:
                pass
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/give_attempts")
async def give_attempts(request: Request, data: dict, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user_id = data.get("user_id")
    attempts = data.get("attempts", 0)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.attempts_left += attempts
        db.commit()
        log_staff_action(db, staff.id, f"gave {attempts} attempts to user {user_id}", target_user_id=user_id)
    return {"status": "ok"}

# ---------- Массовые операции ----------
@app.post("/mass_give_attempts")
async def mass_give_attempts(request: Request, data: dict, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user_ids = data.get("user_ids", [])
    attempts = data.get("attempts", 0)
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.attempts_left += attempts
    db.commit()
    log_staff_action(db, staff.id, f"mass give {attempts} attempts to users: {user_ids}")
    return {"status": "ok"}

@app.post("/mass_activate")
async def mass_activate(request: Request, data: dict, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user_ids = data.get("user_ids", [])
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.is_active = True
            user.is_banned = False
    db.commit()
    log_staff_action(db, staff.id, f"mass activate users: {user_ids}")
    return {"status": "ok"}

@app.post("/mass_ban")
async def mass_ban(request: Request, data: dict, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        raise HTTPException(status_code=401)
    if staff.role == "buyer":
        raise HTTPException(status_code=403, detail="У баеров нет прав на это действие")
        
    user_ids = data.get("user_ids", [])
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.is_banned = True
            user.is_active = False
    db.commit()
    log_staff_action(db, staff.id, f"mass ban users: {user_ids}")
    return {"status": "ok"}

# ---------- Удаление пользователей (одиночное и массовое) ----------
@app.post("/delete_user")
async def delete_user(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    # ЗДЕСЬ ИСПРАВЛЕНИЕ:
    if not staff or staff.role != "admin":
        raise HTTPException(status_code=403, detail="Только для администраторов")
    
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        db.query(PredictionLog).filter(PredictionLog.user_id == user.id).delete()
        db.delete(user)
        db.commit()
        log_staff_action(db, staff.id, f"deleted user {user_id}", target_user_id=user_id)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/mass_delete_users")
async def mass_delete_users(request: Request, data: dict, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    # ЗДЕСЬ ИСПРАВЛЕНИЕ:
    if not staff or staff.role != "admin":
        raise HTTPException(status_code=403, detail="Только для администраторов")
    
    user_ids = data.get("user_ids", [])
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            db.query(PredictionLog).filter(PredictionLog.user_id == user.id).delete()
            db.delete(user)
    db.commit()
    log_staff_action(db, staff.id, f"mass deleted users: {user_ids}")
    return {"status": "ok"}

# ---------- Просмотр логов прогнозов ----------
@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, search: str = Query(None), page: int = Query(1), db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")
    query = db.query(PredictionLog)
    if search:
        query = query.filter(
            (PredictionLog.match_description.contains(search)) |
            (PredictionLog.user_id.cast(String).contains(search))
        )
    total = query.count()
    per_page = 20
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    logs = query.order_by(PredictionLog.created_at.desc()).offset(offset).limit(per_page).all()
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "logs": logs,
        "page": page,
        "total_pages": total_pages,
        "search_query": search or "",
        "staff_role": staff.role
    })
@app.post("/admin/delete_log")
async def delete_log(request: Request, log_id: int = Form(...), db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff or staff.role != "admin":
        raise HTTPException(status_code=403, detail="Только для администраторов")
    
    log_entry = db.query(PredictionLog).filter(PredictionLog.id == log_id).first()
    if log_entry:
        db.delete(log_entry)
        db.commit()
        log_staff_action(db, staff.id, f"deleted log {log_id}")
    return RedirectResponse(url="/logs", status_code=303)    

# ---------- Управление сотрудниками (только для admin) ----------
@app.get("/admin/staff", response_class=HTMLResponse)
async def list_staff(request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff or staff.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    all_staff = db.query(Staff).all()
    return templates.TemplateResponse("staff_list.html", {
        "request": request,
        "staff": all_staff
    })

@app.post("/admin/staff/create")
async def create_manager(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db)
):
    admin_staff = await get_current_staff(request, db)
    if not admin_staff or admin_staff.role != "admin":
        raise HTTPException(status_code=403)
    hashed = pwd_context.hash(password)
    new_staff = Staff(username=username, password_hash=hashed, role=role, is_active=True)
    db.add(new_staff)
    db.commit()
    log_staff_action(db, admin_staff.id, f"created staff {username} with role {role}")
    return RedirectResponse(url="/admin/staff", status_code=303)

@app.post("/admin/staff/toggle")
async def toggle_staff(request: Request, staff_id: int = Form(...), db: Session = Depends(get_db)):
    admin_staff = await get_current_staff(request, db)
    if not admin_staff or admin_staff.role != "admin":
        raise HTTPException(status_code=403)
    target = db.query(Staff).filter(Staff.id == staff_id).first()
    if target and target.id != admin_staff.id:
        target.is_active = not target.is_active
        if not target.is_active:
            target.session_token = None
        db.commit()
        log_staff_action(db, admin_staff.id, f"toggled staff {target.username} active={target.is_active}")
    return RedirectResponse(url="/admin/staff", status_code=303)

# ---------- SSE для уведомлений о новых заявках ----------
class Notifier:
    def __init__(self):
        self.connections: list[asyncio.Queue] = []

    async def push(self, msg: dict):
        for q in self.connections:
            await q.put(msg)

    async def connect(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.connections.append(q)
        return q

    def remove(self, q: asyncio.Queue):
        if q in self.connections:
            self.connections.remove(q)

notifier = Notifier()

def get_current_pending_count(db: Session) -> int:
    return db.query(User).filter(User.is_active == False, User.is_banned == False).count()

@app.get("/api/stream_leads")
async def stream_leads(request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        return {"error": "Unauthorized"}
    async def event_generator():
        queue = await notifier.connect()
        yield {"data": json.dumps({"count": get_current_pending_count(db)}), "event": "update"}
        try:
            while True:
                data = await queue.get()
                yield {"data": json.dumps(data), "event": "update"}
        except asyncio.CancelledError:
            pass
        finally:
            notifier.remove(queue)
    return EventSourceResponse(
        event_generator(), 
        ping=20,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# ---------- Аналитика (Этап 2) ----------
@app.get("/api/analytics")
async def get_analytics(
    request: Request, 
    start_date: str = Query(None), 
    end_date: str = Query(None), 
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        return {"error": "Unauthorized"}
        
    now = datetime.utcnow()
    # 1. Парсим даты из календарика. Если их нет — берем последние 7 дней по умолчанию
    if start_date and end_date:
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
        except ValueError:
            start_dt = now - timedelta(days=6)
            end_dt = now
    else:
        start_dt = now - timedelta(days=6)
        end_dt = now

    # 2. Воронка конверсии (СТРОГО ЗА ВЫБРАННЫЙ ПЕРИОД)
    registered = db.query(User).filter(User.created_at >= start_dt, User.created_at <= end_dt).count()
    activated = db.query(User).filter(User.is_active == True, User.created_at >= start_dt, User.created_at <= end_dt).count()
    predicted = db.query(PredictionLog.user_id).filter(PredictionLog.created_at >= start_dt, PredictionLog.created_at <= end_dt).distinct().count()
    
    # 3. Мертвые души (Это всегда текущий снимок базы, не зависит от календаря)
    three_days_ago = now - timedelta(days=3)
    dead_souls = db.query(User).filter(
        User.is_active == True,
        (User.last_activity < three_days_ago) | (User.last_activity == None)
    ).count()
    
    # 4. Собираем активность для графика за выбранный период
    users_period = db.query(User).filter(User.created_at >= start_dt, User.created_at <= end_dt).all()
    logs_period = db.query(PredictionLog).filter(PredictionLog.created_at >= start_dt, PredictionLog.created_at <= end_dt).all()
    
    reg_dict = {}
    for u in users_period:
        if u.created_at:
            d = u.created_at.strftime('%Y-%m-%d')
            reg_dict[d] = reg_dict.get(d, 0) + 1
            
    pred_dict = {}
    for log in logs_period:
        if log.created_at:
            d = log.created_at.strftime('%Y-%m-%d')
            pred_dict[d] = pred_dict.get(d, 0) + 1
            
    # Генерируем массив всех дат от start_dt до end_dt
    delta_days = (end_dt - start_dt).days
    if delta_days < 0: delta_days = 0
    if delta_days > 365: delta_days = 365 # Защита, чтобы график не завис при выборе 10 лет
    
    dates = [(start_dt + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(delta_days + 1)]
    
    return {
        "funnel": {"registered": registered, "activated": activated, "predicted": predicted},
        "dead_souls": dead_souls,
        "chart": {
            "dates": dates,
            "registrations": [reg_dict.get(d, 0) for d in dates],
            "predictions": [pred_dict.get(d, 0) for d in dates]
        }
    }

# ---------- Эндпоинт регистрации ----------
@app.get("/register_request")
async def register_request(
    request: Request, # <-- Добавили Request для получения IP и заголовков
    bet_id: str, 
    init_data: str = Query(None), 
    source: str = Query(None), 
    db: Session = Depends(get_db)
):
    try:
        # Вытаскиваем IP и User-Agent (Устройство)
        ip_address = request.client.host
        # Если бэкенд за Cloudflare или Railway Proxy, реальный IP лежит в заголовке X-Forwarded-For
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            ip_address = forwarded_for.split(",")[0]
            
        user_agent = request.headers.get("User-Agent", "Unknown")
        
        # Простейший парсинг устройства
        os_device = "Unknown"
        if "iPhone" in user_agent or "iPad" in user_agent: os_device = "iOS"
        elif "Android" in user_agent: os_device = "Android"
        elif "Windows" in user_agent: os_device = "Windows"
        elif "Mac OS" in user_agent: os_device = "macOS"

        telegram_id = None
        if init_data:
            validated = validate_telegram_data(init_data, BOT_TOKEN)
            user_data = json.loads(validated.get('user', '{}'))
            telegram_id = user_data.get('id')
            
        existing_user = db.query(User).filter(User.bet_id == bet_id).first()
        if existing_user:
            if existing_user.telegram_id is None and telegram_id is not None:
                existing_user.telegram_id = telegram_id
            if source and not existing_user.source:
                existing_user.source = source
            # Обновляем IP при повторном входе
            existing_user.ip_address = ip_address
            existing_user.os_device = os_device
            existing_user.browser = user_agent[:200] # Сохраняем часть юзер-агента
            db.commit()
            return {"status": "ok", "already_exists": True}
            
        new_user = User(
            telegram_id=telegram_id,
            bet_id=bet_id,
            attempts_left=0,
            is_active=False,
            is_banned=False,
            source=source,
            ip_address=ip_address,
            os_device=os_device,
            browser=user_agent[:200] # Ограничиваем длину
        )
        db.add(new_user)
        db.commit()
        pending_count = db.query(User).filter(User.is_active == False, User.is_banned == False).count()
        await notifier.push({"count": pending_count})
        return {"status": "ok", "created": True}
    except Exception as e:
        print(f"[ERROR] Register error for bet_id={bet_id}: {e}")
        return {"status": "error", "message": str(e)}

# ---------- Эндпоинты для WebApp ----------
class MatchInfo(BaseModel):
    team1: str
    team2: str

@app.post("/webapp/predict")
async def webapp_predict(user_id: str = Form(...), text: str = Form(None), photo: UploadFile = File(None)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.bet_id == user_id).first()
        if not user:
            return {"error": "User not found. Please register via /start in Telegram bot."}
        if not user.is_active or user.is_banned:
            return {"error": "Account not active or banned."}
        if user.attempts_left <= 0:
            return {"error": "No attempts left. Contact manager to refill."}

        team1 = team2 = None
        if photo:
            try:
                photo_bytes = await photo.read()
                image_part = genai_types.Part.from_bytes(
                    data=photo_bytes,
                    mime_type=photo.content_type or "image/png",
                )
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        image_part,
                        "Extract football team names from this screenshot. Return only the team names, no extra text."
                    ],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=MatchInfo,
                        temperature=0.1
                    )
                )
                data = json.loads(response.text)
                team1 = data.get("team1", "").strip()
                team2 = data.get("team2", "").strip()
            except Exception as e:
                print(f"[ERROR] Photo processing: {e}")
                return {"error": "Error processing photo."}
        elif text:
            parts = re.split(r'[-–—]', text)
            if len(parts) >= 2:
                team1 = parts[0].strip()
                team2 = parts[1].strip()
            else:
                return {"error": "Invalid format. Use 'Team A - Team B'."}
        else:
            return {"error": "No input."}

        if not team1 or not team2 or team1 == "Unknown" or team2 == "Unknown":
            return {"error": "Could not determine team names."}

        stats1 = await get_team_stats(team1)
        stats2 = await get_team_stats(team2)
        pred = calculate_prediction(stats1, stats2)
        winner = pred["winner"]
        confidence = pred["confidence"]
        analysis_text = await generate_prediction_text(team1, team2, stats1, stats2, winner, confidence)
        winner_name = team1 if winner == "team1" else (team2 if winner == "team2" else "Ничья")
        total_over = random.randint(55, 75)
        corners_over = random.randint(55, 75)
        additional = f"Тотал голов (2.5): OVER ({total_over}%)\nТотал угловых (9.5): OVER ({corners_over}%)"

        user.attempts_left -= 1
        user.last_activity = datetime.utcnow()
        db.commit()
        full_text = f"Победитель: {winner_name}\nУверенность: {confidence}%\n{analysis_text}"
        await save_prediction_log(user.id, f"{team1} - {team2}", winner, confidence, full_text, additional)
        return {
            "prediction": {"winner": winner_name, "confidence": confidence},
            "additional": additional,
            "prediction_text": analysis_text
        }
    finally:
        db.close()

@app.get("/webapp/news")
async def webapp_news():
    current_time = time.time()
    if current_time - news_cache["last_update"] < NEWS_CACHE_TTL and news_cache["data"]:
        return {"news": news_cache["data"]}
    try:
        rss_url = "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=es-419&gl=US&ceid=US:es-419"
        feed = feedparser.parse(rss_url)
        news_list = []
        for entry in feed.entries[:15]:
            description = entry.get('summary', entry.get('description', ''))
            if description:
                description = re.sub(r'<.*?>', '', description)
                if len(description) > 120:
                    description = description[:117] + '...'
            news_list.append({
                "title": entry.title,
                "link": entry.link,
                "pubDate": entry.get('published', datetime.now().isoformat()),
                "description": description if description else "Нет описания"
            })
        news_cache["data"] = news_list
        news_cache["last_update"] = current_time
        return {"news": news_list}
    except Exception as e:
        print(f"News error: {e}")
        if news_cache["data"]:
            return {"news": news_cache["data"]}
        return {"news": []}

# ---------- Эндпоинты для фронтенда ----------
@app.get("/user_status")
async def user_status(bet_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.bet_id == bet_id).first()
        if not user:
            return {"status": "not_found"}
        return {
            "status": "active" if (user.is_active and not user.is_banned) else ("banned" if user.is_banned else "pending"),
            "attempts": user.attempts_left if (user.is_active and not user.is_banned) else 0
        }
    finally:
        db.close()

@app.get("/user_history")
async def user_history(bet_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.bet_id == bet_id).first()
        if not user:
            return {"history": []}
        logs = db.query(PredictionLog).filter(PredictionLog.user_id == user.id).order_by(PredictionLog.created_at.desc()).all()
        history = [{"created_at": log.created_at.isoformat(), "match_description": log.match_description, "winner": log.winner, "confidence": log.confidence} for log in logs]
        return {"history": history}
    finally:
        db.close()

# ---------- Запуск ----------
async def start_bot():
    await bot.delete_webhook()
    await dp.start_polling(bot)

async def run_fastapi():
    port = int(os.getenv("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(start_bot(), run_fastapi())

if __name__ == "__main__":
    asyncio.run(main())
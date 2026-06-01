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
import urllib.request
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from collections import OrderedDict
from io import StringIO
import shutil
import uuid

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
from fastapi import Body
from fastapi import Query
from datetime import datetime, timedelta

# ---- База данных (SQLAlchemy) ----
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Float, BigInteger, Text, func, case
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from sqlalchemy.orm import joinedload

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
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile

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
    
    # --- Поля для арбитража (Этап 4) ---
    click_id = Column(String, nullable=True, index=True) # ID клика из рекламной сети
    cost_per_lead = Column(Float, default=0.0) # Цена за лида (CPA)
    is_blocked_bot = Column(Boolean, default=False) # Флаг отвала (заблокировал бота)

class TrafficEvent(Base):
    """Таблица-журнал для всех микро-конверсий"""
    __tablename__ = "traffic_events"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    telegram_id = Column(BigInteger, nullable=True)
    event_type = Column(String, nullable=False) # 'bot_start', 'lead_registered', 'approved', 'prediction'
    source = Column(String, nullable=True)
    click_id = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

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

class BroadcastLog(Base):
    __tablename__ = "broadcast_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    segment = Column(String)
    text = Column(Text) # Текст рассылки (может быть длинным)
    image_url = Column(String, nullable=True) # Задел на будущее для картинок
    sent_count = Column(Integer, default=0) # Скольким людям успешно ушло
    created_at = Column(DateTime, default=datetime.utcnow)

    staff = relationship("Staff")    

Base.metadata.create_all(bind=engine)

class ScheduledBroadcast(Base):
    __tablename__ = "scheduled_broadcasts"
    id = Column(Integer, primary_key=True, index=True)
    staff_id = Column(Integer, ForeignKey("staff.id"))
    user_ids = Column(Text) # Сохраняем массив ID в виде JSON строки
    text = Column(Text)
    msg_type = Column(String)
    delay = Column(Float, default=0.0)
    file_path = Column(String, nullable=True) # Путь к загруженному медиа
    scheduled_time = Column(DateTime) # Время запуска (в UTC)
    is_completed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    staff = relationship("Staff")

# --- АВТО-МИГРАЦИЯ ДЛЯ POSTGRESQL (Этап 3 и 4) ---
from sqlalchemy.sql import text

try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS ip_address VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS country VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS os_device VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS browser VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS click_id VARCHAR;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS cost_per_lead FLOAT DEFAULT 0.0;"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked_bot BOOLEAN DEFAULT FALSE;"))
        print("✅ Миграция базы данных успешно выполнена (новые арбитражные колонки добавлены)!")
except Exception as e:
    print(f"⚠️ Миграция пропущена: {e}")
# ---------------------------------------------

def log_traffic_event(db: Session, event_type: str, user_id: int = None, telegram_id: int = None, source: str = None, click_id: str = None):
    """Глобальный счетчик событий для воронок"""
    try:
        event = TrafficEvent(
            user_id=user_id,
            telegram_id=telegram_id,
            event_type=event_type,
            source=source,
            click_id=click_id
        )
        db.add(event)
        db.commit()
    except Exception as e:
        print(f"[EVENT ERROR] {e}")
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

# ---------- Гео-локация по IP ----------
def fetch_geo(ip: str) -> str:
    """Синхронная функция запроса к бесплатному API (работает без ключа)"""
    # Если тест на локальном ПК
    if ip in ("127.0.0.1", "localhost", "0.0.0.0"): 
        return "Local"
    try:
        # Делаем запрос к ip-api (лимит 45 запросов в минуту, для нас пока хватит)
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?lang=en", timeout=2.0) as response:
            data = json.loads(response.read().decode())
            if data.get("status") == "success":
                return data.get("country") # Вернет "Peru", "Egypt" и т.д.
    except Exception as e:
        print(f"[GEO ERROR] Не удалось определить страну для {ip}: {e}")
    return "Unknown"

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

async def get_advanced_match_data(team1_name: str, team2_name: str) -> dict:
    """Сбор глубоких данных: Форма, H2H, Забитые/Пропущенные голы"""
    if not API_FOOTBALL_KEY:
        return {"t1_form": [0.5]*5, "t2_form": [0.5]*5, "t1_str": "W W D L W", "t2_str": "L D L W D", "h2h_str": "Нет данных", "t1_gs": 1.5, "t1_gc": 1.0, "t2_gs": 1.2, "t2_gc": 1.5, "h2h_t1_wins": 0, "h2h_t2_wins": 0}

    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": "v3.football.api-sports.io"}
    
    async with httpx.AsyncClient() as client:
        try:
            r1 = await client.get("https://v3.football.api-sports.io/teams", headers=headers, params={"search": team1_name})
            r2 = await client.get("https://v3.football.api-sports.io/teams", headers=headers, params={"search": team2_name})
            id1 = r1.json().get("response", [{}])[0].get("team", {}).get("id") if r1.json().get("response") else None
            id2 = r2.json().get("response", [{}])[0].get("team", {}).get("id") if r2.json().get("response") else None
            if not id1 or not id2: raise ValueError("Команды не найдены")

            f1 = await client.get("https://v3.football.api-sports.io/fixtures", headers=headers, params={"team": id1, "last": 5, "status": "FT"})
            f2 = await client.get("https://v3.football.api-sports.io/fixtures", headers=headers, params={"team": id2, "last": 5, "status": "FT"})
            
            def parse_form(fixtures, team_id):
                num_form, str_form = [], []
                g_scored, g_conceded = 0, 0
                for m in fixtures.get("response", []):
                    home = m["teams"]["home"]["id"] == team_id
                    win = m["teams"]["home"]["winner"] if home else m["teams"]["away"]["winner"]
                    
                    g_scored += m["goals"]["home"] if home else m["goals"]["away"]
                    g_conceded += m["goals"]["away"] if home else m["goals"]["home"]
                    
                    if win is True: num_form.append(1); str_form.append("W")
                    elif win is False: num_form.append(0); str_form.append("L")
                    else: num_form.append(0.5); str_form.append("D")
                num_form.reverse(); str_form.reverse()
                return num_form, " ".join(str_form), g_scored/5, g_conceded/5

            t1_num, t1_str, t1_gs, t1_gc = parse_form(f1.json(), id1)
            t2_num, t2_str, t2_gs, t2_gc = parse_form(f2.json(), id2)

            h2h_res = await client.get("https://v3.football.api-sports.io/fixtures/headtohead", headers=headers, params={"h2h": f"{id1}-{id2}", "last": 3})
            h2h_t1_wins, h2h_t2_wins = 0, 0
            h2h_parsed = []
            for m in h2h_res.json().get("response", []):
                home_win = m["teams"]["home"]["winner"]
                if m["teams"]["home"]["id"] == id1:
                    if home_win is True: h2h_t1_wins += 1
                    elif home_win is False: h2h_t2_wins += 1
                else:
                    if home_win is True: h2h_t2_wins += 1
                    elif home_win is False: h2h_t1_wins += 1
                h2h_parsed.append(f"{m['goals']['home']}-{m['goals']['away']}")

            h2h_str = " | ".join(h2h_parsed) if h2h_parsed else "Нет свежих очных встреч"

            return {
                "t1_form": t1_num if t1_num else [0.5]*5, "t2_form": t2_num if t2_num else [0.5]*5,
                "t1_str": t1_str, "t2_str": t2_str,
                "t1_gs": t1_gs, "t1_gc": t1_gc, "t2_gs": t2_gs, "t2_gc": t2_gc,
                "h2h_t1_wins": h2h_t1_wins, "h2h_t2_wins": h2h_t2_wins, "h2h_str": h2h_str
            }
        except Exception as e:
            print(f"API Error: {e}")
            return {"t1_form": [0.5]*5, "t2_form": [0.5]*5, "t1_str": "?", "t2_str": "?", "h2h_str": "Ошибка", "t1_gs": 1, "t1_gc": 1, "t2_gs": 1, "t2_gc": 1, "h2h_t1_wins": 0, "h2h_t2_wins": 0}

# Кэш для переводов (чтобы не тратить лимиты Gemini на одни и те же команды)
translation_cache = {}

async def translate_team_name(team_name: str) -> str:
    """Умный переводчик с кэшированием и защитой от лимитов"""
    if team_name in translation_cache:
        return translation_cache[team_name]
        
    prompt = f"Переведи название футбольной команды '{team_name}' на английский язык для поиска в базе данных API-Football. Если это аббревиатура (как ПСЖ), напиши стандартное английское название (Paris Saint Germain). В ответе выдай ТОЛЬКО название команды, без кавычек и лишних слов."
    
    # Делаем 3 попытки с паузой, чтобы бесплатный Gemini не захлебнулся
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=prompt)
            if response and response.text:
                result = response.text.strip()
                translation_cache[team_name] = result
                return result
        except Exception as e:
            await asyncio.sleep(1.5) # Пауза перед новой попыткой
    return team_name

async def get_advanced_match_data(team1_name: str, team2_name: str) -> dict:
    """Сбор глубоких данных: Форма, H2H, Забитые/Пропущенные голы"""
    if not API_FOOTBALL_KEY:
        return {"t1_form": [0.5]*5, "t2_form": [0.5]*5, "t1_str": "W W D L W", "t2_str": "L D L W D", "h2h_str": "Нет данных", "t1_gs": 1.5, "t1_gc": 1.0, "t2_gs": 1.2, "t2_gc": 1.5, "h2h_t1_wins": 0, "h2h_t2_wins": 0}

    # Переводим названия команд на английский перед запросом к базе
    eng_team1 = await translate_team_name(team1_name)
    eng_team2 = await translate_team_name(team2_name)

    headers = {"x-rapidapi-key": API_FOOTBALL_KEY, "x-rapidapi-host": "v3.football.api-sports.io"}
    
    async with httpx.AsyncClient() as http_client:
        try:
            r1 = await http_client.get("https://v3.football.api-sports.io/teams", headers=headers, params={"search": eng_team1})
            r2 = await http_client.get("https://v3.football.api-sports.io/teams", headers=headers, params={"search": eng_team2})
            id1 = r1.json().get("response", [{}])[0].get("team", {}).get("id") if r1.json().get("response") else None
            id2 = r2.json().get("response", [{}])[0].get("team", {}).get("id") if r2.json().get("response") else None
            if not id1 or not id2: raise ValueError("Команды не найдены")

            f1 = await http_client.get("https://v3.football.api-sports.io/fixtures", headers=headers, params={"team": id1, "last": 5, "status": "FT"})
            f2 = await http_client.get("https://v3.football.api-sports.io/fixtures", headers=headers, params={"team": id2, "last": 5, "status": "FT"})
            
            def parse_form(fixtures, team_id):
                num_form, str_form = [], []
                g_scored, g_conceded = 0, 0
                for m in fixtures.get("response", []):
                    home = m["teams"]["home"]["id"] == team_id
                    win = m["teams"]["home"]["winner"] if home else m["teams"]["away"]["winner"]
                    
                    g_scored += m["goals"]["home"] if home else m["goals"]["away"]
                    g_conceded += m["goals"]["away"] if home else m["goals"]["home"]
                    
                    if win is True: num_form.append(1); str_form.append("W")
                    elif win is False: num_form.append(0); str_form.append("L")
                    else: num_form.append(0.5); str_form.append("D")
                num_form.reverse(); str_form.reverse()
                return num_form, " ".join(str_form), g_scored/5, g_conceded/5

            t1_num, t1_str, t1_gs, t1_gc = parse_form(f1.json(), id1)
            t2_num, t2_str, t2_gs, t2_gc = parse_form(f2.json(), id2)

            h2h_res = await http_client.get("https://v3.football.api-sports.io/fixtures/headtohead", headers=headers, params={"h2h": f"{id1}-{id2}", "last": 3})
            h2h_t1_wins, h2h_t2_wins = 0, 0
            h2h_parsed = []
            for m in h2h_res.json().get("response", []):
                home_win = m["teams"]["home"]["winner"]
                if m["teams"]["home"]["id"] == id1:
                    if home_win is True: h2h_t1_wins += 1
                    elif home_win is False: h2h_t2_wins += 1
                else:
                    if home_win is True: h2h_t2_wins += 1
                    elif home_win is False: h2h_t1_wins += 1
                h2h_parsed.append(f"{m['goals']['home']}-{m['goals']['away']}")

            h2h_str = " | ".join(h2h_parsed) if h2h_parsed else "Нет свежих очных встреч"

            return {
                "t1_form": t1_num if t1_num else [0.5]*5, "t2_form": t2_num if t2_num else [0.5]*5,
                "t1_str": t1_str, "t2_str": t2_str,
                "t1_gs": t1_gs, "t1_gc": t1_gc, "t2_gs": t2_gs, "t2_gc": t2_gc,
                "h2h_t1_wins": h2h_t1_wins, "h2h_t2_wins": h2h_t2_wins, "h2h_str": h2h_str
            }
        except Exception as e:
            print(f"API Error: {e}")
            return {"t1_form": [0.5]*5, "t2_form": [0.5]*5, "t1_str": "?", "t2_str": "?", "h2h_str": "Ошибка", "t1_gs": 1, "t1_gc": 1, "t2_gs": 1, "t2_gc": 1, "h2h_t1_wins": 0, "h2h_t2_wins": 0}

def calculate_prediction(data: dict) -> dict:
    """Математический движок на базе весов каппера с динамическим пулом доп. исходов"""
    f1_score, f2_score = sum(data['t1_form']), sum(data['t2_form'])
    score1, score2 = 0, 0

    # 1. Форма (15%)
    score1 += (f1_score / 5) * 15
    score2 += (f2_score / 5) * 15

    # 2. Личные встречи (24%)
    h2h_total = data['h2h_t1_wins'] + data['h2h_t2_wins']
    if h2h_total > 0:
        score1 += (data['h2h_t1_wins'] / h2h_total) * 24
        score2 += (data['h2h_t2_wins'] / h2h_total) * 24
    else:
        score1 += 12; score2 += 12

    # 3. Свое поле (24% - отдаем команде 1)
    score1 += 24

    # 4. Класс / Таблица (21% - эмуляция через разницу мячей)
    net1, net2 = data['t1_gs'] - data['t1_gc'], data['t2_gs'] - data['t2_gc']
    if net1 > net2: score1 += 21
    elif net2 > net1: score2 += 21
    else: score1 += 10.5; score2 += 10.5

    diff = abs(score1 - score2)
    avg_goals = (data['t1_gs'] + data['t2_gs'] + data['t1_gc'] + data['t2_gc']) / 2

    import random
    
    # 5. Определение сценария и загрузка пула логичных исходов
    if avg_goals < 2.0 and diff < 15:
        winner = "Ничья"
        confidence = random.uniform(68, 76)
        pool = [
            "Тотал меньше (2.5)", "Тотал меньше (1.5)", "Ничья в 1 тайме", 
            "Обе забьют: НЕТ", "Гол во 2 тайме: НЕТ", "Тотал угловых < 9.5", "Желтые карточки > 3.5"
        ]
        
    elif avg_goals >= 2.8 and diff < 20:
        winner = "Тотал больше (2.5)"
        confidence = random.uniform(78, 86)
        pool = [
            "Обе забьют: ДА", "Гол в 1 тайме", "Тотал больше (3.5)", 
            "Тотал угловых > 9.5", "Гол в обоих таймах", "Удары в створ > 8.5"
        ]

    elif diff > 30:
        winner = "team1" if score1 > score2 else "team2"
        confidence = random.uniform(88, 94)
        pool = [
            "Победа с форой (-1)", "Победа с форой (-1.5)", "ИТБ фаворита (1.5)", 
            "Гол фаворита в 1 тайме", "Фаворит забьет в обоих таймах", 
            "Сухая победа фаворита", "Угловые фаворита > 5.5"
        ]

    else:
        winner = "team1" if score1 > score2 else "team2"
        confidence = random.uniform(75, 84)
        pool = [
            "Тотал угловых > 8.5", "Гол во 2 тайме", "Победа в 1 мяч или ничья", 
            "Обе забьют: ДА", "ИТБ победителя (1.0)", "Желтые карточки > 4.5"
        ]

    # 6. Детектор СКИПА и генерация строки дополнительных исходов
    if confidence < 65:
        winner = "ПРОПУСК (Сложный матч)"
        additional = "Слишком высокие риски. Ставить не рекомендуется."
    else:
        # Выбираем случайное количество исходов (от 2 до 5)
        num_addons = random.randint(2, 5)
        
        # Защита: берем не больше элементов, чем есть в самом пуле
        selected_addons = random.sample(pool, min(num_addons, len(pool)))
        
        # Присваиваем каждому выбранному исходу свой случайный процент (от 68% до 89%)
        addon_strings = []
        for addon in selected_addons:
            addon_conf = random.randint(68, 89)
            addon_strings.append(f"{addon} {addon_conf}%")
        
        # Склеиваем их через разделитель
        additional = " | ".join(addon_strings)

    return {"winner": winner, "confidence": round(confidence, 2), "additional": additional}

async def generate_prediction_text(team1, team2, data, winner, confidence, additional):
    prompt = f"""
    Ты — элитный спортивный аналитик и футбольный журналист. Напиши премиальный разбор матча (7-9 предложений).

    ДАННЫЕ ДЛЯ АНАЛИЗА:
    Матч: {team1} против {team2}
    Команда 1 ({team1}): форма {data['t1_str']}, забивает в среднем {data['t1_gs']}, пропускает {data['t1_gc']}.
    Команда 2 ({team2}): форма {data['t2_str']}, забивает {data['t2_gs']}, пропускает {data['t2_gc']}.
    H2H (очные встречи): {data['h2h_str']}
    Главный прогноз: {winner}
    Доп. исходы: {additional}

    ЖЕСТКИЕ ПРАВИЛА:
    1. ЗАПРЕЩЕНО выдумывать травмы или отсутствующих игроков. Анализируй только стилистику, голы и H2H.
    2. ЗАПРЕЩЕНЫ призывы (ссылки в био, пиши в ЛС).
    3. Обоснуй логически, почему выбран {winner} и {additional}.
    4. Не пиши проценты уверенности в тексте, только аналитика.
    5. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать слова "алгоритм", "нейросеть", "ИИ" или писать от лица машины. Пиши от первого лица ("Мой анализ показывает...") или безлично. Ты — живой эксперт.
    """
    
    # Защита от лимитов: 3 попытки с паузами
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            await asyncio.sleep(1.5)
            
    return "Сложный матч. Рекомендуется опираться на статистику забитых и пропущенных мячей, а также историю личных встреч."

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
    match_data = await get_advanced_match_data(team1, team2)
    pred = calculate_prediction(match_data)
    winner = pred["winner"]
    confidence = pred["confidence"]
    additional = pred.get("additional", "")
    analysis_text = await generate_prediction_text(team1, team2, match_data, winner, confidence, additional)
    
    # Имя победителя (или тотал, если алгоритм выбрал его)
    if winner == "team1": winner_name = team1
    elif winner == "team2": winner_name = team2
    else: winner_name = winner
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
    
    # Умный редирект по ролям
    if staff.role == "buyer":
        response = RedirectResponse(url="/buyer/leads", status_code=303)
    else:
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

# ---------- Панель Баеров (Аналитика трафика) ----------
@app.get("/buyer/leads", response_class=HTMLResponse)
async def buyer_leads_page(
    request: Request,
    search: str = Query(None),
    status: str = Query(None),
    country: str = Query(None), # НОВЫЙ ФИЛЬТР
    source: str = Query(None),  # НОВЫЙ ФИЛЬТР
    page: int = Query(1),
    per_page: int = Query(50),  # Для таблицы делаем 50 по умолчанию
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
    elif status == "pending":
        query = query.filter(User.is_active == False, User.is_banned == False)

    # Применяем новые фильтры
    if country:
        query = query.filter(User.country == country)
    if source:
        query = query.filter(User.source == source)

    # Получаем списки всех уникальных Гео и Источников для выпадающих меню
    distinct_countries = [r[0] for r in db.query(User.country).distinct().all() if r[0]]
    distinct_sources = [r[0] for r in db.query(User.source).distinct().all() if r[0]]

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    users = query.order_by(User.created_at.desc()).offset(offset).limit(per_page).all()

    return templates.TemplateResponse("buyer_leads.html", {
        "request": request,
        "users": users,
        "page": page,
        "total_pages": total_pages,
        "search_query": search or "",
        "status_filter": status or "",
        "country_filter": country or "",
        "source_filter": source or "",
        "distinct_countries": distinct_countries,
        "distinct_sources": distinct_sources,
        "staff_role": staff.role
    })

# ---------- Установка CPA для кампании ----------
@app.post("/admin/analytics/set_cpa")
async def set_campaign_cpa(
    payload: dict = Body(...), 
    db: Session = Depends(get_db), 
    staff=Depends(get_current_staff)
):
    if not staff:
        return {"status": "error", "message": "Unauthorized"}
        
    source = payload.get("source")
    cpa = payload.get("cpa")
    
    try:
        cpa_float = float(cpa)
    except (ValueError, TypeError):
        return {"status": "error", "message": "Неверный формат числа"}

    # Обновляем CPA у всех юзеров с этим источником
    if source == "Органика / Без UTM" or not source:
        db.query(User).filter((User.source == None) | (User.source == "")).update({"cost_per_lead": cpa_float}, synchronize_session=False)
    else:
        db.query(User).filter(User.source == source).update({"cost_per_lead": cpa_float}, synchronize_session=False)
        
    db.commit()
    return {"status": "ok"}

    # ---------- Аналитика Трафика (Воронки) ----------
@app.get("/admin/analytics/traffic", response_class=HTMLResponse)
async def traffic_analytics_page(request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")

    # 1. Агрегация данных по источникам (UTM Source)
    query = db.query(
        User.source,
        func.count(User.id).label('total_leads'),
        func.sum(case((User.is_active == True, 1), else_=0)).label('approved'),
        func.sum(case((User.is_blocked_bot == True, 1), else_=0)).label('blocked'),
        func.max(User.cost_per_lead).label('cpa') # Берем CPA кампании
    ).group_by(User.source).all()

    analytics_data = []
    for row in query:
        source_name = row.source if row.source else "Органика / Без UTM"
        total_leads = row.total_leads or 0
        approved = row.approved or 0
        blocked = row.blocked or 0
        
        # 2. Считаем юзеров, сделавших прогнозы (из TrafficEvent)
        preds_query = db.query(
            func.count(func.distinct(TrafficEvent.user_id)).label('active_users'),
            func.count(TrafficEvent.id).label('total_preds')
        ).filter(
            TrafficEvent.source == row.source, 
            TrafficEvent.event_type == 'prediction'
        ).first()
        
        active_users = preds_query.active_users or 0
        total_preds = preds_query.total_preds or 0

        # 3. Вычисляем конверсии (Воронка)
        lead2appr = round((approved / total_leads * 100), 2) if total_leads > 0 else 0
        appr2act = round((active_users / approved * 100), 2) if approved > 0 else 0
        block_rate = round((blocked / total_leads * 100), 2) if total_leads > 0 else 0
        
        # Среднее кол-во прогнозов на одного активного юзера
        avg_preds = round((total_preds / active_users), 1) if active_users > 0 else 0

        cpa_val = row.cpa or 0.0
        spent = round(total_leads * cpa_val, 2)
        
        analytics_data.append({
            "source": source_name,
            "total_leads": total_leads,
            "approved": approved,
            "lead2appr": lead2appr,
            "active_users": active_users,
            "appr2act": appr2act,
            "total_preds": total_preds,
            "avg_preds": avg_preds,
            "blocked": blocked,
            "block_rate": block_rate,
            "cpa": cpa_val,     # <-- Новое
            "spent": spent      # <-- Новое
        })

    # Сортируем по количеству лидов (от большего к меньшему)
    analytics_data.sort(key=lambda x: x['total_leads'], reverse=True)

    return templates.TemplateResponse("traffic_analytics.html", {
        "request": request,
        "analytics": analytics_data,
        "staff_role": staff.role
    })

    # ---------- Когортный Анализ (Product Retention) ----------
# ---------- Когортный Анализ (Product Retention) ----------
@app.get("/admin/analytics/cohorts", response_class=HTMLResponse)
async def cohorts_page(
    request: Request, 
    start_date: str = Query(None), 
    end_date: str = Query(None), 
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff:
        return RedirectResponse(url="/admin/login")

    # 1. Логика фильтрации по датам
    try:
        if start_date and end_date:
            s_date = datetime.strptime(start_date, '%Y-%m-%d')
            e_date = datetime.strptime(end_date, '%Y-%m-%d')
        else:
            # По умолчанию: последние 14 дней
            e_date = datetime.utcnow()
            s_date = e_date - timedelta(days=14)
    except ValueError:
        e_date = datetime.utcnow()
        s_date = e_date - timedelta(days=14)

    # Округляем начало дня и конец дня
    s_date = s_date.replace(hour=0, minute=0, second=0, microsecond=0)
    e_date = e_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    # 2. Формируем когорты (группируем юзеров по дате регистрации в заданном диапазоне)
    users = db.query(User.id, User.created_at).filter(User.created_at >= s_date, User.created_at <= e_date).all()
    
    cohorts = {}
    user_reg_dates = {}
    for u in users:
        d_str = u.created_at.strftime('%Y-%m-%d')
        if d_str not in cohorts:
            cohorts[d_str] = {"total": 0, "users": set(), "retention": {0:0, 1:0, 2:0, 3:0, 7:0}}
        cohorts[d_str]["total"] += 1
        cohorts[d_str]["users"].add(u.id)
        user_reg_dates[u.id] = u.created_at.replace(hour=0, minute=0, second=0, microsecond=0)

    # 3. Достаем историю генерации прогнозов
    user_ids = list(user_reg_dates.keys())
    if user_ids:
        events = db.query(TrafficEvent.user_id, TrafficEvent.timestamp).filter(
            TrafficEvent.user_id.in_(user_ids),
            TrafficEvent.event_type == 'prediction'
        ).all()

        user_active_days = {}
        for ev in events:
            uid = ev.user_id
            if uid not in user_active_days:
                user_active_days[uid] = set()
                
            ev_d = ev.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            delta_days = (ev_d - user_reg_dates[uid]).days
            
            if delta_days >= 0:
                user_active_days[uid].add(delta_days)

        # 4. Вычисляем Retention (возвраты по дням)
        for cohort_date, data in cohorts.items():
            for uid in data["users"]:
                if uid in user_active_days:
                    days_active = user_active_days[uid]
                    for d in [0, 1, 2, 3, 7]:
                        if d in days_active:
                            data["retention"][d] += 1

    # Форматируем данные для отправки в HTML
    result_cohorts = []
    for d in sorted(cohorts.keys(), reverse=True):
        total = cohorts[d]["total"]
        result_cohorts.append({
            "date": d,
            "total": total,
            "d0": round(cohorts[d]["retention"][0] / total * 100) if total > 0 else 0,
            "d1": round(cohorts[d]["retention"][1] / total * 100) if total > 0 else 0,
            "d2": round(cohorts[d]["retention"][2] / total * 100) if total > 0 else 0,
            "d3": round(cohorts[d]["retention"][3] / total * 100) if total > 0 else 0,
            "d7": round(cohorts[d]["retention"][7] / total * 100) if total > 0 else 0,
        })

    return templates.TemplateResponse("cohorts.html", {
        "request": request,
        "cohorts": result_cohorts,
        "start_date": s_date.strftime('%Y-%m-%d'),
        "end_date": e_date.strftime('%Y-%m-%d'),
        "staff_role": staff.role
    })

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
            except TelegramForbiddenError:
                user.is_blocked_bot = True
                user.is_active = False
                db.commit()
            except Exception:
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

# ---------- Рассылки (Этап 5: PRO-Модуль) ----------

# 1. Движок Spintax и Макросов
def process_message_text(text: str, user) -> str:
    if not text:
        return ""
        
    # 1. СНАЧАЛА ОБРАБАТЫВАЕМ МАКРОСЫ (чтобы Spintax не съел их скобки)
    
    # Пытаемся достать имя. В Telegram это 'full_name' (Имя + Фамилия). Берем только первое слово (Имя).
    f_name = "друг"
    if getattr(user, 'full_name', None):
        f_name = str(user.full_name).split()[0]
    elif getattr(user, 'username', None):
        f_name = str(user.username)
        
    username_macro = f"@{user.username}" if getattr(user, 'username', None) else "друг"
    
    text = text.replace("{first_name}", f_name)
    text = text.replace("{username}", username_macro)
    text = text.replace("{user_id}", str(user.id))
    text = text.replace("{attempts}", str(getattr(user, 'attempts_left', 0)))
    
    # 2. ПОТОМ ОБРАБАТЫВАЕМ SPINTAX: {Вариант1|Вариант2}
    pattern = re.compile(r'\{([^{}]+)\}')
    def replacer(match):
        content = match.group(1)
        # Если внутри нет разделителя '|', значит это случайные скобки текста, не трогаем их
        if '|' not in content:
            return f"{{{content}}}"
        
        options = content.split('|')
        return random.choice(options)
    
    # Крутим цикл, пока в тексте есть валидный Spintax (поддерживает вложенность)
    while pattern.search(text):
        text = pattern.sub(replacer, text)
        
    return text
        
    # Обработка макросов (переменных)
    first_name = getattr(user, 'first_name', '') or "друг"
    username = f"@{user.username}" if getattr(user, 'username', None) else "друг"
    
    text = text.replace("{first_name}", first_name)
    text = text.replace("{username}", username)
    text = text.replace("{user_id}", str(user.id))
    text = text.replace("{attempts}", str(getattr(user, 'attempts_left', 0)))
    
    return text

# 2. Фоновая задача для безопасной рассылки с поддержкой медиа
async def run_broadcast_task(user_ids: list, text: str, msg_type: str, delay: float, staff_id: int, file_path: str = None):
    db = SessionLocal()
    try:
        users_to_send = db.query(User).filter(User.id.in_(user_ids), User.telegram_id.isnot(None), User.is_banned == False).all()
        success_count = 0
        
        # Переменная для сохранения ID файла в Telegram, чтобы не грузить файл 1000 раз
        cached_file_id = None
        
        for u in users_to_send:
            final_text = process_message_text(text, u) if text else ""
            try:
                if msg_type == "text":
                    await bot.send_message(u.telegram_id, final_text, parse_mode="HTML")
                else:
                    # Если файл уже отправлен первому юзеру, берем file_id. Иначе берем файл с диска сервера.
                    media_to_send = cached_file_id if cached_file_id else FSInputFile(file_path)
                    sent_msg = None
                    
                    if msg_type == "media":
                        if file_path.lower().endswith(('.mp4', '.avi', '.mov')):
                            sent_msg = await bot.send_video(u.telegram_id, video=media_to_send, caption=final_text, parse_mode="HTML")
                            if not cached_file_id: cached_file_id = sent_msg.video.file_id
                        else:
                            sent_msg = await bot.send_photo(u.telegram_id, photo=media_to_send, caption=final_text, parse_mode="HTML")
                            if not cached_file_id: cached_file_id = sent_msg.photo[-1].file_id
                            
                    elif msg_type == "video_note":
                        sent_msg = await bot.send_video_note(u.telegram_id, video_note=media_to_send)
                        if not cached_file_id: cached_file_id = sent_msg.video_note.file_id
                        # Кружочки не поддерживают текст внутри себя, поэтому шлем текст отдельным сообщением
                        if final_text:
                            await bot.send_message(u.telegram_id, final_text, parse_mode="HTML")
                            
                    elif msg_type == "voice":
                        sent_msg = await bot.send_voice(u.telegram_id, voice=media_to_send, caption=final_text, parse_mode="HTML")
                        if not cached_file_id: cached_file_id = sent_msg.voice.file_id
                        
                success_count += 1
                
                # Безопасная задержка
                sleep_time = delay if delay > 0 else 0.05
                await asyncio.sleep(sleep_time) 
                
            except TelegramForbiddenError:
                u.is_blocked_bot = True
                u.is_active = False
            except Exception as e:
                print(f"[BROADCAST ERROR] User {u.id}: {e}")
                
        db.commit() 
        broadcast_record = BroadcastLog(staff_id=staff_id, segment="manual_selection", text=f"[{msg_type.upper()}] {text}", sent_count=success_count)
        db.add(broadcast_record)
        log = StaffLog(staff_id=staff_id, action=f"PRO broadcast ({msg_type}) sent to {success_count} users")
        db.add(log)
        db.commit()
        print(f"✅ Рассылка {msg_type} завершена. Успешно: {success_count}/{len(users_to_send)}")
    finally:
        db.close()
        # После окончания рассылки удаляем файл с сервера, чтобы не забивать память
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

# Эндпоинт "История рассылок"
@app.get("/broadcasts", response_class=HTMLResponse)
async def broadcast_logs_page(request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff: return RedirectResponse(url="/admin/login")
    logs = db.query(BroadcastLog).options(joinedload(BroadcastLog.staff)).order_by(BroadcastLog.created_at.desc()).limit(50).all()
    return templates.TemplateResponse("broadcast_logs.html", {"request": request, "logs": logs, "staff_role": staff.role})

# 3. Эндпоинт запуска рассылки (Теперь с Планировщиком)
@app.post("/api/pro_broadcast")
async def api_pro_broadcast(
    request: Request, 
    user_ids: str = Form(...), 
    text: str = Form(""), 
    type: str = Form("text"), 
    delay: float = Form(0.0), 
    scheduled_time: str = Form(None), # <-- НОВОЕ ПОЛЕ (UTC строка)
    media_file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    staff = await get_current_staff(request, db)
    if not staff: raise HTTPException(status_code=401)
    if staff.role == "buyer": raise HTTPException(status_code=403, detail="У баеров нет прав на рассылку")
    
    try:
        u_ids = json.loads(user_ids)
    except:
        return {"status": "error", "message": "Неверный формат ID пользователей"}
    
    file_path = None
    if type != "text" and media_file:
        os.makedirs("uploads", exist_ok=True)
        ext = media_file.filename.split('.')[-1]
        file_path = f"uploads/{uuid.uuid4().hex}.{ext}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(media_file.file, buffer)
            
    if scheduled_time:
        # Конвертируем строку из фронтенда в Python datetime (UTC)
        dt_utc = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')).replace(tzinfo=None)
        
        new_scheduled = ScheduledBroadcast(
            staff_id=staff.id,
            user_ids=user_ids, # Кладем JSON строку
            text=text,
            msg_type=type,
            delay=delay,
            file_path=file_path,
            scheduled_time=dt_utc
        )
        db.add(new_scheduled)
        db.commit()
        return {"status": "ok", "message": "Рассылка успешно запланирована!"}
    else:
        # Запускаем в фоне прямо сейчас
        asyncio.create_task(run_broadcast_task(u_ids, text, type, delay, staff.id, file_path))
        return {"status": "ok", "message": "Рассылка запущена в фоновом режиме!"}

# 4. Эндпоинт для AI Генерации текста (Использует твой Gemini)
class AIGenerateRequest(BaseModel):
    prompt: str

@app.post("/api/generate_ai_message")
async def generate_ai_message(payload: AIGenerateRequest, request: Request, db: Session = Depends(get_db)):
    staff = await get_current_staff(request, db)
    if not staff: raise HTTPException(status_code=401)
    
    system_instruction = """
    Ты — профессиональный копирайтер. Пользователь написал черновик для рассылки в Telegram-боте. 
    Твоя задача — переписать этот текст, сделав его конвертящим, и добавить глубокий Spintax.
    
    ПРАВИЛА КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ:
    1. МАКРОСЫ: Если используешь переменные first_name, username, user_id или attempts, они ДОЛЖНЫ быть строго в фигурных скобках: {attempts}, {first_name}. НЕ УБИРАЙ СКОБКИ!
    2. SPINTAX: Делай вариативность максимальной. Рандомизируй не только отдельные слова, но и целые фразы: {Привет, бро|Салют, {first_name}|Йоу, как успехи}. Сделай 4-6 синонимов на каждое ключевое слово/призыв.
    3. ТОН: Текст должен быть живым, эмоциональным и разговорным. Используй локальный сленг, если это подходит контексту. Никакого сухого официоза.
    4. ЗАПРЕЩЕНО писать банальные маркетинговые фразы вроде "пишите в личку", "ссылка в закрепе" и подобный мусор.
    5. Выдай только готовый текст, без вступительных слов.
    """
    
    try:
        response = await asyncio.to_thread(
            client.models.generate_content, 
            model=MODEL_NAME, 
            contents=[system_instruction, "Черновик менеджера: " + payload.prompt]
        )
        if response and response.text:
            return {"status": "ok", "text": response.text.strip()}
        return {"status": "error", "message": "Пустой ответ от нейросети"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
    request: Request,
    bet_id: str, 
    init_data: str = Query(None), 
    source: str = Query(None), 
    click_id: str = Query(None), # <-- Ловим Click ID от баеров
    db: Session = Depends(get_db)
):
    try:
        # --- ОТЛАДОЧНЫЕ ЛОГИ ДЛЯ ПРОВЕРКИ ТЕЛЕГРАМА ---
        print(f"=========================================")
        print(f"🔍 [DEBUG REG] Пришел запрос для bet_id: {bet_id}")
        print(f"🔍 [DEBUG REG] Наличие init_data: {'ДА' if init_data else 'НЕТ (ПУСТО)'}")
        if init_data:
            print(f"🔍 [DEBUG REG] Сырой init_data: {init_data[:100]}...") 
        # ----------------------------------------------

        clean_bet_id = bet_id.strip()
        if not clean_bet_id.isdigit():
            return {"status": "error", "message": "ID аккаунта должен содержать только цифры."}
            
        valid_prefixes = ("168", "169", "170", "171", "172", "173", "174", "175", "201", "202") 
        if not clean_bet_id.startswith(valid_prefixes):
            return {"status": "error", "message": "Неверный формат ID."}

        ip_address = request.client.host
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            ip_address = forwarded_for.split(",")[0].strip()
            
        user_agent = request.headers.get("User-Agent", "Unknown")
        os_device = "Unknown"
        if "iPhone" in user_agent or "iPad" in user_agent: os_device = "iOS"
        elif "Android" in user_agent: os_device = "Android"
        elif "Windows" in user_agent: os_device = "Windows"
        elif "Mac OS" in user_agent: os_device = "macOS"

        country = await asyncio.to_thread(fetch_geo, ip_address)

        telegram_id = None
        username = None  # Добавили явную переменную для юзернейма
        
        if init_data:
            try:
                validated = validate_telegram_data(init_data, BOT_TOKEN)
                print(f"🔍 [DEBUG REG] Результат валидации: {validated}")
                user_data = json.loads(validated.get('user', '{}'))
                telegram_id = user_data.get('id')
                username = user_data.get('username')
                print(f"🔍 [DEBUG REG] Успешно распарсили: TG_ID={telegram_id}, USR={username}")
            except Exception as val_err:
                print(f"🛑 [DEBUG REG] Ошибка валидации паспорта Телеграма: {val_err}")
            
        print(f"=========================================")
        
        # СЦЕНАРИЙ А: Юзер меняет ID (Telegram ID уже есть в базе)
        if telegram_id:
            user_by_tg = db.query(User).filter(User.telegram_id == telegram_id).first()
            if user_by_tg:
                user_by_tg.bet_id = clean_bet_id
                user_by_tg.username = username
                user_by_tg.ip_address = ip_address
                user_by_tg.country = country
                user_by_tg.os_device = os_device
                user_by_tg.browser = user_agent[:200]
                if source:
                    user_by_tg.source = source
                db.commit()
                return {"status": "ok", "created": True}

        # СЦЕНАРИЙ Б: Кто-то заходит под существующим 1xBet ID
        existing_user = db.query(User).filter(User.bet_id == clean_bet_id).first()
        if existing_user:
            if existing_user.telegram_id is None and telegram_id is not None:
                existing_user.telegram_id = telegram_id
                existing_user.username = username
            if source and not existing_user.source:
                existing_user.source = source
            existing_user.ip_address = ip_address
            existing_user.country = country
            existing_user.os_device = os_device
            existing_user.browser = user_agent[:200]
            db.commit()
            return {"status": "ok", "already_exists": True}
            
        # СЦЕНАРИЙ В: Абсолютно новый пользователь
        new_user = User(
            telegram_id=telegram_id,
            bet_id=clean_bet_id,
            username=username,
            attempts_left=0,
            is_active=False,
            is_banned=False,
            source=source,
            click_id=click_id, # Сохраняем метку
            ip_address=ip_address,
            country=country,
            os_device=os_device,
            browser=user_agent[:200]
        )
        db.add(new_user)
        db.commit()
        
        # Логируем событие "Лид зарегистрирован"
        log_traffic_event(db, event_type="lead_registered", user_id=new_user.id, telegram_id=telegram_id, source=source, click_id=click_id)
        
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

        match_data = await get_advanced_match_data(team1, team2)
        pred = calculate_prediction(match_data)
        winner = pred["winner"]
        confidence = pred["confidence"]
        additional = pred.get("additional", "")
        analysis_text = await generate_prediction_text(team1, team2, match_data, winner, confidence, additional)
        
        if winner == "team1": winner_name = team1
        elif winner == "team2": winner_name = team2
        else: winner_name = winner

        user.attempts_left -= 1
        user.last_activity = datetime.utcnow()
        db.commit()
        
        # Логируем событие "Сделан прогноз" для когортного анализа
        log_traffic_event(db, event_type="prediction", user_id=user.id, telegram_id=user.telegram_id, source=user.source, click_id=user.click_id)

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
        
        # Добавляем подгрузку текста анализа и доп. прогнозов в JSON ответ
        history = [
            {
                "created_at": log.created_at.isoformat(), 
                "match_description": log.match_description, 
                "winner": log.winner, 
                "confidence": log.confidence,
                "analysis_text": log.prediction_text,        # Передаем текст разбора
                "additional": log.additional_predictions     # Передаем тоталы/угловые
            } 
            for log in logs
        ]
        return {"history": history}
    finally:
        db.close()

# ---------- Запуск ----------

# Функция планировщика (проверяет базу раз в минуту)
async def scheduler_loop():
    while True:
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            # Ищем невыполненные рассылки, чье время уже пришло
            pending = db.query(ScheduledBroadcast).filter(
                ScheduledBroadcast.is_completed == False,
                ScheduledBroadcast.scheduled_time <= now
            ).all()

            for pb in pending:
                pb.is_completed = True
                db.commit()
                # Распаковываем юзеров и запускаем стандартную рассылку
                u_ids = json.loads(pb.user_ids)
                asyncio.create_task(run_broadcast_task(u_ids, pb.text, pb.msg_type, pb.delay, pb.staff_id, pb.file_path))
        except Exception as e:
            print(f"[SCHEDULER ERROR] {e}")
        finally:
            db.close()
            
        await asyncio.sleep(60) # Спим 60 секунд до следующей проверки

async def start_bot():
    await bot.delete_webhook()
    await dp.start_polling(bot)

async def run_fastapi():
    port = int(os.getenv("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    asyncio.create_task(scheduler_loop()) # <-- ЗАПУСКАЕМ НАШ ПЛАНИРОВЩИК
    await asyncio.gather(start_bot(), run_fastapi())

if __name__ == "__main__":
    asyncio.run(main())
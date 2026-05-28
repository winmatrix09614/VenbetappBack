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
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from collections import OrderedDict
from io import StringIO

from dotenv import load_dotenv
load_dotenv()

# ---------- Переменные окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")

if not all([BOT_TOKEN, GEMINI_API_KEY, API_FOOTBALL_KEY]):
    print("⚠️ Предупреждение: не все основные переменные окружения заданы. Бот может работать некорректно.")

# ---- Google Gemini SDK ----
from google import genai
import feedparser
import aiohttp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

from fastapi import FastAPI, Request, Form, Query, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Float, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel
from google.genai import types as genai_types
import uvicorn

# ---------- SSE ----------
from sse_starlette.sse import EventSourceResponse

# ---------- Менеджер SSE-подписок (broadcast) ----------
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

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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

# ---------- Вспомогательные функции ----------
async def download_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_path = f"temp_{file_id}.jpg"
    await bot.download_file(file.file_path, file_path)
    return file_path

async def extract_match_from_image(file_id: str) -> dict:
    local_path = await download_photo(file_id)
    try:
        uploaded = client.files.upload(file=local_path)
        prompt = """You are an expert at extracting football match information from ANY screenshot..."""
        response = client.models.generate_content(model=MODEL_NAME, contents=[prompt, uploaded])
        os.remove(local_path)
        text = response.text.strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "team1": data.get("team1", "Unknown").strip(),
                "team2": data.get("team2", "Unknown").strip(),
                "tournament": data.get("tournament", "Unknown").strip()
            }
        else:
            return {"team1": "Unknown", "team2": "Unknown", "tournament": "Unknown"}
    except Exception as e:
        print(f"Error in extract_match_from_image: {e}")
        return {"team1": "Unknown", "team2": "Unknown", "tournament": "Unknown"}

def _fallback_stats():
    return {"last_5": [0.5, 0.5, 0.5, 0.5, 0.5], "injuries": ["Данные временно недоступны"], "home_advantage": 0.0}

async def get_team_stats(team_name: str) -> dict:
    cache_key = team_name.lower().strip()
    if cache_key in team_stats_cache:
        cached_data, cached_time = team_stats_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_data
    headers = {
        'x-apisports-key': API_FOOTBALL_KEY,
        'x-apisports-host': 'v3.football.api-sports.io'
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://v3.football.api-sports.io/teams?search={team_name}', headers=headers) as resp:
            if resp.status != 200:
                return _fallback_stats()
            data = await resp.json()
            if not data.get('response'):
                return _fallback_stats()
            team_id = data['response'][0]['team']['id']
        today = datetime.now()
        one_year_ago = today - timedelta(days=365)
        params = {
            "team": team_id,
            "from": one_year_ago.strftime("%Y-%m-%d"),
            "to": today.strftime("%Y-%m-%d"),
            "status": "FT"
        }
        async with session.get('https://v3.football.api-sports.io/fixtures', headers=headers, params=params) as resp:
            if resp.status != 200:
                return _fallback_stats()
            data = await resp.json()
            fixtures = data.get('response', [])
        fixtures.sort(key=lambda x: x['fixture']['date'], reverse=True)
        last_5 = fixtures[:5]
        if not last_5:
            return _fallback_stats()
        last_5_results = []
        for match in last_5:
            home_team_id = match['teams']['home']['id']
            away_team_id = match['teams']['away']['id']
            home_goals = match['goals']['home']
            away_goals = match['goals']['away']
            if home_goals is None or away_goals is None:
                continue
            if home_team_id == team_id:
                if home_goals > away_goals:
                    last_5_results.append(1)
                elif home_goals < away_goals:
                    last_5_results.append(0)
                else:
                    last_5_results.append(0.5)
            else:
                if away_goals > home_goals:
                    last_5_results.append(1)
                elif away_goals < home_goals:
                    last_5_results.append(0)
                else:
                    last_5_results.append(0.5)
        if not last_5_results:
            return _fallback_stats()
        result = {"last_5": last_5_results, "injuries": [], "home_advantage": 0.1}
        team_stats_cache[cache_key] = (result, time.time())
        if len(team_stats_cache) > 100:
            team_stats_cache.popitem(last=False)
        return result

def calculate_prediction(stats1: dict, stats2: dict) -> dict:
    wins1 = sum(1 for r in stats1['last_5'] if r == 1)
    wins2 = sum(1 for r in stats2['last_5'] if r == 1)
    diff = wins1 - wins2
    confidence = 50 + diff * 8
    if stats1['injuries']:
        confidence -= 7
    if stats2['injuries']:
        confidence += 5
    confidence += stats1['home_advantage'] * 10
    confidence = max(30, min(95, confidence))
    if diff > 0.5:
        winner = "team1"
    elif diff < -0.5:
        winner = "team2"
    else:
        winner = "draw"
    return {"winner": winner, "confidence": round(confidence, 2)}

async def generate_prediction_text(team1, team2, stats1, stats2, winner, confidence):
    injuries1 = ', '.join(stats1['injuries']) if stats1['injuries'] else 'нет'
    injuries2 = ', '.join(stats2['injuries']) if stats2['injuries'] else 'нет'
    prompt = f"""Ты спортивный аналитик. На основе статистики:
Команда {team1}: результаты последних 5 матчей {stats1['last_5']}, травмы: {injuries1}
Команда {team2}: результаты последних 5 матчей {stats2['last_5']}, травмы: {injuries2}
Прогноз: победа {winner} с уверенностью {confidence}%.
Напиши краткий анализ (2-3 предложения) на русском языке."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            print(f"Gemini error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                break
            await asyncio.sleep(2 ** attempt)
    return "Сервис аналитики временно перегружен. Попробуйте позже."

async def save_prediction_log(user_telegram_id: int, match_desc: str, winner: str, confidence: float, full_text: str, additional: str = None):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_telegram_id).first()
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
    await save_prediction_log(message.from_user.id, f"{team1} - {team2}", winner, confidence, result_text, additional)
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
    if user and user.attempts_left > 0:
        user.attempts_left -= 1
        db.commit()
    db.close()

# ---------- Обработчики бота (упрощённо) ----------
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
        is_active=False
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

# ---------- FastAPI приложение ----------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://venbetapp-production.up.railway.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Шаблоны должны лежать в папке templates (статически)
templates = Jinja2Templates(directory="templates")

# ---------- Эндпоинты админ-панели (основные, без изменений) ----------
@app.get("/", response_class=HTMLResponse)
async def admin_login_page():
    return templates.TemplateResponse("admin.html", {"request": {}})

@app.post("/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "admin123":
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(key="admin_auth", value="true")
        return response
    return HTMLResponse("<h3>Invalid credentials</h3><a href='/'>Try again</a>", status_code=401)

@app.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, search: str = Query(None), status: str = Query(None),
                          limit_min: int = Query(None), limit_max: int = Query(None), date_filter: str = Query(None),
                          page: int = Query(1), per_page: int = Query(20)):
    if request.cookies.get("admin_auth") != "true":
        return RedirectResponse(url="/")
    db = SessionLocal()
    query = db.query(User)
    if search:
        query = query.filter((User.telegram_id.contains(search)) | (User.bet_id.contains(search)) | (User.username.contains(search)))
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
    db.close()
    db2 = SessionLocal()
    total_users = db2.query(User).count()
    active_users = db2.query(User).filter(User.is_active == True, User.is_banned == False).count()
    premium_users = db2.query(User).filter(User.is_premium == True).count()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    predictions_today = db2.query(PredictionLog).filter(PredictionLog.created_at >= today_start).count()
    pending_count = db2.query(User).filter(User.is_active == False, User.is_banned == False).count()
    db2.close()
    return templates.TemplateResponse("users.html", {
        "request": request, "users": users, "total_users": total_users,
        "active_users": active_users, "premium_users": premium_users, "predictions_today": predictions_today,
        "page": page, "total_pages": total_pages, "per_page": per_page, "search_query": search or "",
        "status_filter": status or "", "limit_min": limit_min, "limit_max": limit_max, "date_filter": date_filter or "",
        "pending_count": pending_count
    })

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, search: str = Query(None), page: int = Query(1)):
    if request.cookies.get("admin_auth") != "true":
        return RedirectResponse(url="/")
    db = SessionLocal()
    query = db.query(PredictionLog)
    if search:
        query = query.filter((PredictionLog.match_description.contains(search)) | (PredictionLog.user_id.contains(search)))
    total = query.count()
    per_page = 20
    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page
    logs = query.order_by(PredictionLog.created_at.desc()).offset(offset).limit(per_page).all()
    db.close()
    return templates.TemplateResponse("logs.html", {"request": request, "logs": logs, "page": page, "total_pages": total_pages, "search_query": search or ""})

@app.post("/approve")
async def approve_user(user_id: int = Form(...), attempts: int = Form(...)):
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_active = True
        user.is_banned = False
        user.attempts_left = attempts
        user.confirmed_at = datetime.utcnow()
        db.commit()
        if user.telegram_id != 0:
            try:
                await bot.send_message(user.telegram_id, f"✅ Ваш аккаунт активирован! У вас {attempts} прогнозов.")
            except Exception as e:
                print(f"Не удалось отправить сообщение пользователю {user.telegram_id}: {e}")
    db.close()
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/ban")
async def ban_user(user_id: int = Form(...)):
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_banned = True
        user.is_active = False
        db.commit()
        if user.telegram_id != 0:
            try:
                await bot.send_message(user.telegram_id, "❌ Ваш аккаунт заблокирован.")
            except:
                pass
    db.close()
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/premium")
async def set_premium(user_id: int = Form(...)):
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_premium = True
        db.commit()
        if user.telegram_id != 0:
            try:
                await bot.send_message(user.telegram_id, "⭐ Вам выдан премиум-статус!")
            except:
                pass
    db.close()
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/")
    response.delete_cookie("admin_auth")
    return response

# ---------- Массовые операции ----------
@app.post("/mass_give_attempts")
async def mass_give_attempts(request: Request, data: dict):
    if request.cookies.get("admin_auth") != "true":
        return {"error": "Unauthorized"}
    user_ids = data.get("user_ids", [])
    attempts = data.get("attempts", 0)
    db = SessionLocal()
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.attempts_left += attempts
            db.commit()
    db.close()
    return {"status": "ok"}

@app.post("/mass_activate")
async def mass_activate(request: Request, data: dict):
    if request.cookies.get("admin_auth") != "true":
        return {"error": "Unauthorized"}
    user_ids = data.get("user_ids", [])
    db = SessionLocal()
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.is_active = True
            user.is_banned = False
            db.commit()
    db.close()
    return {"status": "ok"}

@app.post("/mass_ban")
async def mass_ban(request: Request, data: dict):
    if request.cookies.get("admin_auth") != "true":
        return {"error": "Unauthorized"}
    user_ids = data.get("user_ids", [])
    db = SessionLocal()
    for uid in user_ids:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            user.is_banned = True
            user.is_active = False
            db.commit()
    db.close()
    return {"status": "ok"}

@app.post("/give_attempts")
async def give_attempts(request: Request, data: dict):
    if request.cookies.get("admin_auth") != "true":
        return {"error": "Unauthorized"}
    user_id = data.get("user_id")
    attempts = data.get("attempts", 0)
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.attempts_left += attempts
        db.commit()
    db.close()
    return {"status": "ok"}

@app.get("/export_users_csv")
async def export_users_csv(request: Request, search: str = Query(None), status: str = Query(None),
                           limit_min: int = Query(None), limit_max: int = Query(None), date_filter: str = Query(None)):
    if request.cookies.get("admin_auth") != "true":
        return RedirectResponse(url="/")
    db = SessionLocal()
    query = db.query(User)
    if search:
        query = query.filter((User.telegram_id.contains(search)) | (User.bet_id.contains(search)) | (User.username.contains(search)))
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
    db.close()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Telegram ID", "1xBet ID", "Username", "Лимит", "Активен", "Забанен", "Premium", "Дата регистрации"])
    for u in users:
        writer.writerow([u.id, u.telegram_id, u.bet_id, u.username or "", u.attempts_left, u.is_active, u.is_banned, u.is_premium, u.created_at])
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=users_export.csv"
    return response

# ---------- SSE (только одна реализация) ----------
def get_current_pending_count() -> int:
    db = SessionLocal()
    count = db.query(User).filter(User.is_active == False, User.is_banned == False).count()
    db.close()
    return count

@app.get("/api/stream_leads")
async def stream_leads(request: Request):
    if request.cookies.get("admin_auth") != "true":
        return {"error": "Unauthorized"}
    
    async def event_generator():
        queue = await notifier.connect()
        # Отправляем текущее состояние
        yield {"data": json.dumps({"count": get_current_pending_count()}), "event": "update"}
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

# ---------- Эндпоинт регистрации (единственный) ----------
@app.get("/register_request")
async def register_request(bet_id: str, init_data: str = Query(None)):
    db = SessionLocal()
    try:
        print(f"[DEBUG] register_request: bet_id={bet_id}, init_data={'provided' if init_data else 'not provided'}")
        telegram_id = None
        if init_data:
            try:
                validated = validate_telegram_data(init_data, BOT_TOKEN)
                user_data = json.loads(validated.get('user', '{}'))
                telegram_id = user_data.get('id')
                print(f"[DEBUG] Validated telegram_id={telegram_id}")
            except Exception as e:
                print(f"[ERROR] Invalid init_data: {e}")
                return {"status": "error", "message": "Invalid Telegram data"}
        
        existing_user = db.query(User).filter(User.bet_id == bet_id).first()
        if existing_user:
            if existing_user.telegram_id is None and telegram_id is not None:
                existing_user.telegram_id = telegram_id
                db.commit()
                print(f"[DEBUG] Updated telegram_id for existing user bet_id={bet_id} -> {telegram_id}")
            return {"status": "ok", "already_exists": True}
        
        new_user = User(
            telegram_id=telegram_id,
            bet_id=bet_id,
            attempts_left=0,
            is_active=False,
            is_banned=False
        )
        db.add(new_user)
        db.commit()
        print(f"[DEBUG] Created new user: bet_id={bet_id}, telegram_id={telegram_id}")
        
        # Отправляем уведомление всем подключённым админам
        pending_count = db.query(User).filter(User.is_active == False, User.is_banned == False).count()
        await notifier.push({"count": pending_count})
        print(f"[DEBUG] Sent notification to {len(notifier.connections)} connections with count={pending_count}")
        
        return {"status": "ok", "created": True}
    except Exception as e:
        print(f"[ERROR] Register error for bet_id={bet_id}: {e}")
        db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()

# ---------- Эндпоинты для WebApp (Mini App) ----------
class MatchInfo(BaseModel):
    team1: str
    team2: str

@app.post("/webapp/predict")
async def webapp_predict(user_id: str = Form(...), text: str = Form(None), photo: UploadFile = File(None)):
    db = SessionLocal()
    user = db.query(User).filter(User.bet_id == user_id).first()
    if not user:
        db.close()
        return {"error": "User not found. Please register via /start in Telegram bot."}
    if not user.is_active or user.is_banned:
        db.close()
        return {"error": "Account not active or banned."}
    if user.attempts_left <= 0:
        db.close()
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
            print(f"[DEBUG] Gemini extracted: team1='{team1}', team2='{team2}'")
        except Exception as e:
            print(f"[ERROR] Photo processing: {e}")
            db.close()
            return {"error": "Error processing photo."}
    elif text:
        parts = re.split(r'[-–—]', text)
        if len(parts) >= 2:
            team1 = parts[0].strip()
            team2 = parts[1].strip()
        else:
            db.close()
            return {"error": "Invalid format. Use 'Team A - Team B'."}
    else:
        db.close()
        return {"error": "No input."}

    if not team1 or not team2 or team1 == "Unknown" or team2 == "Unknown":
        db.close()
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
    db.commit()
    full_text = f"Победитель: {winner_name}\nУверенность: {confidence}%\n{analysis_text}"
    await save_prediction_log(user.telegram_id, f"{team1} - {team2}", winner, confidence, full_text, additional)
    db.close()

    return {
        "prediction": {"winner": winner_name, "confidence": confidence},
        "additional": additional,
        "prediction_text": analysis_text
    }

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

# ---------- Эндпоинты для фронтенда (статус, регистрация, история) ----------
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
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(start_bot(), run_fastapi())

if __name__ == "__main__":
    asyncio.run(main())
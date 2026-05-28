import asyncio
import logging
import os
import re
import json
import random
import time
import csv
import requests
import aiohttp  # добавьте этот импорт в начало файла

from bs4 import BeautifulSoup
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
import requests
import feedparser

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
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Float
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel
from google.genai import types as genai_types
import uvicorn

# ---------- Gemini ----------
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash"

# ---------- Кэш для статистики команд ----------
team_stats_cache = OrderedDict()
CACHE_TTL = 3600

# ---------- Кэш для новостей ----------
news_cache = {"data": [], "last_update": 0}
NEWS_CACHE_TTL = 1800  # 30 минут

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
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
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

# ---------- Вспомогательные функции (скачивание фото, распознавание, статистика) ----------
async def download_photo(file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_path = f"temp_{file_id}.jpg"
    await bot.download_file(file.file_path, file_path)
    return file_path

async def extract_match_from_image(file_id: str) -> dict:
    local_path = await download_photo(file_id)
    try:
        uploaded = client.files.upload(file=local_path)
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
    """Получает реальную статистику команды через API-Football."""
    print(f"[DEBUG] get_team_stats called for team: {team_name}")
    print(f"[DEBUG] API_FOOTBALL_KEY is {'set' if API_FOOTBALL_KEY else 'NOT SET'}")

    cache_key = team_name.lower().strip()
    if cache_key in team_stats_cache:
        cached_data, cached_time = team_stats_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            print(f"[DEBUG] Returning cached data for {team_name}")
            return cached_data

    headers = {
        'x-apisports-key': API_FOOTBALL_KEY,
        'x-apisports-host': 'v3.football.api-sports.io'
    }

    async with aiohttp.ClientSession() as session:
        # 1. Поиск ID команды
        url = f'https://v3.football.api-sports.io/teams?search={team_name}'
        print(f"[DEBUG] Requesting {url}")
        async with session.get(url, headers=headers) as resp:
            print(f"[DEBUG] Response status: {resp.status}")
            if resp.status != 200:
                print(f"[DEBUG] API returned status {resp.status}, using fallback")
                return _fallback_stats()
            data = await resp.json()
            print(f"[DEBUG] API response: {data}")
            if not data.get('response'):
                print(f"[DEBUG] No team found for {team_name}, using fallback")
                return _fallback_stats()
            team_id = data['response'][0]['team']['id']
            print(f"[DEBUG] Found team {team_name} with ID {team_id}")

        # 2. Получение последних 5 матчей
        fixtures_url = f'https://v3.football.api-sports.io/fixtures?team={team_id}&last=5'
        async with session.get(fixtures_url, headers=headers) as resp:
            if resp.status != 200:
                print(f"[DEBUG] Fixtures API error: {resp.status}, using fallback")
                return _fallback_stats()
            data = await resp.json()
            fixtures = data.get('response', [])
            print(f"[DEBUG] Got {len(fixtures)} fixtures")

        if not fixtures:
            print("[DEBUG] No fixtures, using fallback")
            return _fallback_stats()

        last_5_results = []
        for match in fixtures:
            if match['fixture']['status']['short'] != 'FT':
                continue
            home_team_id = match['teams']['home']['id']
            away_team_id = match['teams']['away']['id']
            home_goals = match['goals']['home']
            away_goals = match['goals']['away']
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

        result = {
            "last_5": last_5_results,
            "injuries": [],
            "home_advantage": 0.1
        }
        team_stats_cache[cache_key] = (result, time.time())
        print(f"[DEBUG] Returning result: {result}")
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
    prompt = f"""
Ты спортивный аналитик. На основе статистики:
Команда {team1}: результаты последних 5 матчей {stats1['last_5']}, травмы: {injuries1}
Команда {team2}: результаты последних 5 матчей {stats2['last_5']}, травмы: {injuries2}
Прогноз: победа {winner} с уверенностью {confidence}%.
Напиши краткий анализ (2-3 предложения) на русском языке.
"""
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

templates = Jinja2Templates(directory="templates")
os.makedirs("templates", exist_ok=True)

# Шаблоны админки (создаются автоматически)
with open("templates/admin_base.html", "w", encoding="utf-8") as f:
    f.write("""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Admin Panel{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #f8f9fa; }
        .sidebar { background: #1a1a2e; min-height: 100vh; }
        .sidebar a { color: #ddd; text-decoration: none; padding: 10px; display: block; }
        .sidebar a:hover { background: #0f0f1a; color: white; }
        .content { padding: 20px; }
        .table-responsive { background: white; border-radius: 10px; padding: 15px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
<div class="container-fluid">
    <div class="row">
        <div class="col-md-2 sidebar p-0">
            <div class="p-3">
                <h5 class="text-white">Админ-панель</h5>
                <hr class="bg-light">
                <a href="/dashboard">📊 Пользователи</a>
                <a href="/logs">📜 Логи прогнозов</a>
                <a href="/logout" class="text-danger">🚪 Выйти</a>
            </div>
        </div>
        <div class="col-md-10 content">
            {% block content %}{% endblock %}
        </div>
    </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
    """)

with open("templates/users.html", "w", encoding="utf-8") as f:
    f.write("""
{% extends "admin_base.html" %}
{% block title %}Пользователи{% endblock %}
{% block content %}
<div class="container-fluid px-0">
    <h2 class="mb-4">👥 Управление пользователями</h2>
    <div class="row mb-4">
        <div class="col-md-3"><div class="card text-white bg-primary"><div class="card-body"><h5 class="card-title">Всего пользователей</h5><p class="card-text display-6">{{ total_users }}</p></div></div></div>
        <div class="col-md-3"><div class="card text-white bg-success"><div class="card-body"><h5 class="card-title">Активные</h5><p class="card-text display-6">{{ active_users }}</p></div></div></div>
        <div class="col-md-3"><div class="card text-white bg-warning"><div class="card-body"><h5 class="card-title">Премиум</h5><p class="card-text display-6">{{ premium_users }}</p></div></div></div>
        <div class="col-md-3"><div class="card text-white bg-info"><div class="card-body"><h5 class="card-title">Прогнозов за 24ч</h5><p class="card-text display-6">{{ predictions_today }}</p></div></div></div>
    </div>
    <div class="card mb-4">
        <div class="card-header">🔍 Расширенный фильтр</div>
        <div class="card-body">
            <form method="get" id="filterForm">
                <div class="row">
                    <div class="col-md-3"><label>Поиск</label><input type="text" name="search" class="form-control" value="{{ search_query }}"></div>
                    <div class="col-md-2"><label>Статус</label><select name="status" class="form-select"><option value="">Все</option><option value="active" {% if status_filter == 'active' %}selected{% endif %}>Активен</option><option value="banned" {% if status_filter == 'banned' %}selected{% endif %}>Забанен</option><option value="premium" {% if status_filter == 'premium' %}selected{% endif %}>Премиум</option><option value="pending" {% if status_filter == 'pending' %}selected{% endif %}>Ожидает</option></select></div>
                    <div class="col-md-2"><label>Лимит от</label><input type="number" name="limit_min" class="form-control" value="{{ limit_min }}"></div>
                    <div class="col-md-2"><label>Лимит до</label><input type="number" name="limit_max" class="form-control" value="{{ limit_max }}"></div>
                    <div class="col-md-3"><label>Дата регистрации</label><select name="date_filter" class="form-select"><option value="">Любая</option><option value="today" {% if date_filter == 'today' %}selected{% endif %}>Сегодня</option><option value="week" {% if date_filter == 'week' %}selected{% endif %}>За неделю</option><option value="month" {% if date_filter == 'month' %}selected{% endif %}>За месяц</option></select></div>
                </div>
                <div class="row mt-3">
                    <div class="col-md-12"><button type="submit" class="btn btn-primary">Применить фильтр</button><a href="/dashboard" class="btn btn-secondary">Сбросить</a><button type="button" id="exportCsvBtn" class="btn btn-success float-end">📎 Экспорт CSV</button></div>
                </div>
            </form>
        </div>
    </div>
    <div class="mb-3"><button type="button" id="massGiveAttempts" class="btn btn-outline-primary">➕ Выдать +5 прогнозов выбранным</button><button type="button" id="massActivate" class="btn btn-outline-success">✅ Активировать выбранных</button><button type="button" id="massBan" class="btn btn-outline-danger">🚫 Забанить выбранных</button></div>
    <div class="table-responsive">
        <table class="table table-bordered table-hover" id="usersTable">
            <thead class="table-dark"><tr><th><input type="checkbox" id="selectAll"></th><th>ID</th><th>Telegram ID</th><th>1xBet ID</th><th>Username</th><th>Лимит</th><th>Активен</th><th>Забанен</th><th>Premium</th><th>Действия</th></tr></thead>
            <tbody>{% for u in users %}<tr><td><input type="checkbox" class="userCheckbox" data-user-id="{{ u.id }}"></td><td>{{ u.id }}</td><td>{{ u.telegram_id }}</td><td>{{ u.bet_id }}</td><td>{{ u.username or '-' }}</td><td>{{ u.attempts_left }}</td><td>{{ '✅' if u.is_active else '❌' }}</td><td>{{ '🚫' if u.is_banned else '—' }}</td><td>{{ '⭐' if u.is_premium else '—' }}</td>
            <td><div class="btn-group btn-group-sm"><button class="btn btn-success btn-sm give-attempts" data-id="{{ u.id }}" data-attempts="1">+1</button><button class="btn btn-info btn-sm give-attempts" data-id="{{ u.id }}" data-attempts="5">+5</button><form method="post" action="/approve" style="display:inline;"><input type="hidden" name="user_id" value="{{ u.id }}"><input type="number" name="attempts" value="50" style="width:60px; display:inline;"><button type="submit" class="btn btn-warning btn-sm">Акт.</button></form><form method="post" action="/ban" style="display:inline;"><input type="hidden" name="user_id" value="{{ u.id }}"><button type="submit" class="btn btn-danger btn-sm">Бан</button></form><form method="post" action="/premium" style="display:inline;"><input type="hidden" name="user_id" value="{{ u.id }}"><button type="submit" class="btn btn-secondary btn-sm">Premium</button></form></div></td>
            </tr>{% endfor %}</tbody>
        <tr>
    </div>
    <div class="row mt-3">
        <div class="col-md-3"><select id="perPageSelect" class="form-select w-auto d-inline-block"><option value="20" {% if per_page == 20 %}selected{% endif %}>20</option><option value="50" {% if per_page == 50 %}selected{% endif %}>50</option><option value="100" {% if per_page == 100 %}selected{% endif %}>100</option></select><span>записей на странице</span></div>
        <div class="col-md-9"><nav><ul class="pagination justify-content-end">{% if page > 1 %}<li class="page-item"><a class="page-link" href="?page={{ page-1 }}{% if search_query %}&search={{ search_query }}{% endif %}{% if status_filter %}&status={{ status_filter }}{% endif %}{% if limit_min %}&limit_min={{ limit_min }}{% endif %}{% if limit_max %}&limit_max={{ limit_max }}{% endif %}{% if date_filter %}&date_filter={{ date_filter }}{% endif %}&per_page={{ per_page }}">Назад</a></li>{% endif %}{% for p in range(1, total_pages+1) %}<li class="page-item {% if p == page %}active{% endif %}"><a class="page-link" href="?page={{ p }}{% if search_query %}&search={{ search_query }}{% endif %}{% if status_filter %}&status={{ status_filter }}{% endif %}{% if limit_min %}&limit_min={{ limit_min }}{% endif %}{% if limit_max %}&limit_max={{ limit_max }}{% endif %}{% if date_filter %}&date_filter={{ date_filter }}{% endif %}&per_page={{ per_page }}">{{ p }}</a></li>{% endfor %}{% if page < total_pages %}<li class="page-item"><a class="page-link" href="?page={{ page+1 }}{% if search_query %}&search={{ search_query }}{% endif %}{% if status_filter %}&status={{ status_filter }}{% endif %}{% if limit_min %}&limit_min={{ limit_min }}{% endif %}{% if limit_max %}&limit_max={{ limit_max }}{% endif %}{% if date_filter %}&date_filter={{ date_filter }}{% endif %}&per_page={{ per_page }}">Вперёд</a></li>{% endif %}</ul></nav></div>
    </div>
</div>
<script>
    document.getElementById('massGiveAttempts').addEventListener('click',function(){let s=[];document.querySelectorAll('.userCheckbox:checked').forEach(cb=>s.push(cb.dataset.userId));if(!s.length)return alert('Выберите пользователей');fetch('/mass_give_attempts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_ids:s,attempts:5})}).then(()=>location.reload());});
    document.getElementById('massActivate').addEventListener('click',function(){let s=[];document.querySelectorAll('.userCheckbox:checked').forEach(cb=>s.push(cb.dataset.userId));if(!s.length)return alert('Выберите пользователей');fetch('/mass_activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_ids:s})}).then(()=>location.reload());});
    document.getElementById('massBan').addEventListener('click',function(){let s=[];document.querySelectorAll('.userCheckbox:checked').forEach(cb=>s.push(cb.dataset.userId));if(!s.length)return alert('Выберите пользователей');fetch('/mass_ban',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_ids:s})}).then(()=>location.reload());});
    document.getElementById('exportCsvBtn').addEventListener('click',function(){window.location.href='/export_users_csv?'+new URLSearchParams({search:'{{ search_query }}',status:'{{ status_filter }}',limit_min:'{{ limit_min }}',limit_max:'{{ limit_max }}',date_filter:'{{ date_filter }}'}).toString();});
    document.querySelectorAll('.give-attempts').forEach(btn=>{btn.addEventListener('click',function(){let userId=this.dataset.id,attempts=this.dataset.attempts;fetch('/give_attempts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:userId,attempts:parseInt(attempts)})}).then(()=>location.reload());});});
    document.getElementById('selectAll').addEventListener('change',function(){document.querySelectorAll('.userCheckbox').forEach(cb=>cb.checked=this.checked);});
    document.getElementById('perPageSelect').addEventListener('change',function(){let url=new URL(window.location.href);url.searchParams.set('per_page',this.value);url.searchParams.set('page',1);window.location.href=url.toString();});
</script>
{% endblock %}
    """)

with open("templates/logs.html", "w", encoding="utf-8") as f:
    f.write("""
{% extends "admin_base.html" %}
{% block title %}Логи прогнозов{% endblock %}
{% block content %}
<h2>📜 История прогнозов</h2>
<form method="get" class="mb-3"><div class="row"><div class="col-md-4"><input type="text" name="search" class="form-control" placeholder="Поиск по матчу или пользователю" value="{{ search_query }}"></div><div class="col-md-2"><button type="submit" class="btn btn-primary">Найти</button><a href="/logs" class="btn btn-secondary">Сброс</a></div></div></form>
<div class="table-responsive"><table class="table table-bordered table-hover"><thead class="table-dark"><tr><th>Дата</th><th>ID пользователя</th><th>Матч</th><th>Прогноз</th><th>Уверенность</th><th>Доп. исходы</th><th>Текст ответа</th></tr></thead><tbody>{% for log in logs %}<tr><td>{{ log.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ log.user_id }}</td><td>{{ log.match_description }}</td><td>{{ log.winner }}</td><td>{{ log.confidence }}%</td><td>{{ log.additional_predictions or '-' }}</td><td>{{ log.prediction_text[:150] }}...</td></tr>{% endfor %}</tbody></table></div>
<nav><ul class="pagination justify-content-end">{% if page > 1 %}<li class="page-item"><a class="page-link" href="?page={{ page-1 }}{% if search_query %}&search={{ search_query }}{% endif %}">Назад</a></li>{% endif %}{% for p in range(1, total_pages+1) %}<li class="page-item {% if p == page %}active{% endif %}"><a class="page-link" href="?page={{ p }}{% if search_query %}&search={{ search_query }}{% endif %}">{{ p }}</a></li>{% endfor %}{% if page < total_pages %}<li class="page-item"><a class="page-link" href="?page={{ page+1 }}{% if search_query %}&search={{ search_query }}{% endif %}">Вперёд</a></li>{% endif %}</ul></nav>
{% endblock %}
    """)

with open("templates/admin.html", "w", encoding="utf-8") as f:
    f.write("""
<!DOCTYPE html>
<html>
<head><title>Login</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-light"><div class="container mt-5"><div class="row justify-content-center"><div class="col-md-4"><div class="card"><div class="card-header">Авторизация</div><div class="card-body"><form method="post" action="/login"><input type="text" name="username" class="form-control mb-2" placeholder="Логин" required><input type="password" name="password" class="form-control mb-2" placeholder="Пароль" required><button type="submit" class="btn btn-primary w-100">Войти</button></form></div></div></div></div></div></body>
</html>
    """)

# ---------- Эндпоинты админ-панели ----------
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
    db2.close()
    return templates.TemplateResponse("users.html", {"request": request, "users": users, "total_users": total_users,
        "active_users": active_users, "premium_users": premium_users, "predictions_today": predictions_today,
        "page": page, "total_pages": total_pages, "per_page": per_page, "search_query": search or "",
        "status_filter": status or "", "limit_min": limit_min, "limit_max": limit_max, "date_filter": date_filter or ""})

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

@app.get("/register_request")
async def register_request(bet_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.bet_id == bet_id).first()
        if not user:
            new_user = User(telegram_id=0, bet_id=bet_id, attempts_left=0, is_active=False, is_banned=False)
            db.add(new_user)
            db.commit()
        return {"status": "ok"}
    except Exception as e:
        print(f"Register error: {e}")
        return {"status": "error"}
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
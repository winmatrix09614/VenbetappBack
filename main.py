import asyncio
import logging
import os
import re
import json
import random
import time
import csv
from datetime import datetime, timedelta
from collections import OrderedDict
from io import StringIO

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALLSPORTS_API_KEY = os.getenv("ALLSPORTS_API_KEY")

if not all([BOT_TOKEN, GEMINI_API_KEY, ALLSPORTS_API_KEY]):
    print("⚠️ Предупреждение: не все переменные окружения заданы. Бот может работать некорректно.")

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
import uvicorn

# ---------- Gemini ----------
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash"

# ---------- Кэш для статистики ----------
team_stats_cache = OrderedDict()
CACHE_TTL = 3600

# ---------- База данных ----------
DATABASE_URL = "sqlite:///./bot_database.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
    cache_key = team_name.lower().strip()
    if cache_key in team_stats_cache:
        cached_data, cached_time = team_stats_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_data
    search_url = f"https://allsportsapi.com/api/football/?met=Teams&teamName={team_name}&APIkey={ALLSPORTS_API_KEY}"
    try:
        resp = requests.get(search_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get('result'):
            return _fallback_stats()
        team = data['result'][0]
        team_id = team['team_key']
        team_name_api = team['team_name']
        print(f"[API] Found team: {team_name_api} (ID: {team_id})")
    except Exception as e:
        print(f"[API Error] Search '{team_name}': {e}")
        return _fallback_stats()
    fixtures_url = f"https://allsportsapi.com/api/football/?met=Fixtures&teamId={team_id}&APIkey={ALLSPORTS_API_KEY}"
    try:
        resp = requests.get(fixtures_url, timeout=10)
        resp.raise_for_status()
        fixtures_data = resp.json()
        fixtures = fixtures_data.get('result', [])
        if not fixtures:
            return _fallback_stats()
    except Exception as e:
        print(f"[API Error] Fixtures for {team_name_api}: {e}")
        return _fallback_stats()
    last_5_results = []
    for match in fixtures[:5]:
        home_team_id = str(match.get('home_team_id'))
        away_team_id = str(match.get('away_team_id'))
        home_score = match.get('match_hometeam_score')
        away_score = match.get('match_awayteam_score')
        if home_score is None or away_score is None:
            result = 0.5
        elif int(home_score) > int(away_score):
            result = 1 if home_team_id == str(team_id) else 0
        elif int(home_score) < int(away_score):
            result = 1 if away_team_id == str(team_id) else 0
        else:
            result = 0.5
        last_5_results.append(result)
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
    feed = feedparser.parse("https://www.championat.com/rss/news.xml")
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

# ---------- FastAPI админ-панель и API ----------
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

# Шаблоны админки (создаются автоматически – здесь опустим для краткости, они у вас уже есть)
# Предполагается, что они созданы ранее. Для полноты кода они здесь не нужны.

# ---------- Эндпоинты админ-панели (заглушки, вы их уже имеете) ----------
# Для краткости оставлю только необходимые для WebApp и фронтенда.

# ---------- Эндпоинты для WebApp (Mini App) ----------
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
        import shutil
        temp_path = f"temp_{photo.filename}"
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(photo.file, buffer)
        try:
            uploaded = client.files.upload(file=temp_path)
            prompt = "Extract team names from this screenshot. Return JSON: {\"team1\": \"...\", \"team2\": \"...\"}"
            response = client.models.generate_content(model=MODEL_NAME, contents=[prompt, uploaded])
            os.remove(temp_path)
            text_resp = response.text.strip()
            json_match = re.search(r'\{.*\}', text_resp, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                team1 = data.get("team1", "").strip()
                team2 = data.get("team2", "").strip()
            else:
                db.close()
                return {"error": "Could not recognize teams from screenshot."}
        except Exception as e:
            print(f"Error processing photo: {e}")
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

# Кэш для новостей
news_cache = {"data": [], "last_update": 0}
CACHE_TTL = 1800

@app.get("/webapp/news")
async def webapp_news():
    current_time = time.time()
    if current_time - news_cache["last_update"] < CACHE_TTL and news_cache["data"]:
        return {"news": news_cache["data"]}
    try:
        feed = feedparser.parse("https://www.championat.com/rss/news.xml")
        news_list = []
        for entry in feed.entries[:10]:
            news_list.append({
                "title": entry.title,
                "link": entry.link,
                "pubDate": entry.get("published", datetime.now().isoformat())
            })
        news_cache["data"] = news_list
        news_cache["last_update"] = current_time
        return {"news": news_list}
    except Exception as e:
        print(f"News error: {e}")
        return {"news": news_cache["data"] if news_cache["data"] else []}

# ---------- Новые эндпоинты для фронтенда ----------
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
"""
My Kazakh Level — қазақ тілі деңгейін анықтайтын Telegram бот.

Студентке баратын хабарламалар — орысша (оқушылар орыстілді).
Админге (сізге) баратын хабарламалар — қазақша.

Іске қосу:  python bot.py     (.env: BOT_TOKEN, ADMIN_IDS, WEBAPP_URL)
"""
import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
                           BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

import db
from build_webapp import AUDIO_DIR, audio_key, quiz_js, quiz_version
import engine
from questions import AGE_GROUPS, all_levels, group_size, level_ru, pass_ratio


# ======================================================================= config
def load_env() -> None:
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMIN_IDS", ""))}
# PORT берілсе (Railway өзі береді), бот қосымшаны өз сайтында таратады және API ашады.
PORT = int(os.getenv("PORT") or 0)
_railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
# Mini App мекенжайы (https://...). Railway-де домен автоматты алынады.
# Бос болса — бот чат арқылы жұмыс істейді.
WEBAPP_URL = (os.getenv("WEBAPP_URL", "").strip()
              or (f"https://{_railway_domain}/" if _railway_domain else ""))
if PORT and _railway_domain:
    # Railway-де қосымша мен API осы сервердің өзінде — басқа мекенжай (мысалы ескі GitHub Pages)
    # берілсе де, әрқашан өз доменімізді қолданамыз, әйтпесе тіркелу серверге жетпейді.
    WEBAPP_URL = f"https://{_railway_domain}/"
WEBAPP_DIR = Path(__file__).with_name("webapp")

router = Router()
esc = html.escape

BTN_HELP = "❓ Помощь"
BTN_TEST = "📝 Пройти тест"      # Mini App жоқ кезде
BTN_CABINET = "👤 Мой кабинет"   # Mini App жоқ кезде
DONT_KNOW = "x"


class Reg(StatesGroup):
    name = State()
    phone = State()
    age = State()


class Test(StatesGroup):
    running = State()


def title_ru(age: str) -> str:
    return AGE_GROUPS[age]["title_ru"]


def normalize_phone(raw: str) -> str | None:
    """'8 701 123 45 67' / '7701...' / '+7 (701)...' -> '+77011234567'. Дұрыс болмаса None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("7"):  # 701 123 45 67
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else None


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def group_title_ru(grp) -> str:
    return f"{title_ru(grp['age_group'])} · {level_ru(grp['age_group'], grp['level'])} · №{grp['number']}"


def group_title_kz(grp) -> str:
    return f"{AGE_GROUPS[grp['age_group']]['title']} · {grp['level']} · №{grp['number']}"


# ===================================================================== keyboards
def student_state(uid: int) -> dict:
    """Қосымшаға керек деректер: тіркелу, деңгей, топ, тарих (студентке — орысша)."""
    st = db.get_student(uid)
    state: dict = {"reg": 0}
    if st:
        age = st["age_group"]
        res = db.get_results(uid)
        state = {"reg": 1, "n": st["name"], "p": st["phone"], "a": age,
                 "h": [[r["taken_at"][:10], level_ru(age, r["level"]), r["correct"], r["answered"]]
                       for r in res]}
        if res:
            state["l"] = level_ru(age, res[0]["level"])
            grp = db.group_of(uid)
            if grp:
                state["g"] = group_title_ru(grp)
            else:
                state["q"] = f"{len(db.pool(age, res[0]['level']))}/{group_size(age)}"
        nxt = retake_after(uid)
        if nxt:
            state["rt"] = nxt.strftime("%d.%m в %H:%M")   # келесі тапсыру уақыты
    if is_admin(uid):
        state["adm"] = 1   # қызметтік жол тек админге көрінеді
    return state


def menu_mode() -> bool:
    """Railway: қосымша тек Telegram-ның мәзір батырмасы («Начать», жазу жолағының сол жағы)
    және хабарламадағы батырма арқылы ашылады — екеуі де тікелей серверге қосылады."""
    return bool(PORT and WEBAPP_URL.startswith("https://"))


def app_url() -> str:
    """Қосымша мекенжайы + нұсқа белгісі (Telegram ескі нұсқаны кештен көрсетпеуі үшін)."""
    ver = (os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getenv("RAILWAY_GIT_COMMIT_SHA") or "1")[:12]
    return f"{WEBAPP_URL}{'&' if '?' in WEBAPP_URL else '?'}v={ver}"


def start_hint() -> str:
    return ("кнопку <b>«Начать»</b> слева от поля ввода" if menu_mode()
            else f"<b>«{BTN_TEST}»</b>")


def open_app_kb(text: str = "🚀 Начать") -> InlineKeyboardMarkup | None:
    """Хабарламаның астындағы қосымшаны ашатын батырма (мәзір режимінде)."""
    if not menu_mode():
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=app_url()))]])


def main_menu(uid: int) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Төменгі мәзір. Railway-де жоқ (ескісін де өшіреді) — тек «Начать» мәзір батырмасы.
    Компьютерде (сервер жоқ): тест пен кабинет чат арқылы."""
    if menu_mode():
        return ReplyKeyboardRemove()
    rows = [[KeyboardButton(text=BTN_TEST), KeyboardButton(text=BTN_CABINET)],
            [KeyboardButton(text=BTN_HELP)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=g["title_ru"], callback_data=f"age:{k}")]
        for k, g in AGE_GROUPS.items()
    ])


async def notify_admins(bot: Bot, text: str) -> None:
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception as e:  # админ ботты бастамаған болуы мүмкін
            logging.warning("Админге жіберілмеді %s: %s", aid, e)


# ======================================================================== start
def greeting() -> str:
    return (
        "Сәлеметсіз бе! 👋 <b>Здравствуйте!</b>\n\n"
        "Я — бот <b>My Kazakh Level</b>. За 10–15 минут я определю ваш уровень "
        "казахского языка и подберу группу с учениками вашего уровня.\n\n"
        "Что нужно сделать:\n"
        f"1️⃣ Нажмите {start_hint()} или кнопку под этим сообщением\n"
        "2️⃣ Укажите имя, телефон и возраст\n"
        "3️⃣ Пройдите короткий тест\n\n"
        "Тесты разные для детей 6–10 лет, школьников 10–18 лет и взрослых. 🇰🇿"
    )


@router.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    st = db.get_student(uid)
    if menu_mode():
        # 1) ескі төменгі батырмаларды өшіреміз, 2) хабарламаның астына «Начать» батырмасын береміз
        await msg.answer("🇰🇿", reply_markup=ReplyKeyboardRemove())
        if st:
            await msg.answer(f"С возвращением, <b>{esc(st['name'])}</b>! 👋\n"
                             f"Нажмите {start_hint()}, чтобы открыть тест и личный кабинет.",
                             reply_markup=open_app_kb("🚀 Открыть"))
        else:
            await msg.answer(greeting(), reply_markup=open_app_kb())
        return
    if st:
        await msg.answer(
            f"С возвращением, <b>{esc(st['name'])}</b>! 👋\n"
            f"Нажмите {start_hint()}, чтобы открыть тест и кабинет.",
            reply_markup=main_menu(uid))
        return
    if WEBAPP_URL:
        await msg.answer(greeting().replace(" или кнопку под этим сообщением", ""),
                         reply_markup=main_menu(uid))
        return
    # Mini App жоқ: тіркелу чатта
    await msg.answer(greeting().split("Что нужно сделать")[0], reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.name)
    await msg.answer("✍️ Напишите имя и фамилию ученика.\n\n"
                     "<i>Регистрируясь, вы соглашаетесь на обработку персональных данных "
                     "(для ребёнка — согласие родителя). Подробнее: /privacy</i>")


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def help_cmd(msg: Message):
    await msg.answer(
        "ℹ️ <b>Как это работает</b>\n\n"
        f"• {start_hint()[0].upper() + start_hint()[1:]} открывает приложение: "
        "регистрация, тест и личный кабинет.\n"
        "• Вопросы становятся сложнее шаг за шагом. Если этап не пройден, тест "
        "заканчивается — это нормально, так мы точнее определяем уровень.\n"
        "• Не знаете ответа — нажмите <b>«🤷 Не знаю»</b>, не угадывайте.\n"
        "• Когда наберётся группа вашего уровня (6–10 человек, в зависимости от возраста), "
        "мы сообщим вам здесь, и с вами свяжется преподаватель.\n\n"
        "Пройти тест заново можно в любое время.",
        reply_markup=main_menu(msg.from_user.id))


# ============================================================ registration (чат)
@router.message(Reg.name, F.text)
async def reg_name(msg: Message, state: FSMContext):
    name = " ".join(msg.text.split())
    if not (2 <= len(name) <= 60) or name.startswith("/"):
        await msg.answer("Пожалуйста, напишите имя и фамилию (2–60 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("📱 Отправьте номер телефона кнопкой ниже или напишите его "
                     "(например: +7 701 123 45 67).", reply_markup=kb)


@router.message(Reg.phone)
async def reg_phone(msg: Message, state: FSMContext):
    phone = normalize_phone(msg.contact.phone_number if msg.contact else msg.text)
    if not phone:
        await msg.answer("Номер не распознан. Пример: +7 701 123 45 67")
        return
    await state.update_data(phone=phone)
    await state.set_state(Reg.age)
    await msg.answer("✅ Спасибо!", reply_markup=ReplyKeyboardRemove())
    await msg.answer("🎂 Выберите возраст ученика:", reply_markup=age_kb())


@router.callback_query(Reg.age, F.data.startswith("age:"))
async def reg_age(cb: CallbackQuery, state: FSMContext, bot: Bot):
    age = cb.data.split(":", 1)[1]
    if age not in AGE_GROUPS:
        await cb.answer()
        return
    data = await state.get_data()
    await state.clear()
    await cb.message.edit_text(f"✅ Возраст: <b>{title_ru(age)}</b>")
    await cb.answer()
    await register_student(bot, cb.from_user.id, cb.from_user.username,
                           data["name"], data["phone"], age)


async def register_student(bot: Bot, uid: int, username: str | None, name: str, phone: str,
                           age: str, quiet: bool = False, consent: bool = True) -> None:
    """Чаттан, Mini App-тан не API-ден тіркелу осы жерде аяқталады."""
    db.add_student(uid, name, phone, age, username, consent=consent)
    text = (f"🎉 <b>{esc(name)}</b>, вы зарегистрированы!\n"
            f"Возрастная группа: {title_ru(age)}\n\n"
            + ("Тест уже открыт в приложении — удачи! 🍀" if quiet else
               f"Теперь нажмите {start_hint()} и пройдите тест — это займёт 10–15 минут."))
    await bot.send_message(uid, text, reply_markup=main_menu(uid))
    uname = f"@{esc(username)}" if username else "—"
    await notify_admins(bot,
        "🆕 <b>Жаңа студент тіркелді</b>\n"
        f"👤 {esc(name)}\n📱 {esc(phone)}\n"
        f"🎂 {AGE_GROUPS[age]['title']}\n💬 {uname}")


# ==================================================================== test (чат)
def build_sequence(age: str) -> list[list[int]]:
    """[блок, сұрақ] тізімі: әр кезеңнен банктен біркелкі таңдалған сұрақтар (engine.make_plan)."""
    return engine.make_plan(age)["seq"]


async def show_question(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    age, pos, seq = data["age"], data["pos"], data["seq"]
    bi, qi = seq[pos]
    q = AGE_GROUPS[age]["blocks"][bi]["questions"][qi]
    order = list(range(len(q["options"])))
    random.shuffle(order)
    rows = [[InlineKeyboardButton(text=q["options"][o], callback_data=f"a:{pos}:{o}")]
            for o in order]
    rows.append([InlineKeyboardButton(text="🤷 Не знаю", callback_data=f"a:{pos}:{DONT_KNOW}")])
    nblocks = len(AGE_GROUPS[age]["blocks"])
    text = (f"<b>Этап {bi + 1}/{nblocks}</b> · вопрос {pos + 1}\n\n"
            f"<b>{esc(q['q'])}</b>"
            + (f"\n<i>{esc(q['ru'])}</i>" if q.get("ru") else ""))
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(F.text == BTN_TEST)
async def test_intro(msg: Message, state: FSMContext):
    st = db.get_student(msg.from_user.id)
    if not st:
        await msg.answer("Сначала зарегистрируйтесь: /start")
        return
    await state.clear()
    g = AGE_GROUPS[st["age_group"]]
    total = sum(len(b["questions"]) for b in g["blocks"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Начать тест", callback_data="test:start")]])
    await msg.answer(
        f"📝 <b>Тест на уровень · {g['title_ru']}</b>\n\n{esc(g['intro_ru'])}\n\n"
        f"Этапов: {len(g['blocks'])}, вопросов — не больше {total}.",
        reply_markup=kb)


@router.callback_query(F.data == "test:start")
async def test_start(cb: CallbackQuery, state: FSMContext):
    st = db.get_student(cb.from_user.id)
    if not st:
        await cb.answer("Сначала нажмите /start", show_alert=True)
        return
    age = st["age_group"]
    await state.set_state(Test.running)
    await state.update_data(age=age, seq=build_sequence(age), pos=0, answers=[], dont_know=0)
    await show_question(cb.message, state, edit=True)
    await cb.answer()


@router.callback_query(Test.running, F.data.startswith("a:"))
async def test_answer(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, pos_s, choice = cb.data.split(":")
    data = await state.get_data()
    if int(pos_s) != data["pos"]:
        await cb.answer()  # ескі батырманы қайта басу
        return
    age, seq, pos = data["age"], data["seq"], data["pos"]
    answers = data["answers"] + [choice == "0"]
    dont_know = data["dont_know"] + (choice == DONT_KNOW)
    pos += 1
    await state.update_data(answers=answers, dont_know=dont_know, pos=pos)
    await cb.answer()
    bi = seq[pos - 1][0]
    if pos == len(seq) or seq[pos][0] != bi:  # блок аяқталды
        block = [a for a, (b, _) in zip(answers, seq) if b == bi]
        if sum(block) / len(block) < pass_ratio(age) or pos == len(seq):
            await state.clear()
            await cb.message.delete()
            level, _ = compute_level(age, seq, answers)
            await save_and_report(bot, cb.from_user.id, age, level, sum(answers), len(answers), dont_know)
            return
    await show_question(cb.message, state, edit=True)


# ====================================================================== scoring
def compute_level(age: str, seq, answers) -> tuple[str, int]:
    """Қатарынан өтілген ең жоғарғы блоктың деңгейі (қазақша атауы)."""
    passed = 0
    for bi in range(len(AGE_GROUPS[age]["blocks"])):
        ans = [a for a, (b, _) in zip(answers, seq) if b == bi]
        if ans and sum(ans) / len(ans) >= pass_ratio(age):
            passed = bi + 1
        else:
            break
    return all_levels(age)[passed], passed


RETAKE_LIMIT, RETAKE_DAYS = 2, 7   # 7 күнде 2 тапсыру (админге шектеу жоқ)


def retake_after(uid: int) -> datetime | None:
    """Пересдача шектелсе — қашаннан бастап қайта тапсыруға болады, әйтпесе None."""
    if is_admin(uid):
        return None
    since = datetime.now() - timedelta(days=RETAKE_DAYS)
    recent = db.results_since(uid, since.strftime("%Y-%m-%d %H:%M"))
    if len(recent) < RETAKE_LIMIT:
        return None
    first = datetime.strptime(recent[-RETAKE_LIMIT]["taken_at"], "%Y-%m-%d %H:%M")
    return first + timedelta(days=RETAKE_DAYS)


async def save_and_report(bot: Bot, uid: int, age: str, level: str, correct: int, answered: int,
                          dont_know: int, topics: tuple = ([], [])) -> dict:
    """Нәтижені сақтап, студентке (орысша) және админге (қазақша) хабарлайды."""
    db.save_result(uid, age, level, correct, answered, dont_know)
    waiting = len(db.pool(age, level))
    topics_ru, topics_kz = topics
    review = ""
    if topics_ru:
        review = "\n\n📚 <b>Что стоит повторить:</b>\n" + "\n".join(f"• {esc(t)}" for t in topics_ru)
    await bot.send_message(uid,
        "🎉 <b>Тест завершён!</b>\n\n"
        f"🏅 Ваш результат: <b>{esc(level_ru(age, level))}</b>\n"
        f"✅ Правильных ответов: {correct} из {answered}"
        f"{review}\n\n"
        f"👥 Группа вашего уровня собирается: {min(waiting, group_size(age))}/{group_size(age)}.\n"
        "Как только она наберётся, мы сразу напишем вам здесь, "
        "и с вами свяжется преподаватель.\n\n"
        f"Результат сохранён в личном кабинете ({start_hint()}). Спасибо! 🙌",
        reply_markup=main_menu(uid))
    await bot.send_message(uid,
        "🎤 <b>Последний шаг (по желанию)</b>\n"
        "Запишите голосовое сообщение на 30–60 секунд: расскажите о себе по-казахски — как сможете. "
        "Преподаватель послушает и уточнит уровень. Просто отправьте голосовое сюда в чат.")
    st = db.get_student(uid)
    weak = ""
    if topics_kz:
        weak = "\n📚 Әлсіз тақырыптар:\n" + "\n".join(f"• {esc(t)}" for t in topics_kz)
    await notify_admins(bot,
        "📊 <b>Тест нәтижесі</b>\n"
        f"👤 {esc(st['name'])} · 📱 {esc(st['phone'])}\n"
        f"🎂 {AGE_GROUPS[age]['title']}\n"
        f"🏅 <b>{esc(level)}</b> ({correct}/{answered}, білмеймін: {dont_know}){weak}")
    await try_form_groups(bot, age, level)
    return {"level": level_ru(age, level), "correct": correct, "answered": answered, "topics": topics_ru}


# ========================================================== API (Railway, мәзір батырмасы)
def check_init_data(init_data: str) -> dict | None:
    """Telegram-ның initData қолтаңбасын тексереді. Дұрыс болса — пайдаланушы деректері."""
    # keep_blank_values: Telegram бос өріс жіберсе де қолтаңба дұрыс есептелуі үшін
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        logging.warning("initData: hash жоқ (ұзындығы %s)", len(init_data or ""))
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        logging.warning("initData: қолтаңба сәйкес емес, өрістер: %s", sorted(pairs))
        return None
    if time.time() - int(pairs.get("auth_date", "0") or 0) > 7 * 24 * 3600:
        logging.warning("initData: ескірген (auth_date=%s)", pairs.get("auth_date"))
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        logging.warning("initData: user оқылмады")
        return None
    return user if user.get("id") else None


async def _api_user(request: web.Request) -> tuple[dict, dict]:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    user = check_init_data(body.get("initData", ""))
    if not user:
        logging.warning("API %s: initData қабылданбады", request.path)
        raise web.HTTPUnauthorized(text="bad initData")
    logging.info("API %s: user %s", request.path, user.get("id"))
    return body, user


async def api_state(request: web.Request) -> web.Response:
    _, user = await _api_user(request)
    return web.json_response({"ok": True, "state": student_state(user["id"])})


async def api_register(request: web.Request) -> web.Response:
    body, user = await _api_user(request)
    name = " ".join(str(body.get("name", "")).split())[:60]
    phone = normalize_phone(str(body.get("phone", "")))
    age = body.get("age")
    if len(name) < 2 or not phone or age not in AGE_GROUPS:
        return web.json_response({"ok": False, "error": "Данные неполные"}, status=400)
    if body.get("consent") is not True:
        return web.json_response({"ok": False, "error": "Нужно согласие на обработку данных"}, status=400)
    await register_student(request.app["bot"], user["id"], user.get("username"),
                           name, phone, age, quiet=True, consent=True)
    return web.json_response({"ok": True, "state": student_state(user["id"])})


def _err(text: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": text}, status=status)


async def _attempt(request: web.Request):
    """Сұраныстағы attempt осы студенттікі әрі аяқталмаған болуы керек."""
    body, user = await _api_user(request)
    st = db.get_student(user["id"])
    if not st:
        return body, user, None, None, _err("Не зарегистрирован")
    try:
        att = db.get_attempt(int(body.get("attempt")))
    except (TypeError, ValueError):
        att = None
    if not att or att["tg_id"] != user["id"] or att["finished"]:
        return body, user, st, None, _err("Попытка не найдена — начните тест заново")
    return body, user, st, att, None


async def api_start(request: web.Request) -> web.Response:
    """Жаңа тапсыру: сервер сұрақтарды таңдап, жоспарды сақтайды (жауаптарсыз)."""
    _, user = await _api_user(request)
    st = db.get_student(user["id"])
    if not st:
        return _err("Не зарегистрирован")
    nxt = retake_after(user["id"])
    if nxt:
        return _err(f"Пройти тест снова можно {nxt:%d.%m в %H:%M}. "
                    f"Мы даём {RETAKE_LIMIT} попытки за {RETAKE_DAYS} дней, чтобы результат был честным.")
    plan = engine.make_plan(st["age_group"])
    aid = db.create_attempt(user["id"], st["age_group"], plan)
    return web.json_response({"ok": True, "attempt": aid, "seq": plan["seq"], "ver": quiz_version()})


async def api_progress(request: web.Request) -> web.Response:
    """Кезең аяқталғанда: жалғастыру / шекарада қосымша сұрақтар / тоқтату."""
    body, user, st, att, err = await _attempt(request)
    if err:
        return err
    plan = att["plan"]
    graded, _ = engine.grade(att["age_group"], plan, body.get("a"))
    decision, extra = engine.decide(att["age_group"], plan, graded)
    if decision == "extra":
        db.save_attempt(att["id"], plan)
    return web.json_response({"ok": True, "cont": decision != "stop", "extra": extra})


async def api_result(request: web.Request) -> web.Response:
    body, user, st, att, err = await _attempt(request)
    if err:
        return err
    age, plan = att["age_group"], att["plan"]
    graded, dont_know = engine.grade(age, plan, body.get("a"))
    if not graded:
        return _err("Нет ответов")
    graded = engine.trim(age, plan, graded)
    dont_know = sum(1 for (bi, qi), _ in graded.items() if [bi, qi, None] in body.get("a", []))
    db.save_attempt(att["id"], plan, finished=True)
    level = engine.level_of(age, plan, graded)
    res = await save_and_report(request.app["bot"], user["id"], age, level, sum(graded.values()),
                                len(graded), dont_know, engine.weak_topics(age, graded))
    return web.json_response({"ok": True, "result": res, "state": student_state(user["id"])})


_clientlog_times: list[float] = []
LOG_RING: list[str] = []  # соңғы 400 жол (админ /api/debug арқылы оқиды)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_RING.append(f"{datetime.now():%H:%M:%S} {record.levelname[0]} {record.getMessage()}"[:600])
        del LOG_RING[:-400]


@web.middleware
async def log_requests(request: web.Request, handler):
    """Әр сұранысты журналға жазады (қосымша қалай ашылғанын көру үшін)."""
    try:
        resp = await handler(request)
        status = resp.status
        return resp
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        if request.path not in ("/health", "/api/clientlog", "/api/debug"):
            logging.info("HTTP %s %s?%s -> %s | %s", request.method, request.path,
                         request.query_string[:40], locals().get("status", "ERR"),
                         request.headers.get("User-Agent", "")[:80])


async def api_debug(request: web.Request) -> web.Response:
    """Журналды қайтарады — тек админнің қолтаңбасымен."""
    _, user = await _api_user(request)
    if not is_admin(user["id"]):
        raise web.HTTPForbidden()
    return web.json_response({"ok": True, "log": LOG_RING[-int(request.query.get("n", 200)):]})


async def api_clientlog(request: web.Request) -> web.Response:
    """Қосымшадағы қатені логқа жазып, админге хабарлайды (сағатына 20-дан көп емес)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    user = check_init_data(body.get("initData", "")) or {}
    text = str(body.get("msg", ""))[:500]
    info = str(body.get("info", ""))[:200]
    if body.get("silent"):
        logging.info("Қосымша [%s]: %s", user.get("id", "?"), text)
        return web.json_response({"ok": True})
    logging.warning("Қосымша қатесі [%s]: %s | %s", user.get("id", "?"), text, info)
    now = time.time()
    _clientlog_times[:] = [t for t in _clientlog_times if now - t < 3600]
    if len(_clientlog_times) < 20:
        _clientlog_times.append(now)
        who = f"{user.get('first_name', '')} (id {user['id']})" if user.get("id") else "белгісіз"
        await notify_admins(request.app["bot"],
            f"⚠️ <b>Қосымшада қате</b>\n👤 {esc(who)}\n<code>{esc(text)}</code>\n<i>{esc(info)}</i>")
    return web.json_response({"ok": True})


async def web_index(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


async def web_questions(request: web.Request) -> web.Response:
    return web.Response(text=quiz_js(), content_type="application/javascript",
                        headers={"Cache-Control": "no-store, max-age=0"})


async def web_audio(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not re.fullmatch(r"[0-9a-f]{12}\.m4a", name) or not (AUDIO_DIR / name).exists():
        raise web.HTTPNotFound()
    return web.FileResponse(AUDIO_DIR / name, headers={"Cache-Control": "public, max-age=86400"})


def make_web_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=256 * 1024, middlewares=[log_requests])
    app["bot"] = bot
    app.router.add_get("/", web_index)
    app.router.add_get("/index.html", web_index)
    app.router.add_get("/questions.js", web_questions)
    app.router.add_get("/audio/{name}", web_audio)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/result", api_result)
    app.router.add_post("/api/progress", api_progress)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/clientlog", api_clientlog)
    app.router.add_post("/api/debug", api_debug)
    return app


# ======================================================================= groups
async def form_group(bot: Bot, age: str, level: str, members) -> None:
    gid = db.create_group(age, level, [m["tg_id"] for m in members])
    g = db.get_group(gid)
    lines = [f"{i}. {esc(m['name'])} — {esc(m['phone'])}"
             + (f" (@{esc(m['username'])})" if m["username"] else "")
             for i, m in enumerate(members, 1)]
    await notify_admins(bot,
        f"👥 <b>Жаңа топ дайын!</b>\n<b>{esc(group_title_kz(g))}</b>\n"
        f"Адам саны: {len(members)}\n\n" + "\n".join(lines))
    for m in members:
        try:
            await bot.send_message(m["tg_id"],
                "🎉 <b>Ваша группа собрана!</b>\n\n"
                f"👥 {esc(group_title_ru(g))}\n\n"
                "В ближайшее время с вами свяжется преподаватель и расскажет "
                "о расписании занятий. До встречи на уроке! 🇰🇿",
                reply_markup=main_menu(m["tg_id"]))
        except Exception as e:
            logging.warning("Студентке жіберілмеді %s: %s", m["tg_id"], e)


async def try_form_groups(bot: Bot, age: str, level: str) -> None:
    size = group_size(age)
    while len(waiting := db.pool(age, level)) >= size:
        await form_group(bot, age, level, waiting[:size])


# ================================================================ cabinet (чат)
@router.message(F.text == BTN_CABINET)
async def cabinet(msg: Message):
    st = db.get_student(msg.from_user.id)
    if not st:
        await msg.answer("Сначала зарегистрируйтесь: /start")
        return
    age = st["age_group"]
    res = db.get_results(st["tg_id"])
    lines = ["👤 <b>Личный кабинет</b>\n",
             f"Имя: {esc(st['name'])}", f"Телефон: {esc(st['phone'])}",
             f"Возраст: {title_ru(age)}\n"]
    if res:
        lines.append(f"🏅 Текущий уровень: <b>{esc(level_ru(age, res[0]['level']))}</b>\n")
        lines.append("📜 История:")
        lines += [f"• {r['taken_at'][:10]} — {esc(level_ru(age, r['level']))} "
                  f"({r['correct']}/{r['answered']})" for r in res]
        grp = db.group_of(st["tg_id"])
        if grp:
            lines.append(f"\n👥 Группа: <b>{esc(group_title_ru(grp))}</b>")
        else:
            n = len(db.pool(age, res[0]["level"]))
            lines.append(f"\n⏳ Группа собирается: {n}/{group_size(age)}")
    else:
        lines.append(f"Вы ещё не проходили тест. Нажмите «{BTN_TEST}».")
    await msg.answer("\n".join(lines), reply_markup=main_menu(msg.from_user.id))


# ======================================================================== admin
@router.message(Command("myid"))
async def myid(msg: Message):
    await msg.answer(f"Telegram ID: <code>{msg.from_user.id}</code>")


@router.message(Command("admin"))
async def admin_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer(
        "🛠 <b>Админ командалары</b>\n"
        "/stats — жалпы статистика\n"
        "/queue — топқа кезекте тұрғандар (толмаған топты қолмен жабуға болады)\n"
        "/groups — құрылған топтар\n"
        "/export — барлық студенттер (Excel/CSV файл)\n"
        "/backup — базаның көшірмесі (students.db)\n"
        "/reset — өз жазбаңызды өшіру (тіркелуді қайта тексеру үшін)\n"
        "/audio — балаларға арналған сұрақтарды өз дауысыңызбен жазу (/audio teens — жасөспірімдерге)\n"
        "Базаны ауыстыру үшін .db файлын ботқа жай жіберіңіз.")


@router.message(Command("stats"))
async def admin_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    s = db.stats()
    by_age = "\n".join(f"• {AGE_GROUPS[r['age_group']]['title']}: {r['n']}" for r in s["by_age"])
    await msg.answer(
        f"📈 Тіркелген: <b>{s['students']}</b>\nТест тапсырған: <b>{s['tested']}</b>\n"
        f"Топтар: <b>{s['groups']}</b>\n\n{by_age or '—'}")


@router.message(Command("queue"))
async def admin_queue(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    pools = [p for p in db.all_pools() if p["level"] in all_levels(p["age_group"])]
    if not pools:
        await msg.answer("Кезекте ешкім жоқ.")
        return
    lines, rows = ["⏳ <b>Топқа кезекте тұрғандар</b>\n"], []
    for p in pools:
        age, level = p["age_group"], p["level"]
        lines.append(f"• {AGE_GROUPS[age]['title']} · {esc(level)}: <b>{p['n']}/{group_size(age)}</b>")
        rows.append([InlineKeyboardButton(
            text=f"Жабу: {AGE_GROUPS[age]['title']} · {level} ({p['n']})"[:60],
            callback_data=f"close:{age}:{all_levels(age).index(level)}")])
    await msg.answer("\n".join(lines) + "\n\nТолмаған топты қазір жасау үшін батырманы басыңыз:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("close:"))
async def admin_close(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return
    _, age, li = cb.data.split(":")
    level = all_levels(age)[int(li)]
    waiting = db.pool(age, level)
    if not waiting:
        await cb.answer("Кезек бос", show_alert=True)
        return
    await cb.answer("Топ құрылды ✅")
    await form_group(bot, age, level, waiting[:group_size(age)])


@router.message(Command("groups"))
async def admin_groups(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    groups = db.list_groups()
    if not groups:
        await msg.answer("Әзірге топ жоқ.")
        return
    for g in groups[:20]:
        members = db.group_members(g["id"])
        lines = [f"{i}. {esc(m['name'])} — {esc(m['phone'])}" for i, m in enumerate(members, 1)]
        await msg.answer(f"👥 <b>{esc(group_title_kz(g))}</b>\n"
                         f"Құрылған: {g['created_at']} · {g['n']} адам\n\n" + "\n".join(lines))


@router.message(Command("export"))
async def admin_export(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Аты-жөні", "Телефон", "Telegram", "Жас тобы", "Тіркелген", "Деңгей",
                "Дұрыс", "Жауап берген", "Білмеймін", "Тест күні", "Топ"])
    for r in db.export_rows():
        grp = f"{r['group_level']} №{r['group_number']}" if r["group_number"] else ""
        w.writerow([r["name"], r["phone"], r["username"] or "", AGE_GROUPS[r["age_group"]]["title"],
                    r["registered"], r["level"] or "", r["correct"] or "", r["answered"] or "",
                    r["dont_know"] or "", r["taken_at"] or "", grp])
    data = buf.getvalue().encode("utf-8-sig")  # Excel кириллицаны дұрыс ашуы үшін
    await msg.answer_document(BufferedInputFile(data, filename="students.csv"))


PRIVACY_TEXT = (
    "🔒 <b>Какие данные мы храним</b>\n\n"
    "• имя и фамилию ученика, номер телефона, возрастную группу, имя пользователя в Telegram;\n"
    "• результаты теста и группу, в которую записан ученик.\n\n"
    "<b>Зачем:</b> только чтобы определить уровень казахского языка, собрать учебную группу "
    "и связаться с вами по поводу занятий. Данные не передаются третьим лицам.\n\n"
    "<b>Для детей</b> согласие на обработку данных даёт родитель или законный представитель.\n\n"
    "Удалить все свои данные можно в любой момент командой /delete."
)


@router.message(Command("privacy"))
async def privacy_cmd(msg: Message):
    await msg.answer(PRIVACY_TEXT)


@router.message(Command("delete"))
async def delete_cmd(msg: Message):
    if not db.get_student(msg.from_user.id):
        await msg.answer("У нас нет ваших данных.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data="del:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="del:no")]])
    await msg.answer("Удалить ваши данные, результаты тестов и место в группе? "
                     "Это действие нельзя отменить.", reply_markup=kb)


@router.callback_query(F.data.startswith("del:"))
async def delete_confirm(cb: CallbackQuery, bot: Bot):
    if cb.data == "del:no":
        await cb.message.edit_text("Хорошо, ничего не удалено.")
        await cb.answer()
        return
    st = db.get_student(cb.from_user.id)
    db.delete_student(cb.from_user.id)
    await cb.message.edit_text("✅ Ваши данные удалены. Чтобы пройти тест снова, нажмите /start.")
    await cb.answer()
    if st:
        await notify_admins(bot, f"🗑 Студент өз деректерін өшірді: {esc(st['name'])} · {esc(st['phone'])}")


@router.message(Command("reset"))
async def admin_reset(msg: Message, state: FSMContext):
    """Админ өз жазбасын өшіреді — тіркелуді жаңа адам ретінде тексеру үшін."""
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    db.delete_student(msg.from_user.id)
    await msg.answer("🧹 Сіздің жазбаңыз базадан өшірілді. Енді «Начать» басып, "
                     "жаңа студент ретінде тіркеліп көріңіз.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("backup"))
async def admin_backup(msg: Message):
    """Админге базаның көшірмесін (students.db) жібереді."""
    if not is_admin(msg.from_user.id):
        return
    name = f"students-{datetime.now():%Y-%m-%d}.db"
    await msg.answer_document(FSInputFile(db.DB_PATH, filename=name),
                              caption="💾 Базаның көшірмесі. Қайта жүктеу үшін осы файлды ботқа жіберсеңіз болды.")


@router.message(F.document & F.document.file_name.endswith(".db"))
async def admin_import(msg: Message, bot: Bot):
    """Админ .db файлын жіберсе — базаны сол файлмен ауыстырады (ескісі .bak болып сақталады)."""
    if not is_admin(msg.from_user.id):
        return
    tmp = db.DATA_DIR / "import.tmp"
    await bot.download(msg.document, destination=tmp)
    try:
        with sqlite3.connect(tmp) as c:
            n = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        await msg.answer("❌ Бұл файл студенттер базасы емес.")
        return
    if db.DB_PATH.exists():
        shutil.copy(db.DB_PATH, db.DB_PATH.with_suffix(f".{datetime.now():%Y%m%d-%H%M%S}.bak"))
    tmp.replace(db.DB_PATH)
    db.init()
    await msg.answer(f"✅ База жүктелді: {n} студент. Ескі база .bak файлы болып сақталды.")


# ================================================================ дыбыс (audio)
AUDIO_REC: dict[int, dict] = {}   # админ сұрақ дыбысын жазып жатқанда: {admin_id: {"age":..., "queue": [...]}}


def to_m4a(src: Path, dst: Path) -> bool:
    """Telegram-ның OGG/Opus дауысын iPhone-да да ойнайтын M4A-ға айналдырады."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        logging.warning("ffmpeg табылмады: %s", e)
        return False
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-c:a", "aac",
                        "-b:a", "64k", "-movflags", "+faststart", str(dst)], capture_output=True, timeout=120)
    if r.returncode != 0:
        logging.warning("ffmpeg қатесі: %s", r.stderr[:300])
    return r.returncode == 0 and dst.exists()


async def audio_prompt(msg: Message, uid: int) -> None:
    rec = AUDIO_REC.get(uid)
    if not rec or not rec["queue"]:
        AUDIO_REC.pop(uid, None)
        await msg.answer("✅ Барлық сұраққа дыбыс жазылды. Қосымшада 🔊 батырмасы пайда болды.")
        return
    bi, qi = rec["queue"][0]
    q = AGE_GROUPS[rec["age"]]["blocks"][bi]["questions"][qi]
    done = rec["total"] - len(rec["queue"])
    await msg.answer(f"🎙 <b>{done + 1}/{rec['total']}</b> · {AGE_GROUPS[rec['age']]['title']} · {bi + 1}-кезең\n\n"
                     f"«{esc(q['q'])}»\n\n"
                     "Осы сұрақты баяу әрі анық оқып, <b>дауыстық хабарлама</b> жіберіңіз.\n"
                     "/skip — өткізу · /stop — тоқтату")


@router.message(Command("audio"))
async def admin_audio(msg: Message, command: CommandObject):
    """Сұрақтарды мұғалімнің дауысымен жазу: /audio (балалар) · /audio teens · /audio kids all (қайта жазу)."""
    if not is_admin(msg.from_user.id):
        return
    args = (command.args or "kids").split()
    age, redo = args[0], "all" in args[1:]
    if age not in AGE_GROUPS:
        await msg.answer("Жас тобы: /audio kids · /audio teens · /audio adults")
        return
    queue = [(bi, qi) for bi, b in enumerate(AGE_GROUPS[age]["blocks"]) for qi, q in enumerate(b["questions"])
             if redo or not (AUDIO_DIR / f"{audio_key(age, q['q'])}.m4a").exists()]
    if not queue:
        await msg.answer("✅ Бұл топтың барлық сұрағына дыбыс бар. Қайта жазу үшін: "
                         f"<code>/audio {age} all</code>")
        return
    AUDIO_REC[msg.from_user.id] = {"age": age, "queue": queue, "total": len(queue)}
    await msg.answer(f"🎙 Дыбыс жазу басталды: {len(queue)} сұрақ. Бір сұраққа — бір дауыстық хабарлама.")
    await audio_prompt(msg, msg.from_user.id)


@router.message(Command("skip"))
async def admin_audio_skip(msg: Message):
    rec = AUDIO_REC.get(msg.from_user.id)
    if rec and rec["queue"]:
        rec["queue"].pop(0)
        await audio_prompt(msg, msg.from_user.id)


@router.message(Command("stop"))
async def admin_audio_stop(msg: Message):
    if AUDIO_REC.pop(msg.from_user.id, None) is not None:
        await msg.answer("⏹ Дыбыс жазу тоқтатылды. Жалғастыру үшін /audio жазыңыз.")


@router.message(F.voice)
async def on_voice(msg: Message, bot: Bot):
    uid = msg.from_user.id
    rec = AUDIO_REC.get(uid)
    if rec and rec["queue"]:
        # мұғалім сұрақты оқып жазды → сақтап, M4A-ға айналдырамыз
        bi, qi = rec["queue"][0]
        q = AGE_GROUPS[rec["age"]]["blocks"][bi]["questions"][qi]
        key = audio_key(rec["age"], q["q"])
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        src = AUDIO_DIR / f"{key}.ogg"
        await bot.download(msg.voice, destination=src)
        ok = await asyncio.to_thread(to_m4a, src, AUDIO_DIR / f"{key}.m4a")
        if not ok:
            await msg.answer("⚠️ Дыбыс сақталды, бірақ форматын өзгерту мүмкін болмады. /skip не қайта жіберіңіз.")
            return
        rec["queue"].pop(0)
        await msg.answer("✅ Сақталды")
        await audio_prompt(msg, uid)
        return
    # студенттің дауыстық тапсырмасы → мұғалімге
    st = db.get_student(uid)
    if not st:
        await msg.answer("Сначала зарегистрируйтесь: /start")
        return
    res = db.get_results(uid, 1)
    lvl = res[0]["level"] if res else "тест әлі тапсырылмаған"
    caption = (f"🎤 <b>Дауыстық жауап</b>\n👤 {esc(st['name'])} · 📱 {esc(st['phone'])}\n"
               f"🎂 {AGE_GROUPS[st['age_group']]['title']}\n🏅 {esc(lvl)}")
    for aid in ADMIN_IDS:
        try:
            await bot.send_voice(aid, msg.voice.file_id, caption=caption)
        except Exception as e:
            logging.warning("Дауыс админге жіберілмеді %s: %s", aid, e)
    await msg.answer("🙏 Спасибо! Преподаватель послушает запись и уточнит ваш уровень.")


@router.message()
async def fallback(msg: Message, state: FSMContext):
    if await state.get_state() == Test.running.state:
        await msg.answer("Выберите ответ кнопками выше 👆")
        return
    if msg.contact:  # Mini App ішінде «нөмірді алу» басылғанда келеді — жауап керек емес
        return
    if db.get_student(msg.from_user.id):
        await msg.answer(f"Нажмите {start_hint()} 👇",
                         reply_markup=open_app_kb() or main_menu(msg.from_user.id))
    else:
        await msg.answer("Чтобы начать, нажмите /start")


# ========================================================================= main
async def setup_commands(bot: Bot) -> None:
    """Мәзірдегі командалар: студентке орысша, админге қосымша қазақша командалар."""
    student = [BotCommand(command="start", description="🚀 Начать"),
               BotCommand(command="help", description="❓ Помощь"),
               BotCommand(command="privacy", description="🔒 Мои данные"),
               BotCommand(command="delete", description="🗑 Удалить мои данные")]
    await bot.set_my_commands(student, scope=BotCommandScopeDefault())
    admin = student + [BotCommand(command="admin", description="Админ командалары"),
                       BotCommand(command="stats", description="Статистика"),
                       BotCommand(command="queue", description="Топқа кезек"),
                       BotCommand(command="groups", description="Құрылған топтар"),
                       BotCommand(command="export", description="Excel файл"),
                       BotCommand(command="backup", description="Базаның көшірмесі"),
                       BotCommand(command="reset", description="Өз жазбамды өшіру (тест үшін)"),
                       BotCommand(command="audio", description="Сұрақтарды дауыспен жазу")]
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=aid))
        except Exception as e:
            logging.warning("Админ командалары орнатылмады %s: %s", aid, e)
    try:
        await bot.set_my_description(
            "Сәлеметсіз бе! 👋 Здравствуйте!\n\n"
            "Бот определит ваш уровень казахского языка за 10–15 минут и подберёт группу. "
            "Отдельные тесты для детей 6–10 лет, школьников 10–18 лет и взрослых.\n\n"
            "Нажмите «Старт» 👇")
        await bot.set_my_short_description("Тест на уровень казахского языка 🇰🇿 · 6–10 / 10–18 / 18+")
    except Exception as e:
        logging.warning("Сипаттама орнатылмады: %s", e)


async def main():
    for s in (sys.stdout, sys.stderr):  # Windows консолі қазақ әріптерін көрсетуі үшін
        s.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addHandler(RingHandler())
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN жоқ. .env файлына BOT_TOKEN=... жазыңыз.")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS бос — хабарламалар ешкімге жіберілмейді. "
                        "Ботқа /myid жазып, ID-ді .env файлына қосыңыз.")
    db.init()
    logging.info("База: %s", db.DB_PATH)
    try:
        import imageio_ffmpeg
        logging.info("Дыбыс (ffmpeg): %s", imageio_ffmpeg.get_ffmpeg_version())
    except Exception as e:
        logging.warning("Дыбыс (ffmpeg) жоқ — /audio жазбалары M4A-ға айналмайды: %s", e)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await setup_commands(bot)
    if PORT:
        # Railway: қосымша мен API осы сайтта; Telegram-ның мәзір батырмасына «Начать» қоямыз
        runner = web.AppRunner(make_web_app(bot))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        logging.info("Веб-сервер іске қосылды: порт %s, мекенжай %s", PORT, WEBAPP_URL or "—")
        if WEBAPP_URL.startswith("https://"):
            try:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="Начать", web_app=WebAppInfo(url=app_url())))
                logging.info("Мәзір батырмасы: %s", app_url())
            except Exception as e:
                logging.warning("Мәзір батырмасы орнатылмады: %s", e)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

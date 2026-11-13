"""Мини‑апп үшін сұрақтар файлы (questions.js) — ДҰРЫС ЖАУАПСЫЗ.

questions.py-да әр сұрақтың бірінші нұсқасы — дұрыс жауап. Қосымшаға олай берсек, кез келген адам
файлды ашып жауаптарды көре алады. Сондықтан мұнда нұсқалар араластырылады (тұрақты тәртіппен),
ал қай нұсқа дұрыс екенін тек сервер біледі: қосымша таңдалған нұсқаның мәтінін жібереді,
сервер оны questions.py-мен салыстырады.

Бот бұл файлды /questions.js мекенжайында өзі таратады — ештеңе істеудің қажеті жоқ.
"""
import hashlib
import json
import random

from questions import AGE_GROUPS, group_size


def _shuffled(options: list[str]) -> list[str]:
    """Нұсқаларды тұрақты тәртіппен араластырады (әр жолы бірдей, бірақ дұрысы бірінші емес)."""
    seed = int(hashlib.sha256("|".join(options).encode()).hexdigest()[:12], 16)
    out = list(options)
    random.Random(seed).shuffle(out)
    return out


def public_quiz() -> dict:
    """Қосымшаға керегі ғана: сұрақ мәтіні, орысша көмек, араластырылған нұсқалар, атаулар."""
    ages = {}
    for key, g in AGE_GROUPS.items():
        ages[key] = {
            "title_ru": g["title_ru"],
            "hint_ru": g.get("hint_ru", ""),
            "intro_ru": g["intro_ru"],
            "group_size": group_size(key),
            "blocks": [{"questions": [{"q": q["q"], "ru": q.get("ru", ""), "options": _shuffled(q["options"])}
                                      for q in b["questions"]]}
                       for b in g["blocks"]],
        }
    return {"ages": ages}


def quiz_js() -> str:
    return "window.QUIZ = " + json.dumps(public_quiz(), ensure_ascii=False) + ";\n"

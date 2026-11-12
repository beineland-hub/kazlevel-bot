"""questions.py -> webapp/questions.js (Mini App сұрақтарды осы файлдан алады).

Railway-де бот бұл файлды өзі жасап таратады — ештеңе істеудің қажеті жоқ.
GitHub Pages-те ұстасаңыз: questions.py өзгерткен сайын  python build_webapp.py
іске қосып, webapp/questions.js файлын қайта жүктеңіз.
"""
import json
from pathlib import Path

from questions import AGE_GROUPS, GROUP_SIZE, PASS_RATIO


def quiz_js() -> str:
    data = {"passRatio": PASS_RATIO, "groupSize": GROUP_SIZE, "ages": AGE_GROUPS}
    return "window.QUIZ = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"


if __name__ == "__main__":
    out = Path(__file__).with_name("webapp") / "questions.js"
    out.parent.mkdir(exist_ok=True)
    out.write_text(quiz_js(), encoding="utf-8")
    print("Жазылды:", out)

"""Тест логикасы (Telegram-ға тәуелсіз, тестілеуге ыңғайлы).

* make_plan  — бір тапсыруға арналған сұрақтар жоспары: әр кезеңнен "ask" сұрақ кездейсоқ, бірақ
               банк бойынша біркелкі (сабақтар/модульдер ретімен бөлікке бөліп, әр бөліктен біреу).
* decide     — кезең аяқталғанда: жалғастыру / шекарада тұрса қосымша сұрақ беру / тоқтату.
* level_of   — соңғы деңгей (қатарынан өтілген кезеңдер саны бойынша).
* weak_topics — қате жауап берілген тақырыптар (студентке — орысша, админге — қазақша).
"""
import math
import random

from questions import ADULT_TOPICS, AGE_GROUPS, SCHOOL_MODULES, all_levels, pass_ratio

EXTRA = 2   # шекарада тұрғанда берілетін қосымша сұрақ саны


def make_plan(age: str, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    seq, pools, base = [], {}, {}
    for bi, block in enumerate(AGE_GROUPS[age]["blocks"]):
        n = len(block["questions"])
        ask = min(block.get("ask", n), n)
        # банкты ask бөлікке бөліп, әр бөліктен бір сұрақ — деңгейдің басынан аяғына дейін қамтылады
        chosen = [rng.randrange(n * i // ask, n * (i + 1) // ask) for i in range(ask)]
        rng.shuffle(chosen)
        seq += [[bi, qi] for qi in chosen]
        pools[str(bi)] = [qi for qi in range(n) if qi not in chosen]
        base[str(bi)] = ask
    return {"seq": seq, "pool": pools, "base": base, "extra": {}}


def _block(plan: dict, graded: dict, bi: int) -> tuple[int, int, bool]:
    """(дұрыс саны, жоспардағы сұрақ саны, бәріне жауап берілді ме)."""
    items = [tuple(x) for x in plan["seq"] if x[0] == bi]
    answered = [graded[it] for it in items if it in graded]
    return sum(answered), len(items), len(answered) == len(items)


def block_passed(age: str, plan: dict, graded: dict, bi: int) -> bool:
    correct, _, _ = _block(plan, graded, bi)
    base = plan["base"][str(bi)]
    need = math.ceil(pass_ratio(age) * base - 1e-9)
    k = plan["extra"].get(str(bi), 0)
    # қосымша сұрақ берілсе: негізгіде бір жетпеді, енді қосымшалардың бәрі дұрыс болуы керек
    return correct >= (need - 1 + k if k else need)


def decide(age: str, plan: dict, graded: dict, rng: random.Random | None = None) -> tuple[str, list]:
    """Соңғы жауап берілген кезең бойынша шешім: ("cont"|"extra"|"stop", қосымша сұрақтар)."""
    if not graded:
        return "cont", []
    last_bi = max(bi for bi, _ in graded)
    correct, n, done = _block(plan, graded, last_bi)
    if not done:
        return "cont", []
    nblocks = len(AGE_GROUPS[age]["blocks"])
    if block_passed(age, plan, graded, last_bi):
        return ("cont" if last_bi < nblocks - 1 else "stop"), []
    base = plan["base"][str(last_bi)]
    need = math.ceil(pass_ratio(age) * base - 1e-9)
    pool = plan["pool"].get(str(last_bi), [])
    if str(last_bi) not in plan["extra"] and correct == need - 1 and pool:
        rng = rng or random.Random()
        k = min(EXTRA, len(pool))
        picked = rng.sample(pool, k)
        plan["pool"][str(last_bi)] = [q for q in pool if q not in picked]
        plan["extra"][str(last_bi)] = k
        # қосымша сұрақтар осы кезеңнің соңына қосылады
        idx = max(i for i, x in enumerate(plan["seq"]) if x[0] == last_bi) + 1
        new = [[last_bi, q] for q in picked]
        plan["seq"][idx:idx] = new
        return "extra", new
    return "stop", []


def grade(age: str, plan: dict, items) -> tuple[dict, int]:
    """Жоспардағы сұрақтарға берілген жауаптарды бағалайды. Жауап — нұсқаның мәтіні, null — «Не знаю».
    Нөмір қабылданбайды. Нәтиже: {(блок, сұрақ): дұрыс па}, «білмеймін» саны."""
    allowed = {tuple(x) for x in plan["seq"]}
    blocks = AGE_GROUPS[age]["blocks"]
    graded, dont_know = {}, 0
    for item in (items or [])[:300]:
        try:
            key = (int(item[0]), int(item[1]))
            choice = item[2]
        except (ValueError, TypeError, IndexError):
            continue
        if key not in allowed or key in graded:
            continue
        opts = blocks[key[0]]["questions"][key[1]]["options"]
        if choice is None:
            graded[key] = False
            dont_know += 1
        elif isinstance(choice, str) and choice in opts:
            graded[key] = choice == opts[0]
    return graded, dont_know


def trim(age: str, plan: dict, graded: dict) -> dict:
    """Тест тоқтаған кезеңнен кейінгі жауаптарды есептен шығарады (олар берілмеуі тиіс еді)."""
    keep = set()
    for bi in range(len(AGE_GROUPS[age]["blocks"])):
        keep.add(bi)
        _, _, done = _block(plan, graded, bi)
        if not (done and block_passed(age, plan, graded, bi)):
            break
    return {k: v for k, v in graded.items() if k[0] in keep}


def level_of(age: str, plan: dict, graded: dict) -> str:
    passed = 0
    for bi in range(len(AGE_GROUPS[age]["blocks"])):
        _, _, done = _block(plan, graded, bi)
        if done and block_passed(age, plan, graded, bi):
            passed = bi + 1
        else:
            break
    return all_levels(age)[passed]


def weak_topics(age: str, graded: dict, limit: int = 5) -> tuple[list[str], list[str]]:
    """Қате не «білмеймін» жауап берілген тақырыптар: (орысша — студентке, қазақша — админге)."""
    ru, kz, seen = [], [], set()
    blocks = AGE_GROUPS[age]["blocks"]
    for (bi, qi), ok in sorted(graded.items()):
        if ok:
            continue
        q = blocks[bi]["questions"][qi]
        if "lesson" in q:
            key = ("L", q["lesson"])
            t = ADULT_TOPICS.get(q["lesson"], "")
            r, k = f"урок {q['lesson']} — {t}", f"{q['lesson']}-сабақ — {t}"
        elif "module" in q:
            key = ("M", q["module"])
            kzn, run = SCHOOL_MODULES.get(q["module"], ("", ""))
            r, k = f"модуль {q['module']} «{run}»", f"{q['module']}-модуль «{kzn}»"
        else:
            continue
        if key not in seen:
            seen.add(key)
            ru.append(r)
            kz.append(k)
    return ru[:limit], kz[:limit]

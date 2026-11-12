"""SQLite дерекқоры: студенттер, нәтижелер, топтар."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

# Railway-де DATA_DIR өшпейтін дискіге (Volume) нұсқайды, мысалы /data.
# Компьютерде бос болса — база бот папкасында сақталады.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "students.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def init() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                tg_id       INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                phone       TEXT NOT NULL,
                age_group   TEXT NOT NULL,
                username    TEXT,
                registered  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL REFERENCES students(tg_id),
                age_group   TEXT NOT NULL,
                level       TEXT NOT NULL,
                correct     INTEGER NOT NULL,
                answered    INTEGER NOT NULL,
                dont_know   INTEGER NOT NULL,
                taken_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS groups_ (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                age_group   TEXT NOT NULL,
                level       TEXT NOT NULL,
                number      INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_members (
                group_id    INTEGER NOT NULL REFERENCES groups_(id),
                tg_id       INTEGER NOT NULL UNIQUE REFERENCES students(tg_id)
            );
            """
        )


# ---------------------------------------------------------------- students
def get_student(tg_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM students WHERE tg_id=?", (tg_id,)).fetchone()


def add_student(tg_id: int, name: str, phone: str, age_group: str, username: str | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO students(tg_id, name, phone, age_group, username, registered) "
            "VALUES (?,?,?,?,?,?)",
            (tg_id, name, phone, age_group, username, _now()),
        )


# ---------------------------------------------------------------- results
def save_result(tg_id, age_group, level, correct, answered, dont_know) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO results(tg_id, age_group, level, correct, answered, dont_know, taken_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tg_id, age_group, level, correct, answered, dont_know, _now()),
        )


def get_results(tg_id: int, limit: int = 5):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM results WHERE tg_id=? ORDER BY id DESC LIMIT ?", (tg_id, limit)
        ).fetchall()


# ---------------------------------------------------------------- groups
_POOL_FROM = """
    FROM students s
    JOIN results r ON r.id = (SELECT id FROM results WHERE tg_id = s.tg_id ORDER BY id DESC LIMIT 1)
    WHERE s.tg_id NOT IN (SELECT tg_id FROM group_members)
"""


def pool(age_group: str, level: str):
    """Топқа әлі кірмеген, соңғы нәтижесі осы деңгей болған студенттер (кезек тәртібімен)."""
    with _conn() as c:
        return c.execute(
            "SELECT s.*, r.level AS level, r.taken_at AS taken_at" + _POOL_FROM
            + " AND r.age_group=? AND r.level=? ORDER BY r.id", (age_group, level)
        ).fetchall()


def all_pools():
    """Әр (жас тобы, деңгей) бойынша топқа кезекте тұрғандар саны."""
    with _conn() as c:
        return c.execute(
            "SELECT r.age_group AS age_group, r.level AS level, COUNT(*) AS n" + _POOL_FROM
            + " GROUP BY r.age_group, r.level ORDER BY r.age_group, r.level"
        ).fetchall()


def create_group(age_group: str, level: str, tg_ids: list[int]) -> int:
    with _conn() as c:
        number = c.execute(
            "SELECT COUNT(*) FROM groups_ WHERE age_group=? AND level=?", (age_group, level)
        ).fetchone()[0] + 1
        cur = c.execute(
            "INSERT INTO groups_(age_group, level, number, created_at) VALUES (?,?,?,?)",
            (age_group, level, number, _now()),
        )
        gid = cur.lastrowid
        c.executemany("INSERT INTO group_members(group_id, tg_id) VALUES (?,?)",
                      [(gid, t) for t in tg_ids])
        return gid


def get_group(group_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM groups_ WHERE id=?", (group_id,)).fetchone()


def group_of(tg_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT g.* FROM groups_ g JOIN group_members m ON m.group_id = g.id WHERE m.tg_id=?",
            (tg_id,),
        ).fetchone()


def group_members(group_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT s.* FROM students s JOIN group_members m ON m.tg_id = s.tg_id "
            "WHERE m.group_id=? ORDER BY s.name", (group_id,)
        ).fetchall()


def list_groups():
    with _conn() as c:
        return c.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS n "
            "FROM groups_ g ORDER BY g.id DESC"
        ).fetchall()


# ---------------------------------------------------------------- admin
def stats():
    with _conn() as c:
        return {
            "students": c.execute("SELECT COUNT(*) FROM students").fetchone()[0],
            "tested": c.execute("SELECT COUNT(DISTINCT tg_id) FROM results").fetchone()[0],
            "groups": c.execute("SELECT COUNT(*) FROM groups_").fetchone()[0],
            "by_age": c.execute(
                "SELECT age_group, COUNT(*) AS n FROM students GROUP BY age_group").fetchall(),
        }


def export_rows():
    with _conn() as c:
        return c.execute(
            """
            SELECT s.name, s.phone, s.username, s.age_group, s.registered,
                   r.level, r.correct, r.answered, r.dont_know, r.taken_at,
                   g.number AS group_number, g.level AS group_level
            FROM students s
            LEFT JOIN results r ON r.id = (SELECT id FROM results WHERE tg_id=s.tg_id
                                           ORDER BY id DESC LIMIT 1)
            LEFT JOIN group_members m ON m.tg_id = s.tg_id
            LEFT JOIN groups_ g ON g.id = m.group_id
            ORDER BY s.age_group, r.level, s.name
            """
        ).fetchall()

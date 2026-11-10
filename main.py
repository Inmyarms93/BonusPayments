import sqlite3
import csv
import io
import os
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = "premium.db"
ADMIN_PASSWORD = "gimnazia"  # общий пароль администратора

app = FastAPI(title="Премиальные выплаты")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ----------------------------- БАЗА ДАННЫХ -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'teacher',  -- teacher | admin
        active INTEGER NOT NULL DEFAULT 1,
        note TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS self_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT NOT NULL,
        crit INTEGER NOT NULL,        -- 1..5, 8, 9
        level TEXT,                   -- уровень
        name TEXT,                    -- название достижения/публикации
        qty INTEGER DEFAULT 1,        -- количество (для 5, 8)
        comment TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS self_wide (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT NOT NULL,
        c6 TEXT, c7 TEXT, c10 TEXT, c11 INTEGER, c12 TEXT, c13 TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admin_eval (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT NOT NULL,
        c14 INTEGER DEFAULT 0,  -- 1 = есть нарушение
        c15 INTEGER DEFAULT 0, c16 INTEGER DEFAULT 0, c17 INTEGER DEFAULT 0,
        c18 INTEGER DEFAULT 0, c19 INTEGER DEFAULT 0, c20 INTEGER DEFAULT 0,
        c21 INTEGER DEFAULT 0, c22 INTEGER DEFAULT 0, c23 INTEGER DEFAULT 0,
        c24 INTEGER DEFAULT 0, c25 INTEGER DEFAULT 0,
        c26 TEXT,               -- ниже70 | 70-89 | 90+
        c27 TEXT,               -- нет | 10-15 | >15
        c29 INTEGER DEFAULT 0,
        c30 INTEGER,
        c13_manual INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admin_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT,
        action TEXT,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


init_db()


# ----------------------------- ПРАВИЛА БАЛЛОВ -----------------------------
def points_self_row(crit: int, level: Optional[str], qty: int = 1) -> int:
    """Баллы за построчные критерии самооценки."""
    level = (level or "").lower()
    table = {
        1: {"региональный": 4, "всероссийский": 5},
        2: {"региональный": 10, "всероссийский": 30},
        3: {"школьный": 1, "муниципальный": 2, "региональный": 3},
        4: {"школьный": 1, "муниципальный": 2, "региональный": 3, "всероссийский": 5},
        5: {"": 1},
        8: {"": 1},
        9: {"муниципальный": 2, "региональный": 3, "всероссийский": 5},
    }
    base = table.get(crit, {}).get(level, 0)
    if crit in (5, 8):
        return base * max(qty, 0)
    return base


def points_c11(n: Optional[int]) -> int:
    if n is None:
        return 0
    if n <= 15:
        return 1
    if n <= 35:
        return 2
    if n <= 50:
        return 3
    return 5


def points_admin(row: dict) -> int:
    """Баллы администратора 14-30 (кроме №13 — он ручной)."""
    total = 0
    if row.get("c14"):
        total -= 2
    for k in ("c15", "c16", "c17", "c18", "c19", "c20", "c21", "c22",
              "c23", "c24", "c25", "c29"):
        if row.get(k):
            total += {"c15": 1, "c16": 1, "c17": 1, "c18": 1, "c19": 1,
                      "c20": 1, "c21": 2, "c22": 5, "c23": 5, "c24": 3,
                      "c25": 3, "c29": 1}[k]
    c26 = row.get("c26") or ""
    if c26 == "70-89":
        total += 5
    elif c26 == "90+":
        total += 10
    c27 = row.get("c27") or ""
    if c27 == "10-15":
        total += 1
    elif c27 == ">15":
        total += 2
    total += points_c11(row.get("c30"))
    return total


def teacher_self_total(fio: str, conn) -> int:
    total = 0
    for r in conn.execute("SELECT * FROM self_rows WHERE fio=?", (fio,)):
        total += points_self_row(r["crit"], r["level"], r["qty"])
    w = conn.execute(
        "SELECT * FROM self_wide WHERE fio=? ORDER BY id DESC LIMIT 1", (fio,)
    ).fetchone()
    if w:
        if w["c6"] == "да":
            total += 5
        if w["c7"] == "да":
            total += 5
        total += points_c11(w["c11"])
        if w["c12"] == "да":
            total += 2
    return total

def teacher_self_by_criteria(fio: str, conn) -> dict:
    """Баллы самооценки по каждому критерию 1..12 + описание №13."""
    result = {str(i): 0 for i in range(1, 13)}
    result["c13_desc"] = None

    for r in conn.execute(
        "SELECT * FROM self_rows WHERE fio=? AND substr(created_at,1,7)=?",
        (fio, datetime.now(timezone.utc).strftime("%Y-%m"))
    ):
        crit = str(r["crit"])
        result[crit] = result.get(crit, 0) + points_self_row(
            r["crit"], r["level"], r["qty"]
        )

    w = conn.execute(
        "SELECT * FROM self_wide WHERE fio=? AND substr(created_at,1,7)=? "
        "ORDER BY id DESC LIMIT 1",
        (fio, datetime.now(timezone.utc).strftime("%Y-%m"))
    ).fetchone()
    if w:
        if w["c6"] == "да":
            result["6"] = 5
        if w["c7"] == "да":
            result["7"] = 5
        result["10"] = len((w["c10"] or "").split(",")) if w["c10"] else 0
        result["11"] = points_c11(w["c11"])
        if w["c12"] == "да":
            result["12"] = 2
        result["c13_desc"] = w["c13"]
    return result


def teacher_admin_by_criteria(fio: str, conn) -> dict:
    """Баллы администратора по каждому критерию 14..30 (№13 отдельно)."""
    result = {str(i): 0 for i in range(14, 31)}
    result["c13_manual"] = 0

    a = conn.execute(
        "SELECT * FROM admin_eval WHERE fio=?", (fio,)
    ).fetchone()
    if not a:
        return result

    a = dict(a)
    result["c13_manual"] = a.get("c13_manual") or 0
    if a.get("c14"):
        result["14"] = -2
    result["15"] = 1 if a.get("c15") else 0
    result["16"] = 1 if a.get("c16") else 0
    result["17"] = 1 if a.get("c17") else 0
    result["18"] = 1 if a.get("c18") else 0
    result["19"] = 1 if a.get("c19") else 0
    result["20"] = 1 if a.get("c20") else 0
    result["21"] = 2 if a.get("c21") else 0
    result["22"] = 5 if a.get("c22") else 0
    result["23"] = 5 if a.get("c23") else 0
    result["24"] = 3 if a.get("c24") else 0
    result["25"] = 3 if a.get("c25") else 0
    c26 = a.get("c26") or ""
    result["26"] = 5 if c26 == "70-89" else 10 if c26 == "90+" else 0
    c27 = a.get("c27") or ""
    result["27"] = 1 if c27 == "10-15" else 2 if c27 == ">15" else 0
    result["29"] = 1 if a.get("c29") else 0
    result["30"] = points_c11(a.get("c30"))
    return result

# ----------------------------- МОДЕЛИ -----------------------------
class TeacherIn(BaseModel):
    fio: str
    status: str = "teacher"
    active: int = 1
    note: str = ""


class SelfRowIn(BaseModel):
    fio: str
    crit: int
    level: Optional[str] = None
    name: Optional[str] = None
    qty: int = 1
    comment: Optional[str] = None


class SelfWideIn(BaseModel):
    fio: str
    c6: str
    c7: str
    c10: List[str] = []
    c11: Optional[int] = None
    c12: str
    c13: Optional[str] = None


class AdminEvalIn(BaseModel):
    fio: str
    c14: int = 0
    c15: int = 0; c16: int = 0; c17: int = 0; c18: int = 0; c19: int = 0
    c20: int = 0; c21: int = 0; c22: int = 0; c23: int = 0; c24: int = 0
    c25: int = 0; c29: int = 0
    c26: Optional[str] = None
    c27: Optional[str] = None
    c30: Optional[int] = None
    c13_manual: int = 0


# ----------------------------- ЗАВИСИМОСТИ -----------------------------
def require_admin(x_admin_password: Optional[str] = Header(None)):
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(403, "Неверный пароль администратора")
    return True


# ----------------------------- СТРАНИЦЫ -----------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/static/index.html")


# ----------------------------- API: СПРАВОЧНИК -----------------------------
@app.get("/api/teachers")
def get_teachers(active_only: bool = True):
    conn = db()
    q = "SELECT * FROM teachers"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY fio"
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    return rows


@app.post("/api/teachers")
def add_teacher(t: TeacherIn, _=Depends(require_admin)):
    conn = db()
    try:
        conn.execute(
            "INSERT INTO teachers (fio, status, active, note) VALUES (?,?,?,?)",
            (t.fio.strip(), t.status, t.active, t.note),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Учитель с таким ФИО уже есть")
    conn.close()
    return {"ok": True}


@app.delete("/api/teachers/{fio}")
def del_teacher(fio: str, _=Depends(require_admin)):
    conn = db()
    conn.execute("DELETE FROM teachers WHERE fio=?", (fio,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ----------------------------- API: САМООЦЕНКА -----------------------------
class SelfRowsIn(BaseModel):
    fio: str
    rows: List[SelfRowIn]

@app.delete("/api/self/{fio}/current-month")
def clear_self_month(fio: str):
    conn = db()
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    conn.execute(
        "DELETE FROM self_rows WHERE fio=? AND substr(created_at,1,7)=?",
        (fio, month_prefix),
    )
    conn.execute(
        "DELETE FROM self_wide WHERE fio=? AND substr(created_at,1,7)=?",
        (fio, month_prefix),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/self/rows")
def add_self_rows(payload: SelfRowsIn):
    conn = db()
    created_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    for r in payload.rows:
        if r.name != "" and r.name is not None and r.name!='':
            conn.execute(
                "INSERT INTO self_rows (fio, crit, level, name, qty, comment, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (payload.fio, r.crit, r.level, r.name, r.qty, r.comment, created_at),
            )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/self/wide")
def add_self_wide(w: SelfWideIn):
    conn = db()

    conn.execute(
        "INSERT INTO self_wide (fio, c6, c7, c10, c11, c12, c13, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (w.fio, w.c6, w.c7, ",".join(w.c10), w.c11, w.c12, w.c13,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/self/{fio}")
def get_self(fio: str):
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM self_rows WHERE fio=?", (fio,))]
    wide = conn.execute(
        "SELECT * FROM self_wide WHERE fio=? ORDER BY id DESC LIMIT 1", (fio,)
    ).fetchone()
    conn.close()
    return {"rows": rows, "wide": dict(wide) if wide else None}


# ----------------------------- API: АДМИНИСТРАТОР -----------------------------
@app.post("/api/admin/eval")
def save_admin_eval(e: AdminEvalIn, _=Depends(require_admin)):
    conn = db()
    conn.execute("DELETE FROM admin_eval WHERE fio=?", (e.fio,))
    conn.execute("""
        INSERT INTO admin_eval
        (fio, c14, c15, c16, c17, c18, c19, c20, c21, c22, c23, c24, c25,
         c26, c27, c29, c30, c13_manual, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (e.fio, e.c14, e.c15, e.c16, e.c17, e.c18, e.c19, e.c20, e.c21,
          e.c22, e.c23, e.c24, e.c25, e.c26, e.c27, e.c29, e.c30,
          e.c13_manual, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/admin/evals")
def get_admin_evals(_=Depends(require_admin)):
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM admin_eval ORDER BY fio")]
    conn.close()
    return rows


# ----------------------------- API: СВОДКА -----------------------------
@app.get("/api/summary")
def summary(_=Depends(require_admin)):
    conn = db()
    teachers = [dict(r) for r in conn.execute(
        "SELECT * FROM teachers WHERE active=1 AND status='teacher' ORDER BY fio")]
    out = []
    for t in teachers:
        fio = t["fio"]
        self_scores = teacher_self_by_criteria(fio, conn)
        admin_scores = teacher_admin_by_criteria(fio, conn)

        self_total = sum(v for k, v in self_scores.items()
                         if k not in ("c13_desc",))
        admin_total = sum(v for k, v in admin_scores.items()
                          if k != "c13_manual")
        c13 = admin_scores["c13_manual"]
        total = self_total + c13 + admin_total

        has_self = (conn.execute(
            "SELECT COUNT(*) c FROM self_rows WHERE fio=?", (fio,)
        ).fetchone()["c"] > 0) or (conn.execute(
            "SELECT COUNT(*) c FROM self_wide WHERE fio=?", (fio,)
        ).fetchone()["c"] > 0)
        has_admin = conn.execute(
            "SELECT COUNT(*) c FROM admin_eval WHERE fio=?", (fio,)
        ).fetchone()["c"] > 0

        if not has_self:
            status = "нет данных"
        elif not has_admin:
            status = "ожидает оценку администратора"
        else:
            status = "готово"

        out.append({
            "fio": fio,
            "self": self_scores,          # критерии 1–12 + c13_desc
            "admin": admin_scores,        # критерии 14–30 + c13_manual
            "self_total": self_total,
            "c13": c13,
            "admin_total": admin_total,
            "total": total,
            "status": status,
        })
    conn.close()
    return out


@app.get("/api/summary/export")
def export_csv(_=Depends(require_admin)):
    data = summary()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    header = ["ФИО"] + [f"Крит.{i}" for i in range(1, 31)] + ["№13", "Итог", "Статус"]
    writer.writerow(header)
    for r in data:
        row = [r["fio"]]
        for i in range(1, 13):
            row.append(r["self"].get(str(i), 0))
        row.append(r["c13"])
        for i in range(14, 31):
            row.append(r["admin"].get(str(i), 0))
        row.append(r["total"])
        row.append(r["status"])
        writer.writerow(row)

    content = "\ufeff" + buf.getvalue()          # <-- BOM для Excel
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=summary.csv"})


# ----------------------------- API: ПРАВИЛА -----------------------------
@app.get("/api/rules")
def rules():
    return {
        "self": {
            "1": {"региональный": 4, "всероссийский": 5},
            "2": {"региональный": 10, "всероссийский": 30},
            "3": {"школьный": 1, "муниципальный": 2, "региональный": 3},
            "4": {"школьный": 1, "муниципальный": 2, "региональный": 3,
                  "всероссийский": 5},
            "5": "1 за каждую публикацию",
            "6": {"да": 5, "нет": 0},
            "7": {"да": 5, "нет": 0},
            "8": "1 за каждое мероприятие",
            "9": {"муниципальный": 2, "региональный": 3, "всероссийский": 5},
            "10": "по 1 за каждый пункт",
            "11": {"до15": 1, "16-35": 2, "36-50": 3, ">50": 5},
            "12": {"да": 2, "нет": 0},
            "13": "до 50, вручную администратором",
        },
        "admin": {
            "14": -2, "15": 1, "16": 1, "17": 1, "18": 1, "19": 1, "20": 1,
            "21": 2, "22": 5, "23": 5, "24": 3, "25": 3,
            "26": {"<70": 0, "70-89": 5, "90+": 10},
            "27/28": {"нет": 0, "10-15": 1, ">15": 2},
            "29": 1, "30": {"до15": 1, "16-35": 2, "36-50": 3, ">50": 5},
            "13": "0-50, вручную",
        },
    }

@app.get("/api/teachers")
def get_teachers(active_only: bool = True):
    conn = db()
    q = "SELECT * FROM teachers"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY fio"
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    return rows

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

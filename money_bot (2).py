"""Телеграм-бот «Мои деньги»: карты, расходы/доходы, долги, доходность по карте.

Запуск:
    pip install "python-telegram-bot>=21"
    export BOT_TOKEN="токен от @BotFather"     (Windows: set BOT_TOKEN=...)
    python money_bot.py
Данные каждого пользователя хранятся отдельно в файле money.db рядом с ботом.
"""
import os
import re
import sqlite3
import datetime as dt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

TOKEN = os.environ["BOT_TOKEN"]
db = sqlite3.connect("money.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY, uid INT, name TEXT, start REAL, rate REAL, cash REAL);
CREATE TABLE IF NOT EXISTS tx(id INTEGER PRIMARY KEY, uid INT, card INT, type TEXT, amt REAL, cat TEXT, ts TEXT, debt INT);
CREATE TABLE IF NOT EXISTS debts(id INTEGER PRIMARY KEY, uid INT, who TEXT, amt REAL, card INT, ts TEXT, back INT DEFAULT 0);
""")

HELP = """Я веду учёт денег по вашим картам.

Карты
/addcard Humo 1500000 [процент_годовых] [кэшбэк_%] — добавить карту (название одним словом, потом остаток)
/cards — баланс, расходы за месяц, доход и доходность по каждой карте

Операции
/spend 50000 humo еда — расход
/income 3000000 humo зарплата — доход
/history [карта] — последние операции
/del номер — удалить операцию

Долги
/debt Алишер 200000 [карта] — записать, что дали в долг (с карты или наличными)
/debts — кому и сколько должны
/back номер — долг вернули

Можно просто переслать мне СМС от банка: найду сумму и запишу операцию сам."""


def run(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


def q(sql, *a):
    return run(sql, *a).fetchall()


def fmt(n):
    return f"{round(n):,}".replace(",", " ") + " сум"


def today():
    return dt.date.today().isoformat()


def num(s):
    s = s.replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        other = "." if dec == "," else ","
        s = s.replace(other, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        s = parts[0] + "." + parts[1] if len(parts) == 2 and len(parts[1]) <= 2 else "".join(parts)
    return float(s)


def balance(c):
    r = q("SELECT COALESCE(SUM(CASE WHEN type='in' THEN amt ELSE -amt END),0) FROM tx WHERE card=?", c[0])
    return c[3] + r[0][0]


def stats(c):
    rows = q("SELECT type, SUM(amt) FROM tx WHERE card=? AND debt IS NULL AND substr(ts,1,7)=? GROUP BY type",
             c[0], today()[:7])
    d = dict(rows)
    out, inc = d.get("out", 0), d.get("in", 0)
    b = balance(c)
    cashback = out * c[5] / 100
    interest = max(b, 0) * c[4] / 100 / 12
    total = cashback + interest
    yearly = total * 12 / b * 100 if b > 0 else 0
    return out, inc, cashback, interest, total, yearly


def find_card(uid, name):
    r = q("SELECT * FROM cards WHERE uid=? AND lower(name) LIKE ?", uid, name.lower() + "%")
    return r[0] if r else None


def default_card(uid):
    r = q("SELECT * FROM cards WHERE uid=?", uid)
    return r[0] if len(r) == 1 else None


def card_name(cid):
    r = q("SELECT name FROM cards WHERE id=?", cid)
    return r[0][0] if r else "наличные"


def record(uid, card, kind, amt, cat, debt=None):
    return run("INSERT INTO tx(uid,card,type,amt,cat,ts,debt) VALUES(?,?,?,?,?,?,?)",
               uid, card, kind, amt, cat, today(), debt).lastrowid


async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(HELP)


async def addcard(u: Update, c: ContextTypes.DEFAULT_TYPE):
    a = c.args
    try:
        name, start_bal = a[0], num(a[1])
        rate = num(a[2]) if len(a) > 2 else 0
        cash = num(a[3]) if len(a) > 3 else 0
    except Exception:
        return await u.message.reply_text("Пример: /addcard Humo 1500000 12 1\n(название, остаток, % годовых на остаток, % кэшбэка)")
    run("INSERT INTO cards(uid,name,start,rate,cash) VALUES(?,?,?,?,?)", u.effective_user.id, name, start_bal, rate, cash)
    await u.message.reply_text(f"Карта {name} добавлена. Остаток {fmt(start_bal)}.")


async def cards(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = q("SELECT * FROM cards WHERE uid=?", u.effective_user.id)
    if not rows:
        return await u.message.reply_text("Карт пока нет. Добавьте: /addcard Humo 1500000")
    total, parts = 0, []
    for r in rows:
        b = balance(r)
        total += b
        out, inc, cb, pr, tot, y = stats(r)
        parts.append(f"💳 {r[2]}: {fmt(b)}\n"
                     f"   Расходы за месяц: {fmt(out)}\n   Доходы за месяц: {fmt(inc)}\n"
                     f"   Кэшбэк ({r[5]:g}%): {fmt(cb)}\n   Проценты ({r[4]:g}%): {fmt(pr)}\n"
                     f"   Доход с карты за месяц: {fmt(tot)}\n   Доходность в год: {y:.1f}%")
    await u.message.reply_text("\n\n".join(parts) + f"\n\nВсего на картах: {fmt(total)}")


def op_cmd(kind):
    async def h(u: Update, c: ContextTypes.DEFAULT_TYPE):
        uid, a = u.effective_user.id, c.args
        try:
            amt = num(a[0])
        except Exception:
            return await u.message.reply_text("Пример: /spend 50000 humo еда")
        rest = a[1:]
        card = find_card(uid, rest[0]) if rest else None
        if card:
            rest = rest[1:]
        else:
            card = default_card(uid)
        if not card:
            return await u.message.reply_text("Не понял, с какой карты. Укажите её название или добавьте карту: /addcard")
        rid = record(uid, card[0], kind, amt, " ".join(rest), None)
        sign = "Расход" if kind == "out" else "Доход"
        await u.message.reply_text(f"{sign} {fmt(amt)} записан (#{rid}). Баланс {card[2]}: {fmt(balance(card))}.\nОтменить: /del {rid}")
    return h


async def history(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    card = find_card(uid, c.args[0]) if c.args else None
    sql = "SELECT id,card,type,amt,cat,ts FROM tx WHERE uid=?" + (" AND card=?" if card else "") + " ORDER BY id DESC LIMIT 10"
    rows = q(sql, uid, *([card[0]] if card else []))
    if not rows:
        return await u.message.reply_text("Операций пока нет.")
    await u.message.reply_text("\n".join(
        f"#{r[0]} {r[5]} {'+' if r[2] == 'in' else '−'}{fmt(r[3])} · {card_name(r[1])} · {r[4] or '—'}" for r in rows))


async def delete(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        i = int(c.args[0])
    except Exception:
        return await u.message.reply_text("Пример: /del 12")
    n = run("DELETE FROM tx WHERE id=? AND uid=? AND debt IS NULL", i, u.effective_user.id).rowcount
    await u.message.reply_text("Удалено." if n else "Такой операции нет (операции по долгам удаляются вместе с долгом).")


async def debt(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid, a = u.effective_user.id, c.args
    try:
        who, amt = a[0], num(a[1])
    except Exception:
        return await u.message.reply_text("Пример: /debt Алишер 200000 humo\n(без карты — значит наличными)")
    card = None
    if len(a) > 2:
        card = find_card(uid, a[2])
        if not card:
            return await u.message.reply_text("Такой карты нет. Список: /cards")
    did = run("INSERT INTO debts(uid,who,amt,card,ts) VALUES(?,?,?,?,?)", uid, who, amt, card[0] if card else None, today()).lastrowid
    if card:
        record(uid, card[0], "out", amt, f"Долг: {who}", did)
    await u.message.reply_text(f"Записал: {who} должен {fmt(amt)} (#{did}). Когда вернёт: /back {did}")


async def debts(u: Update, c: ContextTypes.DEFAULT_TYPE):
    rows = q("SELECT id,who,amt,ts FROM debts WHERE uid=? AND back=0 ORDER BY id", u.effective_user.id)
    if not rows:
        return await u.message.reply_text("Вам никто не должен.")
    await u.message.reply_text("\n".join(f"#{r[0]} {r[1]} — {fmt(r[2])} (с {r[3]})" for r in rows)
                               + f"\n\nВсего должны: {fmt(sum(r[2] for r in rows))}\nОтметить возврат: /back номер")


async def back(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    try:
        d = q("SELECT id,who,amt,card FROM debts WHERE id=? AND uid=? AND back=0", int(c.args[0]), uid)[0]
    except Exception:
        return await u.message.reply_text("Пример: /back 3 (номера в /debts)")
    run("UPDATE debts SET back=1 WHERE id=?", d[0])
    if d[3]:
        record(uid, d[3], "in", d[2], f"Возврат долга: {d[1]}", d[0])
    await u.message.reply_text(f"{d[1]} вернул {fmt(d[2])}." + (" Деньги вернулись на карту." if d[3] else ""))


# ---------- разбор СМС от банка ----------
NUM = r"\d[\d\s.,]*\d|\d"
CUR = r"(?:uzs|сум\w*|sum|so[’'`]?m)"
OUT_W = ("списан", "покупк", "оплат", "снят", "pokupka", "spisanie", "oplata", "purchase", "payment", "debit", "chiqim", "xarid")
IN_W = ("пополн", "зачисл", "поступл", "popolnenie", "kirim", "credit", "зарплат", "возврат")


def parse_sms(text):
    m = re.search(f"({NUM})\\s*{CUR}", text, re.I) or re.search(f"{CUR}\\s*:?\\s*({NUM})", text, re.I)
    if not m:
        return None
    t = text.lower()
    o, i = any(w in t for w in OUT_W), any(w in t for w in IN_W)
    kind = "out" if o and not i else "in" if i and not o else None
    return num(m.group(1)), kind


async def proceed(u: Update, c: ContextTypes.DEFAULT_TYPE):
    p, uid, m = c.user_data.get("p"), u.effective_user.id, u.effective_message
    if not p:
        return
    if not p["kind"]:
        return await m.reply_text(f"Сумма {fmt(p['amt'])}. Это:", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Расход", callback_data="k:out"), InlineKeyboardButton("Доход", callback_data="k:in")]]))
    if not p["card"]:
        cs = q("SELECT * FROM cards WHERE uid=?", uid)
        if not cs:
            c.user_data.pop("p")
            return await m.reply_text("Сначала добавьте карту: /addcard Humo 1500000")
        if len(cs) == 1:
            p["card"] = cs[0][0]
        else:
            return await m.reply_text("С какой карты?", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(x[2], callback_data=f"c:{x[0]}")] for x in cs]))
    card = q("SELECT * FROM cards WHERE id=?", p["card"])[0]
    rid = record(uid, card[0], p["kind"], p["amt"], p["cat"])
    c.user_data.pop("p")
    sign = "Расход" if p["kind"] == "out" else "Доход"
    await m.reply_text(f"{sign} {fmt(p['amt'])} по карте {card[2]} записан (#{rid}). Баланс: {fmt(balance(card))}.\nОтменить: /del {rid}")


async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    text, uid = u.message.text, u.effective_user.id
    r = parse_sms(text)
    if not r:
        return await u.message.reply_text("Не нашёл сумму. Перешлите СМС от банка или используйте команды: /start")
    card = next((x for x in q("SELECT * FROM cards WHERE uid=?", uid) if x[2].lower() in text.lower()), None)
    c.user_data["p"] = {"amt": r[0], "kind": r[1], "card": card[0] if card else None, "cat": "из СМС"}
    await proceed(u, c)


async def on_button(u: Update, c: ContextTypes.DEFAULT_TYPE):
    qq = u.callback_query
    await qq.answer()
    p = c.user_data.get("p")
    if not p:
        return
    k, v = qq.data.split(":")
    if k == "k":
        p["kind"] = v
    elif k == "c" and q("SELECT 1 FROM cards WHERE id=? AND uid=?", int(v), u.effective_user.id):
        p["card"] = int(v)
    await proceed(u, c)


def main():
    app = Application.builder().token(TOKEN).build()
    for name, fn in [("start", start), ("help", start), ("addcard", addcard), ("cards", cards),
                     ("spend", op_cmd("out")), ("income", op_cmd("in")), ("history", history),
                     ("del", delete), ("debt", debt), ("debts", debts), ("back", back)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Lucero ORB + FVG - motor de señales.

Réplica fiel de la lógica de tu indicador de TradingView (ORB, sesgo, FVG,
modos de retesteo, SL/TP). Descarga velas de 1 minuto, simula la sesión,
guarda docs/data/state.json (lo lee el dashboard) y avisa a Telegram.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

# =====================================================================
# CONFIGURACIÓN: mismos valores que los inputs de tu indicador
# =====================================================================
TZ_OFFSET_HOURS = 2            # "UTC+2"
SESSION_START = (9, 0)         # 09:00
SESSION_END = (12, 30)         # 12:30
ORB_LEN_BARS = 5               # Duración del ORB (velas de 1m)
RETEST_MODE = 2                # 1 = Estricto, 2 = Sucesivo/Libre, 3 = Nuevo Extremo
MAX_EXPANSION = 3.0            # Filtro de expansión máxima (x rango ORB)
SL_OPTION = "Borde FVG"        # "Borde FVG" | "Vela de Entrada" | "Rango ORB"
RR_RATIO = 3.0                 # Ratio R:R
SL_BUFFER = 0.0                # Margen extra del SL (puntos)

SYMBOLS = {
    "MGC": {"yahoo": "MGC=F", "name": "MGC1! · Micro Gold"},
    "MNQ": {"yahoo": "MNQ=F", "name": "MNQZ2026 · Micro Nasdaq"},
}

SESSIONS_TO_KEEP = 4           # sesión actual + historial
NOTIFY_MAX_AGE_MIN = 45        # solo avisa de eventos de los últimos N minutos
STATE_PATH = os.path.join("docs", "data", "state.json")
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))


# =====================================================================
# SIMULACIÓN DE UNA SESIÓN (vela a vela, igual que el Pine Script)
# =====================================================================
def _sl_tp(buy, f, b, orb_high, orb_low, entry):
    if buy:
        if SL_OPTION == "Borde FVG":
            sl = f["bot"] - SL_BUFFER
        elif SL_OPTION == "Vela de Entrada":
            sl = b["l"] - SL_BUFFER
        else:
            sl = orb_low - SL_BUFFER
        tp = entry + abs(entry - sl) * RR_RATIO
    else:
        if SL_OPTION == "Borde FVG":
            sl = f["top"] + SL_BUFFER
        elif SL_OPTION == "Vela de Entrada":
            sl = b["h"] + SL_BUFFER
        else:
            sl = orb_high + SL_BUFFER
        tp = entry - abs(sl - entry) * RR_RATIO
    return sl, tp


def simulate_session(bars):
    """bars: lista de dicts {t,o,h,l,c} SOLO de la ventana de sesión, en orden."""
    S = {"bias": 0, "in_trade": False, "finished": False,
         "entry": None, "sl": None, "tp": None, "active": None, "entry_ev": None}
    fvgs, events = [], []
    orb_high = orb_low = None
    prev_bias = 0

    def invalidate(idx):
        fvgs[idx]["invalid"] = True
        del fvgs[idx]

    def enter(f, idx, buy, b, i):
        f["confirmed"] = True
        S["in_trade"] = True
        S["active"] = idx
        S["entry"] = b["c"]
        S["sl"], S["tp"] = _sl_tp(buy, f, b, orb_high, orb_low, b["c"])
        events.append({"type": "ENTRY", "side": "BUY" if buy else "SELL", "t": b["t"],
                       "entry": S["entry"], "sl": S["sl"], "tp": S["tp"],
                       "rr": RR_RATIO, "result": "OPEN"})
        S["entry_ev"] = len(events) - 1

    for i, b in enumerate(bars):
        n = i + 1
        h, l, c = b["h"], b["l"], b["c"]

        # ---- ORB ----
        if n == 1:
            orb_high, orb_low = h, l
        elif n <= ORB_LEN_BARS:
            orb_high, orb_low = max(orb_high, h), min(orb_low, l)
        orb_ready = n > ORB_LEN_BARS

        # ---- Sesgo (cambio dinámico por ruptura del ORB) ----
        if orb_ready and not S["finished"]:
            if c > orb_high and S["bias"] != 1:
                S["bias"] = 1
                fvgs[:] = [f for f in fvgs if not ((not f["bull"]) and (not f["confirmed"]))]
            elif c < orb_low and S["bias"] != -1:
                S["bias"] = -1
                fvgs[:] = [f for f in fvgs if not (f["bull"] and (not f["confirmed"]))]

        # ---- ALARMA 1: cierre fuera del ORB ----
        if S["bias"] != 0 and S["bias"] != prev_bias:
            events.append({"type": "ORB_BREAK", "side": "UP" if S["bias"] == 1 else "DOWN",
                           "t": b["t"], "price": c, "orb_high": orb_high, "orb_low": orb_low})
        prev_bias = S["bias"]

        # ---- Creación de FVG ----
        can_create = orb_ready and (not S["in_trade"]) and (not S["finished"])
        if can_create and i >= 2:
            h2, l2 = bars[i - 2]["h"], bars[i - 2]["l"]
            is_bull = l > h2
            is_bear = h < l2
            orb_range = orb_high - orb_low
            exceeded = ((S["bias"] == 1 and (h - orb_high) > MAX_EXPANSION * orb_range) or
                        (S["bias"] == -1 and (orb_low - l) > MAX_EXPANSION * orb_range))
            if is_bull and S["bias"] == 1 and not exceeded:
                fvgs.append({"top": l, "bot": h2, "bull": True, "created": i, "t_created": b["t"],
                             "touched": False, "rbar": None, "rhigh": None, "rlow": float("nan"),
                             "invalid": False, "confirmed": False})
            if is_bear and S["bias"] == -1 and not exceeded:
                fvgs.append({"top": l2, "bot": h, "bull": False, "created": i, "t_created": b["t"],
                             "touched": False, "rbar": None, "rhigh": None, "rlow": float("nan"),
                             "invalid": False, "confirmed": False})

        # ---- Evaluación de FVG y entradas ----
        if fvgs and (not S["in_trade"]) and (not S["finished"]):
            for idx in range(len(fvgs) - 1, -1, -1):
                f = fvgs[idx]
                if f["invalid"] or f["confirmed"] or not (i > f["created"]):
                    continue
                physical = l <= f["top"] and h >= f["bot"]

                # ---------- COMPRAS ----------
                if f["bull"] and S["bias"] == 1:
                    if RETEST_MODE == 1:
                        if physical and not f["touched"]:
                            if c < f["bot"]:
                                invalidate(idx)
                                continue
                            f["touched"], f["rbar"], f["rhigh"] = True, i, h
                        if f["touched"] and i > f["rbar"]:
                            if c > f["rhigh"]:
                                enter(f, idx, True, b, i)
                                break
                            else:
                                invalidate(idx)
                    elif RETEST_MODE == 2:
                        if f["touched"] and i > f["rbar"] and c > f["rhigh"]:
                            enter(f, idx, True, b, i)
                            break
                        elif physical:
                            if c < f["bot"]:
                                invalidate(idx)
                                continue
                            f["touched"], f["rbar"], f["rhigh"] = True, i, h
                    else:  # Modo 3
                        if f["touched"] and i > f["rbar"] and c > f["rhigh"]:
                            enter(f, idx, True, b, i)
                            break
                        elif physical:
                            if c < f["bot"]:
                                invalidate(idx)
                                continue
                            if (not f["touched"]) or l <= f["rlow"]:
                                f["touched"], f["rbar"] = True, i
                                f["rhigh"], f["rlow"] = h, l

                # ---------- VENTAS ----------
                elif (not f["bull"]) and S["bias"] == -1:
                    if RETEST_MODE == 1:
                        if physical and not f["touched"]:
                            if c > f["top"]:
                                invalidate(idx)
                                continue
                            f["touched"], f["rbar"], f["rlow"] = True, i, l
                        if f["touched"] and i > f["rbar"]:
                            if c < f["rlow"]:
                                enter(f, idx, False, b, i)
                                break
                            else:
                                invalidate(idx)
                    elif RETEST_MODE == 2:
                        if f["touched"] and i > f["rbar"] and c < f["rlow"]:
                            enter(f, idx, False, b, i)
                            break
                        elif physical:
                            if c > f["top"]:
                                invalidate(idx)
                                continue
                            f["touched"], f["rbar"], f["rlow"] = True, i, l
                    else:  # Modo 3
                        if f["touched"] and i > f["rbar"] and c < f["rlow"]:
                            enter(f, idx, False, b, i)
                            break
                        elif physical:
                            if c > f["top"]:
                                invalidate(idx)
                                continue
                            if (not f["touched"]) or h >= f["rhigh"]:
                                f["touched"], f["rbar"] = True, i
                                f["rlow"], f["rhigh"] = l, h

        # ---- Control TP / SL (igual que el indicador: usa el sesgo actual) ----
        if S["in_trade"]:
            def close_trade(kind):
                events.append({"type": kind, "t": b["t"], "price": S["tp"] if kind == "TP" else S["sl"]})
                if S["entry_ev"] is not None:
                    events[S["entry_ev"]]["result"] = kind
            if S["bias"] == 1:
                if h >= S["tp"]:
                    S["in_trade"], S["finished"] = False, True
                    close_trade("TP")
                elif l <= S["sl"]:
                    S["in_trade"] = False
                    close_trade("SL")
                    if S["active"] is not None and S["active"] < len(fvgs):
                        del fvgs[S["active"]]
                    S["active"] = None
            elif S["bias"] == -1:
                if l <= S["tp"]:
                    S["in_trade"], S["finished"] = False, True
                    close_trade("TP")
                elif h >= S["sl"]:
                    S["in_trade"] = False
                    close_trade("SL")
                    if S["active"] is not None and S["active"] < len(fvgs):
                        del fvgs[S["active"]]
                    S["active"] = None

    n_bars = len(bars)
    active_fvgs = [{"top": f["top"], "bot": f["bot"], "bull": f["bull"],
                    "t": f["t_created"], "touched": f["touched"]}
                   for f in fvgs if not f["invalid"] and not f["confirmed"]]
    return {
        "bias": S["bias"], "orb_high": orb_high, "orb_low": orb_low,
        "orb_ready": n_bars > ORB_LEN_BARS, "in_trade": S["in_trade"],
        "finished": S["finished"], "fvgs": active_fvgs, "events": events,
    }


# =====================================================================
# DATOS
# =====================================================================
def fetch_bars(yahoo_symbol):
    import yfinance as yf  # import tardío para poder probar el motor sin red
    df = yf.Ticker(yahoo_symbol).history(period="5d", interval="1m", prepost=True, auto_adjust=False)
    now = datetime.now(timezone.utc).timestamp()
    bars = []
    for ts, row in df.iterrows():
        if row[["Open", "High", "Low", "Close"]].isna().any():
            continue
        t = int(ts.timestamp())
        if t + 60 > now:          # descarta la vela que aún se está formando
            continue
        bars.append({"t": t, "o": float(row["Open"]), "h": float(row["High"]),
                     "l": float(row["Low"]), "c": float(row["Close"])})
    bars.sort(key=lambda x: x["t"])
    return bars


def split_sessions(bars):
    """Agrupa las velas dentro de la ventana de sesión, por día local."""
    s_min = SESSION_START[0] * 60 + SESSION_START[1]
    e_min = SESSION_END[0] * 60 + SESSION_END[1]
    days = {}
    for b in bars:
        loc = datetime.fromtimestamp(b["t"], tz=LOCAL_TZ)
        m = loc.hour * 60 + loc.minute
        if s_min <= m < e_min:
            days.setdefault(loc.date().isoformat(), []).append(b)
    keys = sorted(days.keys())[-SESSIONS_TO_KEEP:]
    return [(k, days[k]) for k in keys]


# =====================================================================
# TELEGRAM
# =====================================================================
def _hhmm(t):
    return datetime.fromtimestamp(t, tz=LOCAL_TZ).strftime("%H:%M")


def build_message(code, ev, now_ts):
    delay = max(0, int((now_ts - ev["t"]) / 60))
    foot = (f"\n\n⏱ Vela de las {_hhmm(ev['t'])} (UTC+{TZ_OFFSET_HOURS}). "
            f"Aviso ~{delay} min después: datos con retraso.")
    if ev["type"] == "ORB_BREAK":
        up = ev["side"] == "UP"
        return (f"{'🔔⬆️' if up else '🔔⬇️'} <b>ALARMA 1: RUPTURA DEL ORB</b>\n"
                f"<b>{code}</b> (1m)\n"
                f"La vela cerró {'POR ENCIMA' if up else 'POR DEBAJO'} del ORB "
                f"(sesgo {'ALCISTA' if up else 'BAJISTA'}).\n\n"
                f"📍 Cierre: <code>{ev['price']:.2f}</code>\n"
                f"🔼 ORB High: <code>{ev['orb_high']:.2f}</code>\n"
                f"🔽 ORB Low: <code>{ev['orb_low']:.2f}</code>\n\n"
                f"👀 Atento: espera el FVG y el retest para la entrada." + foot)
    buy = ev["side"] == "BUY"
    return (f"{'🟢' if buy else '🔴'} <b>ALARMA 2: SEÑAL DE ENTRADA</b>\n"
            f"<b>{'COMPRA' if buy else 'VENTA'}</b> {code} (1m)\n\n"
            f"📍 Entrada (cierre envolvente): <code>{ev['entry']:.2f}</code>\n"
            f"🛑 Stop Loss: <code>{ev['sl']:.2f}</code>\n"
            f"🎯 Take Profit: <code>{ev['tp']:.2f}</code>\n"
            f"⚖️ R:R = 1:{ev['rr']}" + foot)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("Telegram no configurado: se omite el aviso.")
        return False
    data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:  # noqa
        print("Error enviando a Telegram:", e)
        return False


# =====================================================================
# PRINCIPAL
# =====================================================================
def status_text(res, session_closed):
    if res["finished"]:
        return "Sesión finalizada: TP alcanzado"
    if res["in_trade"]:
        last = [e for e in res["events"] if e["type"] == "ENTRY"][-1]
        return "Operación abierta: " + ("COMPRA" if last["side"] == "BUY" else "VENTA")
    if session_closed:
        return "Sesión cerrada"
    if not res["orb_ready"]:
        return "Formando el ORB"
    if res["bias"] == 0:
        return "ORB listo: esperando ruptura"
    side = "alcista" if res["bias"] == 1 else "bajista"
    return f"Sesgo {side}: esperando FVG y retest"


def main():
    now_ts = datetime.now(timezone.utc).timestamp()
    now_local = datetime.fromtimestamp(now_ts, tz=LOCAL_TZ)

    prev = {}
    if os.path.exists(STATE_PATH):
        try:
            prev = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            prev = {}
    sent = set(prev.get("sent", []))

    out = {"updated": int(now_ts), "tz_offset": TZ_OFFSET_HOURS,
           "session": f"{SESSION_START[0]:02d}:{SESSION_START[1]:02d}-{SESSION_END[0]:02d}:{SESSION_END[1]:02d}",
           "config": {"retest_mode": RETEST_MODE, "sl_option": SL_OPTION, "rr": RR_RATIO,
                      "orb_bars": ORB_LEN_BARS},
           "symbols": {}}

    for code, cfg in SYMBOLS.items():
        try:
            bars = fetch_bars(cfg["yahoo"])
            sessions = split_sessions(bars)
            if not sessions:
                raise RuntimeError("Sin velas en la ventana de sesión")
        except Exception as e:  # noqa
            print(f"[{code}] error: {e}")
            old = prev.get("symbols", {}).get(code, {"name": cfg["name"]})
            old["error"] = str(e)
            out["symbols"][code] = old
            continue

        results = [(d, bs, simulate_session(bs)) for d, bs in sessions]
        day, day_bars, res = results[-1]
        session_closed = (day != now_local.date().isoformat()) or \
                         (now_local.hour * 60 + now_local.minute >= SESSION_END[0] * 60 + SESSION_END[1])
        last_bar = bars[-1]

        # --- avisos de Telegram (solo sesión actual, sin duplicados) ---
        for ev in res["events"]:
            if ev["type"] not in ("ORB_BREAK", "ENTRY"):
                continue
            key = f"{code}|{day}|{ev['type']}|{ev['t']}"
            if key in sent:
                continue
            age_min = (now_ts - ev["t"]) / 60
            if age_min <= NOTIFY_MAX_AGE_MIN:
                if send_telegram(build_message(code, ev, now_ts)):
                    sent.add(key)
                    print(f"[{code}] aviso enviado: {key}")

        history = []
        for d, bs, r in results[:-1]:
            history.append({"date": d, "bias": r["bias"], "orb_high": r["orb_high"],
                            "orb_low": r["orb_low"], "events": r["events"]})
        history.reverse()

        out["symbols"][code] = {
            "name": cfg["name"], "yahoo": cfg["yahoo"],
            "last_price": last_bar["c"], "last_bar_t": last_bar["t"],
            "data_age_min": int((now_ts - last_bar["t"]) / 60),
            "today": {"date": day, "status": status_text(res, session_closed),
                      "bias": res["bias"], "orb_high": res["orb_high"], "orb_low": res["orb_low"],
                      "bars": day_bars, "fvgs": res["fvgs"], "events": res["events"]},
            "history": history,
        }

    out["sent"] = sorted(sent)[-300:]
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    print("state.json actualizado")


if __name__ == "__main__":
    sys.exit(main())

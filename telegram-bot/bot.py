import os
import asyncio
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
ESPN_SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={event_id}"
ESPN_HEADERS    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Thresholds for "good movement" — at least ONE must be satisfied
MIN_TOTAL_SHOTS       = 4   # total shots combined (home + away)
MIN_SHOTS_ON_TARGET   = 2   # shots on target combined
MIN_CORNERS           = 3   # corner kicks combined
MIN_DANGEROUS_ATTACKS = 25  # dangerous attacks combined

# Thresholds for "high intensity" alert — match is on fire
INTENSE_SHOTS    = 8   # total shots
INTENSE_CORNERS  = 5   # corner kicks
INTENSE_ATTACKS  = 55  # dangerous attacks

is_active    = False
monitor_task = None

# match_id -> {score, since_minute, alerted_05, alerted_15, alerted_25}
match_states: dict = {}


# ─── Helpers ────────────────────────────────────────────────────────────────

def flag_emoji(alpha2: str) -> str:
    if not alpha2 or len(alpha2) != 2:
        return ""
    return chr(0x1F1E6 + ord(alpha2[0].upper()) - 65) + chr(0x1F1E6 + ord(alpha2[1].upper()) - 65)


# Map ESPN season slugs → (country, league_name, alpha2)
_LEAGUE_MAP = {
    "english-premier-league":    ("England",     "Premier League",    "GB"),
    "english.1":                 ("England",     "Premier League",    "GB"),
    "spanish-la-liga":           ("Spain",       "La Liga",           "ES"),
    "spanish.1":                 ("Spain",       "La Liga",           "ES"),
    "german-bundesliga":         ("Germany",     "Bundesliga",        "DE"),
    "german.1":                  ("Germany",     "Bundesliga",        "DE"),
    "italian-serie-a":           ("Italy",       "Serie A",           "IT"),
    "italian.1":                 ("Italy",       "Serie A",           "IT"),
    "french-ligue-1":            ("France",      "Ligue 1",           "FR"),
    "french.1":                  ("France",      "Ligue 1",           "FR"),
    "portuguese-primeira-liga":  ("Portugal",    "Primeira Liga",     "PT"),
    "dutch-eredivisie":          ("Netherlands", "Eredivisie",        "NL"),
    "champions-league":          ("Europe",      "Champions League",  "EU"),
    "europa-league":             ("Europe",      "Europa League",     "EU"),
    "turkish-super-lig":         ("Turkey",      "Süper Lig",         "TR"),
    "scottish-premiership":      ("Scotland",    "Premiership",       "GB"),
    "russian-premier-league":    ("Russia",      "Premier League",    "RU"),
    "greek-super-league":        ("Greece",      "Super League",      "GR"),
    "austrian-bundesliga":       ("Austria",     "Bundesliga",        "AT"),
    "belgian-first-division-a":  ("Belgium",     "First Division A",  "BE"),
}

def _slug_to_league(slug: str) -> tuple[str, str, str]:
    for key, val in _LEAGUE_MAP.items():
        if key in slug:
            return val
    parts = [p for p in slug.split("-") if not p.isdigit() and len(p) > 2]
    return "?", " ".join(p.capitalize() for p in parts) or slug, ""

def _parse_minute(display_clock: str) -> int:
    try:
        return int(display_clock.split(":")[0])
    except Exception:
        return 0


def get_live_matches() -> list:
    """
    Returns normalized match dicts (same structure the monitor loop expects)
    for all 1st-half matches from ESPN scoreboard API.
    """
    try:
        resp = requests.get(ESPN_SCOREBOARD, headers=ESPN_HEADERS, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception as e:
        logger.warning(f"Errore fetch live ESPN: {e}")
        return []

    normalized = []
    for ev in events:
        comps = ev.get("competitions", [])
        if not comps:
            continue
        comp   = comps[0]
        status = comp.get("status", {})
        stype  = status.get("type", {})

        if stype.get("state") != "in":
            continue
        if status.get("period", 0) != 1:
            continue

        minute = _parse_minute(status.get("displayClock", "0:00"))
        slug   = ev.get("season", {}).get("slug", "")
        country, league, alpha2 = _slug_to_league(slug)

        home_score = away_score = 0
        home_name  = away_name  = "?"
        for ct in comp.get("competitors", []):
            side = ct.get("homeAway", "")
            name = ct.get("team", {}).get("displayName", "?")
            try:
                score = int(ct.get("score", 0))
            except (ValueError, TypeError):
                score = 0
            if side == "home":
                home_name, home_score = name, score
            else:
                away_name, away_score = name, score

        normalized.append({
            "id":        ev.get("id"),
            "status":    {"code": 6},
            "time":      {"played": minute},
            "homeScore": {"current": home_score},
            "awayScore": {"current": away_score},
            "homeTeam":  {"name": home_name},
            "awayTeam":  {"name": away_name},
            "tournament": {
                "name":     league,
                "category": {"name": country, "alpha2": alpha2},
            },
        })

    return normalized


def get_match_stats(event_id: str) -> dict | None:
    """
    Returns combined home+away stats dict with keys:
      "Total shots", "Shots on target", "Corner kicks", "Dangerous attacks"
    Returns None if unavailable.
    """
    try:
        url  = ESPN_SUMMARY.format(event_id=event_id)
        resp = requests.get(url, headers=ESPN_HEADERS, timeout=8)
        resp.raise_for_status()
        teams = resp.json().get("boxscore", {}).get("teams", [])
        if not teams:
            return None

        raw: dict[str, int] = {}
        for team in teams:
            for s in team.get("statistics", []):
                name = s.get("name", "")
                try:
                    val = int(s.get("displayValue", 0) or 0)
                except (ValueError, TypeError):
                    val = 0
                raw[name] = raw.get(name, 0) + val

        result = {
            "Total shots":       raw.get("totalShots",   raw.get("shots", 0)),
            "Shots on target":   raw.get("shotsOnGoal",  raw.get("shotsOnTarget", 0)),
            "Corner kicks":      raw.get("corners",      0),
            "Dangerous attacks": raw.get("dangerousAttacks", 0),
        }
        return result if any(result.values()) else None

    except Exception as e:
        logger.warning(f"Errore stats ESPN {event_id}: {e}")
        return None


def evaluate_momentum(stats: dict | None) -> tuple[bool, str, str]:
    """
    Returns (has_momentum: bool, stats_line: str, stars: str).
    If stats is None, fails open (lets alert through) with empty line and 1 star.
    Scoring (0-8 pts → 1-5 ⭐):
      shots     >=  4 → +1,  >= 8 → +2
      on_target >=  2 → +1,  >= 5 → +2
      corners   >=  3 → +1,  >= 6 → +2
      dangerous >= 25 → +1, >= 55 → +2
    """
    if stats is None:
        return True, "", "⭐"

    shots     = stats.get("Total shots",       stats.get("Shots", 0))
    on_target = stats.get("Shots on target",   0)
    corners   = stats.get("Corner kicks",      stats.get("Corners", 0))
    dangerous = stats.get("Dangerous attacks", 0)

    has_momentum = (
        shots     >= MIN_TOTAL_SHOTS       or
        on_target >= MIN_SHOTS_ON_TARGET   or
        corners   >= MIN_CORNERS           or
        dangerous >= MIN_DANGEROUS_ATTACKS
    )

    # Confidence score
    pts = 0
    pts += 2 if shots     >= 8  else (1 if shots     >= 4  else 0)
    pts += 2 if on_target >= 5  else (1 if on_target >= 2  else 0)
    pts += 2 if corners   >= 6  else (1 if corners   >= 3  else 0)
    pts += 2 if dangerous >= 55 else (1 if dangerous >= 25 else 0)
    star_count = 1 if pts <= 0 else 2 if pts <= 2 else 3 if pts <= 4 else 4 if pts <= 6 else 5
    stars = "⭐" * star_count

    parts = []
    if shots:     parts.append(f"Tiri: {shots}")
    if on_target: parts.append(f"In porta: {on_target}")
    if corners:   parts.append(f"Corner: {corners}")
    if dangerous: parts.append(f"Att. per.: {dangerous}")
    stats_line = " | ".join(parts) if parts else ""

    return has_momentum, stats_line, stars


def format_alert(flag, country, league, home, away, hs, aws, minute,
                 bet, note, stats_line: str = "", stars: str = "") -> str:
    lines = [
        f"{flag} <b>{country} — {league}</b>",
        f"⚽ <b>{home} vs {away}</b>",
        f"📊 Risultato: <b>{hs}-{aws}</b>  |  ⏱ <b>{minute}'</b>",
        f"📌 {note}",
    ]
    if stats_line:
        lines.append(f"📈 {stats_line}")
    lines.append(f"💰 Scommetti: <b>{bet}</b>")
    if stars:
        lines.append(f"🎖 Confidenza: <b>{stars}</b>")
    return "\n".join(lines)


# ─── Keep-alive HTTP server (required for Render free tier) ─────────────────

HTTP_PORT = int(os.environ.get("PORT", 10000))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", HTTP_PORT), _HealthHandler)
        logger.info(f"Health server in ascolto sulla porta {HTTP_PORT}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server non avviato: {e}")


# ─── Monitor Loop ────────────────────────────────────────────────────────────

async def monitor_loop(context: ContextTypes.DEFAULT_TYPE):
    global is_active, match_states

    logger.info("Monitor loop avviato.")
    while is_active:
        events = get_live_matches()
        logger.info(f"Partite live trovate: {len(events)}")

        for ev in events:
            try:
                status = ev.get("status", {})
                # Sofascore statusCode 6 = 1st half in progress
                if status.get("code") != 6:
                    continue

                match_id = ev.get("id")
                minute   = ev.get("time", {}).get("played", 0)

                hs    = ev.get("homeScore", {}).get("current", 0)
                aws   = ev.get("awayScore", {}).get("current", 0)
                total = hs + aws
                score_key = f"{hs}-{aws}"

                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")

                tournament = ev.get("tournament", {})
                league     = tournament.get("name", "?")
                category   = tournament.get("category", {})
                country    = category.get("name", "?")
                alpha2     = category.get("alpha2", "")
                flag       = flag_emoji(alpha2)

                # ── Initialise / update state ───────────────────────────────
                if match_id not in match_states:
                    match_states[match_id] = {
                        "score":        score_key,
                        "since_minute": 0 if total == 0 else minute,
                        "alerted_05":   False,
                        "alerted_15":   False,
                        "alerted_25":   False,
                        "alerted_intense": False,
                    }
                else:
                    state = match_states[match_id]
                    if state["score"] != score_key:
                        state["score"]        = score_key
                        state["since_minute"] = minute
                        if total >= 3:
                            state["alerted_05"] = True
                            state["alerted_15"] = True
                            state["alerted_25"] = True
                        elif total == 2:
                            state["alerted_05"] = True
                            state["alerted_15"] = True
                        elif total == 1:
                            state["alerted_05"] = True

                state = match_states[match_id]
                held  = minute - state["since_minute"]

                # ── Check alert conditions ──────────────────────────────────
                needs_alert = (
                    (total == 0 and minute >= 15 and not state["alerted_05"]) or
                    (total == 1 and held  >= 10  and not state["alerted_15"]) or
                    (total == 2 and held  >= 6   and not state["alerted_25"])
                )

                # ── Fetch stats if needed for intensity or betting alert ────
                needs_stats = needs_alert or not state["alerted_intense"]
                stats       = get_match_stats(match_id) if needs_stats else None
                has_momentum, stats_line, stars = evaluate_momentum(stats)

                # ── High-intensity check (independent of score/time) ────────
                if stats and not state["alerted_intense"]:
                    shots     = stats.get("Total shots",       stats.get("Shots", 0))
                    corners   = stats.get("Corner kicks",      stats.get("Corners", 0))
                    dangerous = stats.get("Dangerous attacks", 0)
                    is_intense = (
                        (shots >= INTENSE_SHOTS and corners >= INTENSE_CORNERS) or
                        (dangerous >= INTENSE_ATTACKS and corners >= INTENSE_CORNERS)
                    )
                    if is_intense:
                        state["alerted_intense"] = True
                        intense_msg = (
                            f"🔥 <b>PARTITA AD ALTA INTENSITÀ</b>\n"
                            f"{flag} <b>{country} — {league}</b>\n"
                            f"⚽ <b>{home} vs {away}</b>\n"
                            f"📊 Risultato: <b>{hs}-{aws}</b>  |  ⏱ <b>{minute}'</b>\n"
                            f"📈 {stats_line}\n"
                            f"🎖 Confidenza: <b>{stars}</b>\n"
                            f"⚠️ <b>Attenzione: partita molto viva!</b>"
                        )
                        await context.bot.send_message(
                            chat_id=CHAT_ID, text=intense_msg, parse_mode="HTML"
                        )
                        logger.info(f"Alert ALTA INTENSITÀ → {home} vs {away}")

                if not needs_alert:
                    continue

                if not has_momentum:
                    logger.info(
                        f"Skipped {home} vs {away} ({score_key} @{minute}') "
                        f"— bassa attività offensiva"
                    )
                    continue

                # ── Send relevant alerts ────────────────────────────────────
                if total == 0 and minute >= 15 and not state["alerted_05"]:
                    state["alerted_05"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 0.5 HT",
                        f"0-0 da {minute} minuti",
                        stats_line, stars,
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
                    logger.info(f"Alert Over 0.5 HT → {home} vs {away}")

                if total == 1 and held >= 10 and not state["alerted_15"]:
                    state["alerted_15"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 1.5 HT",
                        f"1 gol da {held} minuti",
                        stats_line, stars,
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
                    logger.info(f"Alert Over 1.5 HT → {home} vs {away}")

                if total == 2 and held >= 6 and not state["alerted_25"]:
                    state["alerted_25"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 2.5 HT",
                        f"2 gol da {held} minuti",
                        stats_line, stars,
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
                    logger.info(f"Alert Over 2.5 HT → {home} vs {away}")

            except Exception as e:
                logger.error(f"Errore su match {ev.get('id')}: {e}")

        await asyncio.sleep(35)

    logger.info("Monitor loop fermato.")


# ─── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("✅ ACCENDI", callback_data="accendi"),
        InlineKeyboardButton("❌ SPEGNI",  callback_data="spegni"),
    ]]
    await update.message.reply_text(
        "🤖 <b>Bot Calcio Live</b>\n\n"
        "Premi <b>ACCENDI</b> per iniziare il monitoraggio delle partite live.\n"
        "Premi <b>SPEGNI</b> per fermare il bot.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def stato_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_active:
        await update.message.reply_text("❌ Bot <b>SPENTO</b>.\nUsa /start per accenderlo.", parse_mode="HTML")
        return

    total = len(match_states)
    alerted = sum(
        1 for s in match_states.values()
        if s["alerted_05"] or s["alerted_15"] or s["alerted_25"]
    )
    intense = sum(1 for s in match_states.values() if s.get("alerted_intense"))
    pending = total - alerted

    lines = [
        "✅ Bot <b>ACCESO</b>\n",
        f"📋 Partite monitorate nel 1° tempo: <b>{total}</b>",
        f"🔔 Alert scommesse inviati: <b>{alerted}</b>",
        f"🔥 Alert alta intensità inviati: <b>{intense}</b>",
        f"⏳ In attesa di soglia: <b>{pending}</b>",
    ]

    if match_states:
        lines.append("\n<b>Partite attive:</b>")
        for state in list(match_states.values())[:10]:
            score   = state["score"]
            since   = state["since_minute"]
            a05 = "✓" if state["alerted_05"] else "·"
            a15 = "✓" if state["alerted_15"] else "·"
            a25 = "✓" if state["alerted_25"] else "·"
            fire = "🔥" if state.get("alerted_intense") else "  "
            lines.append(f"  {fire}{score} (dal {since}') — 0.5{a05} 1.5{a15} 2.5{a25}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    examples = [
        ("🇮🇹", "Italy", "Serie A", "Inter", "Napoli", 0, 0, 22,
         "🎯 Over 0.5 HT", "0-0 da 22 minuti",
         "Tiri: 6 | In porta: 3 | Corner: 4 | Att. per.: 41", "⭐⭐⭐"),
        ("🇩🇪", "Germany", "Bundesliga", "Bayern", "Dortmund", 1, 0, 31,
         "🎯 Over 1.5 HT", "1 gol da 14 minuti",
         "Tiri: 8 | In porta: 4 | Corner: 5 | Att. per.: 52", "⭐⭐⭐⭐"),
        ("🇪🇸", "Spain", "La Liga", "Barcelona", "Real Madrid", 1, 1, 38,
         "🎯 Over 2.5 HT", "2 gol da 9 minuti",
         "Tiri: 11 | In porta: 5 | Corner: 7 | Att. per.: 63", "⭐⭐⭐⭐⭐"),
    ]
    await update.message.reply_text("🧪 <b>Test alert — messaggi di esempio:</b>", parse_mode="HTML")
    for flag, country, league, home, away, hs, aws, minute, bet, note, stats, stars in examples:
        msg = format_alert(flag, country, league, home, away, hs, aws, minute, bet, note, stats, stars)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="HTML")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_active, monitor_task, match_states

    query = update.callback_query
    await query.answer()

    if query.data == "accendi":
        if is_active:
            await query.edit_message_text("✅ Il bot è già <b>ACCESO</b>.", parse_mode="HTML")
            return
        is_active = True
        match_states.clear()
        monitor_task = asyncio.create_task(monitor_loop(context))
        await query.edit_message_text(
            "✅ Bot <b>ACCESO</b>\n\n"
            "Controllo partite ogni 35 secondi.\n"
            "Alert inviati <b>solo se c'è movimento offensivo</b>:\n"
            "• 0-0 da ≥18 min + attività → <b>Over 0.5 HT</b>\n"
            "• 1 gol da ≥12 min + attività → <b>Over 1.5 HT</b>\n"
            "• 2 gol da ≥8 min + attività → <b>Over 2.5 HT</b>",
            parse_mode="HTML",
        )

    elif query.data == "spegni":
        if not is_active:
            await query.edit_message_text("❌ Il bot è già <b>SPENTO</b>.", parse_mode="HTML")
            return
        is_active = False
        match_states.clear()
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
        await query.edit_message_text("❌ Bot <b>SPENTO</b>.\nPremi ACCENDI per riavviarlo.", parse_mode="HTML")


# ─── Main ────────────────────────────────────────────────────────────────────

async def auto_start_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_active, monitor_task
    if is_active:
        return
    is_active = True
    monitor_task = asyncio.create_task(monitor_loop(context))
    logger.info("Monitoraggio avviato automaticamente via job.")


def main():
    # Start keep-alive HTTP server in background (needed for Render free tier)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stato", stato_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Auto-start monitoring 5 seconds after bot is fully ready
    app.job_queue.run_once(auto_start_job, when=5)

    logger.info("Bot avviato — monitoraggio partirà tra 5 secondi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

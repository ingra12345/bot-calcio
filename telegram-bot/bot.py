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

SOFASCORE_LIVE_URL  = "https://api.sofascore.com/api/v1/sport/football/events/live"
SOFASCORE_STATS_URL = "https://api.sofascore.com/api/v1/event/{event_id}/statistics"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
}

# Thresholds for "good movement" — at least ONE must be satisfied
MIN_TOTAL_SHOTS       = 5   # total shots combined (home + away)
MIN_SHOTS_ON_TARGET   = 3   # shots on target combined
MIN_CORNERS           = 4   # corner kicks combined
MIN_DANGEROUS_ATTACKS = 35  # dangerous attacks combined

is_active    = False
monitor_task = None

# match_id -> {score, since_minute, alerted_05, alerted_15, alerted_25}
match_states: dict = {}


# ─── Helpers ────────────────────────────────────────────────────────────────

def flag_emoji(alpha2: str) -> str:
    if not alpha2 or len(alpha2) != 2:
        return ""
    return chr(0x1F1E6 + ord(alpha2[0].upper()) - 65) + chr(0x1F1E6 + ord(alpha2[1].upper()) - 65)


def get_live_matches() -> list:
    try:
        resp = requests.get(SOFASCORE_LIVE_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        logger.warning(f"Errore fetch live: {e}")
        return []


def get_match_stats(event_id: int) -> dict | None:
    """
    Returns a flat dict of combined (home+away) stat values for the match,
    e.g. {"Total shots": 7, "Shots on target": 3, "Corner kicks": 4, ...}
    Returns None if the request fails.
    """
    try:
        url  = SOFASCORE_STATS_URL.format(event_id=event_id)
        resp = requests.get(url, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        combined: dict[str, int] = {}
        for period_block in data.get("statistics", []):
            if period_block.get("period") != "ALL":
                continue
            for group in period_block.get("groups", []):
                for item in group.get("statisticsItems", []):
                    name = item.get("name", "")
                    try:
                        h = int(str(item.get("homeValue", 0) or 0))
                        a = int(str(item.get("awayValue", 0) or 0))
                        combined[name] = h + a
                    except (ValueError, TypeError):
                        pass
        return combined if combined else None

    except Exception as e:
        logger.warning(f"Errore stats match {event_id}: {e}")
        return None


def evaluate_momentum(stats: dict | None) -> tuple[bool, str]:
    """
    Returns (has_momentum: bool, stats_line: str).
    If stats is None, fails open (lets alert through) with an empty line.
    """
    if stats is None:
        return True, ""

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

    parts = []
    if shots:     parts.append(f"Tiri: {shots}")
    if on_target: parts.append(f"In porta: {on_target}")
    if corners:   parts.append(f"Corner: {corners}")
    if dangerous: parts.append(f"Att. per.: {dangerous}")
    stats_line = " | ".join(parts) if parts else ""

    return has_momentum, stats_line


def format_alert(flag, country, league, home, away, hs, aws, minute,
                 bet, note, stats_line: str = "") -> str:
    lines = [
        f"{flag} <b>{country} — {league}</b>",
        f"⚽ <b>{home} vs {away}</b>",
        f"📊 Risultato: <b>{hs}-{aws}</b>  |  ⏱ <b>{minute}'</b>",
        f"📌 {note}",
    ]
    if stats_line:
        lines.append(f"📈 {stats_line}")
    lines.append(f"💰 Scommetti: <b>{bet}</b>")
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
                        "score":       score_key,
                        "since_minute": 0 if total == 0 else minute,
                        "alerted_05":  False,
                        "alerted_15":  False,
                        "alerted_25":  False,
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
                    (total == 0 and minute >= 18 and not state["alerted_05"]) or
                    (total == 1 and held  >= 12  and not state["alerted_15"]) or
                    (total == 2 and held  >= 8   and not state["alerted_25"])
                )

                if not needs_alert:
                    continue

                # ── Fetch statistics only when an alert is pending ──────────
                stats = get_match_stats(match_id)
                has_momentum, stats_line = evaluate_momentum(stats)

                if not has_momentum:
                    logger.info(
                        f"Skipped {home} vs {away} ({score_key} @{minute}') "
                        f"— bassa attività offensiva"
                    )
                    continue

                # ── Send relevant alerts ────────────────────────────────────
                if total == 0 and minute >= 18 and not state["alerted_05"]:
                    state["alerted_05"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 0.5 HT",
                        f"0-0 da {minute} minuti",
                        stats_line,
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
                    logger.info(f"Alert Over 0.5 HT → {home} vs {away}")

                if total == 1 and held >= 12 and not state["alerted_15"]:
                    state["alerted_15"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 1.5 HT",
                        f"1 gol da {held} minuti",
                        stats_line,
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
                    logger.info(f"Alert Over 1.5 HT → {home} vs {away}")

                if total == 2 and held >= 8 and not state["alerted_25"]:
                    state["alerted_25"] = True
                    msg = format_alert(
                        flag, country, league, home, away, hs, aws, minute,
                        "🎯 Over 2.5 HT",
                        f"2 gol da {held} minuti",
                        stats_line,
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
    pending = total - alerted

    lines = [
        "✅ Bot <b>ACCESO</b>\n",
        f"📋 Partite monitorate nel 1° tempo: <b>{total}</b>",
        f"🔔 Alert già inviati: <b>{alerted}</b>",
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
            lines.append(f"  {score} (dal {since}') — 0.5{a05} 1.5{a15} 2.5{a25}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    examples = [
        ("🇮🇹", "Italy", "Serie A", "Inter", "Napoli", 0, 0, 22,
         "🎯 Over 0.5 HT", "0-0 da 22 minuti",
         "Tiri: 6 | In porta: 3 | Corner: 4 | Att. per.: 41"),
        ("🇩🇪", "Germany", "Bundesliga", "Bayern", "Dortmund", 1, 0, 31,
         "🎯 Over 1.5 HT", "1 gol da 14 minuti",
         "Tiri: 8 | In porta: 4 | Corner: 5 | Att. per.: 52"),
        ("🇪🇸", "Spain", "La Liga", "Barcelona", "Real Madrid", 1, 1, 38,
         "🎯 Over 2.5 HT", "2 gol da 9 minuti",
         "Tiri: 11 | In porta: 5 | Corner: 7 | Att. per.: 63"),
    ]
    await update.message.reply_text("🧪 <b>Test alert — messaggi di esempio:</b>", parse_mode="HTML")
    for flag, country, league, home, away, hs, aws, minute, bet, note, stats in examples:
        msg = format_alert(flag, country, league, home, away, hs, aws, minute, bet, note, stats)
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

def main():
    # Start keep-alive HTTP server in background (needed for Render free tier)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stato", stato_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot avviato — in attesa di comandi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

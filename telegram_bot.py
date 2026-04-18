"""Telegram: client HTTP low-level, broadcast multi-target, poller dei comandi.

Tre responsabilità separate:

- `TelegramClient`     : wrapper sulle API Bot (sendMessage, editMessageReplyMarkup,
                         answerCallbackQuery, getUpdates).
- `TelegramNotifier`   : consuma lo storage e fa fan-out (canale + subscriber
                         filtrati per icao24). Espone `broadcast(text, heli_key)`.
- `TelegramPoller`     : thread di background che fa long-polling su `getUpdates`
                         e dispaccia alla `CommandHandler`.
- `CommandHandler`     : gestisce i / comandi e i callback delle inline keyboard
                         per gestire le iscrizioni per-utente.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import requests

from detector import (
    Helicopter,
    LandingSite,
    simulate_flight,
)
from storage import Storage

log = logging.getLogger("heli-tracker.telegram")

TELEGRAM_API = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Low-level client
# ---------------------------------------------------------------------------


class TelegramClient:
    def __init__(self, token: str):
        self.base = f"{TELEGRAM_API}/bot{token}"
        self.session = requests.Session()

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_preview: bool = True,
    ) -> requests.Response | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return self.session.post(
                f"{self.base}/sendMessage", json=payload, timeout=15
            )
        except requests.RequestException as e:
            log.warning("sendMessage %s error: %s", chat_id, e)
            return None

    def edit_message_reply_markup(
        self,
        chat_id: int | str,
        message_id: int,
        reply_markup: dict[str, Any],
    ) -> None:
        try:
            self.session.post(
                f"{self.base}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reply_markup,
                },
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("editMessageReplyMarkup error: %s", e)

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self.session.post(
                f"{self.base}/answerCallbackQuery", json=payload, timeout=10
            )
        except requests.RequestException as e:
            log.warning("answerCallbackQuery error: %s", e)

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """Registra il menu comandi in Telegram: fa apparire l'icona '/'
        nella chat e abilita l'autocomplete."""
        try:
            self.session.post(
                f"{self.base}/setMyCommands",
                json={"commands": commands},
                timeout=10,
            )
        except requests.RequestException as e:
            log.warning("setMyCommands error: %s", e)

    def get_updates(
        self, offset: int | None, timeout_s: int = 25
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_s,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset
        try:
            r = self.session.get(
                f"{self.base}/getUpdates",
                params=params,
                timeout=timeout_s + 10,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except requests.RequestException as e:
            log.warning("getUpdates error: %s", e)
            return []


# ---------------------------------------------------------------------------
# Notifier: fa fan-out al canale + ai subscriber iscritti a quell'icao24
# ---------------------------------------------------------------------------


class TelegramNotifier:
    """Fan-out di un messaggio al canale + subscriber del bot (filtrati per icao24).

    Implementa il `Notifier` protocol atteso da detector.process_update.
    """

    def __init__(
        self,
        client: TelegramClient,
        channel_chat_id: str | None,
        storage: Storage,
    ):
        self.client = client
        self.channel_chat_id = channel_chat_id
        self.storage = storage

    def broadcast(self, text: str, helicopter_key: str) -> None:
        # 1. Canale: ogni evento, sempre, se configurato
        if self.channel_chat_id:
            self._send_with_cleanup(self.channel_chat_id, text)
        # 2. Subscriber del bot iscritti a questo specifico mezzo
        for chat_id in self.storage.subscribers_for(helicopter_key):
            self._send_with_cleanup(chat_id, text)

    def send_direct(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> requests.Response | None:
        r = self.client.send_message(chat_id, text, reply_markup=reply_markup)
        if r is not None and r.status_code != 200:
            log.warning(
                "sendMessage %s → %d %s",
                chat_id,
                r.status_code,
                r.text[:200] if r.text else "",
            )
        return r

    def _send_with_cleanup(self, chat_id: int | str, text: str) -> None:
        r = self.send_direct(chat_id, text)
        if r is None:
            return
        if r.status_code == 403 and isinstance(chat_id, int):
            log.info("Utente %s ha bloccato il bot → rimuovo iscrizioni.", chat_id)
            self.storage.remove_subscriber(chat_id)
        elif r.status_code == 429:
            retry = 1
            try:
                retry = int(r.json().get("parameters", {}).get("retry_after", 1))
            except ValueError:
                pass
            log.info("Rate limited su %s → retry in %ds", chat_id, retry)
            time.sleep(retry)
            self.send_direct(chat_id, text)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


HELP_TEXT = (
    "✅ <b>heli-tracker bot</b>\n\n"
    "Ti notifico decolli e atterraggi degli elicotteri di elisoccorso che scegli.\n\n"
    "<b>Comandi disponibili:</b>\n"
    "/subscribe — scegli quali mezzi seguire (menu interattivo)\n"
    "/list — vedi le tue iscrizioni attuali\n"
    "/all — iscriviti a tutti i mezzi\n"
    "/none — rimuovi tutte le iscrizioni\n"
    "/stop — cancellati del tutto dal bot\n"
    "/help — rivedi questo messaggio"
)


class CommandHandler:
    def __init__(
        self,
        notifier: TelegramNotifier,
        storage: Storage,
        helicopters: list[Helicopter],
        sites: list[LandingSite],
        admin_chat_id: int | None,
    ):
        self.notifier = notifier
        self.storage = storage
        self.helicopters = helicopters
        self.sites = sites
        self.admin_chat_id = admin_chat_id

    # --- dispatch ------------------------------------------------------------

    def on_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        # Ignora messaggi dai canali (sono i post del bot stesso o di altri
        # admin — non comandi da processare). I comandi si danno in DM.
        if chat_type == "channel":
            return
        if chat_id is None:
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        cmd = text.split(None, 1)[0].split("@", 1)[0]

        handlers: dict[str, Callable[[int], None]] = {
            "/start": self.cmd_start,
            "/help": self.cmd_help,
            "/subscribe": self.cmd_subscribe,
            "/list": self.cmd_list,
            "/all": self.cmd_all,
            "/none": self.cmd_none,
            "/stop": self.cmd_stop,
            "/mock": self.cmd_mock,
        }
        fn = handlers.get(cmd)
        if fn is None:
            self.notifier.send_direct(
                chat_id, "Comando sconosciuto. Usa /help per la lista."
            )
            return
        try:
            fn(chat_id)
        except Exception:
            log.exception("Errore nel comando %s per chat %s", cmd, chat_id)
            self.notifier.send_direct(chat_id, "⚠️ Errore interno, riprova.")

    def on_callback_query(self, cq: dict[str, Any]) -> None:
        cq_id = cq.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = cq.get("data", "")
        if chat_id is None or message_id is None or not cq_id:
            return

        if data.startswith("t:"):
            heli_key = data[2:]
            valid_keys = {h.icao24 for h in self.helicopters}
            if heli_key not in valid_keys:
                self.notifier.client.answer_callback_query(cq_id, "Mezzo non valido")
                return
            self.storage.add_subscriber(chat_id)
            subs = self.storage.subscriptions_of(chat_id)
            new_sub = heli_key not in subs
            self.storage.set_subscription(chat_id, heli_key, new_sub)
            self.notifier.client.edit_message_reply_markup(
                chat_id, message_id, self._subscription_keyboard(chat_id)
            )
            self.notifier.client.answer_callback_query(
                cq_id, "✓ Iscritto" if new_sub else "✗ Rimosso"
            )
        else:
            self.notifier.client.answer_callback_query(cq_id, "Azione sconosciuta")

    # --- builders ------------------------------------------------------------

    def _subscription_keyboard(self, chat_id: int) -> dict[str, Any]:
        subs = self.storage.subscriptions_of(chat_id)
        rows = []
        for h in self.helicopters:
            prefix = "✓" if h.icao24 in subs else "✗"
            rows.append(
                [
                    {
                        "text": f"{prefix} {h.display_name}",
                        "callback_data": f"t:{h.icao24}",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    # --- commands ------------------------------------------------------------

    def cmd_start(self, chat_id: int) -> None:
        is_new = self.storage.add_subscriber(chat_id)
        if self.admin_chat_id is not None and chat_id == self.admin_chat_id:
            self.storage.mark_admin(chat_id)
        greeting = (
            "👋 Benvenuto!\n\n" if is_new else "👋 Bentornato.\n\n"
        ) + HELP_TEXT
        self.notifier.send_direct(chat_id, greeting)

    def cmd_help(self, chat_id: int) -> None:
        self.notifier.send_direct(chat_id, HELP_TEXT)

    def cmd_subscribe(self, chat_id: int) -> None:
        self.storage.add_subscriber(chat_id)
        if not self.helicopters:
            self.notifier.send_direct(chat_id, "Nessun elicottero configurato.")
            return
        self.notifier.send_direct(
            chat_id,
            "Tocca un mezzo per iscriverti/disiscriverti.\n"
            "✓ = iscritto   ✗ = non iscritto",
            reply_markup=self._subscription_keyboard(chat_id),
        )

    def cmd_list(self, chat_id: int) -> None:
        subs = self.storage.subscriptions_of(chat_id)
        if not subs:
            self.notifier.send_direct(
                chat_id,
                "Non sei iscritto a nessun mezzo. Usa /subscribe per scegliere.",
            )
            return
        names = [h.display_name for h in self.helicopters if h.icao24 in subs]
        lines = ["📋 <b>Le tue iscrizioni:</b>"] + [f"• {n}" for n in names]
        self.notifier.send_direct(chat_id, "\n".join(lines))

    def cmd_all(self, chat_id: int) -> None:
        self.storage.add_subscriber(chat_id)
        keys = [h.icao24 for h in self.helicopters if h.icao24]
        self.storage.set_all_subscriptions(chat_id, keys)
        self.notifier.send_direct(
            chat_id, f"✓ Iscritto a tutti i {len(keys)} mezzi."
        )

    def cmd_none(self, chat_id: int) -> None:
        self.storage.set_all_subscriptions(chat_id, [])
        self.notifier.send_direct(
            chat_id,
            "✗ Tutte le iscrizioni rimosse. Usa /subscribe per sceglierle di nuovo.",
        )

    def cmd_stop(self, chat_id: int) -> None:
        self.storage.remove_subscriber(chat_id)
        self.notifier.send_direct(
            chat_id,
            "Cancellato dal bot. Non riceverai più notifiche. Puoi tornare con /start.",
        )

    def cmd_mock(self, chat_id: int) -> None:
        if self.admin_chat_id is None or chat_id != self.admin_chat_id:
            self.notifier.send_direct(chat_id, "Comando riservato all'admin.")
            return
        self.notifier.send_direct(chat_id, "Lancio simulazione decollo+atterraggio…")
        try:
            simulate_flight(
                self.helicopters,
                self.sites,
                send=lambda text: self.notifier.send_direct(chat_id, text),
            )
            self.notifier.send_direct(chat_id, "Mock completato.")
        except Exception as e:
            log.exception("Errore in /mock")
            self.notifier.send_direct(chat_id, f"Errore: {e}")


# ---------------------------------------------------------------------------
# Long-polling thread
# ---------------------------------------------------------------------------


class TelegramPoller(threading.Thread):
    def __init__(
        self,
        client: TelegramClient,
        storage: Storage,
        handler: CommandHandler,
        stop_flag: dict[str, bool],
    ):
        super().__init__(daemon=True, name="telegram-poller")
        self.client = client
        self.storage = storage
        self.handler = handler
        self.stop_flag = stop_flag

    def run(self) -> None:
        offset_str = self.storage.get_state("last_update_id")
        offset = int(offset_str) + 1 if offset_str else None
        log.info("Telegram poller avviato (offset=%s)", offset)
        while not self.stop_flag.get("flag"):
            updates = self.client.get_updates(offset, timeout_s=25)
            for u in updates:
                try:
                    if "message" in u:
                        self.handler.on_message(u["message"])
                    elif "callback_query" in u:
                        self.handler.on_callback_query(u["callback_query"])
                except Exception:
                    log.exception(
                        "Errore processando update %s", u.get("update_id")
                    )
                update_id = u.get("update_id")
                if update_id is not None:
                    offset = update_id + 1
                    self.storage.set_state("last_update_id", str(update_id))
        log.info("Telegram poller stopped.")

"""
Manages multiple instagrapi Client instances keyed by account_id.
Each account runs its MQTT realtime loop in a dedicated background thread.
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from instagrapi import Client

log = logging.getLogger("ig-sidecar.accounts")


@dataclass
class IGMessage:
    account_id: str
    thread_id: str
    item_id: str
    user_id: str
    text: str
    timestamp: str
    is_group: bool = False


@dataclass
class AccountEntry:
    account_id: str
    client: Client
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)


class AccountManager:
    def __init__(self) -> None:
        self._accounts: dict[str, AccountEntry] = {}
        self._lock = threading.Lock()
        # Callbacks receive IGMessage; called from realtime background thread
        self._message_handlers: list[Callable[[IGMessage], None]] = []
        self._error_handlers: list[Callable[[str, Exception], None]] = []
        # Cache: numeric user_id (str) → username (str); avoids repeated API lookups
        self._uid_to_username: dict[str, str] = {}

    def on_message(self, handler: Callable[[IGMessage], None]) -> None:
        self._message_handlers.append(handler)

    def on_error(self, handler: Callable[[str, Exception], None]) -> None:
        self._error_handlers.append(handler)

    def _resolve_username(self, account_id: str, numeric_uid: str) -> str:
        """Resolve numeric user_id to username (cached). Falls back to numeric_uid on error."""
        if numeric_uid in self._uid_to_username:
            return self._uid_to_username[numeric_uid]
        entry = self._accounts.get(account_id)
        if entry and numeric_uid.isdigit():
            try:
                username = entry.client.username_from_user_id(int(numeric_uid))
                self._uid_to_username[numeric_uid] = username
                log.info("resolved uid %s → @%s", numeric_uid, username)
                return username
            except Exception as exc:
                log.warning("could not resolve uid %s: %s", numeric_uid, exc)
        return numeric_uid

    def _dispatch_message(self, account_id: str, data: dict) -> None:
        msg_data = data.get("message", {})
        text = msg_data.get("text")
        if not text:
            return
        numeric_uid = str(msg_data.get("user_id", ""))
        # Resolve to username so Brain can match against creator channels stored by username
        sender_id = self._resolve_username(account_id, numeric_uid)
        msg = IGMessage(
            account_id=account_id,
            thread_id=str(msg_data.get("thread_id", "")),
            item_id=str(msg_data.get("item_id", "")),
            user_id=sender_id,
            text=text,
            timestamp=str(msg_data.get("timestamp", "")),
            is_group=bool(msg_data.get("is_group", False)),
        )
        for h in self._message_handlers:
            try:
                h(msg)
            except Exception:
                log.exception("message handler error")

    def _dispatch_error(self, account_id: str, exc: Exception) -> None:
        for h in self._error_handlers:
            try:
                h(account_id, exc)
            except Exception:
                log.exception("error handler error")

    def _realtime_loop(self, entry: AccountEntry) -> None:
        """Blocks in a read loop until stop_event is set or an exception occurs."""
        client = entry.client
        account_id = entry.account_id

        def on_message(data: dict) -> None:
            self._dispatch_message(account_id, data)

        RETRY_DELAYS = [0, 3, 10, 30]
        for attempt, delay in enumerate(RETRY_DELAYS):
            if entry.stop_event.is_set():
                return
            if delay:
                time.sleep(delay)
            try:
                client.realtime_on("message", on_message)
                rt = client.realtime_connect()
                # Check stop_event between connect and subscribe to avoid race with teardown
                if entry.stop_event.is_set():
                    return
                rt.direct_subscribe()
                log.info("realtime connected for %s", account_id)
                while not entry.stop_event.is_set():
                    try:
                        client.realtime_read_once()
                    except (TimeoutError, OSError) as read_exc:
                        # read timeout is normal keepalive behaviour — keep looping
                        if "timed out" in str(read_exc).lower() or isinstance(read_exc, TimeoutError):
                            continue
                        raise  # unexpected socket error → outer retry
                return  # clean exit via stop_event
            except Exception as exc:
                if entry.stop_event.is_set():
                    return
                log.warning("realtime error for %s (attempt %d): %s", account_id, attempt + 1, exc)
                if attempt == len(RETRY_DELAYS) - 1:
                    log.exception("realtime failed after all retries for %s", account_id)
                    self._dispatch_error(account_id, exc)

    @staticmethod
    def _extract_cookies(sessionid: str) -> list[dict]:
        """
        sessionid may be:
          - a plain cookie value string  → wrap in minimal cookie list
          - a JSON array of cookie dicts → use as-is (browser Cookie-Editor export)
        """
        stripped = sessionid.strip()
        if stripped.startswith("["):
            try:
                cookies = json.loads(stripped)
                if isinstance(cookies, list):
                    return cookies
            except Exception:
                pass
        return [{"name": "sessionid", "value": sessionid, "domain": ".instagram.com"}]

    def login(self, account_id: str, sessionid: str, rapidapi_key: str = "") -> dict:
        """Login with sessionid cookie (plain value or JSON cookie array). Returns {username, user_id}."""
        import tempfile, os

        with self._lock:
            # Teardown any existing entry first
            if account_id in self._accounts:
                self._teardown_locked(account_id)

            cookies = self._extract_cookies(sessionid)

            # Write cookies to a temp file; login_from_cookie_file restores full session state
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            try:
                json.dump(cookies, tmp)
                tmp.close()
                client = Client(rapidapi_key=rapidapi_key or None)
                client.login_from_cookie_file(tmp.name)
            finally:
                os.unlink(tmp.name)

            username = client.username
            user_id = str(client.user_id)

            entry = AccountEntry(account_id=account_id, client=client)
            t = threading.Thread(
                target=self._realtime_loop,
                args=(entry,),
                daemon=True,
                name=f"ig-rt-{account_id}",
            )
            entry.thread = t
            self._accounts[account_id] = entry
            t.start()

            log.info("logged in %s as @%s (uid=%s)", account_id, username, user_id)
            return {"username": username, "user_id": user_id}

    def logout(self, account_id: str) -> None:
        with self._lock:
            self._teardown_locked(account_id)

    def _teardown_locked(self, account_id: str) -> None:
        entry = self._accounts.pop(account_id, None)
        if not entry:
            return
        entry.stop_event.set()
        try:
            entry.client.realtime_disconnect()
        except Exception:
            pass
        log.info("logged out %s", account_id)

    def get_client(self, account_id: str) -> Client:
        entry = self._accounts.get(account_id)
        if not entry:
            raise KeyError(f"account {account_id!r} not logged in")
        return entry.client

    def has_account(self, account_id: str) -> bool:
        return account_id in self._accounts

    def get_profile(self, account_id: str) -> dict:
        client = self.get_client(account_id)
        return {"username": client.username, "user_id": str(client.user_id)}

    def send_message(self, account_id: str, thread_id: str, text: str) -> dict:
        """Send to thread_id or user_id. Returns {item_id, thread_id}.

        thread_id can be:
          - a long IG thread ID (≥18 digits) → send via thread_ids
          - a short numeric user ID           → send via user_ids (creates/reuses thread)
        """
        client = self.get_client(account_id)
        # IG thread IDs are 128-bit integers (~39 digits).
        # User IDs are shorter (≤15 digits). Use length as heuristic.
        if thread_id.isdigit() and len(thread_id) <= 15:
            msg = client.direct_send(text, user_ids=[int(thread_id)])
        else:
            msg = client.direct_send(text, thread_ids=[thread_id])
        return {
            "item_id": str(msg.id),
            "thread_id": str(msg.thread_id) if msg.thread_id else thread_id,
        }

    def send_typing(self, account_id: str, thread_id: str) -> None:
        client = self.get_client(account_id)
        try:
            client.direct_send_seen(thread_id)
        except Exception:
            pass

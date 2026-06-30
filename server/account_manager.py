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
    user_id: str   # username for non-self (Brain matches by handle); numeric uid for self-messages
    uid: str       # always the creator's numeric uid (for PATCH /v2/channels/uid)
    text: str
    timestamp: str
    is_group: bool = False
    is_self: bool = False


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
        # uid → username cache (shared across accounts, uids are globally unique)
        self._uid_to_username: dict[str, str] = {}
        # item_ids already dispatched this session — prevents re-emitting admin messages
        self._dispatched_item_ids: set[str] = set()
        # item_ids sent via our own /send endpoint — MQTT fires is_self=True for these
        # too, but the command executor already recorded them in Brain, so skip them.
        self._self_sent_item_ids: set[str] = set()

    def on_message(self, handler: Callable[[IGMessage], None]) -> None:
        self._message_handlers.append(handler)

    def on_error(self, handler: Callable[[str, Exception], None]) -> None:
        self._error_handlers.append(handler)


    def _fetch_thread_messages(self, account_id: str, thread_id: str, amount: int = 10) -> list:
        """Fetch recent messages in a thread. Returns [] on error."""
        entry = self._accounts.get(account_id)
        if not entry:
            return []
        try:
            thread = entry.client.direct_thread(int(thread_id), amount=amount)
            return list(thread.messages)
        except Exception as exc:
            log.warning("fetch_thread_messages failed thread=%s: %s", thread_id, exc)
            return []

    def _get_thread_other_user(self, account_id: str, thread_id: str) -> str | None:
        """Return the OTHER participant's numeric user_id in a DM thread (not the admin)."""
        entry = self._accounts.get(account_id)
        if not entry:
            return None
        try:
            own_uid = str(entry.client.user_id)
            thread = entry.client.direct_thread(int(thread_id), amount=1)
            for u in thread.users:
                uid = str(u.pk)
                if uid != own_uid:
                    return uid
            return None
        except Exception as exc:
            log.warning("get_thread_other_user failed thread=%s: %s", thread_id, exc)
            return None

    def _get_username_for_uid(self, account_id: str, numeric_uid: str) -> str | None:
        """Resolve numeric uid → username. Results are cached for the session lifetime."""
        cached = self._uid_to_username.get(numeric_uid)
        if cached:
            return cached
        entry = self._accounts.get(account_id)
        if not entry:
            return None
        try:
            info = entry.client.user_info(int(numeric_uid))
            username = info.username
            if username:
                self._uid_to_username[numeric_uid] = username
            return username or None
        except Exception as exc:
            log.warning("get_username_for_uid uid=%s: %s", numeric_uid, exc)
            return None

    def _dispatch_message(self, account_id: str, data: dict) -> None:
        log.debug("raw message event for %s: %s", account_id, data)
        msg_data = data.get("message", {})
        text = msg_data.get("text")
        item_type = msg_data.get("item_type", "")
        thread_id = str(msg_data.get("thread_id", ""))
        item_id = str(msg_data.get("item_id", ""))

        entry = self._accounts.get(account_id)
        own_uid = str(entry.client.user_id) if entry else ""

        numeric_uid = str(msg_data.get("user_id", ""))
        is_self = bool(numeric_uid and own_uid and numeric_uid == own_uid)

        # MQTT delta notifications don't carry text — fetch thread once for text
        # extraction AND to scan for any admin (IG web) messages in the batch.
        thread_messages: list = []
        if thread_id and item_id:
            thread_messages = self._fetch_thread_messages(account_id, thread_id, amount=10)

        if not text and item_type == "text" and thread_messages:
            for m in thread_messages:
                if str(m.id) == item_id and m.text:
                    text = m.text
                    break

        if text:
            if is_self and item_id in self._self_sent_item_ids:
                # This MQTT echo is for a message we sent via /send (Hiip UI command).
                # The command executor already recorded it in Brain — skip to avoid duplicate.
                self._self_sent_item_ids.discard(item_id)
                log.debug("skipping MQTT echo for self-sent item=%s", item_id)
                return

            if is_self:
                # Admin sent from their IG app — find the OTHER participant (creator).
                creator_uid = self._get_thread_other_user(account_id, thread_id) if thread_id else None
                if not creator_uid:
                    log.warning("self-message: could not resolve creator uid for thread=%s", thread_id)
                    return
                uid = creator_uid
                # Resolve to username: Brain may use sender_id as fallback when thread_id
                # is not yet registered (e.g. admin-initiated conversation).
                username = self._get_username_for_uid(account_id, creator_uid)
                sender_id = username if username else creator_uid
                log.debug("self-message: creator uid=%s username=%s", creator_uid, username)
            else:
                # Non-self: resolve username so Brain can match creator_channels by handle.
                # Also keep the numeric uid separately for PATCH /v2/channels/uid.
                uid = numeric_uid
                username = self._get_username_for_uid(account_id, numeric_uid)
                if username:
                    sender_id = username
                    log.debug("resolved uid=%s → @%s", numeric_uid, username)
                else:
                    # Fallback: send numeric uid (Brain may match by uid if already registered)
                    sender_id = numeric_uid
                    log.warning("could not resolve username for uid=%s, sending numeric uid", numeric_uid)

            # IG MQTT timestamps are microseconds; convert to milliseconds for Brain
            raw_ts = msg_data.get("timestamp", "")
            try:
                ts_ms = str(int(raw_ts) // 1000)
            except (TypeError, ValueError):
                ts_ms = str(raw_ts)

            msg = IGMessage(
                account_id=account_id,
                thread_id=thread_id,
                item_id=item_id,
                user_id=sender_id,
                uid=uid,
                text=text,
                timestamp=ts_ms,
                is_group=bool(msg_data.get("is_group", False)),
                is_self=is_self,
            )
            self._dispatched_item_ids.add(item_id)
            for h in self._message_handlers:
                try:
                    h(msg)
                except Exception:
                    log.exception("message handler error")
        else:
            log.debug("no text in message for %s, msg_data keys: %s", account_id, list(msg_data.keys()))

        # After handling the MQTT event (from creator), scan the thread batch for any
        # admin messages sent from IG web that haven't been dispatched. MQTT is
        # mobile-only — IG web sends don't trigger events, so we piggyback here.
        if not is_self and thread_messages and own_uid:
            creator_uid_for_self = numeric_uid or (
                self._get_thread_other_user(account_id, thread_id) or ""
            )
            for m in thread_messages:
                batch_item_id = str(m.id)
                if batch_item_id in self._dispatched_item_ids:
                    continue
                if str(m.user_id) != own_uid:
                    continue
                if not m.text:
                    continue
                self._dispatched_item_ids.add(batch_item_id)
                try:
                    m_ts_ms = str(int(m.timestamp) // 1000) if m.timestamp else ""
                except (TypeError, ValueError):
                    m_ts_ms = str(m.timestamp) if m.timestamp else ""
                self_msg = IGMessage(
                    account_id=account_id,
                    thread_id=thread_id,
                    item_id=batch_item_id,
                    user_id=creator_uid_for_self,
                    uid=creator_uid_for_self,
                    text=m.text,
                    timestamp=m_ts_ms,
                    is_group=False,
                    is_self=True,
                )
                log.info("dispatching missed admin message item=%s thread=%s", batch_item_id, thread_id)
                for h in self._message_handlers:
                    try:
                        h(self_msg)
                    except Exception:
                        log.exception("self-message handler error")

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

        # Register handler ONCE before the retry loop to avoid accumulating
        # duplicate handlers across reconnect attempts.
        client.realtime_on("message", on_message)

        RETRY_DELAYS = [0, 3, 10, 30]
        for attempt, delay in enumerate(RETRY_DELAYS):
            if entry.stop_event.is_set():
                return
            if delay:
                time.sleep(delay)
            try:
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
        item_id = str(msg.id)
        # Mark as sent by us so the MQTT echo (is_self=True) doesn't get
        # dispatched — the command executor already recorded it in Brain.
        self._self_sent_item_ids.add(item_id)
        return {
            "item_id": item_id,
            "thread_id": str(msg.thread_id) if msg.thread_id else thread_id,
        }

    def send_typing(self, account_id: str, thread_id: str) -> None:
        client = self.get_client(account_id)
        try:
            client.direct_send_seen(thread_id)
        except Exception:
            pass

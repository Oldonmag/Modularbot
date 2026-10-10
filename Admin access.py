# -*- coding: utf-8 -*-
# AdminControl v2.1 — управление правами через userbot-мост
#
# Команды (только владельцы бота):
#   /admin    — выдать права администратора с настройкой прав и должности
#   /unadmin  — снять с администратора
#   /ban      — снять с админа (если был) и заблокировать
#   /unban    — разблокировать
#   /kick     — снять с админа (если был) и выкинуть
#   /mute     — мут с сохранением/восстановлением прав админа
#   /mute ... --delete  — удалять сообщения через Middleware (права не снимать)
#   /mute all [время]   — закрыть чат для всех (кроме администраторов и ботов)
#   /unmute   — снять мут с пользователя
#   /unmute all         — открыть чат
#
# Формат цели: реплай | @username | ID
# Middleware: AdminControlMiddleware — пассивное наблюдение (FSM + delete-мут)

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import F, types
from aiogram.filters import Command

from telethon.tl.functions.channels import (
    EditAdminRequest,
    EditBannedRequest,
    GetParticipantRequest,
)
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChatAdminRights,
    ChatBannedRights,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} мин."
    elif seconds < 86400:
        return f"{seconds // 3600} ч."
    else:
        return f"{seconds // 86400} д."


def _parse_duration(text: str) -> Optional[int]:
    """30m / 2h / 1d / число (минуты). None если не распознано."""
    text = text.strip().lower()
    if not text:
        return None
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("h"):
            return int(text[:-1]) * 3600
        if text.endswith("d"):
            return int(text[:-1]) * 86400
        return int(text) * 60
    except ValueError:
        return None


def _user_name(entity) -> str:
    """Красивое имя из Telethon entity."""
    parts = []
    fn = getattr(entity, "first_name", None)
    ln = getattr(entity, "last_name", None)
    if fn:
        parts.append(fn)
    if ln:
        parts.append(ln)
    if parts:
        return " ".join(parts)
    un = getattr(entity, "username", None)
    if un:
        return f"@{un}"
    return str(getattr(entity, "id", entity))


def _parse_cmd_args(message: types.Message):
    """
    Разбирает аргументы команды.
    Возвращает (target_raw | None, flags: set, extra: list)
    target_raw — @user/id если нет реплая, иначе None
    flags — {'--delete', '--rank', ...}
    extra — остальные позиционные аргументы
    """
    parts = (message.text or "").split()[1:]
    target_raw = None
    flags = set()
    extra = []

    i = 0
    while i < len(parts):
        part = parts[i]
        if part.startswith("--"):
            flags.add(part.lower())
        elif target_raw is None and not message.reply_to_message:
            target_raw = part.lstrip("@")
            try:
                target_raw = int(target_raw)
            except ValueError:
                pass
        else:
            extra.append(part)
        i += 1

    return target_raw, flags, extra


async def _resolve_target(bot_instance, message: types.Message):
    """
    Определяет цель из реплая или первого аргумента.
    Возвращает (entity | None, error_str | None).
    """
    target_raw = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_raw = message.reply_to_message.from_user.id
    else:
        parts = (message.text or "").split()[1:]
        for part in parts:
            if not part.startswith("--"):
                target_raw = part.lstrip("@")
                try:
                    target_raw = int(target_raw)
                except ValueError:
                    pass
                break

    if target_raw is None:
        return None, "⚠️ Укажи цель: ответь на сообщение, @username или ID."

    try:
        entity = await bot_instance.userbot("get_entity", target_raw)
        return entity, None
    except Exception as e:
        return None, f"❌ Не удалось найти пользователя: {e}"


async def _get_admin_rights(bot_instance, chat_id: int, entity):
    """
    Если участник — администратор, возвращает (ChatAdminRights, rank).
    Иначе (None, '').
    """
    try:
        tg_chat = await bot_instance.userbot("get_entity", chat_id)
        result = await bot_instance.raw(
            GetParticipantRequest(channel=tg_chat, participant=entity)
        )
        p = result.participant
        if isinstance(p, ChannelParticipantAdmin):
            return p.admin_rights, (p.rank or "")
        return None, ""
    except Exception as e:
        logger.warning(f"get_admin_rights error: {e}")
        return None, ""


async def _strip_admin(bot_instance, tg_chat, entity):
    """Снимает все права администратора."""
    await bot_instance.raw(
        EditAdminRequest(
            channel=tg_chat,
            user_id=entity,
            admin_rights=ChatAdminRights(
                change_info=False,
                post_messages=False,
                edit_messages=False,
                delete_messages=False,
                ban_users=False,
                invite_users=False,
                pin_messages=False,
                add_admins=False,
                anonymous=False,
                manage_call=False,
                other=False,
                manage_topics=False,
            ),
            rank="",
        )
    )


# ═══════════════════════════════════════════════════════════════════════════════
# МОДУЛЬ
# ═══════════════════════════════════════════════════════════════════════════════

# Псевдонимы прав для /admin
_RIGHT_ALIASES = {
    "change_info":    "change_info",
    "info":           "change_info",
    "post":           "post_messages",
    "post_messages":  "post_messages",
    "edit":           "edit_messages",
    "edit_messages":  "edit_messages",
    "delete":         "delete_messages",
    "delete_messages":"delete_messages",
    "ban":            "ban_users",
    "ban_users":      "ban_users",
    "invite":         "invite_users",
    "invite_users":   "invite_users",
    "pin":            "pin_messages",
    "pin_messages":   "pin_messages",
    "add_admins":     "add_admins",
    "add":            "add_admins",
    "anon":           "anonymous",
    "anonymous":      "anonymous",
    "call":           "manage_call",
    "manage_call":    "manage_call",
    "topics":         "manage_topics",
    "manage_topics":  "manage_topics",
    "other":          "other",
}

_RIGHT_LABELS = {
    "change_info":    "Изменять инфо",
    "post_messages":  "Постить",
    "edit_messages":  "Редактировать чужие",
    "delete_messages":"Удалять сообщения",
    "ban_users":      "Банить",
    "invite_users":   "Приглашать",
    "pin_messages":   "Закреплять",
    "add_admins":     "Добавлять админов",
    "anonymous":      "Аноним",
    "manage_call":    "Управлять голосом",
    "other":          "Прочее",
    "manage_topics":  "Темы",
}

# Стандартный набор прав для нового администратора
_DEFAULT_ADMIN_RIGHTS = {
    "change_info":    False,
    "post_messages":  False,
    "edit_messages":  False,
    "delete_messages":True,
    "ban_users":      False,
    "invite_users":   True,
    "pin_messages":   False,
    "add_admins":     False,
    "anonymous":      False,
    "manage_call":    True,
    "other":          True,
    "manage_topics":  False,
}

_HELP_TEXTS = {
    "admin": (
        "👑 <b>/admin</b> — выдать права администратора\n\n"
        "<b>Использование:</b>\n"
        "  <code>/admin @user</code> — стандартный набор прав\n"
        "  <code>/admin @user all</code> — все права\n"
        "  <code>/admin @user all -ban</code> — все права кроме бана\n"
        "  <code>/admin @user +delete +pin --rank Мод</code> — только delete+pin\n\n"
        "<b>Права (флаги + включить / - выключить):</b>\n"
        "  <code>change_info</code>  / <code>info</code>    — изменять инфо чата\n"
        "  <code>post_messages</code> / <code>post</code>  — постить в канале\n"
        "  <code>edit_messages</code> / <code>edit</code>  — редактировать чужие\n"
        "  <code>delete_messages</code> / <code>delete</code> — удалять сообщения ✅\n"
        "  <code>ban_users</code>   / <code>ban</code>     — банить участников\n"
        "  <code>invite_users</code> / <code>invite</code> — приглашать ✅\n"
        "  <code>pin_messages</code> / <code>pin</code>    — закреплять\n"
        "  <code>add_admins</code>  / <code>add</code>     — назначать админов\n"
        "  <code>anonymous</code>   / <code>anon</code>    — писать от имени чата\n"
        "  <code>manage_call</code> / <code>call</code>    — управлять голосом ✅\n"
        "  <code>manage_topics</code> / <code>topics</code> — управлять темами\n"
        "  <code>other</code>                              — прочие права ✅\n"
        "  <code>all</code>                                — все сразу\n\n"
        "✅ = выдаётся по умолчанию\n"
        "<b>Должность:</b> <code>--rank Модератор</code> или <code>-rank Модератор</code>"
    ),
    "unadmin": (
        "🔻 <b>/unadmin</b> — снять с администратора\n\n"
        "<b>Использование:</b>\n"
        "  <code>/unadmin @user</code>\n"
        "  <code>/unadmin 123456789</code>\n"
        "  ответом на сообщение: <code>/unadmin</code>"
    ),
    "ban": (
        "🔨 <b>/ban</b> — заблокировать участника\n\n"
        "Если цель — администратор, сначала снимает с поста, затем банит.\n\n"
        "<b>Использование:</b>\n"
        "  <code>/ban @user</code>\n"
        "  <code>/ban 123456789</code>\n"
        "  ответом на сообщение: <code>/ban</code>"
    ),
    "unban": (
        "✅ <b>/unban</b> — разблокировать участника\n\n"
        "<b>Использование:</b>\n"
        "  <code>/unban @user</code>\n"
        "  <code>/unban 123456789</code>\n"
        "  ответом на сообщение: <code>/unban</code>"
    ),
    "kick": (
        "👢 <b>/kick</b> — выкинуть из чата (может вернуться по ссылке)\n\n"
        "Если цель — администратор, сначала снимает с поста, затем кикает.\n\n"
        "<b>Использование:</b>\n"
        "  <code>/kick @user</code>\n"
        "  <code>/kick 123456789</code>\n"
        "  ответом на сообщение: <code>/kick</code>"
    ),
    "mute": (
        "🔇 <b>/mute</b> — замутить участника или закрыть чат\n\n"
        "<b>Обычный мут:</b>\n"
        "  <code>/mute @user 30m</code>\n"
        "  <code>/mute @user 2h</code>\n"
        "  <code>/mute @user 1d</code>\n"
        "  <code>/mute @user навсегда</code>\n"
        "  Без времени — бот спросит в ЛС\n"
        "  Если цель — администратор: права сохраняются и возвращаются после мута\n\n"
        "<b>Режим --delete:</b>\n"
        "  <code>/mute @user 1h --delete</code>\n"
        "  Middleware удаляет сообщения, права не снимаются\n\n"
        "<b>Мут всего чата:</b>\n"
        "  <code>/mute all 30m</code> — закрывает чат, сохраняет права всех админов\n"
        "  <code>/mute all --delete</code> — удаляет сообщения всех кроме вас\n"
        "  <code>/unmute @user</code> — разрешить конкретному писать (в --delete режиме)\n"
        "  <code>/unmute all</code> — открыть чат и вернуть все права"
    ),
    "unmute": (
        "🔊 <b>/unmute</b> — снять мут\n\n"
        "<b>Использование:</b>\n"
        "  <code>/unmute @user</code> — снять мут с участника\n"
        "  <code>/unmute all</code> — открыть чат (после /mute all)\n\n"
        "В режиме <code>/mute all --delete</code>:\n"
        "  <code>/unmute @user</code> добавляет участника в whitelist — его сообщения больше не удаляются"
    ),
}


class AdminControlModule:
    """
    Управление правами участников через userbot-мост.

    ┌──────────────────────────────────────────────────────────────┐
    │  /admin  [цель] [+/-право ...] [--rank Должность]           │
    │  /unadmin [цель]                                            │
    │  /ban    [цель]                                             │
    │  /unban  [цель]                                             │
    │  /kick   [цель]                                             │
    │  /mute   [цель] [30m|2h|1d|навсегда] [--delete]            │
    └──────────────────────────────────────────────────────────────┘

    Права для /admin (по умолчанию: delete+invite+call+other):
      +/-change_info  +/-post    +/-edit   +/-delete
      +/-ban          +/-invite  +/-pin    +/-add_admins
      +/-anon         +/-call    +/-topics +/-other
      +/-all  (все сразу)

    --delete в /mute:
      Middleware удаляет все входящие сообщения пользователя.
      Права администратора НЕ снимаются.

    Обычный /mute:
      Если цель — админ: сохраняет права, снимает, мутит,
      после окончания — автоматически возвращает права и должность.
    """

    def __init__(self, name: str, bot_instance):
        self.name = name
        self.bot = bot_instance  # ModularBotMod
        self.dp = bot_instance.dp
        self.enabled = True
        self._data = {}

        # Ожидание ввода длительности: {user_id: pending_dict}
        self._pending_mute: Dict[int, dict] = {}

        # delete-мут: {chat_id: {user_id: until_ts_or_None}}
        self._delete_muted: Dict[int, Dict[int, Optional[float]]] = {}

        # all-мут: {chat_id: {old_perms, caller_id, until_ts, ...}}
        self._all_muted: Dict[int, dict] = {}

        # обычный MTProto-мут: {chat_id: {user_id: {name, until_ts}}}
        self._mtproto_muted: Dict[int, Dict[int, dict]] = {}

        self._middleware_registered = False

        # Путь к файлу данных
        self._data_path = "ModularBot/data/admin_control.json"

    # ── Персистентность ──────────────────────────────────────────────────────

    def _load_db(self) -> dict:
        """Читает файл данных. Возвращает пустой dict если файла нет."""
        import json, os
        if not os.path.exists(self._data_path):
            return {}
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"admin_control: ошибка чтения данных: {e}")
            return {}

    def _save_db(self, db: dict):
        """Сохраняет dict в файл данных."""
        import json, os
        os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
        try:
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"admin_control: ошибка записи данных: {e}")

    def _save_admin_rights(self, chat_id: int, user_id: int, rights_dict: dict, rank: str):
        """Сохраняет права снятого/кикнутого/забаненного админа в файл."""
        db = self._load_db()
        saved = db.setdefault("saved_admins", {})
        saved[f"{chat_id}:{user_id}"] = {"rights": rights_dict, "rank": rank}
        self._save_db(db)

    def _pop_saved_admin(self, chat_id: int, user_id: int) -> Optional[dict]:
        """Возвращает и удаляет сохранённые права админа из файла."""
        db = self._load_db()
        saved = db.get("saved_admins", {})
        key = f"{chat_id}:{user_id}"
        record = saved.pop(key, None)
        if record is not None:
            db["saved_admins"] = saved
            self._save_db(db)
        return record

    def _has_saved_admin(self, chat_id: int, user_id: int) -> bool:
        db = self._load_db()
        return f"{chat_id}:{user_id}" in db.get("saved_admins", {})

    def _save_mute_state(self):
        """Сохраняет текущее состояние мутов в файл."""
        db = self._load_db()

        # all_muted — сохраняем без old_perms (объект aiogram, не сериализуется)
        muted_chats = {}
        for chat_id, state in self._all_muted.items():
            entry = {
                "mode":         state.get("mode"),
                "caller_id":    state.get("caller_id"),
                "until_ts":     state.get("until_ts"),
                "saved_admins": state.get("saved_admins", {}),
                "whitelist":    list(state.get("whitelist", set())),
            }
            muted_chats[str(chat_id)] = entry

        # individual delete-mutes
        ind_mutes = {}
        for chat_id, users in self._delete_muted.items():
            ind_mutes[str(chat_id)] = {str(uid): ts for uid, ts in users.items()}

        # MTProto-муты
        mtproto = {}
        for chat_id, users in self._mtproto_muted.items():
            mtproto[str(chat_id)] = {str(uid): data for uid, data in users.items()}

        db["muted_chats"] = muted_chats
        db["individual_mutes"] = ind_mutes
        db["mtproto_mutes"] = mtproto
        self._save_db(db)

    def _restore_mute_state(self):
        """Восстанавливает состояние мутов из файла при загрузке."""
        db = self._load_db()
        now_ts = datetime.now(tz=timezone.utc).timestamp()

        # all_muted
        for chat_id_str, state in db.get("muted_chats", {}).items():
            chat_id = int(chat_id_str)
            until_ts = state.get("until_ts")
            if until_ts is not None and until_ts <= now_ts:
                continue  # истёк
            self._all_muted[chat_id] = {
                "mode":         state.get("mode"),
                "caller_id":    state.get("caller_id"),
                "until_ts":     until_ts,
                "saved_admins": state.get("saved_admins", {}),
                "whitelist":    set(state.get("whitelist", [])),
                "old_perms":    None,  # объект недоступен после рестарта
            }
            # Перезапускаем таймер если временный
            if until_ts:
                remaining = int(until_ts - now_ts)
                if remaining > 0:
                    asyncio.create_task(self._restore_all_later(chat_id, remaining))

        # individual delete-mutes
        for chat_id_str, users in db.get("individual_mutes", {}).items():
            chat_id = int(chat_id_str)
            active = {}
            for uid_str, ts in users.items():
                if ts is None or ts > now_ts:
                    active[int(uid_str)] = ts
            if active:
                self._delete_muted[chat_id] = active

        # MTProto-муты
        for chat_id_str, users in db.get("mtproto_mutes", {}).items():
            chat_id = int(chat_id_str)
            active = {}
            for uid_str, data in users.items():
                uid = int(uid_str)
                until_ts = data.get("until_ts")
                if until_ts is None or until_ts > now_ts:
                    active[uid] = data
            if active:
                self._mtproto_muted[chat_id] = active

        # Перезапускаем таймеры восстановления прав для индивидуальных мутов-
        # у которых есть сохранённые права (бот мог перезапуститься до истечения мута)
        saved_admins = db.get("saved_admins", {})
        for key, record in saved_admins.items():
            try:
                chat_id_s, uid_s = key.split(":", 1)
                chat_id = int(chat_id_s)
                uid = int(uid_s)
            except ValueError:
                continue

            # Ищем until_ts в mtproto_muted
            mute_data = self._mtproto_muted.get(chat_id, {}).get(uid)
            if not mute_data:
                continue
            until_ts = mute_data.get("until_ts")
            if until_ts is None:
                # Бессрочный мут — права никогда не вернутся автоматически,
                # поэтому запись сохраняем — /admin вернёт вручную
                continue
            if until_ts <= now_ts:
                # Мут уже истёк пока бот был выключен — восстанавливаем сразу
                rights_raw = record["rights"]
                rank = record["rank"]
                asyncio.create_task(
                    self._restore_admin_from_db(chat_id, uid, rights_raw, rank, delay=0)
                )
            else:
                remaining = int(until_ts - now_ts)
                rights_raw = record["rights"]
                rank = record["rank"]
                asyncio.create_task(
                    self._restore_admin_from_db(chat_id, uid, rights_raw, rank, delay=remaining)
                )

    # ── BotModule API ────────────────────────────────────────────────────────

    def get_info(self):
        return {
            "name": "AdminControl",
            "version": "2.0",
            "description": "admin / unadmin / ban / unban / kick / mute (--delete) через userbot",
        }

    def allows_admin_access(self) -> bool:
        return False

    def get_commands(self) -> dict:
        return {
            "/admin [цель] [all/+/-право] [--rank Должность]": "выдать права администратора  → /admin help",
            "/unadmin [цель]":                                 "снять с администратора        → /unadmin help",
            "/ban [цель]":                                     "снять с админа и забанить     → /ban help",
            "/unban [цель]":                                   "разблокировать                → /unban help",
            "/kick [цель]":                                    "снять с админа и выкинуть     → /kick help",
            "/mute [цель/all] [время] [--delete]":             "мут участника или чата        → /mute help",
            "/unmute [цель/all]":                              "снять мут                     → /unmute help",
            "/muted":                                          "список активных мутов в чате",
        }

    async def on_load(self):
        self._restore_mute_state()
        logger.info("AdminControl v2: загружен, состояние восстановлено")

    async def on_unload(self):
        self._save_mute_state()
        logger.info("AdminControl v2: выгружен, состояние сохранено")

    def get_all_data(self):
        return self._data.copy()

    def set_all_data(self, data):
        self._data = data.copy()

    # ── setup_handlers ───────────────────────────────────────────────────────

    async def setup_handlers(self):
        dp = self.dp

        # Один middleware на оба дела:
        # 1) delete-мут — удаляет сообщения после handler
        # 2) FSM ввод длительности — обрабатывает pending после handler
        # Оба пассивные: сначала handler, потом своя логика
        if not self._middleware_registered:
            module_ref = self

            from aiogram import BaseMiddleware
            from typing import Callable, Dict, Any, Awaitable

            class AdminControlMiddleware(BaseMiddleware):
                async def __call__(
                    self,
                    handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
                    event: types.Message,
                    data: Dict[str, Any],
                ) -> Any:
                    # СНАЧАЛА — все остальные обработчики
                    result = await handler(event, data)

                    if not isinstance(event, types.Message) or not event.from_user:
                        return result

                    chat_id = event.chat.id
                    user_id = event.from_user.id

                    # ── FSM: ввод длительности мута ──────────────────────────
                    # Только если есть pending для этого юзера и это не команда.
                    # Чат может быть любым — владелец может писать и из группы.
                    if (
                        user_id in module_ref._pending_mute
                        and event.text
                        and not event.text.startswith("/")
                    ):
                        await module_ref._handle_pending_duration(event)
                        return result

                    # ── delete-мут: индивидуальный ───────────────────────────
                    chat_dm = module_ref._delete_muted.get(chat_id)
                    if chat_dm and user_id in chat_dm:
                        until = chat_dm[user_id]
                        now_ts = datetime.now(tz=timezone.utc).timestamp()

                        if until is not None and until <= now_ts:
                            del chat_dm[user_id]
                            logger.info(f"DeleteMute истёк: uid={user_id} chat={chat_id}")
                        else:
                            try:
                                await event.delete()
                            except Exception as e:
                                logger.warning(f"DeleteMute: не удалось удалить: {e}")
                        return result

                    # ── delete-мут: /mute all --delete ────────────────────────
                    all_state = module_ref._all_muted.get(chat_id)
                    if all_state and all_state.get("mode") == "delete":
                        whitelist = all_state.get("whitelist", set())
                        until_ts = all_state.get("until_ts")
                        now_ts = datetime.now(tz=timezone.utc).timestamp()

                        # Время истекло — снимаем весь режим
                        if until_ts is not None and until_ts <= now_ts:
                            module_ref._all_muted.pop(chat_id, None)
                            logger.info(f"mute_all delete истёк: chat={chat_id}")
                            return result

                        # Боты и whitelist пропускаем
                        if event.from_user.is_bot or user_id in whitelist:
                            return result

                        try:
                            await event.delete()
                        except Exception as e:
                            logger.warning(f"mute_all delete uid={user_id}: {e}")

                    return result

            dp.message.middleware(AdminControlMiddleware())
            self._middleware_registered = True
            logger.info("AdminControl: AdminControlMiddleware зарегистрирован")

        @dp.message(Command("admin"))
        async def cmd_admin(message: types.Message):
            await self._handle_admin(message)

        @dp.message(Command("unadmin"))
        async def cmd_unadmin(message: types.Message):
            await self._handle_unadmin(message)

        @dp.message(Command("ban"))
        async def cmd_ban(message: types.Message):
            await self._handle_ban(message)

        @dp.message(Command("unban"))
        async def cmd_unban(message: types.Message):
            await self._handle_unban(message)

        @dp.message(Command("kick"))
        async def cmd_kick(message: types.Message):
            await self._handle_kick(message)

        @dp.message(Command("mute"))
        async def cmd_mute(message: types.Message):
            await self._handle_mute(message)

        @dp.message(Command("unmute"))
        async def cmd_unmute(message: types.Message):
            await self._handle_unmute(message)

        @dp.message(Command("muted"))
        async def cmd_muted(message: types.Message):
            await self._handle_muted(message)

        logger.info("AdminControl v2: хендлеры зарегистрированы")

    # ── Проверки ─────────────────────────────────────────────────────────────

    def _is_owner(self, uid: int) -> bool:
        """Владелец или СтАдмин — оба могут использовать AdminControl."""
        checker = getattr(self.bot, "is_owner_or_senior", None)
        if checker:
            return checker(uid)
        # Fallback если ModularBot старый — только владельцы
        return self.bot.is_owner(uid)

    async def _require_group(self, message: types.Message) -> Optional[int]:
        if message.chat.type in ("group", "supergroup", "channel"):
            return message.chat.id
        await message.reply("❌ Только в группах/каналах.")
        return None

    async def _check_help(self, message: types.Message, cmd: str) -> bool:
        """Если первый аргумент — 'help', отправляет справку и возвращает True."""
        parts = (message.text or "").split()
        if len(parts) >= 2 and parts[1].lower() == "help":
            text = _HELP_TEXTS.get(cmd, f"Справка для /{cmd} недоступна.")
            await message.answer(text, parse_mode="HTML")
            return True
        return False

    def _get_user_display(self, chat_id: int, user_id: int) -> str:
        """
        Возвращает читаемое имя пользователя.
        Ищет в ChatMembers если модуль загружен, иначе просто ID.
        """
        chat_members = self.bot.modules.get("ChatMembers")
        if chat_members:
            try:
                member = chat_members.get_member(chat_id, user_id)
                if member:
                    first = member.get("first_name", "")
                    last = member.get("last_name", "")
                    name = (first + " " + last).strip()
                    username = member.get("username", "")
                    if name and username:
                        return f"{name} (@{username})"
                    if name:
                        return name
                    if username:
                        return f"@{username}"
            except Exception:
                pass
        return str(user_id)

    # ════════════════════════════════════════════════════════════════════════
    # /admin
    # ════════════════════════════════════════════════════════════════════════

    def _parse_rights_args(self, parts: list):
        """
        Парсит список строк вида [+/-право, ..., --rank / -rank, Должность, ...].
        Возвращает (rights_dict, rank_str).

        Поддерживает:
          all / +all / -all        — все права вкл / вкл / выкл
          --rank Текст / -rank Текст
          +право / -право
        """
        rights = _DEFAULT_ADMIN_RIGHTS.copy()
        rank = ""
        i = 0
        while i < len(parts):
            part = parts[i]
            low = part.lower().lstrip("+-").lstrip("-")  # убираем -- и +/-

            # --rank или -rank
            if low == "rank":
                rank_parts = []
                i += 1
                while i < len(parts) and not parts[i].startswith(("-", "+")):
                    rank_parts.append(parts[i])
                    i += 1
                rank = " ".join(rank_parts)
                continue

            # all / +all / -all
            if low == "all":
                val = not part.startswith("-")  # "-all" → False, остальное → True
                for k in rights:
                    rights[k] = val
                i += 1
                continue

            # +право / -право
            if part and part[0] in ("+", "-"):
                sign = part[0]
                key_raw = part[1:].lower()
                canon = _RIGHT_ALIASES.get(key_raw)
                if canon:
                    rights[canon] = (sign == "+")
                else:
                    logger.warning(f"/admin: неизвестное право '{key_raw}'")

            i += 1

        return rights, rank

    async def _handle_admin(self, message: types.Message):
        if await self._check_help(message, "admin"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        # Аргументы после цели
        raw_parts = (message.text or "").split()[1:]
        skip = 0 if message.reply_to_message else 1
        rights_args = raw_parts[skip:]

        # ── Если нет аргументов и есть сохранённые права — восстанавливаем ───
        if not rights_args and self._has_saved_admin(chat_id, entity.id):
            record = self._pop_saved_admin(chat_id, entity.id)
            rights_raw = record["rights"]
            rank = record["rank"]
            try:
                tg_chat = await self.bot.userbot("get_entity", chat_id)
                user_entity = await self.bot.userbot("get_entity", entity.id)
                restored = ChatAdminRights(
                    change_info=     rights_raw.get("change_info")     or False,
                    post_messages=   rights_raw.get("post_messages")   or False,
                    edit_messages=   rights_raw.get("edit_messages")   or False,
                    delete_messages= rights_raw.get("delete_messages") or False,
                    ban_users=       rights_raw.get("ban_users")       or False,
                    invite_users=    rights_raw.get("invite_users")    or False,
                    pin_messages=    rights_raw.get("pin_messages")    or False,
                    add_admins=      rights_raw.get("add_admins")      or False,
                    anonymous=       rights_raw.get("anonymous")       or False,
                    manage_call=     rights_raw.get("manage_call")     or False,
                    other=           rights_raw.get("other")           or False,
                    manage_topics=   rights_raw.get("manage_topics")   or False,
                )
                await self.bot.raw(
                    EditAdminRequest(
                        channel=tg_chat,
                        user_id=user_entity,
                        admin_rights=restored,
                        rank=rank,
                    )
                )
                name = _user_name(entity)
                rank_line = f"\n📌 <b>Должность:</b> {rank}" if rank else ""
                rights_text = "\n".join(
                    f"  {'✅' if v else '❌'} {_RIGHT_LABELS.get(k, k)}"
                    for k, v in rights_raw.items()
                    if k in _RIGHT_LABELS
                )
                await message.reply(
                    f"👑 <b>{name}</b> — права восстановлены из сохранения{rank_line}\n\n"
                    f"<b>Права:</b>\n{rights_text}",
                    parse_mode="HTML",
                )
                logger.info(f"admin restore: uid={entity.id} chat={chat_id} rank={rank!r}")
            except Exception as e:
                await message.reply(f"❌ Ошибка при восстановлении: {e}")
                logger.error(f"admin restore error: {e}")
            return

        # ── Обычное назначение по аргументам ─────────────────────────────────
        rights, rank = self._parse_rights_args(rights_args)

        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)

            await self.bot.raw(
                EditAdminRequest(
                    channel=tg_chat,
                    user_id=entity,
                    admin_rights=ChatAdminRights(
                        change_info=rights["change_info"],
                        post_messages=rights["post_messages"],
                        edit_messages=rights["edit_messages"],
                        delete_messages=rights["delete_messages"],
                        ban_users=rights["ban_users"],
                        invite_users=rights["invite_users"],
                        pin_messages=rights["pin_messages"],
                        add_admins=rights["add_admins"],
                        anonymous=rights["anonymous"],
                        manage_call=rights["manage_call"],
                        other=rights["other"],
                        manage_topics=rights["manage_topics"],
                    ),
                    rank=rank,
                )
            )

            name = _user_name(entity)
            rank_line = f"\n📌 <b>Должность:</b> {rank}" if rank else ""
            rights_text = "\n".join(
                f"  {'✅' if v else '❌'} {_RIGHT_LABELS.get(k, k)}"
                for k, v in rights.items()
            )

            await message.reply(
                f"👑 <b>{name}</b> назначен администратором{rank_line}\n\n"
                f"<b>Права:</b>\n{rights_text}",
                parse_mode="HTML",
            )
            logger.info(f"admin: uid={entity.id} chat={chat_id} rank={rank!r}")

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"admin error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # /unadmin
    # ════════════════════════════════════════════════════════════════════════

    async def _handle_unadmin(self, message: types.Message):
        if await self._check_help(message, "unadmin"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            await _strip_admin(self.bot, tg_chat, entity)
            name = _user_name(entity)
            # Удаляем сохранённые права — снятие с должности явное, восстанавливать не нужно
            if self._has_saved_admin(chat_id, entity.id):
                self._pop_saved_admin(chat_id, entity.id)
            await message.reply(f"🔻 <b>{name}</b> снят с администратора.", parse_mode="HTML")
            logger.info(f"unadmin: uid={entity.id} chat={chat_id}")
        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"unadmin error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # /ban — снять с админа (если был) и заблокировать
    # ════════════════════════════════════════════════════════════════════════

    async def _handle_ban(self, message: types.Message):
        if await self._check_help(message, "ban"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            name = _user_name(entity)

            admin_rights, admin_rank = await _get_admin_rights(self.bot, chat_id, entity)
            was_admin = admin_rights is not None
            if was_admin:
                # Сохраняем права в файл перед снятием
                r = admin_rights
                self._save_admin_rights(chat_id, entity.id, {
                    "change_info":     r.change_info,
                    "post_messages":   r.post_messages,
                    "edit_messages":   r.edit_messages,
                    "delete_messages": r.delete_messages,
                    "ban_users":       r.ban_users,
                    "invite_users":    r.invite_users,
                    "pin_messages":    r.pin_messages,
                    "add_admins":      r.add_admins,
                    "anonymous":       r.anonymous,
                    "manage_call":     r.manage_call,
                    "other":           r.other,
                    "manage_topics":   getattr(r, "manage_topics", False),
                }, admin_rank)
                await _strip_admin(self.bot, tg_chat, entity)

            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(
                        until_date=None,
                        view_messages=True,
                        send_messages=True,
                        send_media=True,
                        send_stickers=True,
                        send_gifs=True,
                        send_games=True,
                        send_inline=True,
                        embed_links=True,
                    ),
                )
            )

            suffix = " <i>(снят с админки, права сохранены)</i>" if was_admin else ""
            await message.reply(f"🔨 <b>{name}</b> заблокирован.{suffix}", parse_mode="HTML")
            logger.info(f"ban: uid={entity.id} chat={chat_id} was_admin={was_admin}")

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"ban error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # /unban
    # ════════════════════════════════════════════════════════════════════════

    async def _handle_unban(self, message: types.Message):
        if await self._check_help(message, "unban"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(
                        until_date=None,
                        view_messages=False,
                        send_messages=False,
                        send_media=False,
                        send_stickers=False,
                        send_gifs=False,
                        send_games=False,
                        send_inline=False,
                        embed_links=False,
                    ),
                )
            )
            name = _user_name(entity)
            await message.reply(f"✅ <b>{name}</b> разблокирован.", parse_mode="HTML")
            logger.info(f"unban: uid={entity.id} chat={chat_id}")

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"unban error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # /kick — снять с админа (если был) и выкинуть
    # ════════════════════════════════════════════════════════════════════════

    async def _handle_kick(self, message: types.Message):
        if await self._check_help(message, "kick"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            name = _user_name(entity)

            admin_rights, admin_rank = await _get_admin_rights(self.bot, chat_id, entity)
            was_admin = admin_rights is not None
            if was_admin:
                # Сохраняем права в файл перед снятием
                r = admin_rights
                self._save_admin_rights(chat_id, entity.id, {
                    "change_info":     r.change_info,
                    "post_messages":   r.post_messages,
                    "edit_messages":   r.edit_messages,
                    "delete_messages": r.delete_messages,
                    "ban_users":       r.ban_users,
                    "invite_users":    r.invite_users,
                    "pin_messages":    r.pin_messages,
                    "add_admins":      r.add_admins,
                    "anonymous":       r.anonymous,
                    "manage_call":     r.manage_call,
                    "other":           r.other,
                    "manage_topics":   getattr(r, "manage_topics", False),
                }, admin_rank)
                await _strip_admin(self.bot, tg_chat, entity)

            # Кик = бан + немедленный разбан (может вернуться по ссылке)
            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(until_date=None, view_messages=True),
                )
            )
            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(until_date=None, view_messages=False),
                )
            )

            suffix = " <i>(снят с админки, права сохранены)</i>" if was_admin else ""
            await message.reply(f"👢 <b>{name}</b> выкинут.{suffix}", parse_mode="HTML")
            logger.info(f"kick: uid={entity.id} chat={chat_id} was_admin={was_admin}")

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"kick error: {e}")

    # ── /muted ────────────────────────────────────────────────────────────────

    async def _handle_muted(self, message: types.Message):
        """Показывает кто сейчас замучен в этом чате."""
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        lines = []
        now_ts = datetime.now(tz=timezone.utc).timestamp()

        # ── /mute all (обычный или --delete) ─────────────────────────────────
        all_state = self._all_muted.get(chat_id)
        if all_state:
            mode = all_state.get("mode")
            until_ts = all_state.get("until_ts")
            caller_id = all_state.get("caller_id")

            if until_ts and until_ts <= now_ts:
                # Истёк — чистим
                self._all_muted.pop(chat_id, None)
            else:
                until_str = (
                    datetime.fromtimestamp(until_ts, tz=timezone(timedelta(hours=3))).strftime("%d.%m %H:%M МСК")
                    if until_ts else "навсегда"
                )
                if mode == "delete":
                    whitelist = all_state.get("whitelist", set())
                    wl_str = ", ".join(f"<code>{uid}</code>" for uid in whitelist if uid != caller_id)
                    lines.append(
                        f"🗑 <b>Режим удаления</b> до {until_str}\n"
                        f"   Whitelist (могут писать): {wl_str or 'никого'}"
                    )
                else:
                    saved = all_state.get("saved_admins", {})
                    lines.append(
                        f"🔇 <b>Чат закрыт</b> до {until_str}\n"
                        f"   Сохранено прав: {len(saved)} админов"
                    )

        # ── Индивидуальные delete-муты ────────────────────────────────────────
        chat_dm = self._delete_muted.get(chat_id, {})
        expired = []
        for uid, until in chat_dm.items():
            if until is not None and until <= now_ts:
                expired.append(uid)
            else:
                until_str = (
                    datetime.fromtimestamp(until, tz=timezone(timedelta(hours=3))).strftime("%d.%m %H:%M МСК")
                    if until else "навсегда"
                )
                display = self._get_user_display(chat_id, uid)
                lines.append(f"🗑 <b>{display}</b> — удаление сообщений до {until_str}")
        for uid in expired:
            del chat_dm[uid]

        # ── Обычные MTProto-муты ──────────────────────────────────────────────
        chat_mm = self._mtproto_muted.get(chat_id, {})
        expired_mm = []
        for uid, data in chat_mm.items():
            until_ts_m = data.get("until_ts")
            if until_ts_m is not None and until_ts_m <= now_ts:
                expired_mm.append(uid)
            else:
                until_str = (
                    datetime.fromtimestamp(until_ts_m, tz=timezone(timedelta(hours=3))).strftime("%d.%m %H:%M МСК")
                    if until_ts_m else "навсегда"
                )
                display = self._get_user_display(chat_id, uid) or data.get("name") or str(uid)
                lines.append(f"🔇 <b>{display}</b> — до {until_str}")
        for uid in expired_mm:
            del chat_mm[uid]
        for uid in expired_mm:
            del chat_mm[uid]

        if not lines:
            await message.reply("✅ Нет активных мутов в этом чате.", parse_mode="HTML")
            return

        header = f"🔇 <b>Активные муты в чате:</b>\n\n"
        await message.reply(header + "\n".join(lines), parse_mode="HTML")

    # ── /unmute ───────────────────────────────────────────────────────────────

    async def _handle_unmute(self, message: types.Message):
        if await self._check_help(message, "unmute"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        raw_parts = (message.text or "").split()[1:]
        first_arg = raw_parts[0].lower() if raw_parts and not raw_parts[0].startswith("--") else None

        # ── /unmute all ──────────────────────────────────────────────────────
        if first_arg == "all":
            state = self._all_muted.get(chat_id)
            if not state:
                await message.reply("ℹ️ Чат не закрыт командой /mute all.")
                return
            await self._unmute_all_chat(chat_id)
            await message.reply("🔊 <b>Чат открыт.</b>", parse_mode="HTML")
            return

        # ── /unmute @user — разрешить конкретному юзеру писать ───────────────
        # Работает в обоих режимах: MTProto-мут и --delete-мут
        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        name = _user_name(entity)
        actions = []

        # 1) Если активен --delete режим на чате — добавляем в whitelist
        state = self._all_muted.get(chat_id)
        if state and state.get("mode") == "delete":
            state["whitelist"].add(entity.id)
            actions.append("добавлен в whitelist")

        # 2) Убираем из индивидуального delete_muted
        chat_dm = self._delete_muted.get(chat_id, {})
        if entity.id in chat_dm:
            del chat_dm[entity.id]
            actions.append("снят delete-мут")

        # 3) Снимаем MTProto-мут (на случай обычного /mute)
        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(
                        until_date=None,
                        view_messages=False,
                        send_messages=False,
                        send_media=False,
                        send_stickers=False,
                        send_gifs=False,
                        send_games=False,
                        send_inline=False,
                        embed_links=False,
                    ),
                )
            )
            actions.append("снят MTProto-мут")
        except Exception as e:
            logger.warning(f"unmute MTProto: {e}")

        await message.reply(f"🔊 <b>{name}</b> размучен.", parse_mode="HTML")
        # Убираем из MTProto-трекера
        self._mtproto_muted.get(chat_id, {}).pop(entity.id, None)

        # Досрочный размут — восстанавливаем права и удаляем запись
        if self._has_saved_admin(chat_id, entity.id):
            record = self._pop_saved_admin(chat_id, entity.id)
            rights_raw = record["rights"]
            rank = record["rank"]
            try:
                tg_chat_r = await self.bot.userbot("get_entity", chat_id)
                user_entity = await self.bot.userbot("get_entity", entity.id)
                restored = ChatAdminRights(
                    change_info=     rights_raw.get("change_info")     or False,
                    post_messages=   rights_raw.get("post_messages")   or False,
                    edit_messages=   rights_raw.get("edit_messages")   or False,
                    delete_messages= rights_raw.get("delete_messages") or False,
                    ban_users=       rights_raw.get("ban_users")       or False,
                    invite_users=    rights_raw.get("invite_users")    or False,
                    pin_messages=    rights_raw.get("pin_messages")    or False,
                    add_admins=      rights_raw.get("add_admins")      or False,
                    anonymous=       rights_raw.get("anonymous")       or False,
                    manage_call=     rights_raw.get("manage_call")     or False,
                    other=           rights_raw.get("other")           or False,
                    manage_topics=   rights_raw.get("manage_topics")   or False,
                )
                await self.bot.raw(
                    EditAdminRequest(
                        channel=tg_chat_r,
                        user_id=user_entity,
                        admin_rights=restored,
                        rank=rank,
                    )
                )
                rank_line = f" (должность: {rank})" if rank else ""
                await message.reply(
                    f"👑 <b>{name}</b> — права администратора восстановлены{rank_line}.",
                    parse_mode="HTML"
                )
                logger.info(f"unmute restore admin: uid={entity.id} chat={chat_id}")
            except Exception as e:
                logger.error(f"unmute restore admin error: {e}")

        logger.info(f"unmute: uid={entity.id} chat={chat_id} actions={actions}")
        self._save_mute_state()

    # ── /mute ─────────────────────────────────────────────────────────────────

    async def _handle_mute(self, message: types.Message):
        if await self._check_help(message, "mute"): return
        if not self._is_owner(message.from_user.id):
            return

        chat_id = await self._require_group(message)
        if not chat_id:
            return

        raw_parts = (message.text or "").split()[1:]
        flags = {p.lower() for p in raw_parts if p.startswith("--")}
        pos_args = [p for p in raw_parts if not p.startswith("--")]
        delete_mode = "--delete" in flags

        first_arg = pos_args[0].lower() if pos_args else None

        # ── /mute all [время] [--delete] ─────────────────────────────────────
        if first_arg == "all":
            dur_str = pos_args[1] if len(pos_args) > 1 else None

            if dur_str:
                if dur_str.lower() in ("навсегда", "forever", "perma", "0"):
                    seconds = None
                else:
                    seconds = _parse_duration(dur_str)
                    if seconds is None:
                        await message.reply(
                            "⚠️ Не понял длительность.\n"
                            "Формат: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                            parse_mode="HTML",
                        )
                        return
                await self._apply_mute_all(message, chat_id, seconds, delete_mode)
            else:
                self._pending_mute[message.from_user.id] = {
                    "chat_id": chat_id,
                    "entity": None,
                    "delete_mode": delete_mode,
                }
                hint = " (режим удаления)" if delete_mode else ""
                try:
                    await self.bot.bot.send_message(
                        message.from_user.id,
                        f"⏱ На сколько закрыть чат{hint}?\n"
                        "Введи: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                        parse_mode="HTML",
                    )
                    if message.chat.type != "private":
                        await message.reply("📩 Ответь в ЛС боту.")
                except Exception:
                    await message.reply(
                        f"⏱ На сколько закрыть чат{hint}?\n"
                        "Введи: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                        parse_mode="HTML",
                    )
            return

        # ── Обычный /mute [цель] [время] [--delete] ──────────────────────────
        entity, err = await _resolve_target(self.bot, message)
        if err:
            await message.reply(err)
            return

        # Длительность — первый pos_arg если не реплай, или всё равно второй
        dur_index = 0 if message.reply_to_message else 1
        dur_str = pos_args[dur_index] if len(pos_args) > dur_index else None

        if dur_str:
            if dur_str.lower() in ("навсегда", "forever", "perma", "0"):
                seconds = None
            else:
                seconds = _parse_duration(dur_str)
                if seconds is None:
                    await message.reply(
                        "⚠️ Не понял длительность.\n"
                        "Формат: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                        parse_mode="HTML",
                    )
                    return
            await self._apply_mute(message, chat_id, entity, seconds, delete_mode)
        else:
            self._pending_mute[message.from_user.id] = {
                "chat_id": chat_id,
                "entity": entity,
                "delete_mode": delete_mode,
            }
            hint = " (удаление сообщений)" if delete_mode else ""
            # Спрашиваем в ЛС — чтобы ответ не перехватили другие хендлеры в группе
            try:
                await self.bot.bot.send_message(
                    message.from_user.id,
                    f"⏱ На сколько замутить{hint}?\n"
                    "Введи: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                    parse_mode="HTML",
                )
                if message.chat.type != "private":
                    await message.reply("📩 Ответь в ЛС боту.")
            except Exception:
                # Если ЛС недоступно — спрашиваем прямо здесь
                await message.reply(
                    f"⏱ На сколько замутить{hint}?\n"
                    "Введи: <code>30m</code> / <code>2h</code> / <code>1d</code> / <code>навсегда</code>",
                    parse_mode="HTML",
                )

    async def _handle_pending_duration(self, message: types.Message):
        uid = message.from_user.id
        pending = self._pending_mute.pop(uid, None)
        if not pending:
            return

        text = (message.text or "").strip().lower()
        if text in ("навсегда", "forever", "perma", "0"):
            seconds = None
        else:
            seconds = _parse_duration(text)
            if seconds is None:
                await message.reply(
                    "⚠️ Не понял. Попробуй снова: <code>/mute</code>",
                    parse_mode="HTML",
                )
                return

        if pending["entity"] is None:
            await self._apply_mute_all(message, pending["chat_id"], seconds, pending["delete_mode"])
        else:
            await self._apply_mute(
                message,
                pending["chat_id"],
                pending["entity"],
                seconds,
                pending["delete_mode"],
            )

    # ── /mute all — логика ────────────────────────────────────────────────────

    async def _apply_mute_all(
        self,
        message: types.Message,
        chat_id: int,
        seconds: Optional[int],
        delete_mode: bool,
    ):
        dur_text = _fmt_duration(seconds) if seconds else "навсегда"
        caller_id = message.from_user.id
        until_ts = None
        if seconds:
            until_ts = (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).timestamp()

        # ── Режим --delete: Middleware удаляет сообщения всех кроме whitelist ─
        if delete_mode:
            self._all_muted[chat_id] = {
                "mode": "delete",
                "caller_id": caller_id,
                "until_ts": until_ts,
                "whitelist": {caller_id},
            }
            time_line = f"на {dur_text}" if seconds else "навсегда"
            await message.reply(
                f"🗑 <b>Режим удаления сообщений включён {time_line}.</b>\n"
                f"<i>Сообщения всех кроме вас удаляются автоматически.\n"
                f"/unmute @user — разрешить конкретному участнику.</i>",
                parse_mode="HTML",
            )
            logger.info(f"mute_all delete: chat={chat_id} seconds={seconds} caller={caller_id}")
            self._save_mute_state()
            if seconds:
                asyncio.create_task(self._restore_all_later(chat_id, seconds))
            return

        # ── Обычный режим: Bot API закрывает чат, права админов сохраняем ────
        try:
            # 1) Сохраняем текущие дефолтные права чата для восстановления
            chat_info = await self.bot.bot.get_chat(chat_id)
            old_perms = chat_info.permissions

            # 2) Сохраняем права всех текущих админов через MTProto
            #    Используем фильтр ChannelParticipantsAdmins — сразу только админы,
            #    данные участника уже содержат admin_rights без доп. запросов
            saved_admins = {}  # {user_id: {"rights": dict, "rank": str}}
            tg_chat = await self.bot.userbot("get_entity", chat_id)

            from telethon.tl.types import ChannelParticipantAdmin
            from telethon.tl.functions.channels import GetParticipantsRequest
            from telethon.tl.types import ChannelParticipantsAdmins, InputChannelFromMessage

            async for user in self.bot.client.iter_participants(
                tg_chat, filter=ChannelParticipantsAdmins()
            ):
                if user.bot or user.id == caller_id:
                    continue
                try:
                    from telethon.tl.functions.channels import GetParticipantRequest
                    result = await self.bot.raw(
                        GetParticipantRequest(channel=tg_chat, participant=user)
                    )
                    p = result.participant
                    if isinstance(p, ChannelParticipantAdmin):
                        r = p.admin_rights
                        saved_admins[user.id] = {
                            "rights": {
                                "change_info":     r.change_info,
                                "post_messages":   r.post_messages,
                                "edit_messages":   r.edit_messages,
                                "delete_messages": r.delete_messages,
                                "ban_users":       r.ban_users,
                                "invite_users":    r.invite_users,
                                "pin_messages":    r.pin_messages,
                                "add_admins":      r.add_admins,
                                "anonymous":       r.anonymous,
                                "manage_call":     r.manage_call,
                                "other":           r.other,
                            },
                            "rank": p.rank or "",
                        }
                        logger.info(f"mute_all: сохранены права admin uid={user.id} rank='{p.rank}'")
                        # Снимаем с поста — иначе set_chat_permissions их не затронет
                        await _strip_admin(self.bot, tg_chat, user)
                        await asyncio.sleep(0.5)  # даём Telegram применить изменение
                except Exception as e:
                    logger.warning(f"mute_all: get_participant {user.id}: {e}")

            # 3) Сохраняем состояние
            self._all_muted[chat_id] = {
                "mode": "mute",
                "caller_id": caller_id,
                "until_ts": until_ts,
                "old_perms": old_perms,
                "saved_admins": saved_admins,
            }

            # 4) Закрываем чат — только send_messages=False, остальное не трогаем
            await self.bot.bot.set_chat_permissions(
                chat_id,
                types.ChatPermissions(can_send_messages=False),
            )

            time_line = f"на {dur_text}" if seconds else "навсегда"
            admin_note = f", сохранено прав: {len(saved_admins)} админов" if saved_admins else ""
            await message.reply(
                f"🔇 <b>Чат закрыт {time_line}.</b>{admin_note}\n"
                f"<i>Писать можете только вы и администраторы.</i>",
                parse_mode="HTML",
            )
            logger.info(f"mute_all: chat={chat_id} seconds={seconds} admins_saved={len(saved_admins)}")
            self._save_mute_state()

            if seconds:
                asyncio.create_task(self._restore_all_later(chat_id, seconds))

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"mute_all error: {e}")

    async def _restore_all_later(self, chat_id: int, delay: int):
        await asyncio.sleep(delay)
        await self._unmute_all_chat(chat_id)
        logger.info(f"mute_all автоснят: chat={chat_id}")

    async def _unmute_all_chat(self, chat_id: int):
        """Восстанавливает состояние чата после /mute all."""
        state = self._all_muted.pop(chat_id, None)
        if not state:
            return

        mode = state.get("mode")

        # ── delete-режим: просто снимаем запись, Middleware перестанет удалять
        if mode == "delete":
            logger.info(f"mute_all delete снят: chat={chat_id}")
            self._save_mute_state()
            return

        # ── mute-режим: восстанавливаем Bot API права и права каждого админа
        if mode == "mute":
            # 1) Восстанавливаем дефолтные права чата
            #    Явно выставляем can_send_messages=True — гарантированно открываем чат
            old_perms = state.get("old_perms")
            try:
                if old_perms:
                    # Копируем старые права но с can_send_messages=True
                    await self.bot.bot.set_chat_permissions(
                        chat_id,
                        types.ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=getattr(old_perms, "can_send_media_messages", True),
                            can_send_polls=getattr(old_perms, "can_send_polls", True),
                            can_send_other_messages=getattr(old_perms, "can_send_other_messages", True),
                            can_add_web_page_previews=getattr(old_perms, "can_add_web_page_previews", True),
                            can_change_info=getattr(old_perms, "can_change_info", False),
                            can_invite_users=getattr(old_perms, "can_invite_users", True),
                            can_pin_messages=getattr(old_perms, "can_pin_messages", False),
                        ),
                    )
                else:
                    # old_perms не сохранились — просто открываем
                    await self.bot.bot.set_chat_permissions(
                        chat_id,
                        types.ChatPermissions(can_send_messages=True),
                    )
                logger.info(f"mute_all: права чата восстановлены chat={chat_id}")
            except Exception as e:
                logger.error(f"restore chat perms chat={chat_id}: {e}")

            # 2) Восстанавливаем права каждого сохранённого админа
            saved_admins = state.get("saved_admins", {})
            if not saved_admins:
                return

            tg_chat = await self.bot.userbot("get_entity", chat_id)

            for uid, data in saved_admins.items():
                rights_raw = data["rights"]
                rank = data["rank"]
                try:
                    user_entity = await self.bot.userbot("get_entity", uid)
                    restored = ChatAdminRights(
                        change_info=     rights_raw.get("change_info")     or False,
                        post_messages=   rights_raw.get("post_messages")   or False,
                        edit_messages=   rights_raw.get("edit_messages")   or False,
                        delete_messages= rights_raw.get("delete_messages") or False,
                        ban_users=       rights_raw.get("ban_users")       or False,
                        invite_users=    rights_raw.get("invite_users")    or False,
                        pin_messages=    rights_raw.get("pin_messages")    or False,
                        add_admins=      rights_raw.get("add_admins")      or False,
                        anonymous=       rights_raw.get("anonymous")       or False,
                        manage_call=     rights_raw.get("manage_call")     or False,
                        other=           rights_raw.get("other")           or False,
                        manage_topics=   False,
                    )
                    await self.bot.raw(
                        EditAdminRequest(
                            channel=tg_chat,
                            user_id=user_entity,
                            admin_rights=restored,
                            rank=rank,
                        )
                    )
                    logger.info(f"mute_all: права admin uid={uid} восстановлены rank='{rank}'")
                except Exception as e:
                    logger.warning(f"restore admin uid={uid}: {e}")

            self._save_mute_state()

    async def _apply_mute(
        self,
        message: types.Message,
        chat_id: int,
        entity,
        seconds: Optional[int],
        delete_mode: bool,
    ):
        name = _user_name(entity)
        dur_text = _fmt_duration(seconds) if seconds else "навсегда"

        # ── Режим --delete: только Middleware, права не трогаем ──────────────
        if delete_mode:
            until_ts = None
            if seconds:
                until_ts = (
                    datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)
                ).timestamp()

            if chat_id not in self._delete_muted:
                self._delete_muted[chat_id] = {}
            self._delete_muted[chat_id][entity.id] = until_ts

            await message.reply(
                f"🗑 <b>{name}</b> — удаление сообщений на {dur_text}.\n"
                f"<i>Права администратора не изменены.</i>",
                parse_mode="HTML",
            )
            logger.info(f"delete-mute: uid={entity.id} chat={chat_id} seconds={seconds}")
            self._save_mute_state()
            return

        # ── Обычный мут ──────────────────────────────────────────────────────
        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)

            # Проверяем и сохраняем права если админ
            admin_rights, admin_rank = await _get_admin_rights(self.bot, chat_id, entity)
            was_admin = admin_rights is not None

            if was_admin:
                await _strip_admin(self.bot, tg_chat, entity)
                r = admin_rights
                self._save_admin_rights(chat_id, entity.id, {
                    "change_info":     r.change_info,
                    "post_messages":   r.post_messages,
                    "edit_messages":   r.edit_messages,
                    "delete_messages": r.delete_messages,
                    "ban_users":       r.ban_users,
                    "invite_users":    r.invite_users,
                    "pin_messages":    r.pin_messages,
                    "add_admins":      r.add_admins,
                    "anonymous":       r.anonymous,
                    "manage_call":     r.manage_call,
                    "other":           r.other,
                    "manage_topics":   getattr(r, "manage_topics", False),
                }, admin_rank)
            if seconds:
                until = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)

            await self.bot.raw(
                EditBannedRequest(
                    channel=tg_chat,
                    participant=entity,
                    banned_rights=ChatBannedRights(
                        until_date=until,
                        send_messages=True,
                        send_media=True,
                        send_stickers=True,
                        send_gifs=True,
                        send_games=True,
                        send_inline=True,
                        embed_links=True,
                    ),
                )
            )

            if was_admin and seconds:
                note = "\n<i>⏳ Права администратора вернутся после мута.</i>"
            elif was_admin and not seconds:
                note = "\n<i>Права администратора сняты навсегда.</i>"
            else:
                note = ""

            await message.reply(
                f"🔇 <b>{name}</b> замучен на {dur_text}.{note}",
                parse_mode="HTML",
            )
            logger.info(f"mute: uid={entity.id} chat={chat_id} seconds={seconds} was_admin={was_admin}")

            # Записываем в трекер для /muted
            until_ts = until.timestamp() if until else None
            if chat_id not in self._mtproto_muted:
                self._mtproto_muted[chat_id] = {}
            self._mtproto_muted[chat_id][entity.id] = {
                "name": name,
                "until_ts": until_ts,
            }
            self._save_mute_state()

            # Восстановление прав после мута
            if was_admin and seconds:
                asyncio.create_task(
                    self._restore_admin_later(
                        chat_id=chat_id,
                        entity_id=entity.id,
                        rights=admin_rights,
                        rank=admin_rank,
                        delay=seconds,
                    )
                )

        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
            logger.error(f"mute error: {e}")

    async def _restore_admin_from_db(
        self,
        chat_id: int,
        entity_id: int,
        rights_raw: dict,
        rank: str,
        delay: int,
    ):
        """
        Восстанавливает права администратора из сырого dict (после рестарта бота).
        Вызывается из _restore_mute_state вместо _restore_admin_later,
        когда объект ChatAdminRights уже недоступен (потерян при рестарте).
        """
        if delay > 0:
            await asyncio.sleep(delay)

        # Если запись уже удалена — права восстановили досрочно (/unmute) или вручную (/admin)
        if not self._has_saved_admin(chat_id, entity_id):
            self._mtproto_muted.get(chat_id, {}).pop(entity_id, None)
            self._save_mute_state()
            return
        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            entity = await self.bot.userbot("get_entity", entity_id)
            restored = ChatAdminRights(
                change_info=     rights_raw.get("change_info")     or False,
                post_messages=   rights_raw.get("post_messages")   or False,
                edit_messages=   rights_raw.get("edit_messages")   or False,
                delete_messages= rights_raw.get("delete_messages") or False,
                ban_users=       rights_raw.get("ban_users")       or False,
                invite_users=    rights_raw.get("invite_users")    or False,
                pin_messages=    rights_raw.get("pin_messages")    or False,
                add_admins=      rights_raw.get("add_admins")      or False,
                anonymous=       rights_raw.get("anonymous")       or False,
                manage_call=     rights_raw.get("manage_call")     or False,
                other=           rights_raw.get("other")           or False,
                manage_topics=   rights_raw.get("manage_topics")   or False,
            )
            await self.bot.raw(
                EditAdminRequest(
                    channel=tg_chat,
                    user_id=entity,
                    admin_rights=restored,
                    rank=rank,
                )
            )
            logger.info(f"Права восстановлены (после рестарта): uid={entity_id} chat={chat_id}")
            self._pop_saved_admin(chat_id, entity_id)
            self._mtproto_muted.get(chat_id, {}).pop(entity_id, None)
            self._save_mute_state()
        except Exception as e:
            logger.error(f"Ошибка восстановления прав (после рестарта) uid={entity_id}: {e}")

    async def _restore_admin_later(
        self,
        chat_id: int,
        entity_id: int,
        rights: ChatAdminRights,
        rank: str,
        delay: int,
    ):
        """Ждёт delay секунд, затем восстанавливает права администратора."""
        await asyncio.sleep(delay)
        # Если запись уже удалена — значит права восстановили досрочно или сняли с должности
        if not self._has_saved_admin(chat_id, entity_id):
            logger.info(f"_restore_admin_later: запись уже удалена uid={entity_id}, пропускаем")
            self._mtproto_muted.get(chat_id, {}).pop(entity_id, None)
            self._save_mute_state()
            return
        try:
            tg_chat = await self.bot.userbot("get_entity", chat_id)
            entity = await self.bot.userbot("get_entity", entity_id)
            await self.bot.raw(
                EditAdminRequest(
                    channel=tg_chat,
                    user_id=entity,
                    admin_rights=rights,
                    rank=rank,
                )
            )
            logger.info(f"Права восстановлены: uid={entity_id} chat={chat_id}")
            # Удаляем запись из файла
            self._pop_saved_admin(chat_id, entity_id)
            self._mtproto_muted.get(chat_id, {}).pop(entity_id, None)
            self._save_mute_state()
        except Exception as e:
            logger.error(f"Ошибка восстановления прав uid={entity_id}: {e}")

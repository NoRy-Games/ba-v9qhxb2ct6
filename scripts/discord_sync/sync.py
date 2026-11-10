"""Idempotent Discord publishing, with credentials kept only in process memory.

The renderer is deliberately separate. --check is a pure render/validation pass;
--dry-run performs only Discord GET requests; --apply never deletes messages or
changes channels, roles, or permission overwrites.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

API_ROOT = "https://discord.com/api/v10"
CHANNEL_ALIASES = ("changelog", "characters", "balance")
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_MESSAGE_HISTORY = 1 << 16
EMBED_LINKS = 1 << 14
PIN_MESSAGES = 1 << 51
ADMINISTRATOR = 1 << 3
SUPPRESS_EMBEDS = 1 << 2
SUPPRESS_NOTIFICATIONS = 1 << 12
KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")
SNOWFLAKE_PATTERN = re.compile(r"[0-9]{1,22}\Z")
GITHUB_LINK = re.compile(r"https?://(?:www\.)?(?:github\.com|raw\.githubusercontent\.com|api\.github\.com)(?:[/:]|$)", re.I)
PORTUGUESE_COPY = re.compile(r"\b(?:personagens|habilidades|atualizações|alterações|duração|próximo|referência|administradores|inalterado|comparação)\b|\bFonte\s*:", re.I)


class SyncError(Exception):
    """A safe, actionable error whose text contains no credential or API body."""


class ApiError(SyncError):
    def __init__(self, status: int | str, method: str = "GET", path: str = "/", code: int | None = None):
        self.status = status
        self.method = method
        self.path = path
        self.code = code
        suffix = f"; Discord code {code}" if isinstance(code, int) else ""
        super().__init__(f"Discord {method} {path}: HTTP {status}{suffix}")


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SNOWFLAKE_PATTERN.fullmatch(value):
        raise SyncError(f"Invalid {label}; IDs must be decimal strings.")
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        raise SyncError("Invalid stable message key.")
    return value


def _integer(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise SyncError(f"Invalid {label}.") from None
    if result < 0 or isinstance(value, bool):
        raise SyncError(f"Invalid {label}.")
    return result


def utf16_length(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise SyncError("Rendered copy contains invalid Unicode.") from None


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise SyncError("Sync configuration must be a JSON object.")
    for label in ("guild_id", "bot_id", "bot_role_id"):
        _id(config.get(label), label)
    channels = config.get("channel_ids")
    if not isinstance(channels, dict) or set(channels) != set(CHANNEL_ALIASES):
        raise SyncError("channel_ids must define changelog, characters and balance.")
    for alias in CHANNEL_ALIASES:
        _id(channels[alias], "channel ID")
    if len(set(channels.values())) != len(CHANNEL_ALIASES):
        raise SyncError("Each content channel must have a distinct ID.")
    language = config.get("language", "en")
    if language not in ("en", "en-US", "en-GB"):
        raise SyncError("Discord sync supports English copy only.")
    seeds = config.get("seed_bindings", {})
    if not isinstance(seeds, dict):
        raise SyncError("seed_bindings must be an object.")
    for key, message_id in seeds.items():
        _key(key)
        _id(message_id, "seed message ID")
    if len(set(seeds.values())) != len(seeds):
        raise SyncError("Seed message IDs must not be shared between stable keys.")
    return config


def _text(value: Any, label: str, limit: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SyncError(f"Invalid rendered {label}.")
    if utf16_length(value) > limit:
        raise SyncError(f"Rendered {label} exceeds the Discord UTF-16 limit.")
    return value


def _visible_text(embed: dict) -> list[str]:
    result = [embed.get("title", ""), embed.get("description", "")]
    result += [embed.get("footer", {}).get("text", ""), embed.get("author", {}).get("name", "")]
    for field in embed.get("fields", []):
        result.extend((field.get("name", ""), field.get("value", "")))
    return result


def validate_rendered(rows: list[dict], config: dict) -> list[dict]:
    """Validate the whole batch before access checks or any write; force quiet sends."""
    validate_config(config)
    if not isinstance(rows, list) or not rows:
        raise SyncError("The renderer must return a nonempty list of messages.")
    clean = copy.deepcopy(rows)
    keys: set[str] = set()
    headers: set[str] = set()
    for row in clean:
        if not isinstance(row, dict) or set(row) != {"key", "channel", "payload"}:
            raise SyncError("Each rendered row must contain key, channel and payload.")
        key = _key(row["key"])
        if key in keys:
            raise SyncError("The renderer returned duplicate stable keys.")
        keys.add(key)
        alias = row["channel"]
        if alias not in CHANNEL_ALIASES:
            raise SyncError("The renderer selected an unknown channel alias.")
        if key == alias + ":index":
            headers.add(alias)
        payload = row["payload"]
        if not isinstance(payload, dict) or set(payload) - {"content", "embeds", "flags", "allowed_mentions"}:
            raise SyncError("Rendered payload contains unsupported message features.")
        content = _text(payload.get("content", ""), "message content", 2000)
        embeds = payload.get("embeds", [])
        if not isinstance(embeds, list) or len(embeds) > 10:
            raise SyncError("Rendered embed count exceeds the Discord limit.")
        visible = [content]
        combined = 0
        for embed in embeds:
            allowed = {"title", "description", "url", "timestamp", "color", "footer", "image", "thumbnail", "author", "fields", "type"}
            if not isinstance(embed, dict) or set(embed) - allowed:
                raise SyncError("Rendered embed contains unsupported fields.")
            if embed.get("type", "rich") != "rich":
                raise SyncError("Only authored rich embeds are supported.")
            combined += utf16_length(_text(embed.get("title", ""), "embed title", 256))
            combined += utf16_length(_text(embed.get("description", ""), "embed description", 4096))
            footer = embed.get("footer", {})
            author = embed.get("author", {})
            if not isinstance(footer, dict) or set(footer) - {"text", "icon_url"}:
                raise SyncError("Invalid rendered embed footer.")
            if not isinstance(author, dict) or set(author) - {"name", "url", "icon_url"}:
                raise SyncError("Invalid rendered embed author.")
            combined += utf16_length(_text(footer.get("text", ""), "embed footer", 2048))
            combined += utf16_length(_text(author.get("name", ""), "embed author", 256))
            fields = embed.get("fields", [])
            if not isinstance(fields, list) or len(fields) > 25:
                raise SyncError("Rendered embed field count exceeds the Discord limit.")
            for field in fields:
                if not isinstance(field, dict) or set(field) - {"name", "value", "inline"}:
                    raise SyncError("Invalid rendered embed field.")
                combined += utf16_length(_text(field.get("name"), "field name", 256, allow_empty=False))
                combined += utf16_length(_text(field.get("value"), "field value", 1024, allow_empty=False))
                if "inline" in field and not isinstance(field["inline"], bool):
                    raise SyncError("Rendered field inline value must be boolean.")
            if "color" in embed and (type(embed["color"]) is not int or not 0 <= embed["color"] <= 0xFFFFFF):
                raise SyncError("Invalid embed color.")
            # URLs are permitted for images, but never as explicit source/version links.
            for url in (embed.get("url", ""), author.get("url", "")):
                if not isinstance(url, str) or GITHUB_LINK.search(url):
                    raise SyncError("Explicit GitHub links are not allowed in Discord copy.")
            for field_name in ("image", "thumbnail"):
                if field_name in embed:
                    image = embed[field_name]
                    if not isinstance(image, dict) or set(image) != {"url"} or not isinstance(image["url"], str) or not image["url"].startswith("https://"):
                        raise SyncError("Embed images must use HTTPS image URLs.")
            visible.extend(_visible_text(embed))
        if combined > 6000:
            raise SyncError("Combined embed copy exceeds 6000 UTF-16 units.")
        if not content and not embeds:
            raise SyncError("The renderer returned an empty message.")
        copy_text = "\n".join(visible)
        if GITHUB_LINK.search(copy_text):
            raise SyncError("Visible GitHub links are not allowed in Discord copy.")
        if PORTUGUESE_COPY.search(copy_text):
            raise SyncError("Rendered copy contains Portuguese; English is required.")
        payload["content"] = content
        payload["embeds"] = embeds
        payload["allowed_mentions"] = {"parse": []}
        payload["flags"] = SUPPRESS_NOTIFICATIONS | (0 if embeds else SUPPRESS_EMBEDS)
    if headers != set(CHANNEL_ALIASES):
        raise SyncError("Each content channel requires its stable index header.")
    return clean


def _canonical_embed(embed: dict) -> dict:
    """Retain authored data only, dropping Discord's rich/proxy/dimension metadata."""
    result: dict[str, Any] = {}
    for key in ("title", "description", "url", "timestamp", "color"):
        if key in embed and embed[key] not in (None, ""):
            result[key] = embed[key]
    for key, allowed in (("author", ("name", "url", "icon_url")), ("footer", ("text", "icon_url")), ("image", ("url",)), ("thumbnail", ("url",))):
        if isinstance(embed.get(key), dict):
            value = {name: embed[key][name] for name in allowed if name in embed[key] and embed[key][name] not in (None, "")}
            if value:
                result[key] = value
    if embed.get("fields"):
        result["fields"] = [{"name": f.get("name", ""), "value": f.get("value", ""), "inline": bool(f.get("inline", False))} for f in embed["fields"]]
    return result


def canonical_payload(payload: dict) -> dict:
    return {
        "content": payload.get("content") or "",
        "embeds": [] if _integer(payload.get("flags", 0), "message flags") & SUPPRESS_EMBEDS else [_canonical_embed(e) for e in payload.get("embeds") or []],
        # Silence is a create-time flag, not mutable message content.
        "flags": _integer(payload.get("flags", 0), "message flags") & SUPPRESS_EMBEDS,
    }


def payload_equivalent(actual: dict, payload: dict) -> bool:
    return canonical_payload(actual) == canonical_payload(payload)


def payload_fingerprint(payload: dict) -> str:
    value = json.dumps(canonical_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_nonce(config: dict, key: str, channel_id: str) -> str:
    value = ":".join((config["guild_id"], config["bot_id"], channel_id, key))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def merge_seed_bindings(config: dict, state: dict) -> dict:
    validate_config(config)
    if not isinstance(state, dict) or state.get("version", 1) != 1:
        raise SyncError("State format is unsupported; restore the state file before publishing.")
    if state.get("guild_id", config["guild_id"]) != config["guild_id"] or state.get("bot_id", config["bot_id"]) != config["bot_id"]:
        raise SyncError("The state file belongs to another guild or bot.")
    state["version"] = 1
    state["guild_id"] = config["guild_id"]
    state["bot_id"] = config["bot_id"]
    bindings = state.setdefault("bindings", {})
    pending = state.setdefault("pending", {})
    if not isinstance(bindings, dict) or not isinstance(pending, dict):
        raise SyncError("State bindings and pending records must be objects.")
    for key, message_id in bindings.items():
        _key(key)
        _id(message_id, "state message ID")
    for key, message_id in config.get("seed_bindings", {}).items():
        if key in bindings and bindings[key] != message_id:
            raise SyncError("A seed binding conflicts with durable state; review it before publishing.")
        bindings.setdefault(key, message_id)
    if len(set(bindings.values())) != len(bindings):
        raise SyncError("State message IDs are shared between stable keys.")
    for key, record in pending.items():
        _key(key)
        if not isinstance(record, dict):
            raise SyncError("Invalid pending creation state.")
        _id(record.get("channel_id"), "pending channel ID")
        if not isinstance(record.get("nonce"), str) or not re.fullmatch(r"[a-f0-9]{24}", record["nonce"]):
            raise SyncError("Invalid pending message nonce.")
        fingerprint = record.get("payload_hash", record.get("marker"))
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise SyncError("Invalid pending payload fingerprint.")
        record["payload_hash"] = fingerprint
    return state


def load_state(path: Path | None, config: dict) -> dict:
    if path is None or not path.exists():
        return merge_seed_bindings(config, {})
    if path.is_symlink() or not path.is_file():
        raise SyncError("The durable state path must be a regular file, not a symlink.")
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError()
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise SyncError("The state file cannot be read or is corrupt; restore it before publishing.") from None
    return merge_seed_bindings(config, state)


def write_state_atomic(path: Path, state: dict) -> None:
    """Replace complete nonsecret state atomically, leaving old state on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise SyncError("Refusing to replace a symlinked state file.")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".discord-state-", suffix=".json", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise SyncError("Durable state could not be saved; publishing stopped to prevent duplicates.") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DiscordClient:
    """Fixed-origin API client. No response body, token or headers enter errors."""
    def __init__(self, token: str, *, timeout: float = 20, max_retries: int = 5):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,}", token):
            raise SyncError("DISCORD_BOT_TOKEN is missing or malformed.")
        self._token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.opener = urllib.request.build_opener(NoRedirects())

    @staticmethod
    def _allowed(method: str, path: str) -> bool:
        if method == "GET":
            return bool(re.fullmatch(r"/(?:users/@me|guilds/\d+(?:/members/\d+)?|channels/\d+(?:/messages(?:/\d+|\?limit=100(?:&before=\d+)?)?)?)", path))
        if method == "POST":
            return bool(re.fullmatch(r"/channels/\d+/messages", path))
        if method == "PATCH":
            return bool(re.fullmatch(r"/channels/\d+/messages/\d+", path))
        if method == "PUT":
            return bool(re.fullmatch(r"/channels/\d+/messages/pins/\d+", path))
        return False

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        if not self._allowed(method, path):
            raise SyncError("Unsupported Discord operation; no deletion or access changes are allowed.")
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        waited = 0.0
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(API_ROOT + path, data=raw, method=method, headers={
                "Authorization": "Bot " + self._token,
                "User-Agent": "NoRyDiscordSync/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise SyncError("Discord returned an oversized response.")
                    if not body:
                        return {}
                    try:
                        return json.loads(body)
                    except (ValueError, UnicodeError):
                        raise SyncError("Discord returned an invalid JSON response.") from None
            except urllib.error.HTTPError as error:
                try:
                    body = json.loads(error.read(1024 * 1024))
                except Exception:
                    body = {}
                code = body.get("code") if isinstance(body, dict) and type(body.get("code")) is int else None
                if error.code == 429 and attempt < self.max_retries:
                    try:
                        delay = float(body.get("retry_after", error.headers.get("Retry-After", "1")))
                    except (ValueError, TypeError, AttributeError):
                        delay = 1.0
                    if not math.isfinite(delay) or delay < 0 or delay > 30 or waited + delay > 120:
                        raise ApiError(429, method, path, code) from None
                    time.sleep(max(delay, 0.1))
                    waited += max(delay, 0.1)
                    continue
                raise ApiError(error.code, method, path, code) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise ApiError("connection_error", method, path) from None
        raise ApiError(429, method, path)


class SyncEngine:
    def __init__(self, config: dict, state: dict, client: Any, repo_root: Path, render_func: Any, state_path: Path | None = None):
        self.config = copy.deepcopy(validate_config(config))
        self.state = merge_seed_bindings(self.config, copy.deepcopy(state))
        self.client = client
        self.repo_root = Path(repo_root)
        self.render_func = render_func
        self.state_path = None if state_path is None else Path(state_path)
        self.messages: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}
        self.guild: dict = {}
        self.member: dict = {}
        self._recent_cache: dict[str, list] = {}

    def _render(self) -> list[dict]:
        try:
            rows = self.render_func(self.repo_root, self.config, dict(self.state["bindings"]))
        except SyncError:
            raise
        except Exception:
            raise SyncError("Rendering failed; no Discord content was written.") from None
        return validate_rendered(rows, self.config)

    def _persist(self) -> None:
        if self.state_path is None:
            raise SyncError("Apply requires a durable state path outside the checkout.")
        write_state_atomic(self.state_path, self.state)

    def _private_guard(self, channel: dict, guild: dict) -> None:
        roles = {str(role.get("id")): role for role in guild.get("roles", [])}
        everyone = self.config["guild_id"]
        overwrites = channel.get("permission_overwrites")
        if not isinstance(overwrites, list):
            raise SyncError("The administrator channel has no verifiable privacy overwrites.")
        everyone_overwrites = [o for o in overwrites if str(o.get("id")) == everyone and _integer(o.get("type", 0), "overwrite type") == 0]
        if len(everyone_overwrites) != 1:
            raise SyncError("The administrator channel must explicitly deny View Channel to @everyone.")
        baseline = everyone_overwrites[0]
        if not (_integer(baseline.get("deny", 0), "overwrite deny") & VIEW_CHANNEL) or (_integer(baseline.get("allow", 0), "overwrite allow") & VIEW_CHANNEL):
            raise SyncError("The administrator channel must deny View Channel to @everyone, without an allow.")
        owner = str(guild.get("owner_id", ""))
        for overwrite in overwrites:
            if not (_integer(overwrite.get("allow", 0), "overwrite allow") & VIEW_CHANNEL):
                continue
            subject = str(overwrite.get("id", ""))
            kind = _integer(overwrite.get("type", 0), "overwrite type")
            if kind == 0:
                if subject == self.config["bot_role_id"]:
                    continue
                role = roles.get(subject)
                if not role or not (_integer(role.get("permissions", 0), "role permissions") & ADMINISTRATOR):
                    raise SyncError("A nonadministrator role can view the private balance channel; fix access before publishing.")
            elif kind == 1:
                if subject not in (self.config["bot_id"], owner):
                    raise SyncError("A member override exposes the balance channel; only the bot or guild owner may have one.")
            else:
                raise SyncError("Unknown private channel overwrite type.")

    def _effective_permissions(self, channel: dict, roles: dict[str, dict]) -> int:
        everyone = self.config["guild_id"]
        member_roles = {str(role) for role in self.member.get("roles", [])}
        base = _integer(roles[everyone].get("permissions", 0), "everyone permissions")
        for role_id in member_roles:
            if role_id not in roles:
                raise SyncError("The bot has an unknown role; permission checks cannot be completed.")
            base |= _integer(roles[role_id].get("permissions", 0), "bot role permissions")
        if base & ADMINISTRATOR:
            return (1 << 64) - 1
        overwrites = channel.get("permission_overwrites", [])
        for overwrite in overwrites:
            if str(overwrite.get("id")) == everyone and _integer(overwrite.get("type", 0), "overwrite type") == 0:
                base &= ~_integer(overwrite.get("deny", 0), "overwrite deny")
                base |= _integer(overwrite.get("allow", 0), "overwrite allow")
        deny = allow = 0
        for overwrite in overwrites:
            if _integer(overwrite.get("type", 0), "overwrite type") == 0 and str(overwrite.get("id")) in member_roles:
                deny |= _integer(overwrite.get("deny", 0), "overwrite deny")
                allow |= _integer(overwrite.get("allow", 0), "overwrite allow")
        base = (base & ~deny) | allow
        for overwrite in overwrites:
            if _integer(overwrite.get("type", 0), "overwrite type") == 1 and str(overwrite.get("id")) == self.config["bot_id"]:
                base &= ~_integer(overwrite.get("deny", 0), "overwrite deny")
                base |= _integer(overwrite.get("allow", 0), "overwrite allow")
        return base

    def _preflight(self, rows: list[dict]) -> None:
        user = self.client.request("GET", "/users/@me")
        if not isinstance(user, dict) or user.get("bot") is not True or str(user.get("id")) != self.config["bot_id"]:
            raise SyncError("The configured credential is not the expected bot.")
        self.guild = self.client.request("GET", "/guilds/" + self.config["guild_id"])
        if not isinstance(self.guild, dict) or str(self.guild.get("id")) != self.config["guild_id"]:
            raise SyncError("The guild identity did not match the configuration.")
        _id(str(self.guild.get("owner_id", "")), "guild owner ID")
        roles = {str(role.get("id")): role for role in self.guild.get("roles", [])}
        if self.config["guild_id"] not in roles or self.config["bot_role_id"] not in roles:
            raise SyncError("The guild's everyone or configured bot role is missing.")
        self.member = self.client.request("GET", f'/guilds/{self.config["guild_id"]}/members/{self.config["bot_id"]}')
        if not isinstance(self.member, dict) or self.config["bot_role_id"] not in self.member.get("roles", []):
            raise SyncError("The expected bot role is not attached to the bot.")
        for alias, channel_id in self.config["channel_ids"].items():
            channel = self.client.request("GET", "/channels/" + channel_id)
            if not isinstance(channel, dict) or str(channel.get("id")) != channel_id or str(channel.get("guild_id")) != self.config["guild_id"] or channel.get("type") != 0:
                raise SyncError("A configured channel is not a guild text channel in the expected guild.")
            self.channels[alias] = channel
        # Privacy is verified for the complete batch before fetching/editing posts.
        self._private_guard(self.channels["balance"], self.guild)
        for alias, channel in self.channels.items():
            required = VIEW_CHANNEL | READ_MESSAGE_HISTORY | SEND_MESSAGES | PIN_MESSAGES
            if any(row["channel"] == alias and row["payload"].get("embeds") for row in rows):
                required |= EMBED_LINKS
            if self._effective_permissions(channel, roles) & required != required:
                raise SyncError(f"The bot lacks required message or pin permissions in the {alias} channel.")

    def _guard_balance_write(self, row: dict) -> None:
        if row["channel"] == "balance":
            guild = self.client.request("GET", "/guilds/" + self.config["guild_id"])
            channel = self.client.request("GET", "/channels/" + self.config["channel_ids"]["balance"])
            if str(guild.get("id")) != self.config["guild_id"] or str(channel.get("id")) != self.config["channel_ids"]["balance"] or str(channel.get("guild_id")) != self.config["guild_id"] or channel.get("type") != 0:
                raise SyncError("The private channel identity changed during publishing.")
            self._private_guard(channel, guild)

    def _message_identity(self, message: dict, channel_id: str, expected_id: str | None = None) -> dict:
        if not isinstance(message, dict) or str(message.get("author", {}).get("id")) != self.config["bot_id"]:
            raise SyncError("A bound message has another author; it will not be edited.")
        if str(message.get("channel_id")) != channel_id:
            raise SyncError("A bound message belongs to another channel.")
        _id(str(message.get("id", "")), "Discord message ID")
        if expected_id is not None and str(message["id"]) != expected_id:
            raise SyncError("Discord returned a message with an unexpected ID.")
        return message

    def _read_message(self, row: dict, message_id: str) -> dict:
        channel_id = self.config["channel_ids"][row["channel"]]
        try:
            message = self.client.request("GET", f"/channels/{channel_id}/messages/{message_id}")
        except ApiError as error:
            if error.status == 404:
                raise SyncError(f'Known message for {row["key"]} is missing. Review its binding; sync will not silently recreate it.') from None
            raise
        return self._message_identity(message, channel_id, message_id)

    def _recover(self, row: dict) -> dict | None:
        key = row["key"]
        record = self.state["pending"].get(key)
        if record is None:
            return None
        channel_id = self.config["channel_ids"][row["channel"]]
        if record["channel_id"] != channel_id or record["nonce"] != deterministic_nonce(self.config, key, channel_id):
            raise SyncError("Pending creation does not match the configured destination.")
        if channel_id not in self._recent_cache:
            recent = self.client.request("GET", f"/channels/{channel_id}/messages?limit=100")
            if not isinstance(recent, list):
                raise SyncError("Discord returned an invalid recent-message list.")
            self._recent_cache[channel_id] = recent
        fingerprint = record["payload_hash"]
        matches = []
        for message in self._recent_cache[channel_id]:
            if str(message.get("author", {}).get("id")) != self.config["bot_id"] or str(message.get("channel_id")) != channel_id:
                continue
            if str(message.get("nonce", "")) == record["nonce"] or payload_fingerprint(message) == fingerprint:
                matches.append(message)
        if len(matches) > 1:
            raise SyncError("Crash recovery found ambiguous matching posts; review pending state before publishing.")
        if matches:
            return self._message_identity(matches[0], channel_id)
        # Once a POST may have reached Discord, absence from a bounded recent list
        # does not prove it was never created. Fail closed instead of duplicating.
        if record.get("attempted", False):
            raise SyncError("A pending creation is not in recent history. Recover its ID manually before retrying to prevent duplicate posts.")
        return None

    @staticmethod
    def _header(key: str) -> bool:
        return bool(re.fullmatch(r"(?:changelog|characters|balance):index(?::[A-Za-z0-9_-]+)?", key))

    def _pin_keys(self, rows: list[dict]) -> set[str]:
        keys = {row["key"] for row in rows if self._header(row["key"])}
        if self.config.get("pin_latest_changelog", True):
            versions = [(int(match.group(1)), row["key"]) for row in rows if (match := re.fullmatch(r"changelog:v([0-9]+)", row["key"]))]
            if versions:
                keys.add(max(versions)[1])
        return keys

    def run(self, apply: bool) -> dict:
        if type(apply) is not bool:
            raise SyncError("Publishing mode must be explicit.")
        if apply and self.state_path is None:
            raise SyncError("Apply requires a durable state path outside the checkout.")
        if apply:
            assert self.state_path is not None
            resolved_state = self.state_path.resolve()
            resolved_root = self.repo_root.resolve()
            if not self.state_path.is_absolute() or resolved_state == resolved_root or resolved_root in resolved_state.parents or self.state_path.is_symlink():
                raise SyncError("Apply requires a regular durable state file outside the checkout.")
        rows = self._render()
        self._preflight(rows)
        recovered = 0
        for row in rows:
            key = row["key"]
            message_id = self.state["bindings"].get(key)
            if message_id:
                self.messages[key] = self._read_message(row, message_id)
            else:
                recovered_message = self._recover(row)
                if recovered_message:
                    message_id = str(recovered_message["id"])
                    if message_id in self.state["bindings"].values():
                        raise SyncError("Recovered message ID is already bound to another post.")
                    self.state["bindings"][key] = message_id
                    self.state["pending"].pop(key, None)
                    self.messages[key] = recovered_message
                    recovered += 1
        # All known post authors have now been checked, before the first write.
        rows = self._render()
        keys_before = {row["key"]: row["channel"] for row in rows}
        pin_keys = self._pin_keys(rows)
        to_create = [row for row in rows if row["key"] not in self.state["bindings"]]
        to_update = [row for row in rows if row["key"] in self.messages and not payload_equivalent(self.messages[row["key"]], row["payload"])]
        to_pin = [row for row in rows if row["key"] in pin_keys and not self.messages.get(row["key"], {}).get("pinned", False)]
        summary = {"mode": "apply" if apply else "dry-run", "checked": len(rows), "created": len(to_create), "updated": len(to_update), "pinned": len(to_pin), "recovered": recovered, "unchanged": len(rows) - len(to_create) - len(to_update)}
        if not apply:
            summary["indices_refresh_after_create"] = bool(to_create)
            return summary
        if recovered:
            self._persist()
        created = updated = pinned = 0
        touched: set[str] = set()
        for row in to_create:
            key = row["key"]
            channel_id = self.config["channel_ids"][row["channel"]]
            self._guard_balance_write(row)
            nonce = deterministic_nonce(self.config, key, channel_id)
            record = {"nonce": nonce, "channel_id": channel_id, "payload_hash": payload_fingerprint(row["payload"]), "attempted": False}
            self.state["pending"][key] = record
            self._persist()  # No POST can happen before recovery metadata is durable.
            record["attempted"] = True
            self._persist()
            payload = copy.deepcopy(row["payload"])
            payload.update({"nonce": nonce, "enforce_nonce": True})
            response = self.client.request("POST", f"/channels/{channel_id}/messages", payload)
            self._message_identity(response, channel_id)
            message_id = str(response["id"])
            if message_id in self.state["bindings"].values():
                raise SyncError("Discord returned a message ID already used by another stable post.")
            self.state["bindings"][key] = message_id
            self.state["pending"].pop(key, None)
            self._persist()  # Save each created ID before any other Discord write.
            self.messages[key] = response
            touched.add(key)
            created += 1
        final_rows = self._render()
        if {row["key"]: row["channel"] for row in final_rows} != keys_before:
            raise SyncError("The renderer changed its stable message set after binding IDs.")
        pin_keys = self._pin_keys(final_rows)
        for row in final_rows:
            key = row["key"]
            actual = self.messages[key]
            channel_id = self.config["channel_ids"][row["channel"]]
            message_id = self.state["bindings"][key]
            if not payload_equivalent(actual, row["payload"]):
                self._guard_balance_write(row)
                payload = copy.deepcopy(row["payload"])
                payload["flags"] = (_integer(actual.get("flags", 0), "existing message flags") & ~SUPPRESS_EMBEDS) | (payload["flags"] & SUPPRESS_EMBEDS)
                response = self.client.request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)
                self.messages[key] = self._message_identity(response, channel_id, message_id)
                touched.add(key)
                updated += 1
            if key in pin_keys and not self.messages[key].get("pinned", False):
                self.client.request("PUT", f"/channels/{channel_id}/messages/pins/{message_id}")
                self.messages[key]["pinned"] = True
                pinned += 1
        # Verify authored content and required pins from fresh reads, never responses alone.
        self._guard_balance_write({"channel": "balance"})
        for row in final_rows:
            message = self._read_message(row, self.state["bindings"][row["key"]])
            if not payload_equivalent(message, row["payload"]):
                raise SyncError("Readback did not match authored content; inspect the job before rerunning.")
            if row["key"] in pin_keys and not message.get("pinned", False):
                raise SyncError("An index or latest changelog pin could not be verified.")
        self._persist()
        summary.update(created=created, updated=updated, pinned=pinned, unchanged=len(final_rows) - len(touched), verified=len(final_rows))
        return summary


def _state_path(repo_root: Path, *, required: bool) -> Path | None:
    value = os.environ.get("DISCORD_SYNC_STATE_PATH")
    if not value:
        if required:
            raise SyncError("Set DISCORD_SYNC_STATE_PATH to a durable absolute file outside the checkout.")
        return None
    path = Path(value)
    if not path.is_absolute():
        raise SyncError("DISCORD_SYNC_STATE_PATH must be absolute.")
    resolved = path.resolve()
    if resolved == repo_root or repo_root in resolved.parents:
        raise SyncError("Durable Discord state must be outside the repository checkout.")
    if path.is_symlink():
        raise SyncError("The durable state path must not be a symlink.")
    return resolved


def _load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise SyncError("The sync configuration is missing or invalid JSON.") from None
    return validate_config(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or synchronize the existing Bizarre Arena Discord posts.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Render and validate without credentials or network.")
    mode.add_argument("--dry-run", action="store_true", help="Read Discord and report a plan without writing anything.")
    mode.add_argument("--apply", action="store_true", help="Apply safe updates, preserving existing message IDs.")
    parser.add_argument("--root", "--repo-root", dest="repo_root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config", type=Path, default=Path(".discord/sync.json"))
    args = parser.parse_args(argv)
    try:
        root = args.repo_root.resolve()
        config_path = args.config if args.config.is_absolute() else root / args.config
        config = _load_config(config_path)
        from .render import render
        if args.check:
            try:
                rows = validate_rendered(render(root, config, dict(config.get("seed_bindings", {}))), config)
            except SyncError:
                raise
            except Exception:
                raise SyncError("Rendering failed; no network calls were made.") from None
            print(json.dumps({"mode": "check", "messages": len(rows), "channels": {alias: sum(row["channel"] == alias for row in rows) for alias in CHANNEL_ALIASES}}, sort_keys=True))
            return 0
        state_path = _state_path(root, required=args.apply)
        client = DiscordClient(os.environ.get("DISCORD_BOT_TOKEN", ""))
        if not args.apply:
            state = load_state(state_path, config)
            summary = SyncEngine(config, state, client, root, render, state_path).run(apply=False)
        else:
            assert state_path is not None
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = state_path.with_name(state_path.name + ".lock")
            if lock_path.is_symlink():
                raise SyncError("The state lock must not be a symlink.")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with os.fdopen(lock_fd, "a+") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    state = load_state(state_path, config)
                    summary = SyncEngine(config, state, client, root, render, state_path).run(apply=True)
            except BlockingIOError:
                raise SyncError("Another Discord sync is already running against this state file.") from None
        print(json.dumps(summary, sort_keys=True))
        return 0
    except SyncError as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception:
        # Never include exception repr/traceback: network errors can retain requests.
        print(json.dumps({"ok": False, "error": "Sync failed; diagnostic details were suppressed to protect credentials."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Render English repository data into Discord messages; no network or subprocesses.

Source prose must already be English. English table headers and an explicit
language declaration are checked; clearly Portuguese prose fails closed. This
module never translates source text. Bindings map stable document keys to the
Discord message IDs previously created by the publisher.
"""

from __future__ import annotations

import copy
import json
import math
import posixpath
import re
from collections import OrderedDict
from pathlib import Path, PurePosixPath

DEFAULT_ASSET_REF = "97bd3babc457798bfc39604f11a30a82b5f3cd23"
FIELDS = (
    ("damage", "Damage", ""), ("areaDamage", "Area damage", ""),
    ("dotDamage", "Damage/tick", ""), ("duration", "Duration", " s"),
    ("statusDuration", "Effect", " s"), ("cooldown", "Wait", " s"),
    ("cost", "Energy", ""), ("range", "Range", " m"),
    ("radius", "Radius", " m"), ("hits", "Hits", ""),
)
IDENTITY = ("tag", "character", "skill", "profile")
YELLOW, RESET = "\x1b[1;33m", "\x1b[0m"
_PORTUGUESE = re.compile(
    r"\b(?:habilidades?|personagens?|alteraç(?:ão|ões)|alteracoes|valores|atual|"
    r"atualizado|dano|duração|heróis|herois|mudanças?|mudancas|agora|servidor|"
    r"jogadores?|após|somente|permite|inimigos?|enquanto|aumenta|diminui|reduz|"
    r"mantém|recarga|alcance|energia|efeitos?|segundos|não|voce|você|porém|"
    r"apenas|golpes|ataques|paralisa|congela|invisível|invisivel|descrição|"
    r"descricao|também|depois|nenhum|causa)\b", re.IGNORECASE,
)
_GITHUB_URL = r"https?://(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/[^\s<>)]*"


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _english(value: str, context: str) -> str:
    if not isinstance(value, str) or _PORTUGUESE.search(value):
        raise ValueError(f"English source prose is required: {context}; no automatic translation is performed")
    return value


def _text(value: str) -> str:
    """Remove source links while retaining their readable label."""
    value = re.sub(r"!?\[([^\]]*)\]\((" + _GITHUB_URL + r")\)", r"\1", value)
    value = re.sub(_GITHUB_URL, "", value)
    return value.strip()


def _public_description(value: str) -> str:
    """Keep source mechanics qualitative, without changing gameplay counts.

    These rules apply to prose only, never to skill names, the changelog, or
    administrator data. Counts such as 'three bullets' remain meaningful.
    """
    number = r"\d+(?:\.\d+)?"
    time_unit = r"(?:milliseconds?|ms|seconds?|secs?|s|minutes?|mins?)"
    timing = number + r"\s*" + time_unit + r"\b"

    def received(match):
        amount = float(match.group("amount"))
        modifier = "less damage" if amount < 100 else "more damage" if amount > 100 else "damage"
        return match.group("verb") + " " + modifier

    value = re.sub(r"\b(?P<verb>takes?|receives?)\s+(?:only\s+)?(?P<amount>" + number + r")\s*%\s+(?:of\s+)?(?:the\s+)?damage\b",
                   received, value, flags=re.I)

    def health(match):
        verb, owner = match.group("verb"), match.group("owner")
        if verb.lower() in ("take", "takes"):
            return ("Drains" if verb[0].isupper() else "drains") + " " + owner + " health"
        return verb + " some of " + owner + " health"

    value = re.sub(r"\b(?P<verb>takes?|steals?|removes?|drains?)\s+(?:half|" + number + r"\s*%)\s+(?:of\s+)?(?P<owner>their|your|its|the target's)\s+health\b",
                   health, value, flags=re.I)
    value = re.sub(r"\bat\s+half\s+speed\b", "more slowly", value, flags=re.I)
    value = re.sub(r"\bat\s+(?:double|triple)\s+speed\b", "faster", value, flags=re.I)
    value = re.sub(r"\bat\s+(" + number + r")\s*%\s+speed\b",
                   lambda m: "more slowly" if float(m.group(1)) < 100 else "faster" if float(m.group(1)) > 100 else "at normal speed",
                   value, flags=re.I)
    value = re.sub(r"\bhalf\s+damage\b", "reduced damage", value, flags=re.I)
    value = re.sub(r"\b(?:double|triple)\s+damage\b", "increased damage", value, flags=re.I)
    value = re.sub(r"\bhalf\s+(your|their|its)\s+health\b", r"reduced \1 health", value, flags=re.I)
    value = re.sub(r"\b(?:by|to|at)\s+" + number + r"\s*%", "", value, flags=re.I)
    value = re.sub(number + r"\s*%\s+(?:of\s+)?", "", value, flags=re.I)

    value = re.sub(r"\b(lasts?|persists?)(?:\s+for)?\s+" + timing, r"\1 temporarily", value, flags=re.I)
    value = re.sub(r"\b(?:for|during)\s+" + timing, "temporarily", value, flags=re.I)
    value = re.sub(r"\b(?:after|in)\s+" + timing, "after a delay", value, flags=re.I)
    value = re.sub(r"\bevery\s+" + timing, "periodically", value, flags=re.I)
    value = re.sub(r"\b(?:a\s+)?" + number + r"(?:\s*-\s*|\s+)(?:millisecond|second|minute)s?(?:\s*-\s*|\s+)cooldown\b",
                   "a cooldown", value, flags=re.I)
    value = re.sub(r"\b(cooldown|duration)(?:\s+of|\s*:)?\s+" + timing, r"\1", value, flags=re.I)
    value = re.sub(r"\b(deals?|inflicts?)\s+" + number + r"\s+(?:points?\s+of\s+)?damage\b", r"\1 damage", value, flags=re.I)
    value = re.sub(r"\b(costs?|consumes?|drains?)\s+" + number + r"\s+energy(?:\s+per\s+second)?\b", r"\1 energy", value, flags=re.I)
    value = re.sub(r"\b(?:within|up to)\s+" + number + r"\s*(?:m|meters?|metres?)\b", "within range", value, flags=re.I)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value.strip()


def _fmt(value, present=True) -> str:
    if not present:
        return "—"
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format(value, ".12g")
    return str(value)


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def _changed(before: dict, after: dict, key: str) -> bool:
    return (key in before) != (key in after) or not _same(before.get(key), after.get(key))


def _split(value: str, limit: int) -> list[str]:
    """Split without dropping non-whitespace data or splitting a Unicode scalar."""
    chunks = []
    remaining = value.strip()
    while utf16_len(remaining) > limit:
        size, end = 0, 0
        for end, char in enumerate(remaining, 1):
            size += utf16_len(char)
            if size > limit:
                end -= 1
                break
        cut = remaining.rfind("\n", 0, end + 1)
        if cut < end // 2:
            cut = remaining.rfind(" ", 0, end + 1)
        if cut < end // 2:
            cut = end
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _embed_len(embed: dict) -> int:
    return sum(utf16_len(embed.get(k, "")) for k in ("title", "description")) + sum(
        utf16_len(f["name"]) + utf16_len(f["value"]) for f in embed.get("fields", [])
    ) + utf16_len(embed.get("footer", {}).get("text", "")) + utf16_len(embed.get("author", {}).get("name", ""))


def validate_payload(payload: dict) -> None:
    if utf16_len(payload.get("content", "")) > 2000:
        raise ValueError("Discord content exceeds 2000 UTF-16 characters")
    embeds = payload.get("embeds", [])
    if len(embeds) > 10 or sum(_embed_len(e) for e in embeds) > 6000:
        raise ValueError("Discord embed aggregate exceeds its limit")
    if not payload.get("content") and not embeds:
        raise ValueError("Empty Discord message")
    for embed in embeds:
        for key, limit in (("title", 256), ("description", 4096)):
            if utf16_len(embed.get(key, "")) > limit:
                raise ValueError(f"Discord embed {key} exceeds its limit")
        if utf16_len(embed.get("footer", {}).get("text", "")) > 2048:
            raise ValueError("Discord footer exceeds its limit")
        if len(embed.get("fields", [])) > 25:
            raise ValueError("Too many Discord embed fields")
        for field in embed.get("fields", []):
            if not 0 < utf16_len(field["name"]) <= 256 or not 0 < utf16_len(field["value"]) <= 1024:
                raise ValueError("Discord embed field exceeds its limit")


def _record(key: str, channel: str, payload: dict) -> dict:
    payload["allowed_mentions"] = {"parse": []}
    validate_payload(payload)
    return {"key": key, "channel": channel, "payload": payload}


def _field(name: str, value: str) -> list[dict]:
    if utf16_len(name) > 240:
        raise ValueError("Source skill name is too long for a Discord field")
    return [{"name": name + (f" (continued {i})" if i > 1 else ""), "value": chunk, "inline": False}
            for i, chunk in enumerate(_split(value, 1000), 1)]


def _cards(key: str, channel: str, embed: dict, fields: list[dict]) -> list[dict]:
    records, current, page = [], copy.deepcopy(embed), 1
    current["fields"] = []
    for field in fields:
        trial = copy.deepcopy(current)
        trial["fields"].append(field)
        if len(trial["fields"]) > 25 or _embed_len(trial) > 5800:
            records.append(_record(key if page == 1 else f"{key}:{page}", channel, {"embeds": [current]}))
            page += 1
            current = copy.deepcopy(embed)
            current["fields"] = []
        current["fields"].append(field)
    records.append(_record(key if page == 1 else f"{key}:{page}", channel, {"embeds": [current]}))
    return records


def _link(key: str, label: str, channel: str, config: dict, bindings: dict[str, str]) -> str:
    if key not in bindings:
        return label
    channels = config.get("channel_ids", config.get("channels", {}))
    ids = (str(config.get("guild_id", "")), str(channels.get(channel, "")), str(bindings[key]))
    if not all(x.isdigit() for x in ids):
        raise ValueError("Discord IDs are required to render a bound index link")
    return f"[{label}](https://discord.com/channels/{ids[0]}/{ids[1]}/{ids[2]})"


def _index(key: str, channel: str, title: str, entries: list[tuple[str, str]],
           base: dict, config: dict, bindings: dict[str, str]) -> list[dict]:
    fields, lines, start = [], [], 1
    for number, (target, label) in enumerate(entries, 1):
        line = _link(target, label, channel, config, bindings)
        if utf16_len(line) > 1000:
            raise ValueError("Source index label is too long")
        if lines and utf16_len("\n".join(lines + [line])) > 1000:
            fields.append({"name": f"Entries {start}–{number - 1}", "value": "\n".join(lines), "inline": False})
            lines, start = [], number
        lines.append(line)
    if lines:
        fields.append({"name": f"Entries {start}–{len(entries)}", "value": "\n".join(lines), "inline": False})
    records, current, page = [], copy.deepcopy(base), 1
    index_embed = {"title": title, "color": 2483945, "fields": []}
    current.setdefault("embeds", []).append(index_embed)
    for field in fields:
        trial = copy.deepcopy(current)
        trial["embeds"][-1]["fields"].append(field)
        if len(trial["embeds"][-1]["fields"]) > 25 or sum(_embed_len(e) for e in trial["embeds"]) > 5800:
            if not index_embed["fields"]:
                current["embeds"].pop()
            records.append(_record(key if page == 1 else f"{key}:{page}", channel, current))
            page += 1
            index_embed = {"title": title + " (continued)", "color": 2483945, "fields": []}
            current = {"embeds": [index_embed]}
        index_embed["fields"].append(field)
    if not index_embed["fields"] and len(current["embeds"]) > 1:
        current["embeds"].pop()
    records.append(_record(key if page == 1 else f"{key}:{page}", channel, current))
    return records


def _cells(line: str) -> list[str]:
    return [s.strip().replace("\\|", "|") for s in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _wiki(root: Path) -> dict[str, dict]:
    result = {}
    for path in sorted((root / "wiki/characters").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        headings = [line[2:].strip() for line in lines if line.startswith("# ")]
        if not headings or "## Skills" not in lines:
            raise ValueError("Character pages require an English '# Character' heading and '## Skills' section")
        header = next((i for i, line in enumerate(lines) if line.strip().startswith("|")
                       and _cells(line) == ["Slot", "Skill", "Tag", "What it does"]), None)
        if header is None:
            raise ValueError("Character skill tables require the English headers Slot, Skill, Tag, What it does")
        image = re.search(r"!\[[^\]]*\]\(([^)]+)\)", text)
        image_path = None
        if image:
            relative = image.group(1).strip()
            if ":" in relative or relative.startswith("/"):
                raise ValueError("Portraits must be repository-relative image paths")
            image_path = posixpath.normpath(str(PurePosixPath(path.relative_to(root).as_posix()).parent / relative))
            if image_path.startswith("../"):
                raise ValueError("Portrait path escapes the repository")
        count = 0
        for line in lines[header + 1:]:
            if not line.strip().startswith("|"):
                break
            cells = _cells(line)
            if cells and all(re.fullmatch(r"[:\-\s]+", cell) for cell in cells):
                continue
            if len(cells) != 4:
                raise ValueError("Malformed English character skill table")
            slot, name, tag, description = cells
            tag = tag.strip("`")
            if tag in result or not re.match(r"^(Attack|Skill|Ultimate|Passive|Special)(?:\s|$)", slot):
                raise ValueError("Duplicate wiki tag or non-English skill control label")
            result[tag] = {"character": headings[0], "name": name,
                           "slot": slot.split("(", 1)[0].strip(), "image": image_path,
                           "description": _text(_english(description, "character description"))}
            count += 1
        if not count:
            raise ValueError("Character skill table is empty")
    return result


def _skills(data: dict, context: str) -> dict[str, dict]:
    result = {}
    for skill in data.get("skills", []):
        if not isinstance(skill, dict) or not re.fullmatch(r"Skill\.[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+", str(skill.get("tag", ""))):
            raise ValueError(f"Invalid skill tag in {context}")
        if skill["tag"] in result:
            raise ValueError(f"Duplicate skill tag in {context}")
        for key in ("character", "skill"):
            if not isinstance(skill.get(key), str) or not skill[key] or utf16_len(skill[key]) > 220:
                raise ValueError(f"Invalid skill identity in {context}")
        for key, _, _ in FIELDS:
            value = skill.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"Non-numeric skill field in {context}")
        if skill.get("note"):
            _english(skill["note"], f"{context} skill note")
        result[skill["tag"]] = skill
    if not result:
        raise ValueError(f"No skills in {context}")
    return result


def _portrait(image: str | None, config: dict) -> dict:
    if not image:
        return {}
    repository = config.get("repository", "NoRy-Games/ba-v9qhxb2ct6")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid asset repository")
    ref = config.get("asset_refs", {}).get(image, config.get("asset_ref", DEFAULT_ASSET_REF))
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(ref)):
        raise ValueError("Portrait asset references must be immutable commit SHAs")
    return {"thumbnail": {"url": f"https://raw.githubusercontent.com/{repository}/{ref}/{image}"}}


def render(repo_root: Path, config: dict, bindings: dict[str, str]) -> list[dict]:
    """Return all reviewed logical documents without making external changes."""
    language = str(config.get("source_language", "en")).lower()
    if language != "en" and not language.startswith("en-"):
        raise ValueError("English repository prose is required; automatic translation is unavailable")
    root = Path(repo_root)
    current = json.loads((root / "balance.json").read_text(encoding="utf-8"))
    original = json.loads((root / "wiki/original-values.json").read_text(encoding="utf-8"))
    version = current.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"v\d+", version):
        raise ValueError("Current balance version must use the vN form")
    now, before, wiki = _skills(current, "current values"), _skills(original, "original values"), _wiki(root)
    if now.keys() != before.keys() or now.keys() != wiki.keys():
        raise ValueError("Current, original, and English wiki skill tags must match exactly")
    characters = OrderedDict()
    ids = {}
    for tag, skill in now.items():
        if any(skill.get(key) != before[tag].get(key) for key in IDENTITY):
            raise ValueError("Skill identities disagree between original and current sources")
        entry = wiki[tag]
        if entry["character"] != skill["character"] or entry["name"] != skill["skill"]:
            raise ValueError("Wiki skill identity does not match the balance data")
        ident = tag.split(".")[1]
        if ident in ids and ids[ident] != skill["character"]:
            raise ValueError("A stable character tag prefix is shared by different characters")
        ids[ident] = skill["character"]
        char = characters.setdefault(skill["character"], {"id": ident, "skills": [], "image": entry["image"]})
        if char["id"] != ident or char["image"] != entry["image"]:
            raise ValueError("Character skills must share a stable prefix and portrait")
        char["skills"].append(tag)

    changelogs, version_entries = [], []
    paths = sorted((root / "changelog").glob("v*.md"), key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else -1)
    for path in paths:
        if not re.fullmatch(r"v\d+", path.stem):
            raise ValueError("Changelog filenames must use vN.md")
        prose = _text(_english(path.read_text(encoding="utf-8"), "changelog"))
        if not prose:
            raise ValueError("Empty changelog")
        key = "changelog:" + path.stem
        chunks = _split(prose, 1800) if utf16_len(prose) > 1900 else [prose]
        for index, chunk in enumerate(chunks, 1):
            content = chunk if index == 1 else f"**{path.stem} · continued · part {index}**\n{chunk}"
            changelogs.append(_record(key if index == 1 else f"{key}:{index}", "changelog", {"content": content}))
        heading = prose.splitlines()[0].lstrip("# ")
        version_entries.append((key, heading))
    if not any(key == "changelog:" + version for key, _ in version_entries):
        raise ValueError("Current version has no corresponding English changelog")

    footer = lambda section: {"text": f"Bizarre Arena • {section} • {version}"}
    character_cards, balance_cards, changed_overview = [], [], []
    changed_heroes = changed_skills = changed_fields = 0
    for name, character in characters.items():
        public_fields, admin_fields, changes_for_hero = [], [], []
        for tag in character["skills"]:
            skill, old, description = now[tag], before[tag], wiki[tag]
            field_name = f'{description["slot"]} · {skill["skill"]}'
            public_fields += _field(field_name, _public_description(description["description"]))
            rows = [f'  {"Field":<14}{"Original":>9}{version:>9}']
            changes = []
            for key, label, unit in FIELDS:
                a, b = _fmt(old.get(key), key in old), _fmt(skill.get(key), key in skill)
                row = f'{label + unit:<14}{a:>9}{b:>9}'
                different = _changed(old, skill, key)
                rows.append(YELLOW + "* " + row + RESET if different else "  " + row)
                if different:
                    changes.append(f"{label.lower()} {a} → {b}{unit}")
                    changed_fields += 1
            value = f'`{tag}`\n```ansi\n' + "\n".join(rows) + "\n```"
            value += "\n**CHANGED:** " + "; ".join(changes) + "." if changes else "\nNo numeric changes."
            if old.get("note"):
                value += "\n**Original baseline:** " + _text(old["note"])
            if skill.get("note"):
                value += "\n**Current note:** " + _text(skill["note"])
            admin_fields += _field(field_name, value)
            if changes:
                changed_skills += 1
                changes_for_hero.append(f'• {skill["skill"]} — ' + "; ".join(changes))
        if changes_for_hero:
            changed_heroes += 1
            changed_overview += ["", f"**{name}**", *changes_for_hero]
        public_embed = {"title": name, "color": 2483945, "footer": footer("Character guide"), **_portrait(character["image"], config)}
        admin_embed = {"title": name, "description": f"**Original → {version}**", "color": 10181887 if changes_for_hero else 2483945,
                       "footer": footer("Admin reference"), **_portrait(character["image"], config)}
        character_cards += _cards("character:" + character["id"], "characters", public_embed, public_fields)
        balance_cards += _cards("balance:" + character["id"], "balance", admin_embed, admin_fields)

    count_line = f"**{changed_heroes} heroes · {changed_skills} skills · {changed_fields} changed fields**"
    overview = _split(f"## Changed skills · Original → {version}\n" + count_line + "\n" + "\n".join(changed_overview), 1900)
    original_note = _text(_english(original.get("note", "Original game values before balance changes."), "original baseline note"))
    global_rows = []
    for key in sorted(original.get("global", {}).keys() | current.get("global", {}).keys()):
        a, b = original.get("global", {}), current.get("global", {})
        if isinstance(a.get(key), str):
            _english(a[key], "original global rule")
        if isinstance(b.get(key), str):
            _english(b[key], "current global rule")
        global_rows.append(f"**{key}:** {_text(_fmt(a.get(key), key in a))} → {_text(_fmt(b.get(key), key in b))}")
    root_fields = _field("Original baseline", original_note)
    if any(s.get("note") for s in before.values()):
        notes = list(dict.fromkeys(_text(s["note"]) for s in before.values() if s.get("note")))
        root_fields += _field("Original-game notes", "\n".join(notes) + "\nThese describe the original baseline; the current column contains this world's configured values.")
    if global_rows:
        root_fields += _field("Global settings · original → current", "\n".join(global_rows) + "\n`n/a` = null; `—` = absent. Neither means zero.")
    admin_root = {"content": overview[0], "embeds": [{"title": f"Administrative balance · Original → {version}", "color": 10181887,
        "description": f"**{len(characters)} heroes · {len(now)} skills.**\n"
            "**Yellow / * = changed from the original values.** Unmarked rows retain the original values.\n"
            "Damage is per hit; area damage and damage per tick are separate fields. Wait means cooldown; Effect means statusDuration.\n"
            "Missing fields are —, never zero. Equivalent decimal representations are normalized for display only.",
        "fields": root_fields, "footer": footer("Admin reference")} ]}
    entries = [("character:" + c["id"], name) for name, c in characters.items()]
    results = _index("changelog:index", "changelog", "Version index", version_entries, {"content":
        f"# Changelog — Bizarre Arena\n**Current balance: {version}.**\n"
        "Historical entries describe each documented version; later changes may supersede earlier rules."}, config, bindings)
    results += changelogs
    results += _index("characters:index", "characters", "Character index", entries, {"content":
        f"# Characters and skills — Bizarre Arena\n**{len(characters)} characters · {len(now)} skills.**\n"
        "Quick skill descriptions and portraits.\n\n**Controls**\n"
        "• Attack: left mouse button / right VR trigger.\n• Skill: Q / left VR trigger.\n• Ultimate: E / left VR grip."}, config, bindings)
    results += character_cards
    balance_entries = [("balance:" + c["id"], name) for name, c in characters.items()]
    results += _index("balance:index", "balance", "Administrative character index", balance_entries, admin_root, config, bindings)
    results += [_record(f"balance:changes:{i}", "balance", {"content": f"**Changed skills · continued · part {i}**\n{chunk}"})
                for i, chunk in enumerate(overview[1:], 2)]
    results += balance_cards
    keys = [record["key"] for record in results]
    if len(keys) != len(set(keys)):
        raise ValueError("Rendered document keys are not unique")
    return results

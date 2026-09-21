"""Deterministic presentation checks, not a medical or semantic safety classifier.

Hard issues cover only empty text and the explicit emoji preference. Soft issues
are review hints: a repeated invitation or reflection can still be appropriate.
Inspection never rewrites the reply. Scans are linear in the current text, with
a fixed-size recent-history window and a fixed set of bounded text patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str

    def __post_init__(self) -> None:
        if self.severity not in {"hard", "soft"}:
            raise ValueError("Issue severity must be 'hard' or 'soft'.")


LENGTH_LIMITS = {"short": 240, "normal": 700, "long": 3000}
MAX_RECENT_ASSISTANT = 8
MAX_RECENT_CHARS = 3000

# Deliberately avoid blanket removal of the Dingbats and Miscellaneous Symbols
# blocks: they also contain useful ordinary symbols and mathematical notation.
_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # Regional indicator flags.
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
)
_EMOJI_SINGLETONS = {0x1F004, 0x1F0CF, 0x1F18E}
_EMOJI_SINGLETONS.update(range(0x1F191, 0x1F19B))
_EMOJI_BMP = frozenset(
    "⌚⌛⏩⏪⏫⏬⏰⏳◽◾☔☕♈♉♊♋♌♍♎♏♐♑♒♓♿"
    "⚓⚡⚪⚫⚽⚾⛄⛅⛎⛔⛪⛲⛳⛵⛺⛽✅✨❌❎❓❔❕❗"
    "➕➖➗➰➿⬛⬜⭐⭕"
    # These hearts, flowers and faces are commonly used without VS16.
    "☀☁☂☃☺☹☘♥♡❤❥❣❦❧✿❀❁❃❋"
)
# These characters are useful as plain text; strip them only in an explicit
# emoji presentation or a recognized emoji ZWJ sequence.
_EMOJI_OPTIONAL = frozenset(
    "©®™‼⁉↔↕↖↗↘↙↩↪⌨⏏⏭⏮⏯⏱⏲⏸⏹⏺Ⓜ▪▫▶◀◻◼"
    "☄☎☑☠☢☣☦☪☮☯☸☼♀♂♟♠♣♦♨⚒⚔⚕⚖⚗⚙⚛⚜⚠⚧"
    "⚰⚱⛈⛏⛑⛓⛩⛰⛱⛴⛷⛸⛹✂✈✉✌✍✏✒✔✖✝✡✳✴❄❇"
    "➡⤴⤵⬅⬆⬇〰〽㊗㊙🅰🅱🅾🅿🈁🈂🈚🈯🈲🈳🈴🈵🈶🈷🈸🈹🈺🉐🉑"
)
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_ZWJ = "\u200d"


def _emoji_base(char: str) -> bool:
    number = ord(char)
    return (char in _EMOJI_BMP or number in _EMOJI_SINGLETONS
            or any(start <= number <= end for start, end in _EMOJI_RANGES))


def strip_emoji(text: str) -> str:
    """Remove common decorative emoji and their sequence components.

    This is a practical Unicode subset, not a complete or versioned Unicode
    grapheme implementation. Ordinary digits, punctuation, math, and selectors
    or joiners outside recognized emoji are preserved. Whitespace is unchanged;
    terminal controls are the engine's responsibility.
    """
    kept: list[str] = []
    index = 0
    after_emoji = False
    joined_emoji = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char in "#*0123456789":
            end = index + 1
            if end < len(text) and text[end] == "\ufe0f":
                end += 1
            if end < len(text) and text[end] == "\u20e3":
                index = end + 1
                after_emoji, joined_emoji = True, False
                continue
        if (_emoji_base(char)
                or char in _EMOJI_OPTIONAL and (following == "\ufe0f" or joined_emoji)):
            # A joiner immediately before a recognized emoji belongs to that
            # sequence; ordinary script joiners elsewhere remain untouched.
            if kept and kept[-1] == _ZWJ:
                kept.pop()
            after_emoji, joined_emoji = True, False
            index += 1
            continue
        if after_emoji and (char in _VARIATION_SELECTORS
                            or 0xE0020 <= ord(char) <= 0xE007F):
            index += 1
            continue
        if after_emoji and char == _ZWJ:
            joined_emoji = True
            index += 1
            continue
        kept.append(char)
        after_emoji, joined_emoji = False, False
        index += 1
    return "".join(kept)


_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'", "`": "`"}


def _unquoted_text(text: str) -> str:
    """Hide simple balanced, single-line quotations; preserve unmatched ones.

    This does not try to parse arbitrary Markdown or decide whether quoting a
    question is itself an invitation. Apostrophes inside words are not quotes.
    """
    output: list[str] = []
    pending: list[str] = []
    closing = ""
    escaped = False
    for index, char in enumerate(text):
        if char == "\n" and closing:
            output.extend(pending)
            pending, closing = [], ""
        if closing:
            pending.append(char)
            if char == closing and not escaped:
                if char != "'" or index + 1 == len(text) or not text[index + 1].isalnum():
                    # A separator prevents words on either side from joining.
                    output.append(" ")
                    pending, closing = [], ""
        elif char in _QUOTE_PAIRS and not escaped:
            if char == "'" and index and text[index - 1].isalnum():
                output.append(char)
            else:
                pending, closing = [char], _QUOTE_PAIRS[char]
        else:
            output.append(char)
        escaped = char == "\\" and not escaped
    output.extend(pending)
    return "".join(output)


# Each pattern names one recognizable wording family, not all empathic language.
_INVITATIONS = (
    re.compile(r"随时(?:告诉我|跟我说|和我说|来找我)"),
    re.compile(r"(?:愿意说说|愿意[，, ]{0,2}(?:可以)?说说)"),
    re.compile(r"(?:你)?(?:暂时)?不想说(?:也)?(?:没关系|没事)"),
    re.compile(r"我(?:会)?(?:一直)?在(?:这里|这儿)(?:陪(?:着)?你|听你说)"),
    re.compile(r"\bi(?:'m| am) here (?:for you|to listen)\b", re.I),
    re.compile(r"\bfeel free to (?:tell me|share|talk)\b", re.I),
)
_CLOSING = re.compile(
    r"祝(?:你|您)[^。！？!?\n]{0,24}愉快"
    r"|剩下(?:的)?(?:时间|一天|时光)[^。！？!?\n]{0,16}(?:愉快|开心|顺利)"
    r"|\b(?:enjoy|have) (?:the rest of your|a (?:nice|good|great)) "
    r"(?:day|evening|night|weekend)\b", re.I,
)
_REFLECTION_START = re.compile(r"^(?:听起来|你说的是|也就是说|你的意思是)")


def _short_reflection_shape(text: str) -> bool:
    """Recognize a short one-sentence opener, without inferring its meaning."""
    text = text.strip()
    return (len(text) <= 180 and bool(_REFLECTION_START.match(text))
            and not any(mark in text for mark in "?？\n")
            and not any(mark in text.rstrip("。.!！ ") for mark in "。.!！;；"))


def inspect_reply(text: str, *, question_limit: int | None = None, emoji_mode: str = "off",
                  recent_assistant: list[str] | None = None,
                  length_hint: str | None = None, allow_closing: bool = False) -> list[Issue]:
    """Return unique observable issue codes without changing text or history.

    Optional question counts omit simple balanced quotations. ``None`` disables
    the question-count or length heuristic; ordinary companion replies do not
    require these per-turn quotas. Repetition compares the
    current reply with at most eight recent replies, each capped at 3000 chars.
    ``repeated_reflection_heuristic`` means repeated short reflection *shape*;
    it cannot establish paraphrase-only content, usefulness, or clinical quality.
    Unknown non-None length hints use the normal limit. Only ``emoji_mode='off'`` bans
    emoji. All nonempty-content style findings are soft review hints.
    """
    if question_limit is not None and (
            not isinstance(question_limit, int) or isinstance(question_limit, bool) or question_limit < 0):
        raise ValueError("question_limit must be None or a nonnegative integer.")
    issues: list[Issue] = []
    if not text.strip():
        issues.append(Issue("empty_reply", "hard"))
    if emoji_mode == "off" and strip_emoji(text) != text:
        issues.append(Issue("emoji_not_allowed", "hard"))

    unquoted = _unquoted_text(text)
    if question_limit is not None and unquoted.count("?") + unquoted.count("？") > question_limit:
        issues.append(Issue("question_budget", "soft"))
    if length_hint is not None and len(text) > LENGTH_LIMITS.get(length_hint, LENGTH_LIMITS["normal"]):
        issues.append(Issue("length_hint", "soft"))

    recent = [_unquoted_text(item[:MAX_RECENT_CHARS])
              for item in (recent_assistant or [])[-MAX_RECENT_ASSISTANT:]]
    for pattern in _INVITATIONS:
        matches = pattern.finditer(unquoted)
        if next(matches, None) is not None and (
                next(matches, None) is not None or any(pattern.search(item) for item in recent)):
            issues.append(Issue("repeated_invitation", "soft"))
            break
    if (_short_reflection_shape(unquoted)
            and sum(_short_reflection_shape(item) for item in recent[-3:]) >= 2):
        issues.append(Issue("repeated_reflection_heuristic", "soft"))
    if not allow_closing and _CLOSING.search(unquoted):
        issues.append(Issue("premature_closing", "soft"))
    return issues

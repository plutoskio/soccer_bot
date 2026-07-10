from __future__ import annotations

from .database import normalized_name


# NFKD handles combining accents (for example, é -> e) but does not expand
# several standalone Latin letters commonly found in footballer names. This
# table is deliberately used only for contextual name comparison. Canonical
# names and source identity keys continue to use ``normalized_name`` so this
# cannot change existing stable IDs.
API_PLAYER_COMPARISON_TRANSLITERATION = str.maketrans({
    "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ø": "o", "Ø": "O",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ı": "i", "ŋ": "n", "Ŋ": "N",
    "ŧ": "t", "Ŧ": "T", "ħ": "h", "Ħ": "H", "ß": "ss", "ẞ": "SS",
    "ǿ": "oe", "Ǿ": "Oe", "ĸ": "k",
    "ə": "e", "Ə": "E", "ƒ": "f",
})


def api_player_comparison_name(value: str) -> str:
    """Normalize provider names for comparison without changing identity keys."""
    return normalized_name(
        str(value or "").translate(API_PLAYER_COMPARISON_TRANSLITERATION)
    )


def compatible_api_player_names(first: str, second: str) -> bool:
    """Match a full and abbreviated provider name only in strong cases."""
    first_tokens = api_player_comparison_name(first).split()
    second_tokens = api_player_comparison_name(second).split()
    if not first_tokens or not second_tokens:
        return False
    if first_tokens == second_tokens:
        return True
    if sorted(first_tokens) == sorted(second_tokens):
        return True
    left, right = first_tokens[0], second_tokens[0]
    same_initial = left[0] == right[0]
    if same_initial and first_tokens[-1] == second_tokens[-1]:
        return True
    return bool(
        len(first_tokens) == len(second_tokens)
        and first_tokens[1:] == second_tokens[1:]
        and (
            (len(left) == 1 and right.startswith(left))
            or (len(right) == 1 and left.startswith(right))
        )
    )


def compatible_api_player_compound_names(first: str, second: str) -> bool:
    """Compare an abbreviated name with a shortened compound surname."""
    first_tokens = api_player_comparison_name(first).split()
    second_tokens = api_player_comparison_name(second).split()
    if not first_tokens or not second_tokens:
        return False
    left_first, right_first = first_tokens[0], second_tokens[0]
    if left_first[0] != right_first[0]:
        return False
    if len(left_first) != 1 and len(right_first) != 1:
        return False
    left_surnames = {token for token in first_tokens[1:] if len(token) > 1}
    right_surnames = {token for token in second_tokens[1:] if len(token) > 1}
    if not left_surnames or not right_surnames:
        return False
    shorter, longer = (
        (left_surnames, right_surnames)
        if len(left_surnames) <= len(right_surnames)
        else (right_surnames, left_surnames)
    )
    return bool(shorter < longer and all(len(token) >= 4 for token in shorter))

"""matcher.py — Pure stdlib Aho-Corasick matcher for multi-alias skill recognition.

Scales to 10k+ aliases in a single O(n) pass over text.

Public API
----------
build_automaton(alias_map: dict[str, str]) -> automaton
    alias_map: {normalized_alias -> canonical_label}
    Aliases must be pre-normalised (lowercased, whitespace-collapsed).

find(text: str, automaton) -> set[str]
    Returns canonical labels whose aliases appear in text with word boundaries.
    Word-boundary rule (mirrors config._alias_pattern):
        The character immediately before the match start and after the match end
        must NOT be in [a-z0-9].  Text is lowercased before matching.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, FrozenSet, Set, Tuple


# ---------------------------------------------------------------------------
# Trie node
# Each node's `output` stores (canonical_label, alias_length) tuples so that
# find() can recover the span start without a second trie walk.
# ---------------------------------------------------------------------------

class _Node:
    __slots__ = ("children", "fail", "output")

    def __init__(self) -> None:
        self.children: Dict[str, "_Node"] = {}
        self.fail: "_Node | None" = None
        # set of (label: str, alias_len: int)
        self.output: Set[Tuple[str, int]] = set()


class _Automaton:
    """Opaque handle returned by build_automaton and consumed by find."""
    __slots__ = ("root",)

    def __init__(self, root: _Node) -> None:
        self.root = root


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_automaton(alias_map: Dict[str, str]) -> _Automaton:
    """Compile alias_map into an Aho-Corasick automaton.

    Parameters
    ----------
    alias_map:
        {normalised_alias: canonical_label}
        Multiple aliases may map to the same label; duplicate aliases are
        silently last-write-wins (both point to a label so order is fine).
    """
    root = _Node()

    # Phase 1: insert aliases into trie
    for alias, label in alias_map.items():
        if not alias:
            continue
        node = root
        for ch in alias:
            if ch not in node.children:
                node.children[ch] = _Node()
            node = node.children[ch]
        node.output.add((label, len(alias)))

    # Phase 2: BFS to build failure links and propagate outputs
    queue: deque[_Node] = deque()
    for child in root.children.values():
        child.fail = root
        queue.append(child)

    while queue:
        cur = queue.popleft()
        for ch, child in cur.children.items():
            # Find longest proper suffix of (path to cur) + ch that is a trie prefix
            fail = cur.fail
            while fail is not None and ch not in fail.children:
                fail = fail.fail
            child.fail = (
                fail.children[ch]
                if fail is not None and ch in fail.children
                else root
            )
            # Avoid self-loop at root's immediate children
            if child.fail is child:
                child.fail = root
            # Propagate outputs: this node matches everything its failure node does
            child.output = child.output | child.fail.output
            queue.append(child)

    return _Automaton(root)


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------

_WORD_CHAR: FrozenSet[str] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

# Ambiguous short tokens (e.g. the language "R") need a STRICTER neighbour rule
# than the default "not [a-z0-9]": a bare "r" must sit between explicit list
# separators, so "Python, R, SQL" and "R/Python" match but "R&D", "(r)", "R."
# and prose "R" do not. config._alias_pattern imports these two sets so the
# regex path applies the identical rule (the two matchers stay in lockstep).
_STRICT_TOKENS: FrozenSet[str] = frozenset({"r"})
_STRICT_NEIGHBOR_OK: FrozenSet[str] = frozenset(" ,/|\n\t;•")


def _accept(text: str, start: int, end: int) -> bool:
    """Boundary check. Strict tokens require a separator (not just a non-word
    char) on each side; everything else uses the default [a-z0-9] guard."""
    if text[start:end] in _STRICT_TOKENS:
        left_ok = start == 0 or text[start - 1] in _STRICT_NEIGHBOR_OK
        right_ok = end == len(text) or text[end] in _STRICT_NEIGHBOR_OK
        return left_ok and right_ok
    before_ok = start == 0 or text[start - 1] not in _WORD_CHAR
    after_ok = end == len(text) or text[end] not in _WORD_CHAR
    return before_ok and after_ok


def find(text: str, automaton: _Automaton) -> Set[str]:
    """Return canonical labels matched in *text* with word-boundary guards.

    The match is accepted only when the character before the alias start and
    after the alias end are not in [a-z0-9] (same rule as config._alias_pattern).
    """
    text = text.lower()
    results: Set[str] = set()
    node = automaton.root

    for i, ch in enumerate(text):
        # Follow failure links until we can consume ch or reach root
        while node is not automaton.root and ch not in node.children:
            node = node.fail  # type: ignore[assignment]
        if ch in node.children:
            node = node.children[ch]
        # i is the index of the last consumed character
        for label, alias_len in node.output:
            start = i - alias_len + 1
            if _accept(text, start, i + 1):
                results.add(label)

    return results


def find_detailed(text: str, automaton: _Automaton) -> Dict[str, list]:
    """Like find(), but return {label: [(start, end), ...]} for every boundary-
    valid hit. Spans index into the lowercased text. Used by the scorer to weight
    requirements by frequency and JD-region position."""
    text = text.lower()
    results: Dict[str, list] = {}
    node = automaton.root

    for i, ch in enumerate(text):
        while node is not automaton.root and ch not in node.children:
            node = node.fail  # type: ignore[assignment]
        if ch in node.children:
            node = node.children[ch]
        for label, alias_len in node.output:
            start = i - alias_len + 1
            end = i + 1
            if _accept(text, start, end):
                results.setdefault(label, []).append((start, end))

    return results

# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""General-purpose cleaner for text scraped from web pages.

The input is typically markdown produced by an HTML-to-markdown scraper
(html2text / markdownify / trafilatura / etc.), possibly with literal
escape sequences (``\\n``, ``\\uXXXX``, ``\\{``) left over from a JSON dump.

`clean_scraped_text` turns it into plain prose suitable for sending as a
message: no markdown syntax, no URLs, normalized whitespace.
"""

from __future__ import annotations

import html
import re
import unicodedata


# --- Escape decoding --------------------------------------------------------

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_LATEX_LIKE = {
    r"\textcopyright": "©",
    r"\textregistered": "®",
    r"\texttrademark": "™",
    r"\textendash": "–",
    r"\textemdash": "—",
}


def _decode_escapes(text: str) -> str:
    """Decode escape sequences that appear literally (two chars: ``\\`` + ``n``)."""
    text = _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    # Whitespace escapes
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = text.replace("\\t", "\t").replace("\\r", "\n")
    # Backslash-escaped punctuation common in markdown / bibtex dumps
    for ch in "{}%&#_$~^":
        text = text.replace(f"\\{ch}", ch)
    for tok, repl in _LATEX_LIKE.items():
        text = text.replace(tok, repl)
    # HTML entities
    return html.unescape(text)


# --- Markdown / HTML stripping ---------------------------------------------

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_REF_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\[[^\]]*\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_MD_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_MD_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)
_AUTOLINK_RE = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>")
_BARE_URL_RE = re.compile(r"(?<![\w/])(?:https?|ftp)://[^\s)>\]]+")

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$", re.MULTILINE)
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>+\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s{0,6}[*+\-]\s+", re.MULTILINE)
_OL_RE = re.compile(r"^\s{0,6}\d+[.)]\s+", re.MULTILINE)

_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDER_RE = re.compile(r"__(.+?)__", re.DOTALL)
_EM_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_EM_UNDER_RE = re.compile(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)

_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{2,}:?(\s*\|\s*:?-{2,}:?)+\s*\|?\s*$", re.MULTILINE
)
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$", re.MULTILINE)

_INLINE_WS_RE = re.compile(r"[ \t\u00a0\u2007\u202f]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_URL_ONLY_LINE_RE = re.compile(r"^\s*(?:https?|ftp)://\S+\s*$", re.MULTILINE)
_HAS_WORD_CHAR_RE = re.compile(r"[^\W_]", re.UNICODE)

# A real sentence terminator: ``.!?`` preceded by a lowercase letter / digit
# (so initials like "J. L." don't count) and followed by whitespace or end.
_SENTENCE_END_RE = re.compile(r"(?<=[a-z0-9])[.!?][\"')\]]?(?=\s|$)")
_WORD_RE = re.compile(r"\b[^\W\d_]{2,}\b", re.UNICODE)


def _flatten_table_row(match: re.Match[str]) -> str:
    cells = [c.strip() for c in match.group(1).split("|") if c.strip()]
    return "  ".join(cells)


def _strip_markdown(text: str) -> str:
    text = _HTML_COMMENT_RE.sub("", text)
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)

    text = _MD_REF_IMAGE_RE.sub(r"\1", text)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _MD_REF_LINK_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_REF_DEF_RE.sub("", text)
    text = _AUTOLINK_RE.sub("", text)
    text = _BARE_URL_RE.sub("", text)

    text = _HTML_TAG_RE.sub("", text)

    text = _TABLE_SEP_RE.sub("", text)
    text = _TABLE_ROW_RE.sub(_flatten_table_row, text)

    text = _HR_RE.sub("", text)
    text = _SETEXT_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _OL_RE.sub("", text)

    text = _STRIKE_RE.sub(r"\1", text)
    text = _BOLD_STAR_RE.sub(r"\1", text)
    text = _BOLD_UNDER_RE.sub(r"\1", text)
    text = _EM_STAR_RE.sub(r"\1", text)
    text = _EM_UNDER_RE.sub(r"\1", text)

    return text


def _drop_noise_lines(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        # Lines with no letters or digits at all are decorative residue.
        if not _HAS_WORD_CHAR_RE.search(stripped):
            continue
        out.append(line)
    return "\n".join(out)


def _is_prose_line(line: str, min_words: int) -> bool:
    """A line counts as prose if it has enough words and at least one
    real sentence terminator (initials / decimal points don't qualify)."""
    text = line.strip()
    if not text:
        return False
    word_count = len(_WORD_RE.findall(text))
    if word_count < min_words:
        return False
    return _SENTENCE_END_RE.search(text) is not None


def _keep_prose_paragraphs(
    text: str, *, min_words: int
) -> tuple[list[list[str]], list[str]]:
    """Split ``text`` into paragraphs on blank-line boundaries and, within
    each paragraph, keep only the lines that look like sentences.

    Returns ``(kept_paragraphs, discarded_lines)`` where ``kept_paragraphs``
    is a list with one entry per paragraph and each entry is the list of
    prose lines that survived inside that paragraph (lines are NOT merged).
    Paragraphs that end up with no prose lines are dropped entirely.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []
    discarded: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(current.copy())
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # Blank line: paragraph boundary.
            flush()
            continue
        if _is_prose_line(line, min_words=min_words):
            current.append(stripped)
        else:
            discarded.append(stripped)
    flush()
    return paragraphs, discarded


def clean_scraped_text(
    raw: str,
    *,
    decode_escapes: bool = True,
    normalize_unicode: bool = True,
    collapse_blank_lines: bool = True,
    keep_paragraphs_only: bool = True,
    min_words: int = 5,
    return_discarded: bool = False,
) -> list[list[str]] | tuple[list[list[str]], list[str]]:
    """Clean text scraped from a web page into plain prose.

    Args:
        raw: The scraped text (typically markdown, possibly JSON-escaped).
        decode_escapes: Decode literal ``\\n``, ``\\uXXXX``, ``\\{`` and similar.
        normalize_unicode: Apply NFKC normalization (e.g. ``\\u00a0`` → space).
        collapse_blank_lines: Collapse runs of 3+ newlines down to 2.
        keep_paragraphs_only: Drop lines that don't look like full sentences
            (short labels, single names, nav crumbs, list items, headings).
            Adjacent kept lines are joined into paragraphs separated by a
            blank line.
        min_words: Minimum word count for a line to be kept as prose.
        return_discarded: If True, return ``(kept_lines, discarded_lines)``
            as two lists (one entry per paragraph for kept, one entry per
            dropped line for discarded). Useful for debugging.

    Returns:
        By default, a list of paragraphs where each paragraph is itself a
        list of its lines. When ``return_discarded`` is True, a
        ``(kept_paragraphs, discarded_lines)`` tuple where
        ``kept_paragraphs`` has the same list-of-lists shape.
    """
    if not raw:
        return ([], []) if return_discarded else []

    text = raw
    if decode_escapes:
        text = _decode_escapes(text)
    if normalize_unicode:
        text = unicodedata.normalize("NFKC", text)

    text = _strip_markdown(text)
    text = _URL_ONLY_LINE_RE.sub("", text)
    text = _drop_noise_lines(text)

    text = _INLINE_WS_RE.sub(" ", text)
    text = _TRAILING_WS_RE.sub("\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    if collapse_blank_lines:
        text = _MULTI_NL_RE.sub("\n\n", text)

    kept_paragraphs: list[list[str]] = []
    discarded: list[str] = []
    if keep_paragraphs_only:
        kept_paragraphs, discarded = _keep_prose_paragraphs(
            text, min_words=min_words
        )
    else:
        kept_paragraphs = [
            [line] for line in text.splitlines() if line.strip()
        ]

    if return_discarded:
        return kept_paragraphs, discarded
    return kept_paragraphs


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).parent / "raw.txt"),
        help="Path to the scraped text file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Also print the lines that were discarded by the prose filter.",
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=12,
        help="Minimum word count per line to be kept as prose.",
    )
    args = parser.parse_args()

    raw = Path(args.path).read_text(encoding="utf-8")
    kept, discarded = clean_scraped_text(
        raw, min_words=args.min_words, return_discarded=True
    )

    print("=" * 72)
    print(f"KEPT ({len(kept)} paragraphs)")
    print("=" * 72)
    for i, paragraph in enumerate(kept):
        print(f"[paragraph {i}] ({len(paragraph)} lines)")
        for line in paragraph:
            print(f"  - {line}")

    if args.debug:
        print()
        print("=" * 72)
        print(f"DISCARDED ({len(discarded)} lines)")
        print("=" * 72)
        for line in discarded:
            print(f"- {line}")
    
    print('=' * 72)
    all_lines = [line for paragraph in kept for line in paragraph]
    txt = " ".join(all_lines)
    print(
        f"Final cleaned text has {len(txt)} characters, "
        f"{len(txt.split())} words, and {len(all_lines)} lines"
    )
    print(f"Final cleaned text has {len(all_lines)} sentences")
    for i, sent in enumerate(all_lines):
        print(f"{i}. {sent}")

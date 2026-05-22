"""Paranoid Markdown sanitiser for write paths (`wiki_save`, `wiki_log_append`).

Strips the established RAG-poisoning vectors before content is committed:

  - HTML comments — hide arbitrary instructions from rendered output
  - Zero-width characters — used to smuggle hidden tokens past humans
  - Bidi overrides — flip displayed text direction without changing bytes
  - Raw HTML — only a tiny whitelist of structural tags is allowed
  - Inline CSS — `color: white`, `display: none`, `font-size: 0` etc.
  - data: image URLs — never load by reference, always inline encoded

The function returns the cleaned text plus a `SanitizeReport` describing
what was removed. The report is meant to be mirrored into the git
commit message so an operator browsing the repo on Gitea sees exactly
when sanitisation kicked in.

Tightly bounded by design: we don't try to make a Markdown safe-renderer
out of this. We strip known-bad shapes and let the rest pass through.
The agent's wiki skills and the in-VM `CLAUDE.md` disclaimer carry the
rest of the trust story.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Characters that render as zero width. Includes the more obscure
# variation selectors which have been used in real prompt-injection
# samples (see Snyk Labs MCP report).
_ZERO_WIDTH = (
    "​"  # ZERO WIDTH SPACE
    "‌"  # ZERO WIDTH NON-JOINER
    "‍"  # ZERO WIDTH JOINER
    "⁠"  # WORD JOINER
    "﻿"  # ZERO WIDTH NO-BREAK SPACE (BOM)
    "᠎"  # MONGOLIAN VOWEL SEPARATOR
)

# Bidirectional formatting controls. These flip rendered text direction
# and have been weaponised against rendered Markdown in shared KBs.
_BIDI = (
    "‪‫‬‭‮"  # LRE / RLE / PDF / LRO / RLO
    "⁦⁧⁨⁩"        # LRI / RLI / FSI / PDI
)

_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH}]")
_BIDI_RE = re.compile(f"[{_BIDI}]")

# Match `<!-- ... -->` HTML comments across newlines. Non-greedy so
# adjacent comments don't collapse together.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Whitelist of HTML tags that survive sanitisation. Plain structural
# tags only — nothing with event handlers or external references.
_ALLOWED_TAGS = frozenset(
    {"br", "details", "summary", "sub", "sup", "kbd", "abbr"}
)

# Match opening, closing, or self-closing tags. Captures the tag name
# so we can decide allow/strip.
_HTML_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")

# Match Markdown image syntax: ![alt](url "title"). We only target the
# url portion to detect `data:` scheme; alt text can mention anything.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((\s*data:[^)]*)\)")

# Inline style="..." attributes can hide content with `color: white`,
# `display: none`, `visibility: hidden`, font-size 0/1. We strip the
# whole style attribute regardless — there is no legitimate inline-CSS
# reason in wiki content.
_STYLE_ATTR_RE = re.compile(r"""\s+style\s*=\s*(["'])(?:(?!\1).)*\1""", re.IGNORECASE)


@dataclass
class SanitizeReport:
    """Counts of patterns stripped, for commit-message visibility."""

    html_comments: int = 0
    zero_width_chars: int = 0
    bidi_chars: int = 0
    forbidden_tags: int = 0
    style_attrs: int = 0
    data_images: int = 0
    raw_tag_names: list[str] = field(default_factory=list)
    """Names of disallowed tags that were stripped, for the commit msg."""

    @property
    def is_empty(self) -> bool:
        return (
            self.html_comments == 0
            and self.zero_width_chars == 0
            and self.bidi_chars == 0
            and self.forbidden_tags == 0
            and self.style_attrs == 0
            and self.data_images == 0
        )

    def summary(self) -> str:
        """One-line human summary, suitable as a commit-message trailer."""
        if self.is_empty:
            return "sanitize: clean"
        parts = []
        if self.html_comments:
            parts.append(f"{self.html_comments} html-comment")
        if self.zero_width_chars:
            parts.append(f"{self.zero_width_chars} zero-width")
        if self.bidi_chars:
            parts.append(f"{self.bidi_chars} bidi")
        if self.forbidden_tags:
            tags = ",".join(sorted(set(self.raw_tag_names)))
            parts.append(f"{self.forbidden_tags} html-tag({tags})")
        if self.style_attrs:
            parts.append(f"{self.style_attrs} style-attr")
        if self.data_images:
            parts.append(f"{self.data_images} data-image")
        return "sanitize: stripped " + ", ".join(parts)


def sanitize(text: str) -> tuple[str, SanitizeReport]:
    """Return (cleaned_text, report). Idempotent: ``sanitize(sanitize(x)[0])``
    yields the same text and an empty report.
    """
    report = SanitizeReport()

    # re.Pattern.subn already returns (new_string, count).
    text, report.html_comments = _HTML_COMMENT_RE.subn("", text)
    text, report.zero_width_chars = _ZERO_WIDTH_RE.subn("", text)
    text, report.bidi_chars = _BIDI_RE.subn("", text)

    # Strip style attributes from any *remaining* tags (allowed ones too:
    # an allowed `<details style="display:none">` would still be a hiding
    # vector).
    text, report.style_attrs = _STYLE_ATTR_RE.subn("", text)

    # Strip data: image references — replace the whole image with its
    # alt text alone, so the page still reads naturally.
    def _data_image_repl(match: re.Match[str]) -> str:
        report.data_images += 1
        return match.group(1)

    text = _MD_IMAGE_RE.sub(_data_image_repl, text)

    # Walk every HTML tag occurrence; drop ones outside the whitelist.
    def _tag_repl(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if name in _ALLOWED_TAGS:
            return match.group(0)
        report.forbidden_tags += 1
        report.raw_tag_names.append(name)
        return ""

    text = _HTML_TAG_RE.sub(_tag_repl, text)

    return text, report

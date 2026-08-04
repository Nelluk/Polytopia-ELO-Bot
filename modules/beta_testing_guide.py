"""Development-only, repository-backed wider-beta testing guidance."""

from __future__ import annotations

from pathlib import Path


CHECKLIST_PATH = Path(__file__).resolve().parent.parent / 'docs' / 'BETA_WHAT_TO_TEST.md'
MAX_MESSAGE_LENGTH = 1900


def load_checklist(path: Path = CHECKLIST_PATH) -> str:
    """Load the tracked checklist without consulting runtime or database state."""

    return path.read_text(encoding='utf-8').strip()


def message_pages(markdown: str, *, maximum: int = MAX_MESSAGE_LENGTH) -> tuple[str, ...]:
    """Split Markdown at section boundaries while staying below Discord limits."""

    if not markdown.strip():
        return ('## 🧪 WHAT TO TEST\nNo testing items are currently listed.',)
    sections: list[str] = []
    current: list[str] = []
    for line in markdown.strip().splitlines():
        candidate = '\n'.join((*current, line))
        if current and line.startswith('## ') and len(candidate) > maximum:
            sections.append('\n'.join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append('\n'.join(current))

    pages: list[str] = []
    for section in sections:
        if len(section) <= maximum:
            pages.append(section)
            continue
        lines: list[str] = []
        for line in section.splitlines():
            candidate = '\n'.join((*lines, line))
            if lines and len(candidate) > maximum:
                pages.append('\n'.join(lines))
                lines = [line]
            else:
                lines.append(line)
        if lines:
            pages.append('\n'.join(lines))
    return tuple(pages)

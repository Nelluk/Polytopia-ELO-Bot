"""Development-only, repository-backed wider-beta testing guidance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CHECKLIST_PATH = Path(__file__).resolve().parent.parent / 'docs' / 'BETA_WHAT_TO_TEST.md'
MAX_MESSAGE_LENGTH = 1900


@dataclass(frozen=True)
class ChecklistSection:
    key: str
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class ChecklistGuide:
    title: str
    introduction: str
    sections: tuple[ChecklistSection, ...]


def load_checklist(path: Path = CHECKLIST_PATH) -> str:
    """Load the tracked checklist without consulting runtime or database state."""

    return path.read_text(encoding='utf-8').strip()


def _section_key(title: str) -> str:
    normalized = ''.join(
        character.lower() if character.isalnum() else '-'
        for character in title
    )
    return '-'.join(part for part in normalized.split('-') if part)


def parse_checklist(markdown: str) -> ChecklistGuide:
    """Parse the tracked guide into bounded navigable sections.

    The Markdown file remains the durable checklist authority.  Discord gets
    a structured view of that source instead of eight pages of followups.
    """

    title = 'WHAT TO TEST'
    introduction: list[str] = []
    sections: list[ChecklistSection] = []
    current_title: str | None = None
    current_items: list[str] = []
    current_item: list[str] = []

    def finish_item() -> None:
        nonlocal current_item
        if current_item:
            current_items.append(' '.join(current_item).strip())
            current_item = []

    def finish_section() -> None:
        nonlocal current_title, current_items
        finish_item()
        if current_title is not None:
            sections.append(ChecklistSection(
                key=_section_key(current_title),
                title=current_title,
                items=tuple(item for item in current_items if item),
            ))
        current_title = None
        current_items = []

    for raw_line in markdown.strip().splitlines():
        line = raw_line.strip()
        if line.startswith('# ') and not line.startswith('## '):
            title = line[2:].strip().removeprefix('🧪').strip() or title
            continue
        if line.startswith('## '):
            finish_section()
            current_title = line[3:].strip()
            continue
        if current_title is None:
            if line:
                introduction.append(line)
            continue
        if line.startswith('- '):
            finish_item()
            current_item = [line[2:].strip()]
        elif line and current_item:
            current_item.append(line)
    finish_section()
    return ChecklistGuide(
        title=title,
        introduction=' '.join(introduction).strip(),
        sections=tuple(section for section in sections if section.items),
    )


def load_guide(path: Path = CHECKLIST_PATH) -> ChecklistGuide:
    return parse_checklist(load_checklist(path))


def item_pages(
        section: ChecklistSection,
        *,
        maximum_items: int = 5,
        maximum_characters: int = 2800) -> tuple[tuple[str, ...], ...]:
    """Create small item pages without splitting one checklist item."""

    pages: list[tuple[str, ...]] = []
    current: list[str] = []
    length = 0
    for item in section.items:
        item_length = len(item) + 8
        if current and (
                len(current) >= maximum_items
                or length + item_length > maximum_characters):
            pages.append(tuple(current))
            current = []
            length = 0
        current.append(item)
        length += item_length
    if current:
        pages.append(tuple(current))
    return tuple(pages) or (('No tests are listed in this section.',),)


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

import re
from datetime import datetime
from typing import Dict, Any, Optional
from config import settings
from src.ingestion.parser.patterns import (
    DATE_RANGE,
    DATE_ISO,
    DATE_SLASH,
    DATE_ENGLISH,
    TIME_RANGE,
    TIME_PATTERN,
    DUE_DATE_PATTERN,
    START_DATE_PATTERN,
    NATURAL_DATE_PATTERN,
    PRIORITY_PATTERN,
    TAG_PATTERN,
    RECURRENCE_PATTERN,
)


def _resolve_natural_date(
    candidate: str, base: Optional[datetime] = None
) -> Optional[str]:
    """Resolve a natural-language date phrase (e.g. 'tomorrow', 'next friday',
    'in 3 days') to an ISO date string (YYYY-MM-DD). Returns None if it can't
    be resolved. Uses `dateparser` (plain rule-based parsing, no AI/LLM calls).
    """
    if not candidate or not candidate.strip():
        return None
    base = base or datetime.now()
    parse_settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": base}
    try:
        import dateparser

        parsed = dateparser.parse(candidate, settings=parse_settings, languages=["en"])
        if not parsed:
            from dateparser.search import search_dates

            found = search_dates(candidate, settings=parse_settings, languages=["en"])
            if found:
                parsed = found[0][1]
        if parsed:
            explicit_time = re.search(
                r"\b(?:at\s+)?\d{1,2}:\d{2}\s*(?:am|pm)?\b"
                r"|\b(?:at\s+)?\d{1,2}\s*(?:am|pm)\b",
                candidate,
                re.IGNORECASE,
            )
            if explicit_time:
                return parsed.isoformat(timespec="minutes")
            return parsed.date().isoformat()
    except Exception:
        pass
    return None


def _normalize_explicit_due(value: str) -> str:
    value = value.replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        try:
            import dateparser

            parsed = dateparser.parse(value, languages=["en"])
            if parsed:
                return parsed.isoformat(timespec="minutes")
        except Exception:
            pass
    return value


def _extract_heading_level(tag: str) -> Optional[int]:
    """Extract numeric heading level from tag like 'h1', 'h2', etc."""
    if tag and tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
        return int(tag[1])
    return None


def _detect_event(content: str) -> Optional[Dict[str, Any]]:
    """Check if content contains event-like patterns. Returns metadata or None."""
    meta: Dict[str, Any] = {}

    dr = DATE_RANGE.search(content)
    if dr:
        meta["date_start"] = dr.group(1).replace("/", "-")
        meta["date_end"] = dr.group(2).replace("/", "-")
    else:
        dates = DATE_ISO.findall(content) or DATE_SLASH.findall(content)
        en = DATE_ENGLISH.findall(content)
        if dates:
            meta["date"] = dates[0].replace("/", "-")
        elif en:
            meta["date"] = en[0]

    tr = TIME_RANGE.search(content)
    if tr:
        meta["time_start"] = tr.group(1).strip()
        meta["time_end"] = tr.group(2).strip()
    else:
        ts = TIME_PATTERN.findall(content)
        if ts:
            meta["time"] = ts[0].strip()

    return meta if meta else None


def _extract_task_meta(
    content: str, task_text: str, status_val: Optional[str], block_id: int
) -> Dict[str, Any]:
    """Build a task dict with all extracted metadata."""
    todoist_marker = re.search(r"<!--\s*todoist:([^\s>]+)\s*-->", content)
    todoist_id = todoist_marker.group(1) if todoist_marker else None
    task_text = re.sub(r"<!--\s*todoist:[^\s>]+\s*-->", "", task_text).strip()

    project_match = re.search(
        r"(?:^|\s)proj:(?:\"([^\"]+)\"|'([^']+)'|(.+?))(?=\s+(?:#|@|\[|<!--)|$)",
        task_text,
        re.IGNORECASE,
    )
    project_name = next(
        (value.strip() for value in project_match.groups() if value), None
    ) if project_match else None
    if project_match:
        task_text = (task_text[:project_match.start()] + " " + task_text[project_match.end():]).strip()

    is_done = 0
    if status_val:
        normalized = status_val.strip().lower()
        if normalized in {"done", "x", "v"}:
            is_done = 1

    # Due date
    due_date = None

    # 1) Explicit ISO due marker: "due: 2026-04-22" / "@due 2026/04/22" (exact, no parsing needed)
    due_match = DUE_DATE_PATTERN.search(task_text)
    if due_match:
        due_date = _normalize_explicit_due(due_match.group(1))
        task_text = task_text.replace(due_match.group(0), "")
    else:
        # 2) Explicit due marker with a natural-language phrase: "due: tomorrow",
        #    "@due next friday"
        due_marker = re.search(
            r"(?:due:|@due)\s*(.+?)(?=\s+(?:@|#|\[)|$)", task_text, re.IGNORECASE
        )
        if due_marker:
            due_date = _resolve_natural_date(due_marker.group(1))
            if due_date:
                task_text = task_text.replace(due_marker.group(0), "")

        # 3) Bare natural-language date phrase anywhere in the task text, with no
        #    marker at all: "Buy milk tomorrow", "Call mom next friday"
        if not due_date:
            nat_match = NATURAL_DATE_PATTERN.search(task_text)
            if nat_match:
                resolved = _resolve_natural_date(nat_match.group(0))
                if resolved:
                    due_date = resolved
                    task_text = task_text.replace(nat_match.group(0), "")

    # Start date
    start_match = START_DATE_PATTERN.search(task_text)
    start_date = start_match.group(1).replace("/", "-") if start_match else None

    # Priority
    pri_match = PRIORITY_PATTERN.search(task_text)
    priority = None
    if pri_match:
        val = pri_match.group(1)
        priority = "high" if "!" in val else "low" if val.count("?") > 1 else "medium"

    # Tags
    tags_list = TAG_PATTERN.findall(task_text)
    tags = ",".join(tags_list) if tags_list else None

    # Recurrence
    rec_match = RECURRENCE_PATTERN.search(task_text)
    recurrence = rec_match.group(1).lower() if rec_match else None

    # Clean title — strip metadata markers
    clean = task_text
    for pat in (
        DUE_DATE_PATTERN,
        START_DATE_PATTERN,
        PRIORITY_PATTERN,
        RECURRENCE_PATTERN,
    ):
        clean = pat.sub("", clean)
    # Remove tags from title
    clean = TAG_PATTERN.sub("", clean).strip()
    # Collapse whitespace
    clean = re.sub(r"\s{2,}", " ", clean).strip()

    return {
        "raw_text": content,
        "title": clean,
        "is_done": is_done,
        "due_date": due_date,
        "start_date": start_date,
        "priority": priority,
        "tags": tags,
        "todoist_id": todoist_id,
        "todoist_project_name": project_name,
        "recurrence": recurrence,
        "block_id": block_id,
    }

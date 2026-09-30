import re

TASK_PATTERN = re.compile(
    r"""
    ^\s*
    (?:
        # Markdown checkbox form: - [x] text or [ ] text
        (?:[-*]\s+)?\[\s*(?P<status_char>[xXvV~\-]?)\s*\]\s*(?P<checkbox_text>.+)
        |
        # TODO prefix forms: TODO Buy milk or Todo: Buy milk
        (?:TODO|Todo|todo)\s*[:\-]?\s*(?P<todo_text>.+)
        |
        # Explicit todo/done labels
        (?P<label>todo|done)\s*:\s*(?P<label_text>.+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

DATE_ISO = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b")

DATE_SLASH = re.compile(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{4})\b")

DATE_ENGLISH = re.compile(
    r"\b("
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
    r"\s+\d{1,2},?\s*\d{4}"
    r")\b",
    re.IGNORECASE,
)

DATE_RANGE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2})\s*(?:to|\-|until|through)\s*(\d{4}[-/]\d{2}[-/]\d{2})",
    re.IGNORECASE,
)

TIME_PATTERN = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)\b")

TIME_RANGE = re.compile(
    r"\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)\s*[-–to]+\s*"
    r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)\b",
    re.IGNORECASE,
)

PRIORITY_PATTERN = re.compile(r"\[([!?]{1,3})\]")

# FIXED: removed \p{L} (not supported by Python re)
TAG_PATTERN = re.compile(r"(?<![\[\`])#([\w\-]+)", re.UNICODE)

RECURRENCE_PATTERN = re.compile(
    r"\b("
    r"every\s+(?:day|week|month|year|weekday|weekend|"
    r"\d+\s+(?:days?|weeks?|months?|years?))"
    r"|daily|weekly|monthly|yearly|biweekly|"
    r"every\s+other\s+(?:day|week|month|year)"
    r")\b",
    re.IGNORECASE,
)

LINK_PATTERN = re.compile(r"\[\[([^\[\]]+?)\]\]")

DUE_DATE_PATTERN = re.compile(
    r"(?:due:|@due)\s*(\d{4}[-/]\d{2}[-/]\d{2}"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"\s*(?:AM|PM)?(?:Z|[+-]\d{2}:?\d{2})?)?)",
    re.IGNORECASE,
)

START_DATE_PATTERN = re.compile(r"@start\s*(\d{4}[-/]\d{2}[-/]\d{2})", re.IGNORECASE)

# Natural-language relative/weekday date phrases, e.g. "tomorrow", "next friday",
# "in 3 days", "this saturday", "next month". Deliberately narrow (keyword-anchored)
# rather than a free-text scan, so it doesn't misfire on ordinary words in a task
# title (e.g. "may", "will", "no").
NATURAL_DATE_PATTERN = re.compile(
    r"""\b(
        today|tonight|tomorrow|yesterday
        |next\s+(?:mon|tues?|wednes|thurs?|fri|satur|sun)day
        |this\s+(?:mon|tues?|wednes|thurs?|fri|satur|sun)day
        |next\s+(?:week|month|year)
        |in\s+\d+\s+(?:days?|weeks?|months?|years?)
        |on\s+(?:mon|tues?|wednes|thurs?|fri|satur|sun)day
        |(?:mon|tues?|wednes|thurs?|fri|satur|sun)day
    )\b(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)?|"
    r"\s+\d{1,2}:\d{2}\s*(?:AM|PM)?|"
    r"\s+\d{1,2}\s*(?:AM|PM))?""",
    re.IGNORECASE | re.VERBOSE,
)

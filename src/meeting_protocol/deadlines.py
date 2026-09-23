"""Conservative Russian calendar resolver; unsupported expressions stay reviewable."""

import re
from datetime import date, timedelta

from .models import DeadlineType

MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
WEEKDAYS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]


def normalize_deadline(
    raw: str | None, kind: DeadlineType, meeting_date: date | None
) -> tuple[date | None, DeadlineType, bool]:
    if not raw or not raw.strip():
        return None, DeadlineType.absent, False
    value = raw.lower().strip()
    if kind == DeadlineType.event or value.startswith(("после ", "по завершении ")):
        return None, DeadlineType.event, False
    match = re.fullmatch(
        r"(?:до |к )?(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4})(?:\s*года)?)?", value
    )
    if match:
        if not match[3] and meeting_date is None:
            return None, DeadlineType.exact, True
        year = int(match[3]) if match[3] else meeting_date.year  # type: ignore[union-attr]
        try:
            result = date(year, MONTHS.index(match[2]) + 1, int(match[1]))
            return result, DeadlineType.exact, bool(meeting_date and result < meeting_date)
        except ValueError:
            return None, DeadlineType.exact, True
    try:
        return date.fromisoformat(value), DeadlineType.exact, False
    except ValueError:
        pass
    if meeting_date is None:
        return None, kind, True
    if value in {"на этой неделе", "на следующей неделе"}:
        days = 6 - meeting_date.weekday() + (7 if "следующей" in value else 0)
        return meeting_date + timedelta(days=days), DeadlineType.relative, False
    if value == "через две недели":
        return meeting_date + timedelta(days=14), DeadlineType.relative, False
    if value in {"сегодня", "завтра", "послезавтра"}:
        return (
            meeting_date + timedelta(days=["сегодня", "завтра", "послезавтра"].index(value)),
            DeadlineType.relative,
            False,
        )
    if value.startswith(("до ", "к ")):
        for i, stem in enumerate(WEEKDAYS):
            if value[3 if value.startswith("до ") else 2 :].startswith(stem):
                return (
                    meeting_date + timedelta(days=(i - meeting_date.weekday()) % 7),
                    DeadlineType.relative,
                    False,
                )
    return None, kind, True

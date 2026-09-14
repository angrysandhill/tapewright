# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Version strings from four projects, compared as numbers.

yt-dlp prints 2026.08.19, PyPI normalises that to 2026.8.19 and its nightlies to
2026.8.30.232658.dev0, deno.land says v2.9.6, and a Gyan FFmpeg build calls itself
8.1.1-full_build-www.gyan.dev. Compared as strings those sort wrongly ("2026.8.4" comes
after "2026.8.30"); as tuples of ints they do not.
"""

import datetime
import re

_LEADING_NUMBER = re.compile(r"\s*[vVnN]?(\d+(?:\.\d+)*)")


def parse(text):
    """The leading dotted number as a tuple of ints, or None if the text does not start with one."""
    if not text:
        return None
    m = _LEADING_NUMBER.match(text)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _trimmed(t):
    """(8, 1, 0) and (8, 1) are the same release; a plain tuple comparison calls the first newer."""
    t = list(t)
    while len(t) > 1 and t[-1] == 0:
        t.pop()
    return tuple(t)


def is_newer(candidate, installed):
    """True only when both parse and candidate is strictly newer.

    Unparseable means "can't tell", never "newer": a false "outdated" would put a hard
    warning in front of every conversion for no reason.
    """
    a, b = parse(candidate), parse(installed)
    return a is not None and b is not None and _trimmed(a) > _trimmed(b)


def at_least(installed, minimum):
    t = parse(installed)
    return t is not None and _trimmed(t) >= _trimmed(minimum)


def short(text):
    """'8.1.1-full_build-www.gyan.dev' -> '8.1.1', 'v2.9.6' -> '2.9.6'. Unparseable text is returned as is."""
    m = _LEADING_NUMBER.match(text or "")
    return m.group(1) if m else (text or "")


def release_date(version):
    """yt-dlp versions are release dates. None when this one does not read as a real date.

    The year check is what keeps an ordinary version out: 2.9.6 is otherwise a valid date.
    """
    t = parse(version)
    if not t or len(t) < 3 or t[0] < 2000:
        return None
    try:
        return datetime.date(t[0], t[1], t[2])
    except ValueError:
        return None

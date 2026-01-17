import logging

logger = logging.getLogger(__name__)

def seconds_to_human(s):
    if s is None: return "--"
    try:
        s = int(float(s))
    except (ValueError, TypeError):
        return "--"
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, seconds = divmod(s, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return ' '.join(parts)


def render_bar(percent, length=12):
    try:
        p = max(0, min(100, int(float(percent))))
    except Exception as e:
        logger.exception("render_bar failed to parse percent %r: %s", percent, e)
        p = 0
    full = int((p / 100.0) * length)
    return '█' * full + '░' * (length - full) + f" {p}%"

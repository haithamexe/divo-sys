"""What is Claude allowed to do on the speaker? A pure function so it can be tested.

`gui` and `system` are the user and the daemon itself: never restricted. `mcp` (Claude's tools)
and `hook` (automatic status from Claude Code events) are restricted by settings["claude"].
"""
import copy
import datetime

PRIVILEGED = {"gui", "system"}

TOOL_FOR = {
    "face": "set_face", "text": "show_text", "frames": "draw_pixels", "brightness": "set_brightness",
    "mode": "set_mode", "weather": "set_mode", "synctime": "set_mode", "app": "set_mode", "timer": "timer",
    "event": "set_face",  # status events from the hooks
}


def in_window(hm, start, end):
    """Is 'HH:MM' inside [start, end), where the window may wrap past midnight?"""
    if start == end:
        return False
    return start <= hm < end if start < end else (hm >= start or hm < end)


def describe(req):
    cmd = req.get("cmd")
    if cmd == "face":
        return f"face · {req.get('mood')}"
    if cmd == "text":
        return f"text · {str(req.get('text'))[:20]}"
    if cmd == "mode":
        return f"mode · {req.get('mode')}"
    if cmd == "brightness":
        return f"brightness · {req.get('value')}%"
    return str(cmd)


def decide(cfg, req, source, pinned=False, now=None, last_change=0.0, now_ts=0.0):
    """Returns (allowed, reason, request). The request may be adjusted (text trimmed, brightness clamped)."""
    req = copy.deepcopy(req)
    if source in PRIVILEGED:
        return True, None, req
    if not cfg["enabled"]:
        return False, "Claude control is switched off", req
    if source == "hook" and not cfg["hooks"]:
        return False, "automatic status hooks are switched off", req
    cmd = req.get("cmd")
    if cmd not in TOOL_FOR:
        return False, f"'{cmd}' is not something Claude may do", req
    if source == "hook" and cmd not in ("face", "event"):
        return False, "hooks may only send status events and faces", req
    if pinned and not cfg["override_pin"]:
        return False, "the display is pinned by the user", req
    now = now or datetime.datetime.now()
    qh = cfg["quiet_hours"]
    if qh["enabled"] and in_window(now.strftime("%H:%M"), qh["start"], qh["end"]):
        return False, f"quiet hours ({qh['start']}-{qh['end']})", req
    if source == "mcp" and not cfg["tools"].get(TOOL_FOR[cmd], False):
        return False, f"the user disabled the '{TOOL_FOR[cmd]}' tool", req
    if cmd == "face" and not cfg["moods"].get(req.get("mood"), True):
        return False, f"the user disabled the '{req.get('mood')}' face", req
    if source == "mcp" and cfg["min_interval_s"] > 0 and now_ts - last_change < cfg["min_interval_s"]:
        wait = cfg["min_interval_s"] - (now_ts - last_change)
        return False, f"too fast, wait {wait:.1f}s between changes", req
    if cmd == "text":
        req["text"] = str(req.get("text", ""))[: int(cfg["max_text_chars"])]
    if cmd == "brightness":
        lo, hi = int(cfg["brightness_min"]), int(cfg["brightness_max"])
        req["value"] = max(lo, min(hi, int(req.get("value", hi))))
    return True, None, req

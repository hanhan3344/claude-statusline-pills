#!/usr/bin/env python3
"""
1:1 replica of `kaboo-cli statusline` rich rendering for Claude Code.

Reads Claude Code's statusLine JSON from stdin and emits a 3-row pill bar with
matching ANSI 24-bit colors and Nerd Font glyphs.

Customizable: edit PILLS / SEGMENTS / COLORS below or override the quota
fetcher at the bottom (CONFIG -> "quota_fetcher").
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ palettes
# Each theme maps segment -> (bg, fg). Active theme is chosen by pick_theme()
# below based on Claude Code's settings.json `theme` key, falling back to the
# OS appearance.

PALETTES: dict[str, dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    # Catppuccin Mocha (dark) — matches kaboo's bundled "catppuccin-block".
    "dark": {
        "model": ((69, 55, 94),  (203, 166, 247)),  # mauve
        "cwd":   ((54, 56, 72),  (166, 173, 200)),  # subtext
        "ctx":   ((40, 58, 90),  (137, 180, 250)),  # blue
        "flow":  ((30, 72, 72),  (148, 226, 213)),  # teal
        "cost":  ((74, 61, 24),  (249, 226, 175)),  # yellow
        "5h":    ((74, 48, 32),  (250, 179, 135)),  # peach
        "7d":    ((26, 42, 80),  (137, 180, 250)),  # blue
        "ver":   ((42, 44, 58),  (127, 132, 156)),  # overlay
        "clock": ((30, 72, 80),  (137, 220, 235)),  # sky
        "git":   ((42, 69, 48),  (166, 227, 161)),  # green
    },
    # Catppuccin Latte (light) — pastel surfaces + saturated text.
    "light": {
        "model": ((232, 217, 247), (114, 65, 174)),   # mauve on lavender
        "cwd":   ((220, 224, 232), (76,  79,  105)),  # subtext on surface
        "ctx":   ((215, 228, 250), (30,  102, 245)),  # blue on sky
        "flow":  ((212, 240, 232), (23,  146, 153)),  # teal on mint
        "cost":  ((247, 235, 200), (130, 111, 35)),   # yellow on cream
        "5h":    ((253, 222, 200), (192, 91,  43)),   # peach on apricot
        "7d":    ((215, 228, 250), (30,  102, 245)),  # blue on sky
        "ver":   ((230, 232, 240), (108, 111, 133)),  # overlay on surface
        "clock": ((212, 232, 240), (32,  138, 161)),  # sky on ice
        "git":   ((212, 232, 220), (64,  160, 43)),   # green on mint
    },
}


def _read_settings_theme() -> str | None:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(cfg, "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f).get("theme") or "").lower() or None
    except (OSError, json.JSONDecodeError):
        return None


def _detect_os_theme() -> str:
    """macOS: returns 'dark' if AppleInterfaceStyle is set (== Dark), else 'light'."""
    if sys.platform == "darwin":
        try:
            import subprocess
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1,
            )
            if r.returncode == 0 and "dark" in r.stdout.lower():
                return "dark"
            return "light"
        except Exception:
            return "dark"
    # Linux/other: honor COLORFGBG heuristic, default dark
    fgbg = os.environ.get("COLORFGBG", "")
    parts = fgbg.split(";")
    if len(parts) >= 2:
        try:
            return "light" if int(parts[-1]) >= 8 else "dark"
        except ValueError:
            pass
    return "dark"


def pick_theme() -> str:
    """Resolve theme: env override > settings.json > OS."""
    forced = (os.environ.get("CLAUDE_STATUSLINE_THEME") or "").lower()
    if forced in PALETTES:
        return forced
    cc = _read_settings_theme()
    if cc in ("dark", "dark-daltonized"):
        return "dark"
    if cc in ("light", "light-daltonized"):
        return "light"
    # `null` / unset / unknown → follow OS
    return _detect_os_theme()


THEME = pick_theme()
C = {
    f"{seg}_{end}": v
    for seg, (bg, fg) in PALETTES[THEME].items()
    for end, v in (("bg", bg), ("fg", fg))
}

# Nerd Font glyphs (codepoints observed in kaboo binary)
ICON = {
    "model":  "\U000f167a",   # 󱙺  Anthropic / Claude logo
    "cwd":    "\U000f0770",   # 󰝰  folder
    "ctx":    "\U000f035b",   # 󰍛  memory chip
    "flow":   "\U000f0bcd",   # 󰯍  swap-horiz
    "cost":   "",       #   dollar
    "quota":  "",       #   chart bar (5h / 7d)
    "ver":    "",       #   tag
    "clock":  "\U000f0954",   # 󰥔  clock
    "git":    "\U000f02a2",   # 󰊢  git logo
    "up":     "↑",       # ↑
    "down":   "↓",       # ↓
    "recycle": "♻",      # ♻
}

# ------------------------------------------------------------------ formatting

def fmt_tokens(n: float) -> str:
    """kaboo flow-pill formatting:
       42523 -> 42.5k  (1 dp under 100k)
       197k  ->  197k  (no dp at >=100k)
       4.999M -> 5M    (drop trailing .0)
       5.24M -> 5.24M  (2 dp under 10M)
    """
    n = float(n)
    if n >= 1_000_000_000:
        v = n / 1_000_000_000
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return f"{s}B"
    if n >= 1_000_000:
        v = n / 1_000_000
        s = f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else (
            f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}")
        return f"{s}M"
    if n >= 100_000:
        return f"{round(n/1000):.0f}k"
    if n >= 1_000:
        v = n / 1000
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return f"{int(n)}"


def fmt_tokens_compact(n: float) -> str:
    """For ctx pill (e.g. 113k, 1M)."""
    n = float(n)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{int(v)}M" if v == int(v) else f"{v:.1f}M"
    if n >= 1000:
        return f"{round(n/1000):.0f}k"
    return f"{int(n)}"


def progress_bar(pct: float, width: int = 10) -> str:
    """N-cell unicode block bar using 8 fractional steps:
       U+258F=▏(1/8) U+258E=▎(2/8) U+258D=▍(3/8) U+258C=▌(4/8)
       U+258B=▋(5/8) U+258A=▊(6/8) U+2589=▉(7/8) U+2588=█(8/8)
       Threshold: each fractional cell starts at 1/8 of a full cell.
       At 0% renders as all spaces; at 1% (= 0.1 cells = ~0.8 eighths) the
       fractional is below 1, so still spaces. At 2% (= 1.6 eighths) → ▏."""
    eighths = "▏▎▍▌▋▊▉█"
    if pct <= 0:
        return " " * width
    eighths_total = pct / 100.0 * width * 8
    full = int(eighths_total // 8)
    rem = int(eighths_total - full * 8)  # 0..7
    out = "█" * full
    if rem > 0 and full < width:
        out += eighths[rem - 1]
        full += 1
    out += " " * (width - full)
    return out


# ------------------------------------------------------------------ pill

ESC = "\x1b"
RESET = f"{ESC}[0m"


def pill(content: str, fg: tuple[int, int, int], bg: tuple[int, int, int]) -> str:
    fr, fg_, fb = fg
    br, bg_, bb = bg
    return f"{ESC}[48;2;{br};{bg_};{bb}m{ESC}[38;2;{fr};{fg_};{fb}m {content} {RESET}"


# Override Nerd Font / private-use glyph width via env if your terminal
# renders them as double-width (e.g. non-Mono NF variants, WezTerm with
# custom width). Set CLAUDE_STATUSLINE_NF_WIDTH=2 for double-width glyphs.
_NF_WIDTH_OVERRIDE: int | None = None
try:
    _v = os.environ.get("CLAUDE_STATUSLINE_NF_WIDTH")
    if _v is not None:
        _NF_WIDTH_OVERRIDE = int(_v)
except (TypeError, ValueError):
    _NF_WIDTH_OVERRIDE = None


def _char_cell_width(ch: str) -> int:
    """Cell width of a single character, honoring NF width override."""
    cp = ord(ch)
    # Nerd Font ranges (private use + some symbol blocks):
    #   U+E000–U+F8FF      (PUA)
    #   U+F0000–U+FFFFD    (PUA-A/B)
    #   U+100000–U+10FFFD  (PUA-B supplementary)
    #   U+26A0–U+27BF      (misc symbols + dingbats — includes ♻)
    #   U+2190–U+21FF      (arrows — includes ↑↓)
    #   U+2580–U+259F      (block elements — includes █▌▎…)
    if _NF_WIDTH_OVERRIDE is not None and (
        0xE000 <= cp <= 0xF8FF
        or 0xF0000 <= cp <= 0xFFFFD
        or 0x100000 <= cp <= 0x10FFFD
        or 0x2190 <= cp <= 0x21FF
        or 0x2580 <= cp <= 0x259F
        or 0x26A0 <= cp <= 0x27BF
    ):
        return _NF_WIDTH_OVERRIDE
    try:
        import wcwidth  # type: ignore
        w = wcwidth.wcwidth(ch)
    except Exception:
        w = 1 if ord(ch) < 0x1100 or not unicodedata.east_asian_width(ch) in "WF" else 2
    return max(1, w) if w >= 0 else 1


def cell_width(text: str) -> int:
    return sum(_char_cell_width(c) for c in text)


def visible_len(content: str) -> int:
    """Visible cell width used for alignment. Includes the leading + trailing
    space added by pill() around the content."""
    return cell_width(content) + 2


# ------------------------------------------------------------------ data plumbing

def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def parse_transcript(path: str | None) -> dict[str, int]:
    """Sum non-sidechain assistant usage events across a transcript jsonl."""
    out = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") != "assistant":
                    continue
                if evt.get("isSidechain"):
                    continue
                u = (evt.get("message") or {}).get("usage") or {}
                out["input"] += int(u.get("input_tokens") or 0)
                out["output"] += int(u.get("output_tokens") or 0)
                out["cache_create"] += int(u.get("cache_creation_input_tokens") or 0)
                out["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ quota fetcher
# Default: hits the Coco OpenRouter balance endpoint. To use your own:
#   1) edit URL/HEADERS below, OR
#   2) replace fetch_quota() entirely.
# Returns (five_hour_pct, seven_day_pct) where either may be None to render
# "loading..." in that pill.

QUOTA_CACHE = "/tmp/claude-statusline-quota.json"
QUOTA_TTL = 60  # seconds

QUOTA_URL = "http://10.37.192.156:18344/user/balance"
QUOTA_HEADERS = {
    "Authorization": "Bearer ah-0981d51444ca53296626a34df1c726ea197f23717d9819d2dab14ae433db550c",
    "User-Agent": "cc-switch/1.0",
}


def fetch_quota() -> tuple[float | None, float | None]:
    """Returns (five_hour_used_pct, seven_day_used_pct)."""
    now = time.time()
    if os.path.exists(QUOTA_CACHE):
        try:
            st = os.stat(QUOTA_CACHE)
            if now - st.st_mtime < QUOTA_TTL:
                with open(QUOTA_CACHE) as f:
                    cached = json.load(f)
                return cached.get("five_hour"), cached.get("seven_day")
        except (OSError, json.JSONDecodeError):
            pass
    try:
        req = urllib.request.Request(QUOTA_URL, headers=QUOTA_HEADERS)
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("is_active") is False:
            return None, None
        seven_day = float(data.get("used_percent", 0))
        five_hour = None  # API has no 5h data
        try:
            with open(QUOTA_CACHE, "w") as f:
                json.dump({"five_hour": five_hour, "seven_day": seven_day}, f)
        except OSError:
            pass
        return five_hour, seven_day
    except Exception:
        return None, None


# ------------------------------------------------------------------ segments

def model_segment(j: dict[str, Any]) -> str | None:
    name = (j.get("model") or {}).get("display_name")
    if not name:
        return None
    # "Opus 4.7 (1M context)" -> "Opus 4.7 (1M)"
    name = re.sub(r"\s*\(([0-9.]+[kKmM])\s+context\)", r" (\1)", name)
    effort = (j.get("effort") or {}).get("level")
    if effort:
        # insert into the (...) group, or append
        if name.endswith(")"):
            name = name[:-1] + f" | {effort})"
        else:
            name = f"{name} ({effort})"
    return f"{ICON['model']} {name}"


def cwd_segment(j: dict[str, Any]) -> str | None:
    cwd = j.get("cwd")
    if not cwd:
        return None
    # kaboo strips leading slash: "/Users/bytedance" -> "Users/bytedance"
    path = cwd.lstrip("/") or "/"
    return f"{ICON['cwd']} {path}"


def git_segment(j: dict[str, Any]) -> str | None:
    """Mirror kaboo's git pill:
        ` <branch>[ ↑A][ ↓B][ +S][ ~M][ ?U][ +I/-D | +I | -D]`
    where:
        ↑A / ↓B = ahead / behind upstream commits
        +S      = number of staged files
        ~M      = number of modified-but-unstaged files
        ?U      = number of untracked files
        +I/-D   = total inserted / deleted lines (cached + worktree)
    """
    cwd = j.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return None
    import subprocess

    def _git(*args: str, timeout: float = 1.0) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    porcelain = _git("status", "--porcelain=v2", "--branch")
    if porcelain is None:
        return None

    branch: str | None = None
    head_sha: str | None = None
    ahead = behind = 0
    modified = untracked = staged = 0
    for line in porcelain.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.oid "):
            head_sha = line[len("# branch.oid "):].strip()
        elif line.startswith("# branch.ab "):
            parts = line[len("# branch.ab "):].split()
            if len(parts) == 2:
                try:
                    ahead = int(parts[0])
                    behind = -int(parts[1])
                except ValueError:
                    pass
        elif line.startswith("? "):
            untracked += 1
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(" ", 2)[1] if " " in line else "  "
            if len(xy) >= 2:
                if xy[0] != ".":
                    staged += 1
                if xy[1] != ".":
                    modified += 1

    if branch == "(detached)":
        branch = (head_sha or "")[:7] or "HEAD"
    if not branch:
        return None

    ins = dels = 0
    for ref in ("--cached", None):
        out = _git("diff", "--shortstat", *((ref,) if ref else ())) or ""
        m_ins = re.search(r"(\d+) insertion", out)
        m_del = re.search(r"(\d+) deletion", out)
        if m_ins:
            ins += int(m_ins.group(1))
        if m_del:
            dels += int(m_del.group(1))

    parts = [branch]
    if ahead:
        parts.append(f"{ICON['up']}{ahead}")
    if behind:
        parts.append(f"{ICON['down']}{behind}")
    if staged:
        parts.append(f"+{staged}")
    if modified:
        parts.append(f"~{modified}")
    if untracked:
        parts.append(f"?{untracked}")
    if ins and dels:
        parts.append(f"+{ins}/-{dels}")
    elif ins:
        parts.append(f"+{ins}")
    elif dels:
        parts.append(f"-{dels}")
    return f"{ICON['git']} {' '.join(parts)}"


def ctx_segment(j: dict[str, Any]) -> str | None:
    cw = j.get("context_window")
    if not cw:
        return None
    pct = float(cw.get("used_percentage") or 0)
    # kaboo's token count: current request's input + cache_read (the bytes
    # actually loaded into context this turn). During mid-reply states Claude
    # Code sometimes reports input=2 with no cache; we mirror that exactly to
    # stay byte-identical with kaboo (the percentage stays meaningful via
    # used_percentage, which Claude Code computes against the running total).
    cu = cw.get("current_usage") or {}
    used = int(cu.get("input_tokens") or 0) + int(cu.get("cache_read_input_tokens") or 0)
    size = cw.get("context_window_size") or 200_000
    bar = progress_bar(pct, 10)
    return f"{ICON['ctx']} {bar} {pct:.0f}% {fmt_tokens_compact(used)}/{fmt_tokens_compact(size)}"


def flow_segment(j: dict[str, Any]) -> str | None:
    tx = j.get("transcript_path")
    if not tx:
        return None
    s = parse_transcript(tx)
    total = s["input"] + s["output"] + s["cache_create"] + s["cache_read"]
    if total == 0:
        return None
    return (f"{ICON['flow']} {fmt_tokens(s['input'])}{ICON['up']}"
            f"/{fmt_tokens(s['output'])}{ICON['down']} "
            f"{fmt_tokens(s['cache_read'])}{ICON['recycle']}/{fmt_tokens(total)}")


def cost_segment(j: dict[str, Any]) -> str | None:
    cost = (j.get("cost") or {}).get("total_cost_usd")
    if cost is None:
        return None
    return f"{ICON['cost']} ${cost:.2f}"


def quota5h_segment(pct: float | None) -> str:
    if pct is None:
        return f"{ICON['quota']} loading..."
    bar = progress_bar(pct, 10)
    return f"{ICON['quota']} {bar} {pct:.0f}%"


def quota7d_segment(pct: float | None) -> str:
    if pct is None:
        return f"{ICON['quota']} loading..."
    bar = progress_bar(pct, 10)
    return f"{ICON['quota']} {bar} {pct:.0f}%"


def version_segment(j: dict[str, Any]) -> str | None:
    v = j.get("version")
    if not v:
        return None
    return f"{ICON['ver']} {v}"


def clock_segment(_j: dict[str, Any]) -> str:
    return f"{ICON['clock']} {datetime.now().strftime('%H:%M:%S')}"


# ------------------------------------------------------------------ layout

def render(j: dict[str, Any]) -> str:
    five_h, seven_d = fetch_quota()

    # (key, content)
    row1: list[tuple[str, str]] = []
    row2: list[tuple[str, str]] = []
    row3: list[tuple[str, str]] = []

    if (s := model_segment(j)):  row1.append(("model", s))
    if (s := cwd_segment(j)):    row1.append(("cwd",   s))
    if (s := git_segment(j)):    row1.append(("git",   s))

    if (s := ctx_segment(j)):    row2.append(("ctx",   s))
    if (s := flow_segment(j)):   row2.append(("flow",  s))
    if (s := cost_segment(j)):   row2.append(("cost",  s))

    row3.append(("5h",    quota5h_segment(five_h)))
    row3.append(("7d",    quota7d_segment(seven_d)))
    if (s := version_segment(j)): row3.append(("ver",   s))
    row3.append(("clock", clock_segment(j)))

    rows = [row1, row2, row3]
    nat = [[visible_len(c) for _, c in r] for r in rows]

    # ---- column anchors --------------------------------------------------
    # Column 1 alignment: only when row 2 is present.
    if row2:
        col1 = max((row[0] for row in nat if row), default=0)
        for r in nat:
            if r:
                r[0] = col1

    # Alignment design:
    #   - R1 has `git` pill: git (R1-last), cost (R2-last), clock (R3-last)
    #     all share a FIXED-WIDTH column — both left and right edges aligned.
    #     Column width = max of their natural widths.
    #   - R1 has no git: cwd (R1-last) shares its RIGHT edge with cost/clock,
    #     but its LEFT edge is free (so cwd can grow naturally). Cost and
    #     clock still share both edges (same column).
    #   - Slack between row heads and the shared right edge goes to the last
    #     stretchy head pill per row (never `model` or fixed-width `ver`).
    has_git = any(k == "git" for k, _ in row1)

    # Pills that must share BOTH edges (same fixed-width column).
    pinned_both: list[tuple[int, int]] = [(1, len(row2) - 1), (2, len(row3) - 1)]
    if has_git:
        pinned_both.append((0, len(row1) - 1))
    pinned_both_set = set(pinned_both)

    # Pills whose RIGHT edge only must align (left edge free).
    pinned_right_only: list[tuple[int, int]] = (
        [(0, len(row1) - 1)] if (row1 and not has_git) else []
    )
    pinned_right_only_set = set(pinned_right_only)

    # Width of the shared right column = max natural width across pinned_both.
    shared_right_w = max(nat[ri][pi] for ri, pi in pinned_both)
    for ri, pi in pinned_both:
        nat[ri][pi] = shared_right_w

    target = max(sum(nat[ri]) for ri in range(len(rows)) if rows[ri])

    for ri in range(len(rows)):
        if not rows[ri]:
            continue
        slack = target - sum(nat[ri])
        if slack <= 0:
            continue
        keys = [k for k, _ in rows[ri]]
        # Find the last stretchy head pill — never a pinned_both pill.
        ti = len(nat[ri]) - 2
        while ti > 0 and (
            keys[ti] in ("ver", "model")
            or (ri, ti) in pinned_both_set
        ):
            ti -= 1
        # pinned_right_only pills are allowed to grow (left edge is free).
        if ti < 0 and (ri, len(nat[ri]) - 1) in pinned_right_only_set:
            ti = len(nat[ri]) - 1
        if ti >= 0:
            nat[ri][ti] += slack

    sums = [sum(r) for r in nat]

    # ---- render each pill ---------------------------------------------------
    out_lines = []
    for ri, (r, widths) in enumerate(zip(rows, nat)):
        if not r:
            out_lines.append("")
            continue

        rendered = []
        for i, (key, content) in enumerate(r):
            pad = widths[i] - visible_len(content)
            if pad > 0:
                content = content + (" " * pad)
            rendered.append(pill(content, _fg(key), _bg(key)))
        out_lines.append("".join(rendered))
    return "\n".join(out_lines)


def _fg(key: str) -> tuple[int, int, int]: return C[f"{key}_fg"]
def _bg(key: str) -> tuple[int, int, int]: return C[f"{key}_bg"]


# ------------------------------------------------------------------ main

def main() -> None:
    j = read_stdin_json()
    print(render(j))


if __name__ == "__main__":
    main()

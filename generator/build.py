#!/usr/bin/env python3
"""Renders every panel of the profile README into self-contained SVG files.

Standard library only. Text is drawn from embedded glyph outlines and images are inlined, so a
panel never touches the network when it is viewed. Any failure exits non-zero before a file is
written, which leaves the last good panels on the `output` branch untouched.

usage: build.py --out DIR [--fixture data.json]
env:   GH_TOKEN  required unless --fixture is given
"""
import argparse
import base64
import datetime as dt
import json
import math
import os
import random
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

LOGIN = "MTaylanOzturk"
PINNED = ("LeafSWM", "FallingLeaves")
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GLYPHS = json.loads((DATA / "glyphs.json").read_text(encoding="utf-8"))
ICONS = json.loads((DATA / "icons.json").read_text(encoding="utf-8"))

BG, PANEL, TILE, LINE = "#0A0E16", "#0D121C", "#121927", "#222C3C"
INK, INK2, INK3 = "#E8EEF5", "#A4B1C2", "#7C8AA0"
TEAL, CYAN, VIOLET = "#2EE6A6", "#38BDF8", "#A78BFA"
RAMP = ("#1A2433", "#14504F", "#17807A", "#22B8A0", "#2EE6A6", "#8CFFD8")

WIDE, NARROW = 1000, 520


# ─────────────────────────────── data ───────────────────────────────

QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(ownerAffiliations: OWNER, privacy: PUBLIC, first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name description url isFork stargazerCount forkCount
        primaryLanguage { name color }
        languages(first: 8, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
      }
    }
    contributionsCollection {
      contributionYears
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}"""


def graphql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request("https://api.github.com/graphql", body, {
        "Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-panels"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            return payload["data"]
        except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
            if attempt == 3:
                raise
            print(f"graphql retry {attempt + 1}: {error}", file=sys.stderr)
            time.sleep(5 * 2 ** attempt)
    raise AssertionError("unreachable")


def fetch(token):
    user = graphql(QUERY, {"login": LOGIN}, token)["user"]
    years = user["contributionsCollection"]["contributionYears"]
    fields = " ".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", to: "{y}-12-31T23:59:59Z") '
        "{ contributionCalendar { weeks { contributionDays { date contributionCount } } } }" for y in years)
    per_year = graphql("query($login: String!) { user(login: $login) { %s } }" % fields, {"login": LOGIN}, token)["user"]
    history = {}
    for year in per_year.values():
        for week in year["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                history[day["date"]] = day["contributionCount"]
    user["history"] = history
    return user


def digest(user):
    """Reduces the API payload to the numbers the panels draw. Refuses data that looks broken."""
    weeks = [[(d["date"], d["contributionCount"]) for d in w["contributionDays"]]
             for w in user["contributionsCollection"]["contributionCalendar"]["weeks"]]
    year = [day for week in weeks for day in week]
    today = year[-1][0]
    history = {date: count for date, count in user["history"].items() if date <= today}  # years run to 31 Dec
    history.update(dict(year))
    if sum(c for _, c in year) == 0 or len(year) < 300:
        raise SystemExit("contribution calendar looks empty, keeping the previous panels")

    ordered = sorted(history.items())
    best = run = 0
    for _, count in ordered:
        run = run + 1 if count else 0
        best = max(best, run)
    tail = ordered[::-1]
    if tail[0][0] == year[-1][0] and tail[0][1] == 0:  # today may simply not have started yet
        tail = tail[1:]
    current = 0
    for _, count in tail:
        if not count:
            break
        current += 1

    repos = user["repositories"]["nodes"]
    bytes_by_language = json.loads((DATA / "languages.json").read_text(encoding="utf-8"))
    for repo in repos:
        if repo["isFork"]:
            continue
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            bytes_by_language[name] = bytes_by_language.get(name, 0) + edge["size"]
    total_bytes = sum(bytes_by_language.values())
    languages = sorted(((n, b / total_bytes) for n, b in bytes_by_language.items()), key=lambda p: -p[1])

    peak_date, peak = max(year, key=lambda p: p[1])
    created = dt.date.fromisoformat(user["createdAt"][:10])
    latest = dt.date.fromisoformat(year[-1][0])
    return {
        "weeks": weeks, "year": year, "peak": peak, "peak_date": peak_date,
        "year_total": sum(c for _, c in year), "all_time": sum(history.values()),
        "active": sum(1 for _, c in year if c), "days": len(year), "streak": current, "longest": best,
        "repos": user["repositories"]["totalCount"], "stars": sum(r["stargazerCount"] for r in repos),
        "forks": sum(r["forkCount"] for r in repos), "followers": user["followers"]["totalCount"],
        "prs": user["pullRequests"]["totalCount"], "issues": user["issues"]["totalCount"],
        "since": created.year, "years": (latest - created).days // 365,
        "languages": languages, "updated": latest.strftime("%d %b %Y"), "repo_nodes": {r["name"]: r for r in repos},
    }


# ─────────────────────────────── svg kit ─────────────────────────────

def mix(a, b, t):
    pa, pb = [[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (a, b)]
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def fmt(n):
    return f"{n:,}"


def num(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


class Svg:
    """One SVG file. Collects the glyphs it uses so each file carries only its own outlines."""

    def __init__(self, width, height, label, style=""):
        self.w, self.h, self.label, self.style = width, height, label, style
        self.body, self.defs, self.ids = [], [], {}

    def add(self, *parts):
        self.body.extend(parts)
        return self

    def glyph_id(self, font, ch):
        key = (font, ch)
        if key not in self.ids:
            self.ids[key] = f"g{len(self.ids):x}"
            self.defs.append(f'<path id="{self.ids[key]}" d="{GLYPHS[font][ch][0]}"/>')
        return self.ids[key]

    @staticmethod
    def measure(s, size, font="mono", spacing=0):
        table = GLYPHS[font]
        return sum(table[c][1] for c in s) * size / 1000 + spacing * (len(s) - 1)

    def text(self, s, x, y, size, fill=INK, font="mono", anchor="start", spacing=0, extra=""):
        scale = size / 1000
        step = spacing / scale
        width = self.measure(s, size, font, spacing)
        x -= {"start": 0, "middle": width / 2, "end": width}[anchor]
        cursor, uses = 0, []
        for ch in s:
            if ch != " ":
                uses.append(f'<use href="#{self.glyph_id(font, ch)}" x="{round(cursor)}"/>')
            cursor += GLYPHS[font][ch][1] + step
        return f'<g transform="translate({num(x)} {num(y)}) scale({scale:.4f})" fill="{fill}"{extra}>{"".join(uses)}</g>'

    def render(self):
        style = self.style + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.label)}">'
                f'<style>{style}</style><defs>{"".join(self.defs)}</defs>{"".join(self.body)}</svg>')


def panel(label, title, width, height, updated="", style=""):
    """Dark card shared by every data panel. Reads the same on light and dark GitHub themes."""
    svg = Svg(width, height, label, style)
    svg.defs.append(f'<linearGradient id="edge" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{TEAL}"/>'
                    f'<stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>')
    svg.add(f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="14" fill="{PANEL}" stroke="{LINE}"/>',
            f'<rect x="24" y="0" width="{width - 48}" height="1.5" fill="url(#edge)"/>',
            svg.text(title, 24, 39, 18, INK, "sansb"))
    if updated:
        svg.add(f'<circle cx="{width - 24 - svg.measure(updated, 12) - 13}" cy="32" r="3.5" fill="{TEAL}">'
                '<animate attributeName="opacity" values="1;.25;1" dur="2.4s" repeatCount="indefinite"/></circle>',
                svg.text(updated, width - 24, 36, 12, INK3, anchor="end"))
    return svg


def image_uri(name):
    return "data:image/webp;base64," + base64.b64encode((DATA / name).read_bytes()).decode()


# ─────────────────────────────── hero ────────────────────────────────

def hero(d, layout):
    wide = layout == WIDE
    width, height = (WIDE, 400) if wide else (NARROW, 470)
    style = (".fade{animation:fade 14s ease-in-out infinite alternate}"
             "@keyframes fade{0%,20%{opacity:0}80%,100%{opacity:1}}"
             ".up{animation:up .9s ease-out both}@keyframes up{from{transform:translateY(10px);opacity:0}}")
    svg = Svg(width, height, "Mahmut Taylan Öztürk. Backend Developer: Java ecosystem, game server architecture and Roblox development.", style)
    svg.defs.append(f'<clipPath id="card"><rect width="{width}" height="{height}" rx="16"/></clipPath>'
                    '<linearGradient id="scrim" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#070B13"/>'
                    '<stop offset=".5" stop-color="#070B13" stop-opacity=".8"/><stop offset="1" stop-color="#070B13" stop-opacity=".04"/></linearGradient>'
                    f'<linearGradient id="rule" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{TEAL}"/>'
                    f'<stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>')
    image = f'x="{0 if wide else -240}" y="0" width="{WIDE}" height="{height}" preserveAspectRatio="xMidYMid slice"'
    svg.add('<g clip-path="url(#card)">',
            f'<image {image} href="{image_uri("aurora-a.webp")}"/>',
            f'<image class="fade" {image} href="{image_uri("aurora-b.webp")}"/>',
            f'<rect width="{width}" height="{height}" fill="url(#scrim)"/>',
            f'<rect width="{width}" height="2" fill="url(#rule)"/>')
    figures = ((fmt(d["all_time"]), "CONTRIBUTIONS"), ("800K+", "LINES OF JAVA"), ("4", "ROBLOX GAMES"))
    soft = "#C6D3E0"
    if wide:
        left = 56
        svg.add('<g class="up">', svg.text("BACKEND DEVELOPER", left, 92, 13, TEAL, "monob", spacing=3.2),
                svg.text("Mahmut Taylan", left, 164, 68, "#FFFFFF", "sansb", spacing=-.6),
                svg.text("Öztürk", left, 236, 68, "#FFFFFF", "sansb", spacing=-.6),
                svg.text("Java ecosystem, game server architecture", left, 282, 18, soft, "sans"),
                svg.text("and Roblox development.", left, 308, 18, soft, "sans"),
                svg.text("AesirMC   ·   Aksaray University   ·   Türkiye", left, 352, 13, "#93A3B7"), "</g>")
        rx = 690
        svg.add(f'<g class="up" style="animation-delay:.25s"><rect x="{rx - 38}" y="72" width="1" height="256" fill="#FFF" fill-opacity=".16"/>')
        for i, (value, caption) in enumerate(figures):
            y = 112 + i * 92
            svg.add(svg.text(value, rx, y, 44, "#FFFFFF", "sansb"),
                    svg.text(caption, rx, y + 24, 11.5, soft, spacing=1.6))
            if i < 2:
                svg.add(f'<rect x="{rx}" y="{y + 42}" width="250" height="1" fill="#FFF" fill-opacity=".12"/>')
        svg.add("</g>")
    else:
        left = 32
        svg.add('<g class="up">', svg.text("BACKEND DEVELOPER", left, 62, 12.5, TEAL, "monob", spacing=3),
                svg.text("Mahmut Taylan", left, 124, 56, "#FFFFFF", "sansb", spacing=-.5),
                svg.text("Öztürk", left, 184, 56, "#FFFFFF", "sansb", spacing=-.5),
                svg.text("Java ecosystem, game server architecture", left, 228, 16.5, soft, "sans"),
                svg.text("and Roblox development.", left, 252, 16.5, soft, "sans"), "</g>")
        cell = (width - 2 * left) / 3
        svg.add('<g class="up" style="animation-delay:.25s">',
                f'<rect x="{left}" y="288" width="{width - 2 * left}" height="1" fill="#FFF" fill-opacity=".16"/>')
        for i, (value, caption) in enumerate(figures):
            x = left + i * cell
            svg.add(svg.text(value, x, 340, 32, "#FFFFFF", "sansb"))
            for k, part in enumerate(caption.replace("ROBLOX ", "ROBLOX|").replace("LINES OF ", "LINES OF|").split("|")):
                svg.add(svg.text(part, x, 362 + k * 15, 10, soft, spacing=1))
        svg.add("</g>", svg.text("AesirMC   ·   Aksaray University   ·   Türkiye", left, 430, 12.5, "#93A3B7"))
    svg.add(f'<rect x=".75" y=".75" width="{width - 1.5}" height="{height - 1.5}" rx="15.5" fill="none" stroke="#FFF" stroke-opacity=".08" stroke-width="1.5"/></g>')
    return svg


# ─────────────────────────────── stats ───────────────────────────────

def odometer(svg, value, x, y, size, font="sansb", fill=INK, anchor="start"):
    """Rolling digits. The final value is the static layout; the animation only arrives at it."""
    text = fmt(value)
    table = GLYPHS[font]
    width = sum(table[c][1] for c in text) * size / 1000
    if anchor == "end":
        x -= width
    pitch = size * 1.25
    clip = f"o{len(svg.ids)}x{int(x)}y{int(y)}"
    svg.defs.append(f'<clipPath id="{clip}"><rect x="{num(x - 2)}" y="{num(y - size * .92)}" width="{num(width + 4)}" height="{num(size * 1.12)}"/></clipPath>')
    parts, cursor = [], x
    for i, ch in enumerate(text):
        if ch.isdigit():
            digits = "".join(svg.text(str(d), cursor, y + d * pitch, size, fill, font) for d in range(int(ch) + 1))
            parts.append(f'<g class="roll" style="transform:translateY({num(-int(ch) * pitch)}px);animation-delay:{(len(text) - i) * 90}ms">{digits}</g>')
        else:
            parts.append(svg.text(ch, cursor, y, size, fill, font))
        cursor += table[ch][1] * size / 1000
    return f'<g clip-path="url(#{clip})">{"".join(parts)}</g>'


def tile(svg, x, y, w, h, caption, value, unit, note="", size=42, accent=None):
    glow = accent or LINE
    svg.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{TILE}" stroke="{LINE}"/>',
            f'<rect x="{x + 16}" y="{y + 19}" width="6" height="6" fill="{accent or INK3}"/>' if glow else "",
            svg.text(caption, x + 29, y + 26, 11.5, INK2, spacing=1),
            odometer(svg, value, x + 16, y + 40 + size * .86, size))
    if unit:
        svg.add(svg.text(unit, x + 16 + svg.measure(fmt(value), size, "sansb") + 9, y + 40 + size * .86, 14, INK2))
    if note:
        svg.add(svg.text(note, x + 16, y + h - 16, 12, INK3))


def monthly(svg, d, x, y, w, h):
    """One bar per calendar month of the contribution year."""
    totals = {}
    for date, count in d["year"]:
        totals[date[:7]] = totals.get(date[:7], 0) + count
    months = sorted(totals)[-12:]
    top = max(totals[m] for m in months) or 1
    step = w / len(months)
    for i, month in enumerate(months):
        bar = max(2, h * totals[month] / top)
        svg.add(f'<rect class="rise" style="animation-delay:{i * 60}ms" x="{x + i * step:.1f}" y="{y + h - bar:.1f}" '
                f'width="{step - 5:.1f}" height="{bar:.1f}" rx="2" fill="{TEAL if totals[month] == top else "#1F6F68"}"/>')


def stats(d, layout):
    wide = layout == WIDE
    width, height = (WIDE, 416) if wide else (NARROW, 650)
    style = ".roll{animation:roll 1.5s cubic-bezier(.16,.84,.3,1) both}@keyframes roll{from{transform:translateY(0)}}" \
            ".rise{animation:grow .9s cubic-bezier(.2,.9,.3,1) both;transform-box:fill-box;transform-origin:bottom}@keyframes grow{from{transform:scaleY(0)}}"
    label = (f"GitHub stats: {fmt(d['year_total'])} contributions in the last 12 months, {fmt(d['all_time'])} all time, "
             f"longest streak {d['longest']} days, current streak {d['streak']} days, best day {d['peak']}.")
    svg = panel(label, "GitHub Statistics", width, height, f"updated {d['updated']}", style)
    share = round(d["active"] / d["days"] * 100)
    peak_day = dt.date.fromisoformat(d["peak_date"]).strftime("%d %b %Y")
    gap = 12
    left = 24
    if wide:
        hero_w = 372
        small_w, small_h = (width - 48 - hero_w - gap * 2) // 2, 112
        tile(svg, left, 60, hero_w, small_h * 2 + gap, "LAST 12 MONTHS", d["year_total"], "contributions",
             f"active on {d['active']} of {d['days']} days ({share}%)", size=74, accent=TEAL)
        x1, x2 = left + hero_w + gap, left + hero_w + gap * 2 + small_w
        cells = ((x1, 60), (x2, 60), (x1, 60 + small_h + gap), (x2, 60 + small_h + gap))
        strip_y = 60 + small_h * 2 + gap * 2
        strip = (0, strip_y, width - 48, 84)
        months_box = (left + 16, 176, hero_w - 32, 40)
    else:
        small_w, small_h = (width - 48 - gap) // 2, 112
        tile(svg, left, 60, width - 48, 170, "LAST 12 MONTHS", d["year_total"], "contributions",
             f"active on {d['active']} of {d['days']} days ({share}%)", size=70, accent=TEAL)
        x1, x2, y1 = left, left + small_w + gap, 60 + 170 + gap
        cells = ((x1, y1), (x2, y1), (x1, y1 + small_h + gap), (x2, y1 + small_h + gap))
        strip_y = y1 + small_h * 2 + gap * 2
        strip = (0, strip_y, width - 48, 136)
        months_box = (left + 16, 164, width - 80, 34)
    monthly(svg, d, *months_box)
    entries = (("ALL TIME", d["all_time"], "", f"since {d['since']}", CYAN),
               ("BEST DAY", d["peak"], "", peak_day, VIOLET),
               ("LONGEST STREAK", d["longest"], "days", "", TEAL),
               ("CURRENT STREAK", d["streak"], "days", "", CYAN))
    for (x, y), (cap, value, unit, note, accent) in zip(cells, entries):
        tile(svg, x, y, small_w, small_h, cap, value, unit, note, size=34, accent=accent)

    facts = (("PUBLIC REPOS", d["repos"]), ("STARS", d["stars"]), ("FOLLOWERS", d["followers"]),
             ("PULL REQUESTS", d["prs"]), ("ISSUES", d["issues"]), ("YEARS ON GITHUB", d["years"]))
    _, sy, sw, sh = strip
    per_row = 6 if wide else 3
    svg.add(f'<rect x="{left}" y="{sy}" width="{sw}" height="{sh}" rx="10" fill="{TILE}" stroke="{LINE}"/>')
    col_w = sw / per_row
    for i, (cap, value) in enumerate(facts):
        col, row = i % per_row, i // per_row
        fx, fy = left + col * col_w + 16, sy + 20 + row * 62
        svg.add(svg.text(cap, fx, fy + 6, 9.5 if wide else 9.5, INK3, spacing=.6),
                svg.text(fmt(value), fx, fy + 36, 22, INK, "sansb"))
    return svg


# ─────────────────────────────── city ────────────────────────────────

def city(d, layout):
    wide = layout == WIDE
    weeks = d["weeks"] if wide else d["weeks"][-22:]
    width = WIDE if wide else NARROW
    count = len(weeks)
    ax, ay = ((width - 60 - 7 * 8.5) / count, 2.3) if wide else ((width - 60 - 7 * 11) / count, 2.3)
    bx, by = (8.5, 6.8) if wide else (11, 8)
    tall = 118 if wide else 110
    origin_x = 30
    rise = count * ay
    origin_y = 60 + 52 + tall + rise
    height = int(origin_y + 7 * by + 74)
    style = ".wk{animation:rise .7s cubic-bezier(.2,1.1,.35,1) both}@keyframes rise{from{transform:translateY(46px);opacity:0}}"
    label = f"Contribution city: {fmt(d['year_total'])} contributions in the last 12 months, drawn as one voxel column per day."
    svg = panel(label, "Contribution City", width, height, "", style)
    peak = max(c for w in weeks for _, c in w) or 1
    if wide:
        svg.add(svg.text(fmt(d["year_total"]), 30, 112, 46, INK, "sansb"),
                svg.text("contributions in the last 12 months, one tower per day", 30, 138, 13.5, INK2))

    def project(w, day, lift=0.0):
        return origin_x + w * ax + day * bx, origin_y - w * ay + day * by - lift

    def shade(c):
        if c == 0:
            return RAMP[0]
        return RAMP[min(5, 1 + int(math.sqrt(c / peak) * 5))]

    columns, peak_at = [], None
    inset = .13
    for w in range(count - 1, -1, -1):
        cells = []
        for date, c in weeks[w]:
            day = dt.date.fromisoformat(date).isoweekday() % 7
            h = 3 if c == 0 else 8 + math.sqrt(c / peak) * tall
            colour = shade(c)
            w0, w1, d0, d1 = w + inset, w + 1 - inset, day + inset, day + 1 - inset
            a, b, e, f = project(w0, d0, h), project(w1, d0, h), project(w1, d1, h), project(w0, d1, h)
            f2, e2 = project(w0, d1), project(w1, d1)
            a2 = project(w0, d0)
            cells.append(
                f'<path fill="{mix(colour, "#050814", .55)}" d="M{a[0]:.1f} {a[1]:.1f}L{f[0]:.1f} {f[1]:.1f}L{f2[0]:.1f} {f2[1]:.1f}L{a2[0]:.1f} {a2[1]:.1f}Z"/>'
                f'<path fill="{mix(colour, "#050814", .28)}" d="M{f[0]:.1f} {f[1]:.1f}L{e[0]:.1f} {e[1]:.1f}L{e2[0]:.1f} {e2[1]:.1f}L{f2[0]:.1f} {f2[1]:.1f}Z"/>'
                f'<path fill="{mix(colour, "#FFFFFF", .1 if c else 0)}" d="M{a[0]:.1f} {a[1]:.1f}L{b[0]:.1f} {b[1]:.1f}L{e[0]:.1f} {e[1]:.1f}L{f[0]:.1f} {f[1]:.1f}Z"/>')
            if c == peak and peak_at is None:
                peak_at = (project(w + .5, day + .5, h), date)
        columns.append(f'<g class="wk" style="animation-delay:{w * (1800 // count)}ms">{"".join(cells)}</g>')
    svg.add(*columns)

    if peak_at:
        (px, py), date = peak_at
        caption = f"{peak} on {dt.date.fromisoformat(date).strftime('%d %b')}"
        ty = 66
        anchor = "end" if px > width * .7 else "start"
        svg.add(f'<path d="M{px:.1f} {py:.1f}V{ty + 13:.1f}" stroke="{INK3}" stroke-width="1"/>',
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5" fill="{INK}"/>',
                svg.text(caption, px, ty + 8, 12.5, INK, "monob", "end" if anchor == "end" else "middle"))

    # Month ticks along the front edge.
    seen, baseline = None, origin_y + 7 * by + 22
    last_x = -99
    for w, week in enumerate(weeks):
        day = dt.date.fromisoformat(week[0][0])
        if day.month != seen:
            seen = day.month
            x, y = project(w, 7)
            if x - last_x >= 40:
                svg.add(svg.text(day.strftime("%b"), x, y + 20, 11.5, INK3))
                last_x = x

    # Legend.
    lx, ly = 30, height - 26
    svg.add(svg.text("less", lx, ly + 9, 11.5, INK3))
    lx += 38
    for colour in RAMP:
        svg.add(f'<rect x="{lx}" y="{ly - 1}" width="12" height="12" rx="2" fill="{colour}"/>')
        lx += 15
    svg.add(svg.text("more", lx + 5, ly + 9, 11.5, INK3))
    if wide:
        svg.add(svg.text("height and color: contributions per day, square-root scale", width - 30, ly + 9, 11.5, INK3, anchor="end"))
    return svg


# ─────────────────────────────── activity ────────────────────────────

def nice_ceiling(value):
    magnitude = 10 ** math.floor(math.log10(max(value, 1)))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def monotone_path(points):
    """Fritsch-Carlson monotone cubic: smooth, and it never overshoots the data."""
    h = points[1][0] - points[0][0]
    slopes = [(b[1] - a[1]) / h for a, b in zip(points, points[1:])]
    tangents = [slopes[0]]
    for s0, s1 in zip(slopes, slopes[1:]):
        tangents.append(0.0 if s0 * s1 <= 0 else 2 * s0 * s1 / (s0 + s1))
    tangents.append(slopes[-1])
    path = f"M{points[0][0]:.1f} {points[0][1]:.1f}"
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(points, points[1:])):
        path += (f"C{x0 + h / 3:.1f} {y0 + tangents[i] * h / 3:.1f} "
                 f"{x1 - h / 3:.1f} {y1 - tangents[i + 1] * h / 3:.1f} {x1:.1f} {y1:.1f}")
    return path


def activity(d, layout):
    wide = layout == WIDE
    span = 60 if wide else 30
    days = d["year"][-span:]
    width, height = (WIDE, 320) if wide else (NARROW, 300)
    total = sum(c for _, c in days)
    style = (".draw{stroke-dasharray:1;stroke-dashoffset:0;animation:draw 2.2s ease-out both}@keyframes draw{from{stroke-dashoffset:1}}"
             ".area{animation:show 1.2s 1.2s both}.pin{animation:show .5s 2s both}@keyframes show{from{opacity:0}}")
    label = f"Activity graph: {fmt(total)} contributions in the last {span} days."
    svg = panel(label, f"Activity, Last {span} Days", width, height, f"{fmt(total)} in {span} days · {num(total / span)} per day", style)
    left, right, top, bottom = 52, width - 26, 66, height - 42
    ceiling = nice_ceiling(max(c for _, c in days))
    sx = lambda i: left + (right - left) * i / (len(days) - 1)
    sy = lambda v: bottom - (bottom - top) * v / ceiling
    for k in range(5):
        v = ceiling * k / 4
        y = sy(v)
        svg.add(f'<path d="M{left} {y:.1f}H{right}" stroke="{LINE}" stroke-width="1"' + (' stroke-dasharray="2 5"' if k else "") + "/>",
                svg.text(num(v), left - 10, y + 4, 11.5, INK3, anchor="end"))
    points = [(sx(i), sy(c)) for i, (_, c) in enumerate(days)]
    line = monotone_path(points)
    svg.defs.append(f'<linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{TEAL}" stop-opacity=".34"/>'
                    f'<stop offset="1" stop-color="{TEAL}" stop-opacity="0"/></linearGradient>')
    svg.add(f'<path class="area" d="{line}L{right} {bottom}L{left} {bottom}Z" fill="url(#fill)"/>',
            f'<path class="draw" pathLength="1" d="{line}" fill="none" stroke="{TEAL}" stroke-width="2" stroke-linejoin="round"/>')
    tick_every = 10 if wide else 7
    for i in range(0, len(days), tick_every):
        label_x = sx(i)
        svg.add(svg.text(dt.date.fromisoformat(days[i][0]).strftime("%d %b"), label_x, bottom + 22, 11.5, INK3, anchor="start" if i == 0 else "middle"))
    index = max(range(len(days)), key=lambda i: days[i][1])
    px, py = points[index]
    caption = f"{days[index][1]} on {dt.date.fromisoformat(days[index][0]).strftime('%d %b')}"
    anchor = "end" if px > width * .72 else "start"
    svg.add(f'<g class="pin"><circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{TEAL}" stroke="{PANEL}" stroke-width="2"/>'
            + svg.text(caption, px + (-10 if anchor == "end" else 10), py + 4, 12.5, INK, "monob", anchor) + "</g>")
    return svg


# ─────────────────────────────── languages ───────────────────────────

def languages(d, layout):
    wide = layout == WIDE
    entries = d["languages"][:8]
    width = WIDE if wide else NARROW
    columns = 2 if wide else 1
    rows = math.ceil(len(entries) / columns)
    height = 70 + rows * 44 + 8
    style = ".bar{animation:grow 1.2s cubic-bezier(.2,.9,.3,1) both;transform-box:fill-box;transform-origin:left}@keyframes grow{from{transform:scaleX(0)}}"
    summary = ", ".join(f"{n} {s * 100:.1f}%" for n, s in entries[:5])
    svg = panel(f"Top languages: {summary}.", "Top Languages", width, height, "public + private projects", style)
    col_w = (width - 48 - 32 * (columns - 1)) / columns
    name_w, value_w = 92, 56
    track_w = col_w - name_w - value_w
    top = max(s for _, s in entries)
    for i, (name, share) in enumerate(entries):
        col, row = i // rows, i % rows
        x, y = 24 + col * (col_w + 32), 78 + row * 44
        bar = max(3, track_w * share / top)
        fill = TEAL if i == 0 else mix(TEAL, "#1A2433", min(.6, .18 + i * .07))
        svg.add(svg.text(name, x, y + 13, 14, INK),
                f'<rect x="{x + name_w}" y="{y + 2}" width="{num(track_w)}" height="12" rx="3" fill="{TILE}"/>',
                f'<rect class="bar" style="animation-delay:{i * 70}ms" x="{x + name_w}" y="{y + 2}" width="{num(bar)}" height="12" rx="3" fill="{fill}"/>',
                svg.text(f"{share * 100:.1f}%" if share >= .001 else "<0.1%", x + col_w, y + 13, 13, INK2, anchor="end"))
    return svg


# ─────────────────────────────── trophies ────────────────────────────

TIERS = (("C", "BRONZE", "#D69A6A", "#7A4A2B"), ("B", "SILVER", "#D5DEE8", "#6B7A8C"), ("A", "GOLD", "#F7D060", "#9A6B12"),
         ("S", "PLATINUM", "#9FF2E4", "#22786F"), ("SS", "MYTHIC", "#E2B8FF", "#6D3BC2"))


def trophies(d, layout):
    wide = layout == WIDE
    coded = sum(1 for name, _ in d["languages"] if name not in ("CSS", "HTML", "Shell"))
    rules = (
        ("Contributor", d["all_time"], fmt(d["all_time"]), (100, 500, 2000, 5000, 10000)),
        ("Streak", d["longest"], f"{d['longest']} days", (7, 14, 30, 100, 365)),
        ("Consistency", d["active"], f"{d['active']} days", (50, 100, 200, 300, 360)),
        ("Peak Day", d["peak"], f"{d['peak']} in a day", (10, 25, 50, 100, 250)),
        ("Polyglot", coded, f"{coded} languages", (2, 3, 5, 7, 10)),
        ("Veteran", d["years"], f"{d['years']} years", (1, 2, 3, 5, 10)),
        ("Open Source", d["repos"], f"{d['repos']} public repos", (3, 10, 25, 50, 100)),
        ("Community", d["followers"], f"{d['followers']} followers", (5, 25, 100, 500, 2500)),
    )
    earned = []
    for title, value, shown, thresholds in rules:
        tier = sum(1 for t in thresholds if value >= t) - 1
        if tier >= 0:
            earned.append((tier, title, shown))
    earned.sort(key=lambda e: -e[0])
    per_row = 6 if wide else 3
    earned = earned[:6]
    width = WIDE if wide else NARROW
    rows = math.ceil(len(earned) / per_row)
    cell_w, cell_h = (width - 32) / per_row, 172
    height = 60 + rows * cell_h + 8
    style = (".sweep{animation:sweep 6s ease-in-out infinite}@keyframes sweep{0%,55%{transform:translateX(-120px)}85%,100%{transform:translateX(120px)}}"
             ".medal{animation:pop .7s cubic-bezier(.2,1.5,.4,1) both;transform-box:fill-box;transform-origin:center}@keyframes pop{from{transform:scale(.4);opacity:0}}")
    label = "Trophies: " + ", ".join(f"{t} {TIERS[r][1].title()}" for r, t, _ in earned) + "."
    svg = panel(label, "Trophies", width, int(height), style=style)
    R = 44
    for i, (tier, title, shown) in enumerate(earned):
        col, row = i % per_row, i // per_row
        cx, cy = 16 + cell_w * (col + .5), 62 + row * cell_h + R + 6
        letter, name, light, dark = TIERS[tier]
        hexagon = " ".join(f"{cx + R * math.cos(math.radians(60 * k - 90)):.1f},{cy + R * math.sin(math.radians(60 * k - 90)):.1f}" for k in range(6))
        inner = " ".join(f"{cx + (R - 7) * math.cos(math.radians(60 * k - 90)):.1f},{cy + (R - 7) * math.sin(math.radians(60 * k - 90)):.1f}" for k in range(6))
        svg.defs.append(f'<linearGradient id="m{i}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{dark}"/></linearGradient>'
                        f'<clipPath id="h{i}"><polygon points="{hexagon}"/></clipPath>')
        svg.add(f'<g class="medal" style="animation-delay:{i * 110}ms">',
                f'<polygon points="{hexagon}" fill="url(#m{i})"/>',
                f'<polygon points="{inner}" fill="{mix(dark, "#05070D", .72)}" stroke="{light}" stroke-opacity=".5"/>',
                svg.text(letter, cx, cy + 12, 32, light, "sansb", "middle"),
                f'<g clip-path="url(#h{i})"><path class="sweep" style="animation-delay:{i * 400}ms" d="M{cx - 16:.1f} {cy - R}h22l-30 {2 * R}h-22z" fill="#FFF" fill-opacity=".22"/></g></g>',
                svg.text(title, cx, cy + R + 26, 15, INK, "sansb", "middle"),
                svg.text(name, cx, cy + R + 45, 10.5, light, "monob", "middle", 1.4),
                svg.text(shown, cx, cy + R + 63, 11.5, INK3, anchor="middle"))
    return svg


# ─────────────────────────────── stack ───────────────────────────────

STACK = (
    ("LANGUAGES", (("java", "Java", "#F89820"), ("kotlin", "Kotlin", "#A97BFF"), ("typescript", "TypeScript", "#3178C6"),
                   ("python", "Python", "#FFD43B"), ("javascript", "JavaScript", "#F7DF1E"), ("lua", "Lua", "#7C8FF5"), ("text:C#", "C#", "#9B6BDF"))),
    ("DATA", (("mongodb", "MongoDB", "#47A248"), ("redis", "Redis", "#FF5A4F"), ("mariadb", "MariaDB", "#C0765A"),
              ("text:My", "MySQL", "#5B9BD5"), ("postgresql", "PostgreSQL", "#6C93D9"))),
    ("INFRA", (("docker", "Docker", "#2496ED"), ("linux", "Linux", "#FCC624"), ("nginx", "Nginx", "#21B05A"),
               ("grafana", "Grafana", "#F46800"), ("actions", "Actions", "#5A9BF6"), ("gradle", "Gradle", "#3DB8C5"),
               ("git", "Git", "#F05032"))),
    ("GAME DEV", (("roblox", "Roblox", "#E8EEF5"), ("robloxstudio", "Roblox Studio", "#38A7FF"), (None, "Minecraft", "#62B47A"), (None, "PaperMC", "#EDEDED"), (None, "Velocity", "#3CC8D0"),
                     ("pterodactyl", "Pterodactyl", "#6AA6E8"))),
)


def stack(layout):
    wide = layout == WIDE
    width = WIDE if wide else NARROW
    style = ".chip{animation:chip .5s ease-out both}@keyframes chip{from{transform:translateY(10px);opacity:0}}"
    names = ", ".join(n for _, items in STACK for _, n, _ in items)
    rows_out, y, chip_h, order = [], 62, 38, 0
    probe = Svg(0, 0, "")
    origin = 148 if wide else 24
    usable = width - 24
    blocks = []
    for caption, items in STACK:
        lines, x, line = [], origin, []
        for icon, name, colour in items:
            w = 44 + probe.measure(name, 13.5, "sans") + 14
            if x + w > usable and line:
                lines.append(line)
                line, x = [], origin
            line.append((x, w, icon, name, colour))
            x += w + 8
        lines.append(line)
        blocks.append((caption, lines))
    height = 62
    for _, lines in blocks:
        height += (len(lines) * (chip_h + 8)) + (22 if not wide else 0) + 10
    svg = panel(f"Tech stack: {names}.", "Technology Stack", width, height + 8, style=style)
    for caption, lines in blocks:
        label_y = y + 24 if wide else y + 10
        svg.add(svg.text(caption, 24, label_y, 11, INK3, spacing=1.2))
        if not wide:
            y += 20
        for line in lines:
            for x, w, icon, name, colour in line:
                if icon and icon.startswith("text:"):
                    glyph = (f'<rect x="{x + 13:.1f}" y="{y + 10}" width="18" height="18" rx="3" fill="{colour}"/>'
                             + svg.text(icon[5:], x + 22, y + 23, 10, "#0A0E16", "sansb", "middle"))
                elif icon:
                    glyph = f'<path transform="translate({x + 13:.1f} {y + 10}) scale(.75)" fill="{colour}" d="{ICONS[icon]}"/>'
                else:
                    glyph = (f'<g transform="translate({x + 13:.1f} {y + 10})"><path fill="{colour}" d="M9 0l9 4.5-9 4.5-9-4.5z"/>'
                             f'<path fill="{mix(colour, "#050814", .32)}" d="M0 4.5l9 4.5v9l-9-4.5z"/><path fill="{mix(colour, "#050814", .58)}" d="M18 4.5l-9 4.5v9l9-4.5z"/></g>')
                svg.add(f'<g class="chip" style="animation-delay:{order * 45}ms">'
                        f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{chip_h}" rx="9" fill="{TILE}" stroke="{LINE}"/>{glyph}'
                        + svg.text(name, x + 40, y + 24, 13.5, INK, "sans") + "</g>")
                order += 1
            y += chip_h + 8
        y += 10
    return svg


# ─────────────────────────────── small cards ─────────────────────────

def repo_card(node):
    width, height = 490, 176
    style = ".lift{animation:up .8s ease-out both}@keyframes up{from{transform:translateY(10px);opacity:0}}"
    description = node["description"] or "No description yet."
    svg = panel(f"Repository {node['name']}: {description}", "Open Source", width, height, style=style)
    svg.add('<g class="lift">', svg.text(node["name"], 24, 84, 26, INK, "sansb"))
    for i, line in enumerate(textwrap.wrap(description.encode("ascii", "ignore").decode(), 55)[:2]):
        svg.add(svg.text(line, 24, 110 + i * 19, 13, INK2))
    y = height - 18
    x = 24
    language = node["primaryLanguage"]
    if language:
        svg.add(f'<circle cx="{x + 6}" cy="{y - 5}" r="6" fill="{TEAL}"/>', svg.text(language["name"], x + 19, y, 13, INK2))
        x += 19 + svg.measure(language["name"], 13) + 26
    svg.add(svg.text(f"{node['stargazerCount']} stars", x, y, 13, INK2))
    x += svg.measure(f"{node['stargazerCount']} stars", 13) + 26
    svg.add(svg.text(f"{node['forkCount']} forks", x, y, 13, INK2), "</g>")
    return svg


def repo_card_small(node):
    """Phone variant: two cards share a row there, so only the name and the counts stay."""
    width, height = 245, 128
    svg = Svg(width, height, f"Repository {node['name']}")
    size = min(26, 26 * 205 / Svg.measure(node["name"], 26, "sansb"))
    language = node["primaryLanguage"]["name"] if node["primaryLanguage"] else ""
    svg.add(f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="12" fill="{PANEL}" stroke="{LINE}"/>',
            svg.text(node["name"], 20, 56, size, INK, "sansb"),
            svg.text(f"{language} · {node['stargazerCount']} stars", 20, 92, 15, INK2))
    return svg


def button(caption, value, icon, colour):
    probe = Svg(0, 0, "")
    width = int(46 + probe.measure(caption, 10.5, spacing=1) + 0)
    width = int(max(width, 46 + probe.measure(value, 14, "sans")) + 18)
    svg = Svg(width, 52, f"{caption}: {value}")
    svg.add(f'<rect x=".5" y=".5" width="{width - 1}" height="51" rx="11" fill="{PANEL}" stroke="{LINE}"/>',
            f'<path transform="translate(14 15) scale(.92)" fill="{colour}" d="{icon}"/>',
            svg.text(caption, 46, 20, 10.5, INK3, spacing=1), svg.text(value, 46, 39, 14, INK, "sans"))
    return svg


MAIL_ICON = "M2 4h20a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm10 9.2L1.6 6.4v1.9L12 15l10.4-6.7V6.4z"
LINKEDIN_ICON = "M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46zM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13zM7.12 20.45H3.56V9h3.56zM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.23.79 24 1.77 24h20.45c.98 0 1.78-.77 1.78-1.73V1.73C24 .77 23.2 0 22.22 0z"
DISCORD_ICON = "M20.32 4.37A19.8 19.8 0 0 0 15.43 2.8a13.7 13.7 0 0 0-.63 1.28 18.3 18.3 0 0 0-5.5 0 12.6 12.6 0 0 0-.64-1.28 19.7 19.7 0 0 0-4.89 1.57C.67 9.05-.17 13.61.25 18.1a19.9 19.9 0 0 0 6 3.03c.49-.66.92-1.36 1.29-2.1-.71-.26-1.38-.59-2.03-.97.17-.12.34-.25.5-.38a14.2 14.2 0 0 0 12.1 0c.16.13.33.26.5.38-.65.39-1.33.71-2.04.97.37.74.8 1.44 1.29 2.1a19.9 19.9 0 0 0 6-3.03c.5-5.2-.84-9.72-3.54-13.73zM8.02 15.33c-1.18 0-2.16-1.08-2.16-2.42 0-1.33.96-2.42 2.16-2.42 1.21 0 2.18 1.09 2.16 2.42 0 1.34-.96 2.42-2.16 2.42zm7.97 0c-1.18 0-2.15-1.08-2.15-2.42 0-1.33.95-2.42 2.15-2.42 1.21 0 2.18 1.09 2.16 2.42 0 1.34-.95 2.42-2.16 2.42z"


def footer(layout):
    wide = layout == WIDE
    width, height = (WIDE, 170) if wide else (NARROW, 190)
    svg = Svg(width, height, "No tree can grow to heaven unless its roots reach down to hell.")
    svg.defs.append(f'<clipPath id="card"><rect width="{width}" height="{height}" rx="16"/></clipPath>')
    svg.add('<g clip-path="url(#card)">',
            f'<image x="{0 if wide else -240}" y="-160" width="{WIDE}" height="440" preserveAspectRatio="xMidYMid slice" href="{image_uri("aurora-b.webp")}"/>',
            f'<rect width="{width}" height="{height}" fill="#050812" fill-opacity=".3"/>')
    if wide:
        svg.add(svg.text("No tree can grow to heaven unless its roots reach down to hell.", width / 2, 84, 22, "#FFFFFF", "sansb", "middle"))
    else:
        svg.add(svg.text("No tree can grow to heaven", width / 2, 76, 22, "#FFFFFF", "sansb", "middle"),
                svg.text("unless its roots reach down to hell.", width / 2, 108, 22, "#FFFFFF", "sansb", "middle"))
    svg.add(svg.text("CARL JUNG", width / 2, (116 if wide else 142), 11, "#BFD4E6", "mono", "middle", 2.4), "</g>")
    return svg


# ─────────────────────────────── main ────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()

    if args.fixture:
        user = json.loads(args.fixture.read_text(encoding="utf-8"))
    else:
        user = fetch(os.environ["GH_TOKEN"])
    d = digest(user)

    files = {}
    for suffix, layout in (("", WIDE), ("-narrow", NARROW)):
        files[f"hero{suffix}.svg"] = hero(d, layout)
        files[f"stats{suffix}.svg"] = stats(d, layout)
        files[f"city{suffix}.svg"] = city(d, layout)
        files[f"activity{suffix}.svg"] = activity(d, layout)
        files[f"languages{suffix}.svg"] = languages(d, layout)
        files[f"trophies{suffix}.svg"] = trophies(d, layout)
        files[f"stack{suffix}.svg"] = stack(layout)
        files[f"footer{suffix}.svg"] = footer(layout)
    for i, name in enumerate(PINNED, 1):
        files[f"repo-{i}.svg"] = repo_card(d["repo_nodes"][name])
        files[f"repo-{i}-narrow.svg"] = repo_card_small(d["repo_nodes"][name])
    files["btn-linkedin.svg"] = button("LINKEDIN", "Mahmut Taylan Öztürk", LINKEDIN_ICON, "#5AA9F0")
    files["btn-email.svg"] = button("EMAIL", "ozturkmahmuttaylan@gmail.com", MAIL_ICON, TEAL)
    files["btn-discord.svg"] = button("DISCORD", "godback_", DISCORD_ICON, "#8C9EFF")

    rendered = {name: svg.render() for name, svg in files.items()}
    for name, content in rendered.items():  # all files must parse before any of them is written
        ElementTree.fromstring(content)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (args.out / name).write_text(content, encoding="utf-8")
    print(f"wrote {len(rendered)} panels, {sum(map(len, rendered.values())) // 1024} KiB")
    print({k: d[k] for k in ("year_total", "all_time", "active", "streak", "longest", "peak", "peak_date", "repos", "stars", "followers", "prs", "issues", "years")})


if __name__ == "__main__":
    main()

"""
Tabbycat Break Exporter v3.5
  * v3.5: break export keeps the order of Tabbycat's admin break table (the standings RANK), also for teams that are not
          breaking. Their remark (Capped / Ineligible / Reserve / Withdrawn ...) goes into the "break" column and the
          separate remark column is gone (break AND awards exports).
          + optional text next to the values (for Canva bulk create): suffix_break, suffix_points, suffix_score,
            suffix_average  ->  "1ST OPEN BREAKING TEAM", "19 Team Points", "1125 Speaker Points", "81.50 average speaks"
          - speakers are no longer flagged as anonymous in the awards export
Tabbycat Break Exporter v3.4
  * v3.4: + AWARDING export ("export_type": "awards"): the top speakers of the open speaker tab, of any speaker category
          (e.g. unioncup) and - for 3v3 / WSDC - of the reply speaker tab. Columns: rank, speaker, team, average
          (+ the same "intro" rows as the break export: rank + average only, speaker and team blank).
          Ties: everybody with rank <= 10 is included (a 4-way tie for 8th gives 12 speakers, the 12th-ranked is left out).
Tabbycat Break Exporter v3.3
  * v3.3: one profile per debate format (see FORMAT_CONFIG)
          BP    : points, total speaker score, 1sts, 2nds, draw strength by wins       (speakers joined by " & ")
          3v3   : wins, total speaker score                                            (speakers joined by ", ")  AP / Australs / UADC
          WSDC  : wins, average total speaker score (ATSS)                             (speakers joined by ", ")  3-5 speakers
          + the "break" column now follows Tabbycat's Break column (break_rank), never the standings Rank
          + teams that did not break (withdrawn / reserve / capped ...) are still exported, with their remark
Tabbycat Break Exporter v3.2
  * v3.1: code_name column + ALL CAPS ordinals
  * v3.2: + number_of_1sts, number_of_2nds, draw_strength_by_wins (after total_speaker_score)
          + "intro" rows: every ranked breaking team gets a first row with ONLY
            break / points / total_speaker_score / number_of_1sts / number_of_2nds / draw_strength_by_wins,
            followed by its full row (so Google Slides can show an intro slide, then the reveal slide)
          + sturdier API reading (see the notes next to each change)

READ-ONLY: this app only sends GET requests to Tabbycat. It works with an ADMIN token, so the
breaking teams can be exported BEFORE the break is released to the public.
"""

import os
import io
import csv
import time
import re
import bisect
import requests
from collections import Counter

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tabbycat-break-exporter-key-2026")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ---------------------------------------------------------------------------------------------------------------
# Per-format profiles. Rename a header here if your Google Sheet / Apps Script expects other names.
# ---------------------------------------------------------------------------------------------------------------
FORMAT_CONFIG = {
    "bp": {
        "label": "BP",
        "separator": " & ",
        "expected_speakers": (2, 2),
        "headers": ["break", "team", "code_name", "speakers", "points", "total_speaker_score",
                    "number_of_1sts", "number_of_2nds", "draw_strength_by_wins"],
    },
    "3v3": {           # AP / Australs / UADC: ranked by wins, then total speaker score
        "label": "3v3 (AP / Australs / UADC)",
        "separator": ", ",
        "expected_speakers": (3, 3),
        "headers": ["break", "team", "code_name", "speakers", "wins", "total_speaker_score"],
    },
    "wsdc": {          # WSDC: ranked by wins, then average total speaker score (ATSS)
        "label": "WSDC",
        "separator": ", ",
        "expected_speakers": (3, 5),
        "headers": ["break", "team", "code_name", "speakers", "wins", "average_total_speaker_score"],
    },
}
# Columns that are never filled on an "intro" row (it only carries the break rank and the numbers)
INTRO_BLANK_COLUMNS = {"team", "code_name", "speakers"}
ATSS_DECIMALS = 2

# ---------------------------------------------------------------------------------------------------------------
# Awarding slides (speaker tabs). The column names are the slide placeholders: {{rank}} {{speaker}} {{team}} {{average}}
# ---------------------------------------------------------------------------------------------------------------
AWARD_HEADERS = ["rank", "speaker", "team", "average"]
AWARD_INTRO_BLANK = {"speaker", "team"}                 # not filled on an intro row
AWARD_TOP_N = 10                                        # everybody ranked <= this number is exported (ties included)
AWARD_AVG_DECIMALS = 2
AWARD_MARK_TIES = False                                 # True -> tied speakers read "3RD=" instead of "3RD"
# What you may type as the speaker tab: the main tab, the reply tab, or a speaker-category slug (e.g. unioncup)
OPEN_TAB_NAMES = {"open", "all", "overall", "speaker", "speakers", "main", "speaker_tab"}
REPLY_TAB_NAMES = {"replies", "reply", "reply_speaker", "reply_speakers", "replies_tab", "reply_tab"}
AVERAGE_NAMES = ["average", "avg", "speaks_avg", "average_speaker_score", "speaker_average", "mean"]
TOTAL_NAMES = ["total", "speaks_sum", "sum", "total_speaker_score"]
# Endpoints that are tried, in this order (the first one that answers is used; the choice is written to the debug log)
SPEAKER_STANDINGS_PATHS = ["/speakers/standings"]
REPLY_STANDINGS_PATHS = ["/speakers/standings/replies", "/speakers/replies/standings", "/replies/standings",
                         "/speakers/standings/reply"]

REMARK_LABELS = {"C": "Capped", "I": "Ineligible", "D": "Different break", "d": "Disqualified",
                 "t": "Lost coin toss", "w": "Withdrawn", "R": "Reserve"}

FORMAT_ALIASES = {
    "bp": "bp", "wudc": "bp", "eudc": "bp",
    "wsdc": "wsdc", "ws": "wsdc", "wsc": "wsdc", "worldschools": "wsdc", "world_schools": "wsdc",
}


def normalize_format(value):
    """'bp' -> bp, 'wsdc' -> wsdc, anything else ('3v3', 'australs', 'ap', 'uadc' ...) -> 3v3 (as before)."""
    key = re.sub(r"[^a-z0-9_]+", "", str(value or "bp").strip().lower())
    return FORMAT_ALIASES.get(key, "3v3")


def headers_for(fmt):
    return list(FORMAT_CONFIG[fmt]["headers"])


# Names Tabbycat may use for the new metrics (compared after lower-casing and turning spaces/dashes into "_")
FIRSTS_NAMES = ["firsts", "number_of_firsts", "num_firsts", "n_firsts", "1sts", "number_of_1sts", "first_places"]
SECONDS_NAMES = ["seconds", "number_of_seconds", "num_seconds", "n_seconds", "2nds", "number_of_2nds", "second_places"]
DRAW_NAMES = ["draw_strength", "draw_strength_by_wins", "draw_strength_wins", "draw_strength_points",
              "draw_strength_by_points", "opp_wins", "opponent_wins"]
# 3v3 / WSDC (listed in order of preference)
WINS_NAMES = ["wins", "num_wins", "win", "points"]
TSS_NAMES = ["speaks_sum", "total_speaker_score", "total_speaks", "total_speaker_scores", "speaker_score", "speaks"]
ATSS_NAMES = ["speaks_avg", "average_total_speaker_score", "atss", "average_speaker_score", "avg_speaks",
              "average_speaks", "speaks_average"]


def ordinal(n):
    """Convert integer to UPPERCASE ordinal: 1→1ST, 2→2ND, 3→3RD, 4→4TH, etc."""
    if n is None or n == "":
        return ""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n).upper()
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}".upper()


def format_speakers(speakers, debate_format):
    """BP: 'A & B'.  3v3 / WSDC: 'A, B, C' (all speakers on the team, 3-5 for WSDC)."""
    if not speakers:
        return ""
    names = [s.get("name", "") for s in speakers if s.get("name")]
    return FORMAT_CONFIG[normalize_format(debate_format)]["separator"].join(names)


def format_speaker_score(score, debate_format):
    if score is None or score == "":
        return ""
    try:
        if debate_format == "bp":
            return str(int(float(score)))
        else:
            return str(score)
    except (ValueError, TypeError):
        return str(score)


# CSV column -> which optional text box feeds it. The text is added directly in the cell, after the value
# ("19" -> "19 Team Points"), on the intro rows too. Empty cells never get text.
SUFFIX_FIELDS = {
    "break": "break",
    "points": "points", "wins": "points",
    "total_speaker_score": "score", "average_total_speaker_score": "score",
    "average": "average",
}
SUFFIX_KEYS = ("break", "points", "score", "average")


def clean_suffixes(params):
    """Pick the four optional texts out of a form / JSON dict (flat 'suffix_x' keys or a 'suffixes' dict)."""
    nested = params.get("suffixes") if isinstance(params.get("suffixes"), dict) else {}
    out = {}
    for key in SUFFIX_KEYS:
        raw = params.get(f"suffix_{key}")
        if raw in (None, ""):
            raw = nested.get(key)
        text = " ".join(str(raw or "").split())            # one line, trimmed, single spaces
        if text:
            out[key] = text
    return out


def with_suffix(value, text):
    value = "" if value is None else str(value)
    return f"{value} {text}" if value != "" and text else value


def apply_suffixes(headers, values, suffixes, is_breaking=True):
    """values: list in header order -> same list with the optional texts added."""
    if not suffixes:
        return values
    result = []
    for header, value in zip(headers, values):
        key = SUFFIX_FIELDS.get(header)
        text = suffixes.get(key) if key else None
        if key == "break" and not is_breaking:              # "Capped" / "Withdrawn" ... never get "OPEN BREAKING TEAM"
            text = None
        result.append(with_suffix(value, text))
    return result


def award_headers():
    return list(AWARD_HEADERS)


def parse_rank(value):
    """'3', 3, '3=' -> 3 ; anything else -> None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.match(r"\s*(\d+)", str(value))
    return int(match.group(1)) if match else None


def norm_url(value):
    if isinstance(value, dict):
        value = value.get("url") if value.get("url") else value.get("id")
    return str(value).strip().rstrip("/") if value not in (None, "") else ""


def url_tail(value):
    return norm_url(value).rsplit("/", 1)[-1].lower()


def rank_group(rows):
    """
    rows: dicts with 'order' (position in the standings), 'rank' (the standings rank, or None) and 'tiekey'.
    Sets row['group_rank'] with competition ranking (1, 2, 2, 4 ...) inside THIS group of speakers, e.g. a category:
    it only depends on how many group members are strictly ahead, so speakers tied in the full standings stay tied.
    Falls back to comparing 'tiekey' (total + average) in standings order when the API gives no rank.
    Returns the rows in rank order.
    """
    if all(r["rank"] is not None for r in rows):
        rows = sorted(rows, key=lambda r: (r["rank"], r["order"]))
        ranks = [r["rank"] for r in rows]
        for r in rows:
            r["group_rank"] = bisect.bisect_left(ranks, r["rank"]) + 1
    else:
        rows = sorted(rows, key=lambda r: r["order"])
        previous_key, previous_rank = object(), 0
        for position, r in enumerate(rows, start=1):
            if r["tiekey"] != previous_key:
                previous_rank, previous_key = position, r["tiekey"]
            r["group_rank"] = previous_rank
    counts = Counter(r["group_rank"] for r in rows)
    for r in rows:
        r["tied"] = counts[r["group_rank"]] > 1
    return rows


def match_speaker_category(categories, wanted):
    """Exact slug, then exact name, then 'name contains' (same idea as for break categories)."""
    wanted = str(wanted or "").strip().lower()
    for cat in categories:
        if wanted == str(cat.get("slug", "")).lower() or wanted == url_tail(cat.get("url", "")):
            return cat
    for cat in categories:
        if wanted == str(cat.get("name", "")).lower():
            return cat
    for cat in categories:
        if wanted and wanted in str(cat.get("name", "")).lower():
            return cat
    return None


def format_fixed(value, decimals):
    """ATSS and similar: always the same number of decimals (e.g. 228.50 -> '228.50')."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{decimals}f}"
    except (ValueError, TypeError):
        return str(value)


def remark_label(value):
    if value is None or value == "":
        return ""
    text = str(value).strip()
    return REMARK_LABELS.get(text, text)


def rank_number(value):
    """break_rank -> int, or None when it is missing / not a number."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def format_number(value):
    """Metric value -> text. 0 stays '0' (a team can have zero 1sts); 52.0 -> '52'; None -> ''."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def extract_id_from_url(url):
    if not url:
        return None
    match = re.search(r"/([0-9]+)/?$", url.rstrip("/"))
    return int(match.group(1)) if match else None


def _unwrap_results(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "results" in data:
            return data["results"]
    return []


def _norm_metric(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")


def _find_metric(metrics, possible_names):
    """Points / speaker score lookup (unchanged behaviour, plus Tabbycat's real slug 'speaks_sum')."""
    if not metrics:
        return None, []
    lowered = [n.lower() for n in possible_names]
    all_names = []
    for m in metrics:
        name = str(m.get("metric", "")).lower()
        all_names.append(name)
        if name in lowered:
            return m.get("value"), all_names
    for m in metrics:
        name = str(m.get("metric", "")).lower()
        if "speak" in name or "score" in name:
            return m.get("value"), all_names
    return None, all_names


def find_extra_metric(metrics, exact_names, contains, exclude=()):
    """
    Look up one of the new metrics. Returns (found, value).
      1) exact slug match (e.g. 'firsts', 'draw_strength')
      2) otherwise any metric whose name contains `contains` and none of `exclude`
         (so 'draw_strength_speaks' is never mistaken for 'draw strength by wins').
    """
    if not metrics:
        return False, None
    exact = [_norm_metric(n) for n in exact_names]
    normalised = [(_norm_metric(m.get("metric", "")), m) for m in metrics if isinstance(m, dict)]
    for name, m in normalised:
        if name in exact:
            return True, m.get("value")
    for name, m in normalised:
        if contains in name and not any(bad in name for bad in exclude):
            return True, m.get("value")
    return False, None


def find_preferred_metric(metrics, names, require_all=(), require_any=(), exclude=()):
    """
    Look for a metric by slug, trying `names` in order of preference. If none matches, fall back to a metric whose
    name contains all of `require_all` and at least one of `require_any` (when given) and nothing from `exclude`.
    Returns (found, value).
    """
    if not metrics:
        return False, None
    normalised = [(_norm_metric(m.get("metric", "")), m) for m in metrics if isinstance(m, dict)]
    by_name = {}
    for name, m in normalised:
        by_name.setdefault(name, m)
    for want in names:
        if _norm_metric(want) in by_name:
            return True, by_name[_norm_metric(want)].get("value")
    if require_all or require_any:
        for name, m in normalised:
            if all(tok in name for tok in require_all) \
                    and (not require_any or any(tok in name for tok in require_any)) \
                    and not any(bad in name for bad in exclude):
                return True, m.get("value")
    return False, None


def extract_metrics(fmt, metrics, team_obj=None):
    """
    Standings metrics of ONE team -> ({csv header: text}, {csv header: metric was found}) for the given format.
    """
    team_obj = team_obj or {}
    values, found = {}, {}
    if fmt == "bp":
        points, _ = _find_metric(metrics, ["points", "wins", "team_points", "num_wins", "pts"])
        speaker_score, all_names = _find_metric(metrics, [
            "speaks_sum", "speaks", "speaker_score", "total_speaker_score",
            "average_speaker_score", "total_speaks", "avg_speaks",
            "total", "average", "avg", "score", "spk", "speaker",
            "total score", "speaker scores", "cumulative"
        ])
        # test for None instead of "falsy", so a real 0 is no longer replaced by a fallback / left blank
        if points is None or points == "":
            points = team_obj.get("points") if team_obj.get("points") is not None else team_obj.get("wins")
        if speaker_score is None or speaker_score == "":
            speaker_score = team_obj.get("speaker_score") if team_obj.get("speaker_score") is not None \
                else team_obj.get("total_speaker_score")
        values["points"] = format_number(points)
        values["total_speaker_score"] = format_speaker_score(speaker_score, "bp")
        found["points"] = points is not None and points != ""
        found["total_speaker_score"] = speaker_score is not None and speaker_score != ""
        for header, names, contains, exclude in (
                ("number_of_1sts", FIRSTS_NAMES, "first", ()),
                ("number_of_2nds", SECONDS_NAMES, "second", ()),
                ("draw_strength_by_wins", DRAW_NAMES, "draw", ("speak", "score", "margin"))):
            ok, value = find_extra_metric(metrics, names, contains, exclude)
            values[header], found[header] = format_number(value), ok
    else:
        ok, wins = find_preferred_metric(metrics, WINS_NAMES, require_all=("win",), exclude=("draw", "pull", "opp", "strength"))
        values["wins"], found["wins"] = format_number(wins), ok
        if fmt == "3v3":
            ok, tss = find_preferred_metric(metrics, TSS_NAMES, require_all=("speak",),
                                            exclude=("avg", "average", "std", "draw", "margin", "rank"))
            values["total_speaker_score"], found["total_speaker_score"] = format_number(tss), ok
        else:  # wsdc
            ok, atss = find_preferred_metric(metrics, ATSS_NAMES, require_all=("speak",), require_any=("avg", "average"),
                                             exclude=("std", "draw", "margin", "rank"))
            values["average_total_speaker_score"] = format_fixed(atss, ATSS_DECIMALS)
            found["average_total_speaker_score"] = ok
    return values, found


class TabbycatAPI:
    def __init__(self, base_url, token, tournament_slug):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip() if token else ""
        self.slug = tournament_slug.strip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "TabbycatBreakExporter/3.3 (Render; Python requests)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if self.token:
            self.session.headers["Authorization"] = f"Token {self.token}"
        self.debug_log = []

    def _log(self, msg):
        self.debug_log.append(str(msg))

    def _url(self, path):
        return f"{self.base_url}/api/v1/tournaments/{self.slug}{path}"

    def _request(self, method, url, retries=3):
        for attempt in range(retries):
            try:
                time.sleep(0.3)
                if method == "GET":
                    resp = self.session.get(url, timeout=30)
                else:
                    resp = self.session.post(url, json={}, timeout=30)

                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception as e:
                        return {"_error": f"JSON parse error: {e}", "_status": 200, "_text": resp.text[:500]}
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                elif resp.status_code in (301, 302, 307, 308):
                    redirect_url = resp.headers.get("Location", "")
                    if redirect_url:
                        self._log(f"Redirect: {url} -> {redirect_url}")
                        # v3.2: never pass 0 retries (it used to end in "Max retries exceeded" on the last attempt)
                        return self._request(method, redirect_url, retries=max(retries - attempt - 1, 1))
                    return {"_error": f"HTTP {resp.status_code} redirect without Location", "_status": resp.status_code}
                else:
                    return {"_error": f"HTTP {resp.status_code}", "_status": resp.status_code, "_text": resp.text[:500]}
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    return {"_error": str(e), "_status": 0}
                time.sleep(1)
        return {"_error": "Max retries exceeded", "_status": 0}

    def _get_list(self, url):
        """GET a list endpoint. v3.2: also follows a 'next' link if the server ever paginates."""
        data = self._request("GET", url)
        if isinstance(data, dict) and "_error" in data:
            return data
        items = list(_unwrap_results(data))
        guard = 0
        while isinstance(data, dict) and data.get("next") and guard < 50:
            guard += 1
            data = self._request("GET", data["next"])
            if isinstance(data, dict) and "_error" in data:
                break
            items.extend(_unwrap_results(data))
        return items

    @staticmethod
    def _status_of(resp_data):
        if isinstance(resp_data, dict) and "_status" in resp_data:
            return resp_data["_status"]
        return 200

    def test_connection(self, debate_format="bp"):
        fmt = normalize_format(debate_format)
        diagnostics = {"ok": False, "steps": [], "suggestion": ""}
        try:
            resp = self.session.get(self.base_url, timeout=10, allow_redirects=True)
            diagnostics["steps"].append({"step": "Base URL reachable", "status": resp.status_code, "ok": resp.status_code < 500})
        except Exception as e:
            diagnostics["steps"].append({"step": "Base URL", "status": 0, "ok": False, "error": str(e)})
            diagnostics["suggestion"] = "Cannot reach Tabbycat URL. Check for typos."
            return diagnostics

        url = self._url("/teams")
        resp_data = self._request("GET", url)
        status = self._status_of(resp_data)
        diagnostics["steps"].append({"step": "Tournament teams", "status": status, "ok": status == 200})

        url = self._url("/break-categories")
        categories_data = self._request("GET", url)
        status = self._status_of(categories_data)
        diagnostics["steps"].append({"step": "Break categories", "status": status, "ok": status == 200})

        if status == 200:
            diagnostics["ok"] = True
            diagnostics["suggestion"] = "Connection successful!"
            notes = []

            # v3.2: can this token read the (possibly unreleased) break itself?
            categories = _unwrap_results(categories_data)
            if categories:
                cat_id = extract_id_from_url(categories[0].get("url", ""))
                if cat_id:
                    brk = self._request("GET", self._url(f"/break-categories/{cat_id}/break"))
                    bstatus = self._status_of(brk)
                    diagnostics["steps"].append({"step": f"Breaking teams ({categories[0].get('name', 'first category')})",
                                                 "status": bstatus, "ok": bstatus == 200})
                    if bstatus in (401, 403):
                        notes.append("This token cannot read the break yet - use the API token of an admin / tab account.")

            # v3.2: are the metrics for the new columns available in the standings?
            st_data = self._request("GET", self._url("/teams/standings"))
            sstatus = self._status_of(st_data)
            diagnostics["steps"].append({"step": "Team standings", "status": sstatus, "ok": sstatus == 200})
            if sstatus == 200:
                standings = _unwrap_results(st_data)
                names = sorted({m.get("metric", "") for st in standings for m in st.get("metrics", [])})
                metrics = [{"metric": n, "value": 0} for n in names]      # names only: a dummy value marks "present"
                _, found = extract_metrics(fmt, metrics)
                missing = [h for h, ok in found.items() if not ok]
                notes.append(f"Format: {FORMAT_CONFIG[fmt]['label']}. Standings metrics found: " + (", ".join(names) or "none") + ".")
                if missing:
                    notes.append("Not in this tab's standings (columns will be blank): " + ", ".join(missing) +
                                 ". Add them under Tabbycat settings > team standings (precedence or extra metrics).")
            elif sstatus in (401, 403):
                notes.append("Team standings are not readable with this token.")
            # v3.4: speaker tabs for the awarding export
            sp_path, sp_entries, sp_tried = self.get_speaker_standings(replies=False)
            if sp_path:
                diagnostics["steps"].append({"step": f"Speaker standings ({sp_path})", "status": 200, "ok": True})
                names = sorted({m.get("metric", "") for st in sp_entries[:5] for m in st.get("metrics", [])})
                if sp_entries and not find_preferred_metric([{"metric": n, "value": 0} for n in names], AVERAGE_NAMES,
                                                            require_any=("avg", "average", "mean"))[0]:
                    notes.append("Speaker standings have no average metric (found: " + (", ".join(names) or "none") + ").")
            else:
                diagnostics["steps"].append({"step": "Speaker standings (" + "; ".join(sp_tried) + ")", "status": 0, "ok": False})
                notes.append("Speaker standings could not be read, so the awarding export will not work with this token.")
            if fmt != "bp":
                rp_path, _, rp_tried = self.get_speaker_standings(replies=True)
                if rp_path:
                    diagnostics["steps"].append({"step": f"Reply speaker standings ({rp_path})", "status": 200, "ok": True})
                else:
                    diagnostics["steps"].append({"step": "Reply speaker standings (" + "; ".join(rp_tried) + ")", "status": 0, "ok": False})
                    notes.append("Reply speaker standings were not found at the usual addresses.")
            if notes:
                diagnostics["suggestion"] += " " + " ".join(notes)
        elif status == 401:
            diagnostics["suggestion"] = "Token invalid or expired."
        elif status == 403:
            diagnostics["suggestion"] = "Access forbidden."
        elif status == 404:
            diagnostics["suggestion"] = f'Tournament slug "{self.slug}" not found.'
        else:
            diagnostics["suggestion"] = f"Unexpected status {status}."
        return diagnostics

    def get_break_categories(self):
        url = self._url("/break-categories")
        data = self._get_list(url)
        return data if isinstance(data, list) else []

    def get_break_category_by_slug(self, category_slug):
        """
        v3.2: three passes (exact slug, then exact name, then name contains) so that typing 'open'
        can never be captured by an earlier category called e.g. 'Open Novice'.
        """
        categories = self.get_break_categories()
        wanted = category_slug.strip().lower()
        for cat in categories:
            if wanted == str(cat.get("slug", "")).lower():
                return extract_id_from_url(cat.get("url", "")), cat
        for cat in categories:
            if wanted == str(cat.get("name", "")).lower():
                return extract_id_from_url(cat.get("url", "")), cat
        for cat in categories:
            if wanted and wanted in str(cat.get("name", "")).lower():
                return extract_id_from_url(cat.get("url", "")), cat
        if len(categories) == 1:
            cat = categories[0]
            return extract_id_from_url(cat.get("url", "")), cat
        return None, None

    def get_breaking_teams(self, category_id):
        url = self._url(f"/break-categories/{category_id}/break")
        data = self._get_list(url)
        self._log(f"Break endpoint: {url}")
        self._log(f"Break type: {type(data).__name__}")
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Break error: {data.get('_error')} status={data.get('_status')}")
            return []
        result = data
        self._log(f"Break count: {len(result)}")
        if result and isinstance(result[0], dict):
            self._log(f"Break keys: {list(result[0].keys())}")
            self._log(f"First break_rank: {result[0].get('break_rank')}")
            self._log(f"First remark: {result[0].get('remark')}")
        return result

    def get_teams(self):
        url = self._url("/teams")
        data = self._get_list(url)
        return data if isinstance(data, list) else []

    def get_speakers(self):
        """All speakers (name, team, categories, anonymous). None if the list cannot be read."""
        data = self._get_list(self._url("/speakers"))
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Speakers error: {data.get('_error')} status={data.get('_status')}")
            return None
        self._log(f"Speakers: {len(data)}")
        return data

    def get_speaker_categories(self):
        data = self._get_list(self._url("/speaker-categories"))
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Speaker categories error: {data.get('_error')} status={data.get('_status')}")
            return None
        return data

    def get_speaker_standings(self, replies=False):
        """
        -> (path that worked or None, entries, ["path -> HTTP status", ...] for the paths that did not work).
        Replies are only tried on the reply-specific paths, so the main standings can never be mistaken for them.
        """
        tried = []
        for path in (REPLY_STANDINGS_PATHS if replies else SPEAKER_STANDINGS_PATHS):
            data = self._get_list(self._url(path))
            if isinstance(data, dict) and "_error" in data:
                tried.append(f"{path} -> HTTP {data.get('_status')}")
                continue
            self._log(f"Speaker standings ({'replies' if replies else 'main'}) from {path}: {len(data)} entries")
            if data and isinstance(data[0], dict):
                self._log(f"Speaker standings keys: {list(data[0].keys())}")
                metrics = data[0].get("metrics", [])
                if metrics:
                    self._log("Speaker metrics: " + str([m.get("metric", "") for m in metrics]))
            return path, data, tried
        return None, [], tried

    def get_team_standings(self):
        url = self._url("/teams/standings")
        data = self._get_list(url)
        self._log(f"Standings endpoint: {url}")
        self._log(f"Standings type: {type(data).__name__}")
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Standings error: {data.get('_error')} status={data.get('_status')}")
            return []
        result = data
        self._log(f"Standings count: {len(result)}")
        if result and isinstance(result[0], dict):
            self._log(f"Standings keys: {list(result[0].keys())}")
            metrics = result[0].get("metrics", [])
            if metrics:
                names = [m.get("metric", "") for m in metrics]
                self._log(f"Available metrics: {names}")
        return result


def export_break_csv(api, category_slug, debate_format, intro_rows=True, suffixes=None):
    fmt = normalize_format(debate_format)
    headers = headers_for(fmt)
    low, high = FORMAT_CONFIG[fmt]["expected_speakers"]
    suffixes = suffixes or {}

    category_id, category_info = api.get_break_category_by_slug(category_slug)

    if category_id is None:
        available = api.get_break_categories()
        slugs = [c.get("slug", "") for c in available]
        return None, f'Category "{category_slug}" not found. Available: {", ".join(slugs) or "none"}', {}

    api._log(f"Category: {category_info.get('name')} (id={category_id}) | format: {FORMAT_CONFIG[fmt]['label']}")

    breaking = api.get_breaking_teams(category_id)
    if not breaking:
        return None, f'No breaking teams found for "{category_slug}". {" | ".join(api.debug_log)}', {}

    all_teams = api.get_teams()
    team_lookup = {}
    for team in all_teams:
        tid = extract_id_from_url(team.get("url", ""))
        if tid:
            team_lookup[tid] = team
        if "id" in team:
            team_lookup[team["id"]] = team
    api._log(f"Teams lookup: {len(team_lookup)} entries")

    standings = api.get_team_standings()
    standings_lookup = {}
    standings_position = {}                       # team id -> standings rank (or position) = fallback for the order
    for position, st in enumerate(standings, start=1):
        tid = extract_id_from_url(st.get("team", ""))
        if tid:
            standings_lookup[tid] = st
            standings_position[tid] = rank_number(st.get("rank")) or position
        if "id" in st:
            standings_lookup[st["id"]] = st
    api._log(f"Standings lookup: {len(standings_lookup)} entries")

    def team_id_of(bt):
        team_data = bt.get("team")
        if isinstance(team_data, dict):
            return team_data.get("id") or extract_id_from_url(team_data.get("url", ""))
        if isinstance(team_data, str):
            return extract_id_from_url(team_data)
        return None

    def standing_rank(bt):
        """The RANK column of Tabbycat's break table (fallback: the team's standings rank)."""
        rank = rank_number(bt.get("rank"))
        if rank is None:
            rank = standings_position.get(team_id_of(bt))
        return rank

    # Order = the admin break table: by the standings RANK. Teams that are not breaking (capped, ineligible, reserve,
    # withdrawn ...) stay where their rank puts them. Same rank -> the team with the better break rank first.
    indexed = list(enumerate(breaking))
    indexed.sort(key=lambda pair: (standing_rank(pair[1]) is None, standing_rank(pair[1]) or 0,
                                   rank_number(pair[1].get("break_rank")) is None,
                                   rank_number(pair[1].get("break_rank")) or 0, pair[0]))
    breaking = [bt for _, bt in indexed]
    if all(standing_rank(bt) is None for bt in breaking):
        api._log("No standings rank available for the breaking teams: ordered by break rank, others after them")

    rows = []
    metric_found = {}
    odd_speaker_counts = []

    for bt in breaking:
        team_data = bt.get("team")
        team_id = team_id_of(bt)
        team_obj = team_data if isinstance(team_data, dict) else None

        if team_obj is None and team_id and team_id in team_lookup:
            team_obj = team_lookup[team_id]

        if team_obj is None:
            api._log(f"Skipping team_id={team_id}: not found")
            continue

        team_name = (
            team_obj.get("short_name")
            or team_obj.get("long_name")
            or team_obj.get("reference")
            or team_obj.get("code_name")
            or f"Team {team_id}"
        )

        code_name = (
            team_obj.get("code_name")
            or team_obj.get("short_name")
            or ""
        )

        speakers = team_obj.get("speakers", [])
        speakers_str = format_speakers(speakers, fmt)
        n_speakers = len([s for s in speakers if s.get("name")])
        if not low <= n_speakers <= high:
            odd_speaker_counts.append(f"{team_name}: {n_speakers}")

        st = standings_lookup.get(team_id) if team_id else None
        if st:
            values, found = extract_metrics(fmt, st.get("metrics", []), team_obj)
        else:
            api._log(f"No standings for {team_name} (id={team_id})")
            values, found = extract_metrics(fmt, [], team_obj)
        for header, ok in found.items():
            metric_found[header] = metric_found.get(header, False) or ok

        # "break" column = the Break column of Tabbycat's table: the break rank (1ST, 2ND ...) for a breaking team,
        # otherwise its remark (Capped / Ineligible / Reserve / Withdrawn ...)
        break_rank = bt.get("break_rank")
        is_breaking = break_rank is not None and break_rank != ""
        break_text = ordinal(break_rank) if is_breaking else remark_label(bt.get("remark"))

        row = {"break": break_text, "team": team_name, "code_name": code_name, "speakers": speakers_str,
               "_breaking": is_breaking}
        row.update(values)
        rows.append(row)

    if not rows:
        return None, f"No valid data. {' | '.join(api.debug_log)}", {}

    missing = [h for h in headers if h in metric_found and not metric_found[h]]
    if missing:
        api._log("Metrics not found in standings (columns left blank): " + ", ".join(missing))
    if odd_speaker_counts:
        api._log(f"Speaker count outside {low}-{high} for the {FORMAT_CONFIG[fmt]['label']} format: " + "; ".join(odd_speaker_counts[:10]))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    intro_count = 0
    for row in rows:
        full = [row.get(h, "") for h in headers]
        # "intro" row = same break rank + numbers, but no team / code_name / speakers.
        # Only for teams that really break (not for capped / ineligible / reserve / withdrawn teams).
        if intro_rows and row["_breaking"]:
            intro = [("" if h in INTRO_BLANK_COLUMNS else row.get(h, "")) for h in headers]
            writer.writerow(apply_suffixes(headers, intro, suffixes, True))
            intro_count += 1
        writer.writerow(apply_suffixes(headers, full, suffixes, row["_breaking"]))

    metadata = {
        "export_type": "break",
        "category_name": category_info.get("name", category_slug) if category_info else category_slug,
        "category_slug": category_slug,
        "debate_format": fmt,
        "columns": headers,
        "team_count": len(rows),
        "breaking_teams": sum(1 for r in rows if r["_breaking"]),
        "not_breaking_teams": sum(1 for r in rows if not r["_breaking"]),
        "intro_rows": intro_count,
        "row_count": len(rows) + intro_count,
        "text_added_to": sorted(suffixes),
        "missing_metrics": missing,
        "debug": " | ".join(api.debug_log),
    }
    return output.getvalue(), None, metadata


def export_awards_csv(api, award_tab, debate_format, intro_rows=True, top_n=AWARD_TOP_N, suffixes=None):
    """
    Top speakers of one speaker tab -> CSV with the columns rank, speaker, team, average.
    award_tab: 'open' (main speaker tab) | a speaker-category slug such as 'unioncup' | 'replies' (3v3 / WSDC only).
    Everybody whose rank is <= top_n is exported, so ties at the cut-off can make the list longer than top_n.
    """
    fmt = normalize_format(debate_format)
    suffixes = suffixes or {}
    tab = re.sub(r"[^a-z0-9_]+", "_", str(award_tab or "open").strip().lower()).strip("_") or "open"
    is_reply = tab in REPLY_TAB_NAMES
    is_open = tab in OPEN_TAB_NAMES
    try:
        top_n = max(int(top_n), 1)
    except (ValueError, TypeError):
        top_n = AWARD_TOP_N

    if is_reply and fmt == "bp":
        return None, "The reply speaker tab only exists for the 3v3 and WSDC formats (BP has no reply speeches).", {}

    speakers = api.get_speakers()
    if speakers is None:
        return None, "Could not read the speakers list. " + " | ".join(api.debug_log), {}

    category = None
    if not is_open and not is_reply:
        categories = api.get_speaker_categories()
        if categories is None:
            return None, "Could not read the speaker categories. " + " | ".join(api.debug_log), {}
        category = match_speaker_category(categories, tab)
        if category is None:
            options = ["open"] + [c.get("slug") or url_tail(c.get("url", "")) for c in categories] \
                + ([] if fmt == "bp" else ["replies"])
            return None, f'Speaker tab "{award_tab}" not found. Available: {", ".join(options)}', {}
        api._log(f"Speaker category: {category.get('name')} ({category.get('slug')})")

    path, standings, tried = api.get_speaker_standings(replies=is_reply)
    if path is None:
        which = "reply speaker" if is_reply else "speaker"
        status_hint = ""
        if any("401" in t or "403" in t for t in tried):
            status_hint = " The token may not be allowed to read standings: use an admin / tab account token."
        return None, (f"The {which} standings could not be read from the API (tried: {'; '.join(tried)})." + status_hint +
                      " Open the Test Connection box for details."), {}
    if not standings:
        return None, "The speaker standings are empty.", {}

    speaker_lookup = {}
    for sp in speakers:
        if sp.get("id") is not None:
            speaker_lookup[str(sp["id"])] = sp
        if norm_url(sp.get("url")):
            speaker_lookup[norm_url(sp.get("url"))] = sp
    team_lookup = {}
    for team in api.get_teams():
        if team.get("id") is not None:
            team_lookup[str(team["id"])] = team
        if norm_url(team.get("url")):
            team_lookup[norm_url(team.get("url"))] = team

    cat_urls = cat_tails = None
    if category is not None:
        cat_urls = {norm_url(category.get("url")).lower()}
        cat_tails = {str(category.get("slug", "")).lower(), url_tail(category.get("url", ""))}
        cat_tails.discard("")

    rows = []
    skipped_unknown = 0
    for order, st in enumerate(standings):
        ref = st.get("speaker")
        speaker = speaker_lookup.get(norm_url(ref)) or speaker_lookup.get(str(extract_id_from_url(norm_url(ref))))
        if speaker is None:
            skipped_unknown += 1
            continue
        if category is not None:
            mine = {norm_url(c).lower() for c in speaker.get("categories", [])}
            mine_tails = {url_tail(c) for c in speaker.get("categories", [])}
            if not (mine & cat_urls or mine_tails & cat_tails):
                continue
        metrics = st.get("metrics", [])
        _, average = find_preferred_metric(metrics, AVERAGE_NAMES, require_any=("avg", "average", "mean"),
                                           exclude=("trim", "std", "dev", "count", "num", "total", "sum"))
        _, total = find_preferred_metric(metrics, TOTAL_NAMES)
        rows.append({
            "order": order, "speaker": speaker, "average": average,
            "rank": parse_rank(st.get("rank")),
            "tiekey": (format_fixed(total, 4), format_fixed(average, 4)),
        })
    if skipped_unknown:
        api._log(f"{skipped_unknown} standings entries did not match a speaker and were skipped")
    if not rows:
        return None, f'No speakers found for the "{award_tab}" tab. ' + " | ".join(api.debug_log), {}

    rows = rank_group(rows)
    chosen = [r for r in rows if r["group_rank"] <= top_n]

    headers = award_headers()
    out_rows = []
    for r in chosen:
        sp = r["speaker"]
        team_obj = team_lookup.get(norm_url(sp.get("team"))) or team_lookup.get(str(extract_id_from_url(norm_url(sp.get("team")))))
        team_name = ""
        if team_obj:
            team_name = (team_obj.get("short_name") or team_obj.get("long_name") or team_obj.get("reference")
                         or team_obj.get("code_name") or "")
        rank_text = ordinal(r["group_rank"]) + ("=" if AWARD_MARK_TIES and r["tied"] else "")
        out_rows.append({
            "rank": rank_text, "speaker": sp.get("name", ""), "team": team_name,
            "average": format_fixed(r["average"], AWARD_AVG_DECIMALS),
        })

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    intro_count = 0
    for row in out_rows:
        if intro_rows:
            intro = [("" if h in AWARD_INTRO_BLANK else row.get(h, "")) for h in headers]
            writer.writerow(apply_suffixes(headers, intro, suffixes))
            intro_count += 1
        writer.writerow(apply_suffixes(headers, [row.get(h, "") for h in headers], suffixes))

    missing_avg = sum(1 for r in out_rows if r["average"] == "")
    if missing_avg:
        api._log(f"{missing_avg} exported speaker(s) have no average in the standings (cell left blank)")
    label = "Reply speakers" if is_reply else (category.get("name") if category else "Open speakers")
    metadata = {
        "export_type": "awards", "tab": tab, "tab_label": label, "standings_path": path,
        "debate_format": fmt, "columns": headers, "top_n": top_n,
        "speakers_in_tab": len(rows), "exported_speakers": len(out_rows),
        "ties_at_cutoff": len(out_rows) > top_n,
        "text_added_to": sorted(suffixes),
        "intro_rows": intro_count, "row_count": len(out_rows) + intro_count,
        "missing_metrics": ["average"] if missing_avg else [],
        "debug": " | ".join(api.debug_log),
    }
    return output.getvalue(), None, metadata


def run_export(api, params):
    """One entry point for the form and the JSON endpoints. -> (csv_text, error, metadata, filename_suffix)"""
    export_type = str(params.get("export_type") or "break").strip().lower()
    intro_rows = _intro_flag(params.get("intro_rows"))
    suffixes = clean_suffixes(params)
    fmt = normalize_format(params.get("debate_format", "bp"))
    if export_type in ("awards", "award", "speakers"):
        tab = str(params.get("award_tab") or "open").strip().lower()
        csv_text, error, meta = export_awards_csv(api, tab, fmt, intro_rows, params.get("top_n") or AWARD_TOP_N, suffixes)
        return csv_text, error, meta, f"{re.sub(r'[^a-z0-9_]+', '_', tab) or 'open'}_awards"
    category_slug = str(params.get("category_slug") or "").strip().lower()
    csv_text, error, meta = export_break_csv(api, category_slug, fmt, intro_rows, suffixes)
    return csv_text, error, meta, f"{category_slug}_break"


def _intro_flag(value, default=True):
    """intro_rows switch: on by default; 'off' / 'false' / '0' / 'no' (or JSON false) turns it off."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("off", "false", "0", "no")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/test-connection", methods=["POST"])
def test_connection():
    data = request.get_json()
    api = TabbycatAPI(data.get("base_url", ""), data.get("token", ""), data.get("slug", ""))
    fmt = normalize_format(data.get("debate_format", "bp"))
    diagnostics = api.test_connection(fmt)
    if diagnostics["ok"]:
        categories = api.get_break_categories()
        diagnostics["break_categories"] = [
            {"name": c.get("name", ""), "slug": c.get("slug", ""), "url": c.get("url", "")}
            for c in categories
        ]
        speaker_cats = api.get_speaker_categories() or []
        tabs = [{"slug": "open", "name": "Open speakers (main speaker tab)"}]
        tabs += [{"slug": c.get("slug") or url_tail(c.get("url", "")), "name": c.get("name", "")} for c in speaker_cats]
        if fmt != "bp":
            tabs.append({"slug": "replies", "name": "Reply speakers"})
        diagnostics["speaker_tabs"] = tabs
    return jsonify(diagnostics)


def _missing_fields(params):
    """Which required fields are empty (the break export also needs a break category, the awards export does not)."""
    needed = ["base_url", "token", "slug"]
    if str(params.get("export_type") or "break").strip().lower() not in ("awards", "award", "speakers"):
        needed.append("category_slug")
    return [k for k in needed if not str(params.get(k) or "").strip()]


def _clean_params(source):
    return {
        "base_url": str(source.get("base_url") or "").strip(),
        "token": str(source.get("token") or "").strip(),
        "slug": str(source.get("slug") or "").strip(),
        "category_slug": str(source.get("category_slug") or "").strip().lower(),
        "export_type": source.get("export_type"),
        "award_tab": source.get("award_tab"),
        "top_n": source.get("top_n"),
        "debate_format": source.get("debate_format", "bp"),
        "intro_rows": source.get("intro_rows"),
        "suffix_break": source.get("suffix_break"),
        "suffix_points": source.get("suffix_points"),
        "suffix_score": source.get("suffix_score"),
        "suffix_average": source.get("suffix_average"),
        "suffixes": source.get("suffixes"),
    }


@app.route("/export", methods=["POST"])
def export():
    params = _clean_params(request.form)

    if _missing_fields(params):
        flash("All fields are required.", "error")
        return redirect(url_for("index"))

    api = TabbycatAPI(params["base_url"], params["token"], params["slug"])
    csv_data, error, metadata, suffix = run_export(api, params)

    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    filename = f"{params['slug']}_{suffix}.csv"
    buffer = io.BytesIO(csv_data.encode("utf-8"))
    response = send_file(buffer, mimetype="text/csv", as_attachment=True, download_name=filename)
    if metadata.get("missing_metrics"):
        response.headers["X-Missing-Metrics"] = ",".join(metadata["missing_metrics"])
    return response


@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    params = _clean_params(data)
    if _missing_fields(params):
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    api = TabbycatAPI(params["base_url"], params["token"], params["slug"])
    csv_data, error, metadata, _ = run_export(api, params)

    if error:
        return jsonify({"ok": False, "error": error, "debug": metadata.get("debug", "")}), 400

    return jsonify({"ok": True, "csv": csv_data, "metadata": metadata})


@app.route("/api/export-csv", methods=["POST"])
def api_export_csv_raw():
    data = request.get_json()
    if not data:
        return "Error: JSON body required", 400

    params = _clean_params(data)
    api = TabbycatAPI(params["base_url"], params["token"], params["slug"])
    csv_data, error, _, _ = run_export(api, params)

    if error:
        return f"Error: {error}", 400

    return csv_data, 200, {"Content-Type": "text/csv; charset=utf-8"}


@app.route("/api/debug", methods=["POST"])
def api_debug():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "JSON body required"}), 400

    base_url = data.get("base_url", "").strip()
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_slug = data.get("category_slug", "").strip().lower()

    api = TabbycatAPI(base_url, token, slug)
    categories = api.get_break_categories()
    cat_id, cat_info = api.get_break_category_by_slug(category_slug)

    result = {
        "ok": True,
        "break_categories": categories,
        "matched_category": {"id": cat_id, "info": cat_info},
        "debug_log_before": list(api.debug_log),
    }

    if cat_id:
        breaking = api.get_breaking_teams(cat_id)
        result["breaking_teams_raw"] = breaking[:5] if breaking else []
        result["breaking_teams_count"] = len(breaking)

    standings = api.get_team_standings()
    result["standings_raw"] = standings[:3] if standings else []
    result["standings_count"] = len(standings)

    teams = api.get_teams()
    result["teams_raw"] = teams[:2] if teams else []
    result["teams_count"] = len(teams)

    all_metric_names = set()
    for st in standings:
        for m in st.get("metrics", []):
            all_metric_names.add(m.get("metric", ""))
    result["all_metric_names_found"] = sorted(list(all_metric_names))

    result["final_debug_log"] = list(api.debug_log)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

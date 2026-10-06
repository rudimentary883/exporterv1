"""
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
import requests

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tabbycat-break-exporter-key-2026")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# Column order of the exported CSV. Rename a header here if your Google Sheet / Apps Script expects other names.
CSV_HEADERS = [
    "break", "team", "code_name", "speakers", "points", "total_speaker_score",
    "number_of_1sts", "number_of_2nds", "draw_strength_by_wins",
]

# Names Tabbycat may use for the new metrics (compared after lower-casing and turning spaces/dashes into "_")
FIRSTS_NAMES = ["firsts", "number_of_firsts", "num_firsts", "n_firsts", "1sts", "number_of_1sts", "first_places"]
SECONDS_NAMES = ["seconds", "number_of_seconds", "num_seconds", "n_seconds", "2nds", "number_of_2nds", "second_places"]
DRAW_NAMES = ["draw_strength", "draw_strength_by_wins", "draw_strength_wins", "draw_strength_points",
              "draw_strength_by_points", "opp_wins", "opponent_wins"]


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
    if not speakers:
        return ""
    names = [s.get("name", "") for s in speakers if s.get("name")]
    if debate_format == "bp":
        return " & ".join(names)
    else:
        return ", ".join(names)


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


class TabbycatAPI:
    def __init__(self, base_url, token, tournament_slug):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip() if token else ""
        self.slug = tournament_slug.strip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "TabbycatBreakExporter/3.2 (Render; Python requests)",
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

    def test_connection(self):
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
                metrics = [{"metric": n} for n in names]
                missing = []
                if not find_extra_metric(metrics, FIRSTS_NAMES, "first")[0]:
                    missing.append("number_of_1sts")
                if not find_extra_metric(metrics, SECONDS_NAMES, "second")[0]:
                    missing.append("number_of_2nds")
                if not find_extra_metric(metrics, DRAW_NAMES, "draw", ("speak", "score", "margin"))[0]:
                    missing.append("draw_strength_by_wins")
                notes.append("Standings metrics found: " + (", ".join(names) or "none") + ".")
                if missing:
                    notes.append("Not in this tab's standings (columns will be blank): " + ", ".join(missing) +
                                 ". Add them under Tabbycat settings > team standings (precedence or extra metrics).")
            elif sstatus in (401, 403):
                notes.append("Team standings are not readable with this token.")
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


def export_break_csv(api, category_slug, debate_format, intro_rows=True):
    category_id, category_info = api.get_break_category_by_slug(category_slug)

    if category_id is None:
        available = api.get_break_categories()
        slugs = [c.get("slug", "") for c in available]
        return None, f'Category "{category_slug}" not found. Available: {", ".join(slugs) or "none"}', {}

    api._log(f"Category: {category_info.get('name')} (id={category_id})")

    breaking = api.get_breaking_teams(category_id)
    if not breaking:
        return None, f'No breaking teams found for "{category_slug}". {" | ".join(api.debug_log)}', {}

    # v3.2: teams with a break rank first, in break order; teams without one (capped / ineligible ...) keep their order after
    breaking = sorted(breaking, key=lambda bt: (bt.get("break_rank") is None,
                                                bt.get("break_rank") if bt.get("break_rank") is not None else 0))

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
    for st in standings:
        tid = extract_id_from_url(st.get("team", ""))
        if tid:
            standings_lookup[tid] = st
        if "id" in st:
            standings_lookup[st["id"]] = st
    api._log(f"Standings lookup: {len(standings_lookup)} entries")

    seq_counter = 1
    rows = []
    metric_found = {"firsts": False, "seconds": False, "draw": False}

    for bt in breaking:
        team_data = bt.get("team")
        team_id = None
        team_obj = None

        if isinstance(team_data, dict):
            team_obj = team_data
            team_id = team_data.get("id") or extract_id_from_url(team_data.get("url", ""))
        elif isinstance(team_data, str):
            team_id = extract_id_from_url(team_data)

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
        speakers_str = format_speakers(speakers, debate_format)

        st = standings_lookup.get(team_id) if team_id else None
        points = None
        speaker_score = None
        firsts = seconds = draw_strength = None
        all_metric_names = []

        if st:
            metrics = st.get("metrics", [])
            points, _ = _find_metric(metrics, ["points", "wins", "team_points", "num_wins", "pts"])
            speaker_score, all_metric_names = _find_metric(metrics, [
                "speaks_sum", "speaks", "speaker_score", "total_speaker_score",
                "average_speaker_score", "total_speaks", "avg_speaks",
                "total", "average", "avg", "score", "spk", "speaker",
                "total score", "speaker scores", "cumulative"
            ])
            if speaker_score is None or speaker_score == "":
                api._log(f"No speaker score for {team_name}. Metrics: {all_metric_names}")

            found, firsts = find_extra_metric(metrics, FIRSTS_NAMES, "first")
            metric_found["firsts"] |= found
            found, seconds = find_extra_metric(metrics, SECONDS_NAMES, "second")
            metric_found["seconds"] |= found
            found, draw_strength = find_extra_metric(metrics, DRAW_NAMES, "draw", ("speak", "score", "margin"))
            metric_found["draw"] |= found
        else:
            api._log(f"No standings for {team_name} (id={team_id})")

        # v3.2: test for None instead of "falsy", so a real 0 is no longer replaced by a fallback / left blank
        if points is None or points == "":
            points = team_obj.get("points") if team_obj.get("points") is not None else team_obj.get("wins")
        if speaker_score is None or speaker_score == "":
            speaker_score = team_obj.get("speaker_score") if team_obj.get("speaker_score") is not None \
                else team_obj.get("total_speaker_score")

        points_str = format_number(points)
        speaker_score_str = format_speaker_score(speaker_score, debate_format)

        break_rank = bt.get("break_rank")
        if break_rank is not None:
            rank_str = ordinal(seq_counter)
            seq_counter += 1
        else:
            rank_str = ""

        rows.append({
            "break": rank_str,
            "team": team_name,
            "code_name": code_name,
            "speakers": speakers_str,
            "points": points_str,
            "total_speaker_score": speaker_score_str,
            "number_of_1sts": format_number(firsts),
            "number_of_2nds": format_number(seconds),
            "draw_strength_by_wins": format_number(draw_strength),
        })

    if not rows:
        return None, f"No valid data. {' | '.join(api.debug_log)}", {}

    missing = [name for key, name in (("firsts", "number_of_1sts"), ("seconds", "number_of_2nds"),
                                      ("draw", "draw_strength_by_wins")) if not metric_found[key]]
    if missing:
        api._log("Metrics not found in standings (columns left blank): " + ", ".join(missing))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    intro_count = 0
    for row in rows:
        # v3.2: "intro" row = same break rank + numbers, but no team / code_name / speakers.
        # Only for teams that really have a break rank (not for capped / unranked teams).
        if intro_rows and row["break"]:
            writer.writerow([
                row["break"], "", "", "",
                row["points"], row["total_speaker_score"],
                row["number_of_1sts"], row["number_of_2nds"], row["draw_strength_by_wins"],
            ])
            intro_count += 1
        writer.writerow([row[h] for h in CSV_HEADERS])

    metadata = {
        "category_name": category_info.get("name", category_slug) if category_info else category_slug,
        "category_slug": category_slug,
        "debate_format": debate_format,
        "team_count": len(rows),
        "intro_rows": intro_count,
        "row_count": len(rows) + intro_count,
        "missing_metrics": missing,
        "debug": " | ".join(api.debug_log),
    }
    return output.getvalue(), None, metadata


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
    diagnostics = api.test_connection()
    if diagnostics["ok"]:
        categories = api.get_break_categories()
        diagnostics["break_categories"] = [
            {"name": c.get("name", ""), "slug": c.get("slug", ""), "url": c.get("url", "")}
            for c in categories
        ]
    return jsonify(diagnostics)


@app.route("/export", methods=["POST"])
def export():
    base_url = request.form.get("base_url", "").strip()
    token = request.form.get("token", "").strip()
    slug = request.form.get("slug", "").strip()
    category_slug = request.form.get("category_slug", "").strip().lower()
    debate_format = request.form.get("debate_format", "bp")
    intro_rows = _intro_flag(request.form.get("intro_rows"))

    if not all([base_url, token, slug, category_slug]):
        flash("All fields are required.", "error")
        return redirect(url_for("index"))

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format, intro_rows)

    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    filename = f"{slug}_{category_slug}_break.csv"
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

    base_url = data.get("base_url", "").strip()
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_slug = data.get("category_slug", "").strip().lower()
    debate_format = data.get("debate_format", "bp")
    intro_rows = _intro_flag(data.get("intro_rows"))

    if not all([base_url, token, slug, category_slug]):
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format, intro_rows)

    if error:
        return jsonify({"ok": False, "error": error, "debug": metadata.get("debug", "")}), 400

    return jsonify({"ok": True, "csv": csv_data, "metadata": metadata})


@app.route("/api/export-csv", methods=["POST"])
def api_export_csv_raw():
    data = request.get_json()
    if not data:
        return "Error: JSON body required", 400

    base_url = data.get("base_url", "").strip()
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_slug = data.get("category_slug", "").strip().lower()
    debate_format = data.get("debate_format", "bp")
    intro_rows = _intro_flag(data.get("intro_rows"))

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, _ = export_break_csv(api, category_slug, debate_format, intro_rows)

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

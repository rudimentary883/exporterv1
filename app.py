"""
Tabbycat Break Exporter v3.0-C — Fuzzy metric matching + comprehensive debug.
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


def ordinal(n):
    if n is None or n == "":
        return ""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


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


def _find_metric(metrics, possible_names):
    """Exact match first, then fuzzy match."""
    if not metrics:
        return None, []
    
    all_names = []
    # Exact match
    lowered = [n.lower() for n in possible_names]
    for m in metrics:
        name = str(m.get("metric", "")).lower()
        all_names.append(name)
        if name in lowered:
            return m.get("value"), all_names
    
    # Fuzzy match: contains "speak" or "score"
    for m in metrics:
        name = str(m.get("metric", "")).lower()
        if "speak" in name or "score" in name:
            return m.get("value"), all_names
    
    return None, all_names


class TabbycatAPI:
    def __init__(self, base_url, token, tournament_slug):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip() if token else ""
        self.slug = tournament_slug.strip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "TabbycatBreakExporter/3.0-C (Render; Python requests)",
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
                        return self._request(method, redirect_url, retries=retries - attempt - 1)
                    return {"_error": f"HTTP {resp.status_code} redirect without Location", "_status": resp.status_code}
                else:
                    return {"_error": f"HTTP {resp.status_code}", "_status": resp.status_code, "_text": resp.text[:500]}
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    return {"_error": str(e), "_status": 0}
                time.sleep(1)
        return {"_error": "Max retries exceeded", "_status": 0}

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
        status = resp_data.get("_status", 200) if isinstance(resp_data, dict) and "_status" in resp_data else 200
        diagnostics["steps"].append({"step": "Tournament teams", "status": status, "ok": status == 200})

        url = self._url("/break-categories")
        resp_data = self._request("GET", url)
        status = resp_data.get("_status", 200) if isinstance(resp_data, dict) and "_status" in resp_data else 200
        diagnostics["steps"].append({"step": "Break categories", "status": status, "ok": status == 200})

        if status == 200:
            diagnostics["ok"] = True
            diagnostics["suggestion"] = "Connection successful!"
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
        data = self._request("GET", url)
        return _unwrap_results(data)

    def get_break_category_by_slug(self, category_slug):
        categories = self.get_break_categories()
        for cat in categories:
            cat_id = extract_id_from_url(cat.get("url", ""))
            if category_slug.lower() == cat.get("slug", "").lower():
                return cat_id, cat
            if category_slug.lower() in cat.get("name", "").lower():
                return cat_id, cat
        if len(categories) == 1:
            cat = categories[0]
            return extract_id_from_url(cat.get("url", "")), cat
        return None, None

    def get_breaking_teams(self, category_id):
        url = self._url(f"/break-categories/{category_id}/break")
        data = self._request("GET", url)
        self._log(f"Break endpoint: {url}")
        self._log(f"Break type: {type(data).__name__}")
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Break error: {data.get('_error')} status={data.get('_status')}")
            return []
        result = _unwrap_results(data)
        self._log(f"Break count: {len(result)}")
        if result and isinstance(result[0], dict):
            self._log(f"Break keys: {list(result[0].keys())}")
            self._log(f"First break_rank: {result[0].get('break_rank')}")
            self._log(f"First remark: {result[0].get('remark')}")
        return result

    def get_teams(self):
        url = self._url("/teams")
        data = self._request("GET", url)
        return _unwrap_results(data)

    def get_team_standings(self):
        url = self._url("/teams/standings")
        data = self._request("GET", url)
        self._log(f"Standings endpoint: {url}")
        self._log(f"Standings type: {type(data).__name__}")
        if isinstance(data, dict) and "_error" in data:
            self._log(f"Standings error: {data.get('_error')} status={data.get('_status')}")
            return []
        result = _unwrap_results(data)
        self._log(f"Standings count: {len(result)}")
        if result and isinstance(result[0], dict):
            self._log(f"Standings keys: {list(result[0].keys())}")
            metrics = result[0].get("metrics", [])
            if metrics:
                names = [m.get("metric", "") for m in metrics]
                self._log(f"Available metrics: {names}")
        return result


def export_break_csv(api, category_slug, debate_format):
    category_id, category_info = api.get_break_category_by_slug(category_slug)
    if category_id is None:
        available = api.get_break_categories()
        slugs = [c.get("slug", "") for c in available]
        return None, f'Category "{category_slug}" not found. Available: {", ".join(slugs) or "none"}', {}

    api._log(f"Category: {category_info.get('name')} (id={category_id})")

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
    for st in standings:
        tid = extract_id_from_url(st.get("team", ""))
        if tid:
            standings_lookup[tid] = st
        if "id" in st:
            standings_lookup[st["id"]] = st
    api._log(f"Standings lookup: {len(standings_lookup)} entries")

    seq_counter = 1
    rows = []
    
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

        speakers = team_obj.get("speakers", [])
        speakers_str = format_speakers(speakers, debate_format)

        # Get points and speaker score from standings
        st = standings_lookup.get(team_id) if team_id else None
        points = ""
        speaker_score = ""
        all_metric_names = []
        
        if st:
            metrics = st.get("metrics", [])
            points, _ = _find_metric(metrics, ["points", "wins", "team_points", "num_wins", "pts"])
            speaker_score, all_metric_names = _find_metric(metrics, [
                "speaks", "speaker_score", "total_speaker_score",
                "average_speaker_score", "total_speaks", "avg_speaks",
                "total", "average", "avg", "score", "spk", "speaker",
                "total score", "speaker scores", "cumulative"
            ])
            if not speaker_score:
                api._log(f"No speaker score for {team_name}. Metrics: {all_metric_names}")
        else:
            api._log(f"No standings for {team_name} (id={team_id})")

        # Fallback to team object
        if not points:
            points = team_obj.get("points") or team_obj.get("wins") or ""
        if not speaker_score:
            speaker_score = team_obj.get("speaker_score") or team_obj.get("total_speaker_score") or ""

        points_str = str(points) if points is not None else ""
        speaker_score_str = format_speaker_score(speaker_score, debate_format)

        # Break rank
        break_rank = bt.get("break_rank")
        if break_rank is not None:
            rank_str = ordinal(seq_counter)
            seq_counter += 1
        else:
            rank_str = ""

        rows.append({
            "break": rank_str,
            "team": team_name,
            "speakers": speakers_str,
            "points": points_str,
            "total_speaker_score": speaker_score_str,
        })

    if not rows:
        return None, f"No valid data. {' | '.join(api.debug_log)}", {}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["break", "team", "speakers", "points", "total_speaker_score"])
    for row in rows:
        writer.writerow([row["break"], row["team"], row["speakers"], row["points"], row["total_speaker_score"]])

    metadata = {
        "category_name": category_info.get("name", category_slug) if category_info else category_slug,
        "category_slug": category_slug,
        "debate_format": debate_format,
        "team_count": len(rows),
        "debug": " | ".join(api.debug_log),
    }
    return output.getvalue(), None, metadata


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

    if not all([base_url, token, slug, category_slug]):
        flash("All fields are required.", "error")
        return redirect(url_for("index"))

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format)

    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    filename = f"{slug}_{category_slug}_break.csv"
    buffer = io.BytesIO(csv_data.encode("utf-8"))
    return send_file(buffer, mimetype="text/csv", as_attachment=True, download_name=filename)


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

    if not all([base_url, token, slug, category_slug]):
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format)

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

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, _ = export_break_csv(api, category_slug, debate_format)

    if error:
        return f"Error: {error}", 400

    return csv_data, 200, {"Content-Type": "text/csv; charset=utf-8"}


@app.route("/api/debug", methods=["POST"])
def api_debug():
    """Comprehensive debug: dumps raw API responses."""
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
        # Raw breaking teams (first 5)
        breaking = api.get_breaking_teams(cat_id)
        result["breaking_teams_raw"] = breaking[:5] if breaking else []
        result["breaking_teams_count"] = len(breaking)

        # Raw standings (first 3 with full metrics)
        standings = api.get_team_standings()
        result["standings_raw"] = standings[:3] if standings else []
        result["standings_count"] = len(standings)

        # Raw teams (first 2)
        teams = api.get_teams()
        result["teams_raw"] = teams[:2] if teams else []
        result["teams_count"] = len(teams)

        # Try to find metric names from ALL standings
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

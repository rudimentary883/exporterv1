import os
import csv
import io
import requests
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# =============================================================================
# CONFIG — EXACTLY AS ORIGINAL
# =============================================================================
BASE_URL = os.environ.get("TABBYCAT_BASE_URL", "https://ndc2025.calicotab.com")

# =============================================================================
# HELPERS — EXACTLY AS ORIGINAL
# =============================================================================

def _unwrap_results(data):
    """Handle paginated {results: [...]} or plain lists."""
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data if isinstance(data, list) else []


def _find_metric(metrics, keyword):
    """Find a metric by exact or fuzzy match."""
    if not metrics:
        return None
    for m in metrics:
        mk = m.get("metric", "")
        if mk == keyword:
            return m.get("value")
    for m in metrics:
        mk = m.get("metric", "").lower()
        if keyword in mk or mk in keyword:
            return m.get("value")
    return None


def _ordinal(n):
    """1 -> 1st, 2 -> 2nd, 3 -> 3rd, etc."""
    if n is None:
        return ""
    n = int(n)
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _api_get(token, path, params=None):
    """Make an authenticated GET request to the Tabbycat API."""
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Token {token}"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


# =============================================================================
# BREAK EXPORT — EXACTLY AS ORIGINAL (UNTOUCHED)
# =============================================================================

def fetch_break_categories(token, slug):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/break-categories/")
    return _unwrap_results(data)


def fetch_breaking_teams(token, slug, category_id):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/break-categories/{category_id}/break/")
    return _unwrap_results(data)


def fetch_team_standings(token, slug):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/teams/standings/")
    return _unwrap_results(data)


def build_break_csv(token, slug, category_id):
    categories = fetch_break_categories(token, slug)
    cat_name = ""
    for c in categories:
        if str(c.get("id")) == str(category_id):
            cat_name = c.get("name", "")
            break

    breaking = fetch_breaking_teams(token, slug, category_id)
    standings = fetch_team_standings(token, slug)

    standings_map = {}
    for s in standings:
        team_ref = s.get("team")
        if isinstance(team_ref, dict):
            team_key = team_ref.get("url", team_ref.get("id"))
        else:
            team_key = team_ref
        if team_key:
            standings_map[team_key] = s

    teams_data = _api_get(token, f"/api/v1/tournaments/{slug}/teams/")
    teams_list = _unwrap_results(teams_data)
    teams_map = {}
    for t in teams_list:
        teams_map[t.get("url", t.get("id"))] = t

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["break", "team", "speakers", "points", "total_speaker_score"])

    rank_counter = 1
    for entry in breaking:
        team_ref = entry.get("team")
        if isinstance(team_ref, dict):
            team_url = team_ref.get("url", "")
            team_id = team_ref.get("id", "")
        else:
            team_url = team_ref
            team_id = team_ref

        team_obj = teams_map.get(team_url) or teams_map.get(team_id)
        if not team_obj:
            continue

        team_name = team_obj.get("short_name") or team_obj.get("long_name") or team_obj.get("code_name", "")

        speakers = team_obj.get("speakers", [])
        speaker_names = [sp.get("name", "") for sp in speakers]
        if len(speaker_names) >= 3:
            speakers_str = " & ".join(speaker_names)
        else:
            speakers_str = ", ".join(speaker_names)

        st = standings_map.get(team_url) or standings_map.get(team_id)
        points = ""
        total_speaks = ""
        if st:
            metrics = st.get("metrics", [])
            points = _find_metric(metrics, "points") or _find_metric(metrics, "wins") or ""
            total_speaks = _find_metric(metrics, "speaks_sum") or _find_metric(metrics, "speaks") or ""
            if total_speaks != "":
                try:
                    val = float(total_speaks)
                    if len(speaker_names) >= 3:
                        total_speaks = str(int(val))
                    else:
                        total_speaks = str(val)
                except (ValueError, TypeError):
                    pass

        break_rank_raw = entry.get("break_rank")
        if break_rank_raw is None:
            break_rank = ""
        else:
            break_rank = _ordinal(rank_counter)
            rank_counter += 1

        writer.writerow([break_rank, team_name, speakers_str, points, total_speaks])

    return output.getvalue(), cat_name


# =============================================================================
# SPEAKER EXPORT — NEW FEATURE ONLY (uses same BASE_URL, same _api_get)
# =============================================================================

def fetch_speaker_categories(token, slug):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/speaker-categories/")
    return _unwrap_results(data)


def fetch_speaker_standings(token, slug, category_id=None, round_seq=None):
    params = {}
    if category_id:
        params["category"] = category_id
    if round_seq:
        params["round"] = round_seq
    data = _api_get(token, f"/api/v1/tournaments/{slug}/speakers/standings/", params=params)
    return _unwrap_results(data)


def fetch_speakers(token, slug):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/speakers/")
    return _unwrap_results(data)


def fetch_teams(token, slug):
    data = _api_get(token, f"/api/v1/tournaments/{slug}/teams/")
    return _unwrap_results(data)


def build_speaker_csv(token, slug, category_id=None, round_seq=None):
    standings = fetch_speaker_standings(token, slug, category_id, round_seq)
    speakers = fetch_speakers(token, slug)
    teams = fetch_teams(token, slug)

    speakers_map = {}
    for sp in speakers:
        key = sp.get("url", sp.get("id"))
        if key:
            speakers_map[key] = sp

    teams_map = {}
    for t in teams:
        key = t.get("url", t.get("id"))
        if key:
            teams_map[key] = t

    filtered = []
    for entry in standings:
        rank = entry.get("rank")
        if rank is not None and int(rank) <= 10:
            filtered.append(entry)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "speaker", "team", "average"])

    for entry in filtered:
        rank = entry.get("rank")
        is_tied = entry.get("tied", False)
        ordinal_rank = _ordinal(rank)
        rank_str = f"co-{ordinal_rank}" if is_tied else ordinal_rank

        speaker_ref = entry.get("speaker")
        speaker_name = ""
        team_name = ""

        if isinstance(speaker_ref, dict):
            speaker_name = speaker_ref.get("name", "")
            team_ref = speaker_ref.get("team")
        elif isinstance(speaker_ref, str):
            sp = speakers_map.get(speaker_ref)
            if sp:
                speaker_name = sp.get("name", "")
                team_ref = sp.get("team")
            else:
                team_ref = None
        else:
            team_ref = None

        if isinstance(team_ref, dict):
            team_url = team_ref.get("url", "")
            team_id = team_ref.get("id", "")
        elif isinstance(team_ref, str):
            team_url = team_ref
            team_id = team_ref
        else:
            team_url = ""
            team_id = ""

        team_obj = teams_map.get(team_url) or teams_map.get(team_id)
        if team_obj:
            team_name = (
                team_obj.get("short_reference") or
                team_obj.get("short_name") or
                team_obj.get("long_name") or
                team_obj.get("code_name") or
                team_obj.get("reference", "")
            )

        metrics = entry.get("metrics", [])
        average = _find_metric(metrics, "average")
        if average is None:
            average = _find_metric(metrics, "total")
        if average is None:
            average = ""
        else:
            try:
                val = float(average)
                if val == int(val):
                    average = str(int(val))
                else:
                    average = f"{val:.2f}"
            except (ValueError, TypeError):
                average = str(average)

        writer.writerow([rank_str, speaker_name, team_name, average])

    return output.getvalue()


# =============================================================================
# FLASK ROUTES — ORIGINAL BREAK ROUTES UNTOUCHED, NEW SPEAKER ROUTES ADDED
# =============================================================================

@app.route("/")
def index():
    return render_template("index.html")


# --- ORIGINAL BREAK ROUTES (zero changes) ---

@app.route("/test-connection", methods=["POST"])
def test_connection():
    token = request.form.get("token", "").strip()
    slug = request.form.get("slug", "").strip()
    if not token or not slug:
        return jsonify({"ok": False, "error": "Token and slug required"}), 400
    try:
        categories = fetch_break_categories(token, slug)
        return jsonify({"ok": True, "categories": categories})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/export", methods=["POST"])
def export():
    token = request.form.get("token", "").strip()
    slug = request.form.get("slug", "").strip()
    category_id = request.form.get("category_id", "").strip()
    if not token or not slug or not category_id:
        return "Missing fields", 400
    try:
        csv_data, cat_name = build_break_csv(token, slug, category_id)
        filename = f"{slug}_break_{cat_name or category_id}.csv".replace(" ", "_")
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_id = data.get("category_id", "").strip()
    if not token or not slug or not category_id:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        csv_data, cat_name = build_break_csv(token, slug, category_id)
        return jsonify({"ok": True, "csv": csv_data, "metadata": {"category": cat_name}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export-csv", methods=["POST"])
def api_export_csv():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_id = data.get("category_id", "").strip()
    if not token or not slug or not category_id:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        csv_data, _ = build_break_csv(token, slug, category_id)
        return Response(csv_data, mimetype="text/csv")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/debug", methods=["POST"])
def api_debug():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    if not token or not slug:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        categories = fetch_break_categories(token, slug)
        if categories:
            cat_id = categories[0]["id"]
            breaking = fetch_breaking_teams(token, slug, cat_id)
            standings = fetch_team_standings(token, slug)
        else:
            breaking = []
            standings = []
        return jsonify({
            "ok": True,
            "categories": categories,
            "sample_breaking": breaking[:3],
            "sample_standings": standings[:3]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --- NEW SPEAKER ROUTES (added alongside originals) ---

@app.route("/test-speaker-connection", methods=["POST"])
def test_speaker_connection():
    token = request.form.get("token", "").strip()
    slug = request.form.get("slug", "").strip()
    if not token or not slug:
        return jsonify({"ok": False, "error": "Token and slug required"}), 400
    try:
        categories = fetch_speaker_categories(token, slug)
        return jsonify({"ok": True, "categories": categories})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/export-speakers", methods=["POST"])
def export_speakers():
    token = request.form.get("token", "").strip()
    slug = request.form.get("slug", "").strip()
    category_id = request.form.get("category_id", "").strip() or None
    round_seq = request.form.get("round", "").strip() or None
    if not token or not slug:
        return "Missing fields", 400
    try:
        csv_data = build_speaker_csv(token, slug, category_id, round_seq)
        cat_label = category_id or "all"
        filename = f"{slug}_speakers_{cat_label}.csv".replace(" ", "_")
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/api/export-speakers", methods=["POST"])
def api_export_speakers():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_id = data.get("category_id") or None
    round_seq = data.get("round") or None
    if not token or not slug:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        csv_data = build_speaker_csv(token, slug, category_id, round_seq)
        return jsonify({"ok": True, "csv": csv_data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/export-speakers-csv", methods=["POST"])
def api_export_speakers_csv():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    category_id = data.get("category_id") or None
    round_seq = data.get("round") or None
    if not token or not slug:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        csv_data = build_speaker_csv(token, slug, category_id, round_seq)
        return Response(csv_data, mimetype="text/csv")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/debug-speakers", methods=["POST"])
def api_debug_speakers():
    data = request.get_json(force=True) or {}
    token = data.get("token", "").strip()
    slug = data.get("slug", "").strip()
    if not token or not slug:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    try:
        categories = fetch_speaker_categories(token, slug)
        standings = fetch_speaker_standings(token, slug)
        return jsonify({
            "ok": True,
            "categories": categories,
            "sample_standings": standings[:5]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)

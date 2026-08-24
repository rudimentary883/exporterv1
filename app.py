"""
Tabbycat Break Exporter v2.0 — Exports breaking teams to CSV for break slides automation.
Designed for Render Free Tier: 512MB RAM, single worker, 120s timeout.

FIXES in v2.0:
- Correct API endpoint: /break (not /breaking/)
- Fetches team standings separately for points & speaker scores
- Flexible metric name detection
- Extensive debug logging
"""

import os
import io
import csv
import json
import time
import re
import requests

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tabbycat-break-exporter-key-2026')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def ordinal(n):
    """Convert integer to ordinal string: 1→1st, 2→2nd, 3→3rd, 4→4th, etc."""
    if n is None:
        return ''
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def format_speakers(speakers, debate_format):
    """
    Format speaker names for CSV export.
    BP:   'Speaker A & Speaker B'
    3v3:  'Speaker A, Speaker B, Speaker C'
    """
    if not speakers:
        return ''
    names = [s.get('name', '') for s in speakers if s.get('name')]
    if debate_format == 'bp':
        return ' & '.join(names)
    else:
        return ', '.join(names)


def format_speaker_score(score, debate_format):
    """
    Format total speaker score.
    BP:   remove decimal places → int
    3v3:  keep as-is (could be float or int)
    """
    if score is None or score == '':
        return ''
    try:
        if debate_format == 'bp':
            return str(int(float(score)))
        else:
            return str(score)
    except (ValueError, TypeError):
        return str(score)


def extract_id_from_url(url):
    """Extract the numeric ID from a Tabbycat API URL."""
    if not url:
        return None
    match = re.search(r'/([0-9]+)/?$', url.rstrip('/'))
    return int(match.group(1)) if match else None


def _unwrap_results(data):
    """
    Tabbycat API sometimes returns paginated dicts: {"results": [...]}
    and sometimes returns raw lists. This handles both.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if 'results' in data:
            return data['results']
    return []


def _find_metric(metrics, possible_names):
    """
    Find a metric value from standings metrics by trying multiple possible names.
    metrics: list of {'metric': str, 'value': number}
    possible_names: list of strings to try (case-insensitive)
    """
    if not metrics:
        return None
    lowered = [n.lower() for n in possible_names]
    for m in metrics:
        name = str(m.get('metric', '')).lower()
        if name in lowered:
            return m.get('value')
    return None


# =============================================================================
# TABBYCAT API CLIENT
# =============================================================================

class TabbycatAPI:
    def __init__(self, base_url, token, tournament_slug):
        self.base_url = base_url.rstrip('/')
        self.token = token.strip() if token else ''
        self.slug = tournament_slug.strip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'TabbycatBreakExporter/2.0 (Render; Python requests)',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })
        if self.token:
            self.session.headers['Authorization'] = f'Token {self.token}'
        self.debug_log = []

    def _log(self, msg):
        self.debug_log.append(msg)

    def _url(self, path):
        return f"{self.base_url}/api/v1/tournaments/{self.slug}{path}"

    def _request(self, method, url, retries=3):
        for attempt in range(retries):
            try:
                time.sleep(0.3)
                if method == 'GET':
                    resp = self.session.get(url, timeout=30)
                else:
                    resp = self.session.post(url, json={}, timeout=30)

                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception as e:
                        return {'_error': f'JSON parse error: {e}', '_status': 200, '_text': resp.text[:500]}
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                elif resp.status_code in (301, 302, 307, 308):
                    # Follow redirect manually for API calls
                    redirect_url = resp.headers.get('Location', '')
                    if redirect_url:
                        self._log(f'Redirect: {url} → {redirect_url}')
                        return self._request(method, redirect_url, retries=retries - attempt - 1)
                    return {'_error': f'HTTP {resp.status_code} redirect without Location', '_status': resp.status_code}
                else:
                    return {'_error': f'HTTP {resp.status_code}', '_status': resp.status_code, '_text': resp.text[:500]}
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    return {'_error': str(e), '_status': 0}
                time.sleep(1)
        return {'_error': 'Max retries exceeded', '_status': 0}

    def test_connection(self):
        diagnostics = {'ok': False, 'steps': [], 'suggestion': '', 'debug': []}
        try:
            resp = self.session.get(self.base_url, timeout=10, allow_redirects=True)
            diagnostics['steps'].append({
                'step': 'Base URL reachable',
                'status': resp.status_code,
                'ok': resp.status_code < 500
            })
        except Exception as e:
            diagnostics['steps'].append({'step': 'Base URL', 'status': 0, 'ok': False, 'error': str(e)})
            diagnostics['suggestion'] = 'Cannot reach Tabbycat URL. Check for typos.'
            return diagnostics

        # Test tournament teams endpoint
        url = self._url('/teams')
        resp_data = self._request('GET', url)
        if isinstance(resp_data, dict) and '_status' in resp_data:
            status = resp_data['_status']
        else:
            status = 200
        diagnostics['steps'].append({
            'step': 'Tournament teams (GET /teams)',
            'status': status,
            'ok': status == 200
        })

        # Test break categories endpoint
        url = self._url('/break-categories')
        resp_data = self._request('GET', url)
        if isinstance(resp_data, dict) and '_status' in resp_data:
            status = resp_data['_status']
        else:
            status = 200
        diagnostics['steps'].append({
            'step': 'Break categories (GET /break-categories)',
            'status': status,
            'ok': status == 200
        })

        if status == 200:
            diagnostics['ok'] = True
            diagnostics['suggestion'] = 'Connection successful! API is working.'
        elif status == 401:
            diagnostics['suggestion'] = 'Token is invalid or expired. Get a new token from Change Password page.'
        elif status == 403:
            diagnostics['suggestion'] = 'Access forbidden. Check your token permissions.'
        elif status == 404:
            diagnostics['suggestion'] = f'Tournament slug "{self.slug}" not found.'
        else:
            diagnostics['suggestion'] = f'Unexpected status {status}.'

        return diagnostics

    def get_break_categories(self):
        url = self._url('/break-categories')
        data = self._request('GET', url)
        return _unwrap_results(data)

    def get_break_category_by_slug(self, category_slug):
        categories = self.get_break_categories()
        for cat in categories:
            cat_url = cat.get('url', '')
            cat_id = extract_id_from_url(cat_url)
            cat_slug = cat.get('slug', '')
            cat_name = cat.get('name', '')
            if category_slug.lower() == cat_slug.lower():
                return cat_id, cat
            if category_slug.lower() in cat_name.lower():
                return cat_id, cat
        # Fallback: if only one category exists, return it
        if len(categories) == 1:
            cat = categories[0]
            return extract_id_from_url(cat.get('url', '')), cat
        return None, None

    def get_breaking_teams(self, category_id):
        """Fetch breaking teams for a break category. Endpoint: /break (not /breaking/)"""
        url = self._url(f'/break-categories/{category_id}/break')
        data = self._request('GET', url)
        self._log(f'Break endpoint: {url}')
        self._log(f'Break response type: {type(data).__name__}')
        if isinstance(data, dict) and '_error' in data:
            self._log(f'Break error: {data.get("_error")} status={data.get("_status")}')
            return []
        if isinstance(data, dict):
            self._log(f'Break response keys: {list(data.keys())}')
        elif isinstance(data, list):
            self._log(f'Break response count: {len(data)}')
            if len(data) > 0:
                self._log(f'Break first item keys: {list(data[0].keys()) if isinstance(data[0], dict) else "not dict"}')
        return _unwrap_results(data)

    def get_teams(self):
        url = self._url('/teams')
        data = self._request('GET', url)
        return _unwrap_results(data)

    def get_team_standings(self):
        """Fetch team standings to get points and speaker scores."""
        url = self._url('/teams/standings')
        data = self._request('GET', url)
        self._log(f'Standings endpoint: {url}')
        self._log(f'Standings response type: {type(data).__name__}')
        if isinstance(data, dict) and '_error' in data:
            self._log(f'Standings error: {data.get("_error")} status={data.get("_status")}')
            return []
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict):
                self._log(f'Standings first item keys: {list(first.keys())}')
                metrics = first.get('metrics', [])
                if metrics:
                    metric_names = [m.get('metric', '') for m in metrics]
                    self._log(f'Available metrics: {metric_names}')
        return _unwrap_results(data)


# =============================================================================
# CSV EXPORT LOGIC
# =============================================================================

def export_break_csv(api, category_slug, debate_format):
    # 1. Find break category
    category_id, category_info = api.get_break_category_by_slug(category_slug)
    if category_id is None:
        available = api.get_break_categories()
        available_slugs = [c.get('slug', '') for c in available]
        return None, (
            f'Break category "{category_slug}" not found. '
            f'Available slugs: {", ".join(available_slugs) or "none"}'
        ), {}

    api._log(f'Found category: {category_info.get("name")} (id={category_id}, slug={category_info.get("slug")})')

    # 2. Fetch breaking teams
    breaking = api.get_breaking_teams(category_id)
    if not breaking:
        return None, (
            f'No breaking teams found for "{category_slug}". '
            f'Debug: {" | ".join(api.debug_log)}. '
            f'Make sure the break has been generated in Tabbycat admin (Breaks → {category_slug.title()} → Generate Break).'
        ), {}

    api._log(f'Breaking teams count: {len(breaking)}')

    # 3. Fetch all teams (for names and speakers)
    all_teams = api.get_teams()
    team_lookup = {}
    for team in all_teams:
        team_url = team.get('url', '')
        team_id = extract_id_from_url(team_url)
        if team_id:
            team_lookup[team_id] = team
        if 'id' in team:
            team_lookup[team['id']] = team
    api._log(f'Teams fetched: {len(all_teams)}, lookup size: {len(team_lookup)}')

    # 4. Fetch team standings (for points and speaker scores)
    standings = api.get_team_standings()
    standings_lookup = {}
    for st in standings:
        team_url = st.get('team', '')
        team_id = extract_id_from_url(team_url)
        if team_id:
            standings_lookup[team_id] = st
        if 'id' in st:
            standings_lookup[st['id']] = st
    api._log(f'Standings fetched: {len(standings)}, lookup size: {len(standings_lookup)}')

    # 5. Build CSV rows
    rows = []
    missing_team_data = []
    missing_standings = []

    for bt in breaking:
        break_rank = bt.get('break_rank')
        if break_rank is None:
            break_rank = len(rows) + 1

        # Get team reference from breaking team
        team_data = bt.get('team')
        team_id = None
        team_obj = None

        if isinstance(team_data, dict):
            team_obj = team_data
            team_id = team_data.get('id') or extract_id_from_url(team_data.get('url', ''))
        elif isinstance(team_data, str):
            team_id = extract_id_from_url(team_data)

        if team_obj is None and team_id and team_id in team_lookup:
            team_obj = team_lookup[team_id]

        if team_obj is None:
            missing_team_data.append(f'team_id={team_id}')
            continue

        # Team name
        team_name = (team_obj.get('short_name') or
                     team_obj.get('long_name') or
                     team_obj.get('reference') or
                     team_obj.get('code_name') or
                     f'Team {team_id}')

        # Speakers
        speakers = team_obj.get('speakers', [])
        speakers_str = format_speakers(speakers, debate_format)

        # Points and speaker score from standings
        st = standings_lookup.get(team_id) if team_id else None
        points = ''
        speaker_score = ''
        if st:
            metrics = st.get('metrics', [])
            points = _find_metric(metrics, ['points', 'wins', 'team_points', 'num_wins'])
            speaker_score = _find_metric(metrics, [
                'speaks', 'speaker_score', 'total_speaker_score',
                'average_speaker_score', 'total_speaks', 'avg_speaks'
            ])
        else:
            missing_standings.append(f'team_id={team_id} ({team_name})')

        # Fallback: try to get from team object directly (some Tabbycat versions include these)
        if not points:
            points = team_obj.get('points') or team_obj.get('wins') or ''
        if not speaker_score:
            speaker_score = team_obj.get('speaker_score') or team_obj.get('total_speaker_score') or ''

        points_str = str(points) if points is not None else ''
        speaker_score_str = format_speaker_score(speaker_score, debate_format)

        rows.append({
            'break': ordinal(break_rank),
            'team': team_name,
            'speakers': speakers_str,
            'points': points_str,
            'total_speaker_score': speaker_score_str
        })

    if missing_team_data:
        api._log(f'Missing team data for: {", ".join(missing_team_data[:5])}')
    if missing_standings:
        api._log(f'Missing standings for: {", ".join(missing_standings[:5])}')

    if not rows:
        return None, (
            f'No valid breaking team data could be assembled. '
            f'Debug: {" | ".join(api.debug_log)}'
        ), {}

    # 6. Generate CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['break', 'team', 'speakers', 'points', 'total_speaker_score'])
    for row in rows:
        writer.writerow([
            row['break'],
            row['team'],
            row['speakers'],
            row['points'],
            row['total_speaker_score']
        ])

    metadata = {
        'category_name': category_info.get('name', category_slug) if category_info else category_slug,
        'category_slug': category_slug,
        'debate_format': debate_format,
        'team_count': len(rows),
        'debug': ' | '.join(api.debug_log)
    }

    return output.getvalue(), None, metadata


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/test-connection', methods=['POST'])
def test_connection():
    data = request.get_json()
    api = TabbycatAPI(data.get('base_url', ''), data.get('token', ''), data.get('slug', ''))
    diagnostics = api.test_connection()
    if diagnostics['ok']:
        categories = api.get_break_categories()
        diagnostics['break_categories'] = [
            {'name': c.get('name', ''), 'slug': c.get('slug', ''), 'url': c.get('url', '')}
            for c in categories
        ]
    return jsonify(diagnostics)


@app.route('/export', methods=['POST'])
def export():
    base_url = request.form.get('base_url', '').strip()
    token = request.form.get('token', '').strip()
    slug = request.form.get('slug', '').strip()
    category_slug = request.form.get('category_slug', '').strip().lower()
    debate_format = request.form.get('debate_format', 'bp')

    if not all([base_url, token, slug, category_slug]):
        flash('All fields are required.', 'error')
        return redirect(url_for('index'))

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format)

    if error:
        flash(error, 'error')
        return redirect(url_for('index'))

    filename = f"{slug}_{category_slug}_break.csv"
    buffer = io.BytesIO(csv_data.encode('utf-8'))
    return send_file(buffer, mimetype='text/csv', as_attachment=True, download_name=filename)


@app.route('/api/export', methods=['POST'])
def api_export():
    data = request.get_json()
    if not data:
        return jsonify({'ok': False, 'error': 'JSON body required'}), 400

    base_url = data.get('base_url', '').strip()
    token = data.get('token', '').strip()
    slug = data.get('slug', '').strip()
    category_slug = data.get('category_slug', '').strip().lower()
    debate_format = data.get('debate_format', 'bp')

    if not all([base_url, token, slug, category_slug]):
        missing = [f for f in ['base_url', 'token', 'slug', 'category_slug'] if not locals()[f]]
        return jsonify({'ok': False, 'error': f'Missing fields: {", ".join(missing)}'}), 400

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format)

    if error:
        return jsonify({'ok': False, 'error': error, 'debug': metadata.get('debug', '')}), 400

    return jsonify({'ok': True, 'csv': csv_data, 'metadata': metadata})


@app.route('/api/export-csv', methods=['POST'])
def api_export_csv_raw():
    data = request.get_json()
    if not data:
        return 'Error: JSON body required', 400

    base_url = data.get('base_url', '').strip()
    token = data.get('token', '').strip()
    slug = data.get('slug', '').strip()
    category_slug = data.get('category_slug', '').strip().lower()
    debate_format = data.get('debate_format', 'bp')

    api = TabbycatAPI(base_url, token, slug)
    csv_data, error, metadata = export_break_csv(api, category_slug, debate_format)

    if error:
        return f'Error: {error}', 400

    return csv_data, 200, {'Content-Type': 'text/csv; charset=utf-8'}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

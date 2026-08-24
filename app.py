"""
Tabbycat Break Exporter v1.1 — Exports breaking teams to CSV for break slides automation.
Designed for Render Free Tier: 512MB RAM, single worker, 120s timeout.
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
    if score is None:
        return ''
    if debate_format == 'bp':
        return str(int(float(score)))
    else:
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
            'User-Agent': 'TabbycatBreakExporter/1.1 (Render; Python requests)',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })
        if self.token:
            self.session.headers['Authorization'] = f'Token {self.token}'

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
                    return resp.json()
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    return {'_error': f'HTTP {resp.status_code}', '_status': resp.status_code, '_text': resp.text[:500]}
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    return {'_error': str(e), '_status': 0}
                time.sleep(1)
        return {'_error': 'Max retries exceeded', '_status': 0}

    def test_connection(self):
        diagnostics = {'ok': False, 'steps': [], 'suggestion': ''}
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
            if category_slug.lower() in cat.get('slug', '').lower():
                return cat_id, cat
            if category_slug.lower() in cat.get('name', '').lower():
                return cat_id, cat
        if len(categories) == 1:
            cat = categories[0]
            return extract_id_from_url(cat.get('url', '')), cat
        return None, None

    def get_breaking_teams(self, category_id):
        url = self._url(f'/break-categories/{category_id}/breaking/')
        data = self._request('GET', url)
        return _unwrap_results(data)

    def get_teams(self):
        url = self._url('/teams')
        data = self._request('GET', url)
        return _unwrap_results(data)


# =============================================================================
# CSV EXPORT LOGIC
# =============================================================================

def export_break_csv(api, category_slug, debate_format):
    category_id, category_info = api.get_break_category_by_slug(category_slug)
    if category_id is None:
        return None, f'Break category "{category_slug}" not found.', {}

    breaking = api.get_breaking_teams(category_id)
    
    # DEBUG: capture raw response for diagnostics
    debug_url = api._url(f'/break-categories/{category_id}/breaking/')
    raw_response = api._request('GET', debug_url)
    
    if not breaking:
        # Try to give a more helpful error with debug info
        debug_info = ''
        if isinstance(raw_response, dict) and '_error' in raw_response:
            debug_info = f" API error: {raw_response.get('_error')} (status {raw_response.get('_status')})"
        elif isinstance(raw_response, (list, dict)) and len(raw_response) == 0:
            debug_info = " The API returned an empty list."
        
        return None, (
            f'No breaking teams found for "{category_slug}".{debug_info} '
            f'In Tabbycat admin, go to Breaks → {category_slug.title()} → "Generate Break". '
            f'If already generated, the break category slug may be different — click Test Connection to see available slugs.'
        ), {}

    all_teams = api.get_teams()
    team_lookup = {}
    for team in all_teams:
        team_url = team.get('url', '')
        team_id = extract_id_from_url(team_url)
        if team_id:
            team_lookup[team_id] = team
        if 'id' in team:
            team_lookup[team['id']] = team

    rows = []
    for bt in breaking:
        break_rank = bt.get('break_rank') or bt.get('rank')
        if break_rank is None:
            break_rank = len(rows) + 1

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
            continue

        team_name = (team_obj.get('short_name') or
                     team_obj.get('long_name') or
                     team_obj.get('reference') or
                     f'Team {team_id}')

        speakers = team_obj.get('speakers', [])
        speakers_str = format_speakers(speakers, debate_format)

        points = bt.get('points') or bt.get('wins') or bt.get('team_points') or ''

        speaker_score = (bt.get('speaker_score') or
                         bt.get('total_speaker_score') or
                         bt.get('score') or
                         bt.get('total_score') or '')
        speaker_score_str = format_speaker_score(speaker_score, debate_format)

        rows.append({
            'break': ordinal(break_rank),
            'team': team_name,
            'speakers': speakers_str,
            'points': points,
            'total_speaker_score': speaker_score_str
        })

    if not rows:
        return None, 'No valid breaking team data could be assembled. Check that teams have speakers assigned.', {}

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
        'team_count': len(rows)
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
        return jsonify({'ok': False, 'error': error}), 400

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
    csv_data, error, _ = export_break_csv(api, category_slug, debate_format)

    if error:
        return f'Error: {error}', 400

    return csv_data, 200, {'Content-Type': 'text/csv; charset=utf-8'}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

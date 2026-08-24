/**
 * Tabbycat Break Slides Automation — Google Apps Script
 * 
 * Reads break data from a Google Sheet and populates a Google Slides
 * template by duplicating a master slide for each breaking team.
 * 
 * SETUP:
 * 1. Create Google Slides with ONE template slide containing placeholders:
 *    {{break}}, {{team}}, {{speakers}}, {{wins}}, {{speaks}}
 * 2. Create Google Sheet, paste CSV data (with headers) into Sheet1.
 * 3. Open Extensions → Apps Script, paste this entire file.
 * 4. Update SLIDES_ID below with your presentation ID.
 * 5. Run setupTrigger() once, then use the Break Slides menu.
 */

// ============================================
// CONFIGURATION — UPDATE THESE
// ============================================

var SLIDES_ID = 'YOUR_SLIDES_ID_HERE';
var SHEET_NAME = 'Sheet1';
var KEEP_TEMPLATE_SLIDE = true;

// ============================================
// MENU SETUP
// ============================================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Break Slides')
    .addItem('Generate Break Slides', 'generateBreakSlides')
    .addItem('Clear Generated Slides', 'clearGeneratedSlides')
    .addToUi();
}

function setupTrigger() {
  onOpen();
  SpreadsheetApp.getUi().alert('Menu created! Refresh the sheet if you don\'t see it.');
}

// ============================================
// MAIN: Generate Slides
// ============================================

function generateBreakSlides() {
  if (SLIDES_ID === 'YOUR_SLIDES_ID_HERE') {
    SpreadsheetApp.getUi().alert('ERROR: Please set SLIDES_ID in the script code first.');
    return;
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    SpreadsheetApp.getUi().alert('Sheet "' + SHEET_NAME + '" not found!');
    return;
  }

  var values = sheet.getDataRange().getValues();
  if (values.length < 2) {
    SpreadsheetApp.getUi().alert('No data found. Paste CSV with headers first.');
    return;
  }

  var headers = values[0].map(function(h) { return String(h).toLowerCase().trim(); });
  var colBreak = headers.indexOf('break');
  var colTeam = headers.indexOf('team');
  var colSpeakers = headers.indexOf('speakers');
  var colPoints = headers.indexOf('points');
  var colScore = headers.indexOf('total_speaker_score');

  if (colBreak === -1 || colTeam === -1) {
    SpreadsheetApp.getUi().alert('Missing required columns. Expected: break, team, speakers, points, total_speaker_score');
    return;
  }

  var presentation;
  try {
    presentation = SlidesApp.openById(SLIDES_ID);
  } catch (e) {
    SpreadsheetApp.getUi().alert('Could not open presentation. Check SLIDES_ID.');
    return;
  }

  var slides = presentation.getSlides();
  if (slides.length === 0) {
    SpreadsheetApp.getUi().alert('Presentation has no slides. Add a template slide first.');
    return;
  }

  var templateSlide = slides[0];
  var generatedCount = 0;
  var errors = [];

  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var breakRank = colBreak !== -1 ? String(row[colBreak] || '') : '';
    var teamName = colTeam !== -1 ? String(row[colTeam] || '') : '';
    var speakers = colSpeakers !== -1 ? String(row[colSpeakers] || '') : '';
    var points = colPoints !== -1 ? String(row[colPoints] || '') : '';
    var score = colScore !== -1 ? String(row[colScore] || '') : '';

    if (!teamName) continue;

    try {
      var newSlide = templateSlide.duplicate();
      var shapes = newSlide.getShapes();
      for (var j = 0; j < shapes.length; j++) {
        var shape = shapes[j];
        if (shape.getText) {
          var textRange = shape.getText();
          var text = textRange.asString();
          if (text.indexOf('{{') !== -1) {
            text = text.replace(/\{\{break\}\}/g, breakRank);
            text = text.replace(/\{\{team\}\}/g, teamName);
            text = text.replace(/\{\{speakers\}\}/g, speakers);
            text = text.replace(/\{\{wins\}\}/g, points);
            text = text.replace(/\{\{speaks\}\}/g, score);
            textRange.setText(text);
          }
        }
      }
      generatedCount++;
    } catch (e) {
      errors.push('Row ' + (i + 1) + ' (' + teamName + '): ' + e.message);
    }
  }

  if (!KEEP_TEMPLATE_SLIDE && generatedCount > 0) {
    try { templateSlide.remove(); } catch (e) { errors.push('Could not remove template: ' + e.message); }
  }

  var msg = '✅ Generated ' + generatedCount + ' break slide(s).';
  if (errors.length > 0) msg += '\n\n⚠️ Errors (' + errors.length + '):\n' + errors.join('\n');
  SpreadsheetApp.getUi().alert(msg);
}

// ============================================
// CLEAR: Remove all slides except template
// ============================================

function clearGeneratedSlides() {
  if (SLIDES_ID === 'YOUR_SLIDES_ID_HERE') {
    SpreadsheetApp.getUi().alert('ERROR: Please set SLIDES_ID first.');
    return;
  }
  var presentation = SlidesApp.openById(SLIDES_ID);
  var slides = presentation.getSlides();
  var removed = 0;
  for (var i = slides.length - 1; i > 0; i--) {
    try { slides[i].remove(); removed++; } catch (e) {}
  }
  SpreadsheetApp.getUi().alert('Removed ' + removed + ' slide(s). Template preserved.');
}

// ============================================
// BONUS: Fetch CSV directly from Render exporter
// ============================================

var EXPORTER_URL = 'https://your-exporter.onrender.com/api/export-csv';

function fetchBreakDataFromExporter() {
  var payload = {
    base_url: 'https://ndc2025.calicotab.com',
    token: 'YOUR_API_TOKEN_HERE',
    slug: 'ndc2025',
    category_slug: 'open',
    debate_format: 'bp'
  };

  var options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  try {
    var response = UrlFetchApp.fetch(EXPORTER_URL, options);
    var csvText = response.getContentText();
    if (response.getResponseCode() !== 200) {
      SpreadsheetApp.getUi().alert('Exporter error: ' + csvText);
      return;
    }
    var rows = Utilities.parseCsv(csvText);
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
    sheet.clear();
    sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
    SpreadsheetApp.getUi().alert('Data imported! ' + (rows.length - 1) + ' team(s) loaded.');
  } catch (e) {
    SpreadsheetApp.getUi().alert('Error: ' + e.message);
  }
}

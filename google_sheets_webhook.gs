/**
 * Google Apps Script — Webhook d'enregistrement automatique des KPI Laser Quiz
 *
 * Instructions de déploiement (une seule fois, gratuit) :
 * 1. Ouvrir https://docs.google.com/spreadsheets/d/1ZJ-sDjjC74DrQmhNJDXOargA7OH-4HJJ2ecenpQX0sI/edit?gid=0#gid=0
 * 2. Menu : Extensions -> Apps Script
 * 3. Coller ce code dans Code.gs et enregistrer
 * 4. Cliquer sur "Déployer" -> "Nouveau déploiement"
 *    - Type : Application Web
 *    - Exécuter en tant que : Moi
 *    - Qui a accès : Tout le monde
 * 5. Copier l'URL /exec générée et la coller dans Render (variable GOOGLE_SHEETS_WEBHOOK_URL)
 *    ou dans la Régie TV de Laser Quiz (encart "Stats Google Drive").
 */

const SPREADSHEET_ID = "1ZJ-sDjjC74DrQmhNJDXOargA7OH-4HJJ2ecenpQX0sI";
const SHEET_GID = 0;
const HEADERS = [
  "Horodatage",
  "Adresse IP de la session",
  "Nombre de joueurs",
  "Nombre de questions répondues par joueur",
  "Temps de jeu par joueur",
  "Combien de partages de stats"
];

function _getTargetSheet() {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const sheets = ss.getSheets();
  for (let i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === SHEET_GID) {
      return sheets[i];
    }
  }
  return sheets[0];
}

function _ensureHeaders(sheet) {
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.getRange(1, 1, 1, HEADERS.length).setFontWeight("bold");
  }
}

function doGet() {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, service: "Laser Quiz Google Sheets Webhook" }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(10000);
    const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : "{}";
    const payload = JSON.parse(raw);
    const row = Array.isArray(payload.row) ? payload.row.slice(0, HEADERS.length) : [];
    while (row.length < HEADERS.length) row.push("");

    const sheet = _getTargetSheet();
    _ensureHeaders(sheet);

    const gameId = String(payload.game_id || "").trim();
    const props = PropertiesService.getScriptProperties();
    const propKey = gameId ? ("row_" + gameId) : "";
    let rowIndex = propKey ? Number(props.getProperty(propKey) || 0) : 0;

    if (rowIndex >= 2 && rowIndex <= sheet.getLastRow()) {
      sheet.getRange(rowIndex, 1, 1, HEADERS.length).setValues([row]);
    } else {
      sheet.appendRow(row);
      rowIndex = sheet.getLastRow();
      if (propKey) {
        props.setProperty(propKey, String(rowIndex));
      }
    }

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, row: rowIndex, game_id: gameId }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    try {
      lock.releaseLock();
    } catch (_) {}
  }
}

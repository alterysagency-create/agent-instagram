/**
 * Alterys Calendar relay (runs in Google's cloud, even when the PC is off).
 * Enable the Advanced Calendar service in Apps Script before running setup().
 * Store configuration in Project Settings > Script properties:
 *   BACKEND_URL, CALENDAR_ID, ACCOUNT_ID, INGEST_KEY, BOOKING_TITLE_MARKER
 * Optional: INSTAGRAM_FIELD_LABEL (default: "Votre @ Instagram")
 */

function setup() {
  const config = getConfig_();
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    if (trigger.getHandlerFunction() === 'syncCalendarChanges') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  ScriptApp.newTrigger('syncCalendarChanges')
    .forUserCalendar(config.calendarId)
    .onEventUpdated()
    .create();
  PropertiesService.getScriptProperties().deleteProperty('CALENDAR_SYNC_TOKEN');
  syncCalendarChanges();
}

function syncCalendarChanges() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return;
  try {
    syncCalendarChangesLocked_();
  } finally {
    lock.releaseLock();
  }
}

function syncCalendarChangesLocked_() {
  const config = getConfig_();
  const properties = PropertiesService.getScriptProperties();
  let syncToken = properties.getProperty('CALENDAR_SYNC_TOKEN');
  let pageToken;
  let newestSyncToken;
  try {
    do {
      const options = {showDeleted: true, singleEvents: true, maxResults: 2500};
      if (pageToken) options.pageToken = pageToken;
      if (syncToken) {
        options.syncToken = syncToken;
      } else {
        options.timeMin = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
      }
      const response = Calendar.Events.list(config.calendarId, options);
      (response.items || []).forEach(function (event) {
        if (isAlterysBooking_(event, config.titleMarker)) {
          sendEvent_(event, config);
        }
      });
      pageToken = response.nextPageToken;
      newestSyncToken = response.nextSyncToken || newestSyncToken;
    } while (pageToken);
  } catch (error) {
    if (syncToken && /sync token|410|Gone/i.test(String(error))) {
      properties.deleteProperty('CALENDAR_SYNC_TOKEN');
      syncCalendarChangesLocked_();
      return;
    }
    throw error;
  }
  if (newestSyncToken) properties.setProperty('CALENDAR_SYNC_TOKEN', newestSyncToken);
}

function getConfig_() {
  const p = PropertiesService.getScriptProperties().getProperties();
  const required = ['BACKEND_URL', 'CALENDAR_ID', 'ACCOUNT_ID', 'INGEST_KEY', 'BOOKING_TITLE_MARKER'];
  required.forEach(function (key) {
    if (!p[key]) throw new Error('Missing Script Property: ' + key);
  });
  return {
    backendUrl: p.BACKEND_URL.replace(/\/$/, ''),
    calendarId: p.CALENDAR_ID,
    accountId: p.ACCOUNT_ID,
    ingestKey: p.INGEST_KEY,
    titleMarker: p.BOOKING_TITLE_MARKER,
    usernameLabel: p.INSTAGRAM_FIELD_LABEL || 'Votre @ Instagram'
  };
}

function isAlterysBooking_(event, marker) {
  const searchable = [event.summary || '', event.description || ''].join('\n').toLowerCase();
  return searchable.indexOf(marker.toLowerCase()) !== -1;
}

function eventDate_(part) {
  return part && (part.dateTime || (part.date ? part.date + 'T00:00:00Z' : null));
}

function firstExternalAttendee_(event) {
  const attendees = event.attendees || [];
  return attendees.find(function (item) { return !item.self && !item.resource; }) || {};
}

function instagramUsername_(description, label) {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const labeled = new RegExp(escaped + '\\s*[:\\-]\\s*@?([A-Za-z0-9._]{1,30})', 'i')
    .exec(description || '');
  if (labeled) return labeled[1];
  const generic = /(?:instagram|insta)\s*[:\-]\s*@?([A-Za-z0-9._]{1,30})/i.exec(description || '');
  return generic ? generic[1] : null;
}

function meetingUrl_(event) {
  if (event.hangoutLink) return event.hangoutLink;
  const points = event.conferenceData && event.conferenceData.entryPoints || [];
  const video = points.find(function (point) { return point.entryPointType === 'video'; });
  return video && video.uri || null;
}

function sendEvent_(event, config) {
  const attendee = firstExternalAttendee_(event);
  const payload = {
    account_id: config.accountId,
    external_id: event.id,
    status: event.status || 'confirmed',
    title: event.summary || 'Appel découverte Alterys',
    start_at: eventDate_(event.start),
    end_at: eventDate_(event.end),
    timezone: event.start && event.start.timeZone || Session.getScriptTimeZone(),
    attendee_name: attendee.displayName || null,
    attendee_email: attendee.email || null,
    instagram_username: instagramUsername_(event.description, config.usernameLabel),
    meeting_url: meetingUrl_(event),
    event_url: event.htmlLink || null,
    source_updated_at: event.updated || null,
    event_payload: {
      creator_email: event.creator && event.creator.email || null,
      organizer_email: event.organizer && event.organizer.email || null,
      description: (event.description || '').slice(0, 5000)
    }
  };
  if (!payload.start_at || !payload.end_at) return;
  const response = UrlFetchApp.fetch(config.backendUrl + '/api/calendar/events', {
    method: 'post',
    contentType: 'application/json',
    headers: {Authorization: 'Bearer ' + config.ingestKey},
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  const status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    throw new Error('Backend rejected calendar event with HTTP ' + status);
  }
}

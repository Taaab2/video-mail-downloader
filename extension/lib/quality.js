/* אפשרויות האיכות. השמות זהים לשמות שהעובד (process_inbox.py) מכיר –
   התוסף שולח רק את השם, והמיפוי לפורמט של yt-dlp נמצא בצד העובד. */
(function (root) {
  'use strict';

  const QUALITIES = [
    { id: 'best', label: 'האיכות הכי טובה', hint: 'וידאו+אודיו, הקובץ הגדול ביותר' },
    { id: '1080p', label: '1080p', hint: 'עד Full HD' },
    { id: '720p', label: '720p', hint: 'עד HD' },
    { id: '480p', label: '480p', hint: 'קובץ קטן' },
    { id: '360p', label: '360p', hint: 'הקובץ הקטן ביותר' },
    { id: 'audio', label: 'אודיו בלבד (mp3)', hint: 'מתאים לשירים והרצאות' },
  ];

  function byId(id) {
    return QUALITIES.find((q) => q.id === id) || QUALITIES[0];
  }

  root.qualities = { QUALITIES, byId };
})(typeof self !== 'undefined' ? self : this);

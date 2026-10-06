/* פותח את לוח הבקרה כעמוד מלא בלשונית חדשה (במקום חלון קופץ קטן).
 * אין default_popup ב-manifest, ולכן הלחיצה על אייקון התוסף מגיעה לכאן. */

const CONTROL_PAGE = 'popup.html';

function openControlPage() {
  const url = chrome.runtime.getURL(CONTROL_PAGE);
  // אם העמוד כבר פתוח – מתמקדים בו במקום לפתוח עוד לשונית
  chrome.tabs.query({ url }, (tabs) => {
    if (tabs && tabs.length) {
      chrome.tabs.update(tabs[0].id, { active: true });
      if (tabs[0].windowId != null) {
        chrome.windows.update(tabs[0].windowId, { focused: true });
      }
      return;
    }
    chrome.tabs.create({ url });
  });
}

chrome.action.onClicked.addListener(openControlPage);

/* הגדרות מוטמעות בתוסף – הגרסה לדוגמה שנכנסת ל-Git.
   את הקובץ האמיתי config.js יוצר הפרויקט מקומית והוא לא נכנס ל-Git,
   כדי שהטוקן לא יפורסם במאגר ציבורי. */
window.EXT_CONFIG = {
  // טוקן GitHub (Fine-grained): Contents + Actions + Secrets + Variables = Read and write
  TOKEN: "",
  // המאגר שאליו נדחף הקוד, בצורה owner/repo
  REPO: "OWNER/video-mail-downloader",
  // שם קובץ ה-Workflow במאגר
  WORKFLOW: "downloader.yml",
  // שם הסקרוט ששומר את עוגיות יוטיוב
  COOKIES_SECRET: "YT_COOKIES_B64",
  // איכות ברירת מחדל: best | 1080p | 720p | 480p | 360p | audio
  DEFAULT_QUALITY: "best",
  // יעד העלאה: github | drive | both
  DEFAULT_TARGET: "github",
  // האם לקרוא עוגיות גם מ-google.com (לפעמים נדרש ליוטיוב)
  INCLUDE_GOOGLE_COOKIES: true
};

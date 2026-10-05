/* הצפנת sealed-box בדיוק כמו libsodium – כך אפשר לכתוב Secrets ל-GitHub מהדפדפן.
 *
 * מבנה ה"קופסה האטומה" של libsodium:
 *   [מפתח ציבורי זמני (32 בייט)] [ciphertext]
 *   כאשר ה-nonce נגזר כ- blake2b(ephemeral_pk || recipient_pk, 24 בייט).
 * לכן משתמשים ב-tweetnacl (crypto_box) + blake2b, בדיוק כמו crypto_box_seal.
 */
(function (root) {
  'use strict';

  const nacl = root.nacl;
  const blakejs = root.blakejs;

  function b64ToBytes(b64) {
    const clean = String(b64).replace(/[\r\n\s]/g, '');
    const bin = atob(clean);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64(bytes) {
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  /** חותם (seal) טקסט למפתח הציבורי של GitHub ומחזיר base64 מוכן ל-API. */
  function sealToBase64(text, publicKeyB64) {
    if (!nacl || !blakejs) throw new Error('ספריות ההצפנה לא נטענו');
    const recipientPk = b64ToBytes(publicKeyB64);
    if (recipientPk.length !== nacl.box.publicKeyLength) {
      throw new Error('מפתח ציבורי לא תקין מ-GitHub');
    }
    const payload = new TextEncoder().encode(text);
    const ephemeral = nacl.box.keyPair();

    const nonceInput = new Uint8Array(ephemeral.publicKey.length + recipientPk.length);
    nonceInput.set(ephemeral.publicKey, 0);
    nonceInput.set(recipientPk, ephemeral.publicKey.length);
    const nonce = blakejs.blake2b(nonceInput, null, nacl.box.nonceLength);

    const cipher = nacl.box(payload, nonce, recipientPk, ephemeral.secretKey);
    const sealed = new Uint8Array(ephemeral.publicKey.length + cipher.length);
    sealed.set(ephemeral.publicKey, 0);
    sealed.set(cipher, ephemeral.publicKey.length);
    return bytesToB64(sealed);
  }

  /** פותח קופסה אטומה (לבדיקות בלבד – GitHub לא מחזיר סודות). */
  function openFromBase64(sealedB64, publicKeyB64, secretKeyB64) {
    const sealed = b64ToBytes(sealedB64);
    const recipientPk = b64ToBytes(publicKeyB64);
    const secretKey = b64ToBytes(secretKeyB64);
    const ephemeralPk = sealed.subarray(0, nacl.box.publicKeyLength);
    const cipher = sealed.subarray(nacl.box.publicKeyLength);

    const nonceInput = new Uint8Array(ephemeralPk.length + recipientPk.length);
    nonceInput.set(ephemeralPk, 0);
    nonceInput.set(recipientPk, ephemeralPk.length);
    const nonce = blakejs.blake2b(nonceInput, null, nacl.box.nonceLength);

    const opened = nacl.box.open(cipher, nonce, ephemeralPk, secretKey);
    if (!opened) throw new Error('פענוח נכשל');
    return new TextDecoder().decode(opened);
  }

  root.sealedbox = { sealToBase64, openFromBase64, b64ToBytes, bytesToB64 };
})(typeof self !== 'undefined' ? self : this);

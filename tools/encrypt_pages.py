"""把要部署到 GitHub Pages 的 HTML 加密成「密碼鎖」頁。

為什麼不用單純的 JS 密碼檢查：Pages 是靜態站，原始 HTML 任何人都能直接下載。
只擋畫面不擋內容，等於沒鎖。這裡改成把整頁內容用 AES-256-GCM 加密，
瀏覽器輸入密碼後才在本機解密，伺服器上只有密文。

設計：
- 金鑰 = PBKDF2-SHA256(密碼, salt, 600k 次)。全站共用同一個 salt，
  所以解一次就能在各頁之間切換，不必每頁重打密碼。
- 每頁各自隨機 IV。GCM 驗證失敗 = 密碼錯誤。
- 瀏覽器只保存「推導後的金鑰」，不保存密碼本身。預設存 sessionStorage
  （關分頁即失效），勾選「記住此裝置」才存 localStorage。

用法（deploy workflow 內）：
    PAGE_PASSWORD=... python tools/encrypt_pages.py _site
"""
import base64
import glob
import json
import os
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
MIN_PASSWORD_LEN = 8

SHELL = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>🔒</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0f172a; color: #e2e8f0; font-family: system-ui, "Noto Sans TC", sans-serif; padding: 16px; box-sizing: border-box; }
  form { background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 24px 22px;
         width: 100%; max-width: 340px; box-sizing: border-box; }
  h1 { font-size: 1.15rem; margin: 0 0 14px; }
  input[type=password] { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
         border: 1px solid #475569; background: #0f172a; color: #e2e8f0; font-size: 1rem; }
  input[type=password]:focus { outline: 2px solid #60a5fa; border-color: transparent; }
  label { display: flex; gap: 6px; align-items: center; color: #94a3b8; font-size: .85rem; margin: 12px 0 14px; }
  button { width: 100%; padding: 10px; border: 0; border-radius: 8px; background: #2563eb; color: #fff;
         font-size: 1rem; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .6; cursor: wait; }
  .err { color: #f87171; font-size: .85rem; min-height: 1.2em; margin: 10px 0 0; }
</style>
</head>
<body>
<form id="f" autocomplete="on">
  <h1>🔒 請輸入密碼</h1>
  <input type="text" name="username" value="tst_wocker" autocomplete="username" hidden>
  <input id="pw" type="password" autocomplete="current-password" required autofocus>
  <label><input id="rm" type="checkbox"> 記住此裝置</label>
  <button id="go" type="submit">解鎖</button>
  <p class="err" id="err"></p>
</form>
<script>
(function () {
  var P = __PAYLOAD__;
  var KEY = "tstw_key_" + P.salt;
  var b64 = function (s) { return Uint8Array.from(atob(s), function (c) { return c.charCodeAt(0); }); };
  var get = function (st) { try { return window[st].getItem(KEY); } catch (e) { return null; } };
  var put = function (st, v) { try { window[st].setItem(KEY, v); } catch (e) {} };
  var del = function () { ["sessionStorage", "localStorage"].forEach(function (st) {
    try { window[st].removeItem(KEY); } catch (e) {} }); };

  function show(html) {
    // 解鎖後加一顆「鎖定」鈕：清掉本機金鑰並回到密碼頁
    var btn = '<button id="__tstw_lock" title="鎖定" style="position:fixed;right:12px;bottom:12px;z-index:99999;' +
      'width:auto;padding:6px 10px;border:1px solid #475569;border-radius:999px;background:#1e293b;' +
      'color:#e2e8f0;font-size:.85rem;cursor:pointer" onclick="(function(){[\\'sessionStorage\\',\\'localStorage\\']' +
      '.forEach(function(s){try{window[s].removeItem(\\'' + KEY + '\\')}catch(e){}});location.reload()})()">🔒</button>';
    var i = html.lastIndexOf("</body>");
    html = i >= 0 ? html.slice(0, i) + btn + html.slice(i) : html + btn;
    document.open(); document.write(html); document.close();
  }
  async function decryptWith(rawKey) {
    var key = await crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["decrypt"]);
    var pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(P.iv) }, key, b64(P.ct));
    return new TextDecoder().decode(pt);
  }
  async function derive(pw) {
    var base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveBits"]);
    var bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: b64(P.salt), iterations: P.it }, base, 256);
    return new Uint8Array(bits);
  }
  var enc = function (u8) { return btoa(String.fromCharCode.apply(null, u8)); };

  // 外殼頁還在解析時呼叫 document.open 會被瀏覽器忽略，內容變成附加而非取代，
  // 所以自動解鎖要等外殼 load 完成後才寫入
  function whenLoaded(fn) {
    if (document.readyState === "complete") { fn(); } else { window.addEventListener("load", fn, { once: true }); }
  }
  var saved = get("sessionStorage") || get("localStorage");
  if (saved) {
    decryptWith(b64(saved)).then(function (html) { whenLoaded(function () { show(html); }); })
      .catch(function () { del(); });
  }
  document.getElementById("f").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var go = document.getElementById("go"), err = document.getElementById("err");
    if (!window.crypto || !crypto.subtle) { err.textContent = "此瀏覽器不支援解密（需 HTTPS）。"; return; }
    go.disabled = true; err.textContent = "";
    try {
      var raw = await derive(document.getElementById("pw").value);
      var html = await decryptWith(raw);
      put(document.getElementById("rm").checked ? "localStorage" : "sessionStorage", enc(raw));
      whenLoaded(function () { show(html); });
    } catch (e) {
      err.textContent = "密碼錯誤";
      go.disabled = false;
    }
  });
})();
</script>
</body>
</html>
"""


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def encrypt_html(html: str, key: bytes, salt: bytes) -> str:
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, html.encode("utf-8"), None)
    payload = {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
        "it": ITERATIONS,
    }
    return SHELL.replace("__PAYLOAD__", json.dumps(payload))


def main(site_dir: str) -> int:
    password = os.environ.get("PAGE_PASSWORD", "")
    if len(password) < MIN_PASSWORD_LEN:
        # 沒設密碼就不部署：寧可網站停在舊版，也不要把明文頁推上去
        msg = (f"PAGE_PASSWORD 未設定或少於 {MIN_PASSWORD_LEN} 字元。"
               "請到 repo Settings → Secrets and variables → Actions 新增 PAGE_PASSWORD。")
        print(f"::error::{msg}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"❌ {msg}")
        return 1
    salt = os.urandom(16)
    key = derive_key(password, salt)
    files = sorted(glob.glob(os.path.join(site_dir, "*.html")))
    if not files:
        print(f"❌ {site_dir} 內沒有 HTML")
        return 1
    for path in files:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(encrypt_html(html, key, salt))
        print(f"🔒 {os.path.basename(path)}")
    # 擋搜尋引擎收錄（加密頁本身也有 noindex）
    with open(os.path.join(site_dir, "robots.txt"), "w") as f:
        f.write("User-agent: *\nDisallow: /\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_site"))

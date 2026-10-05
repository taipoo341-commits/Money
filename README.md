# 加班費計算系統

目前正式程式放在專案根目錄，入口是 `index.html`。2026-10-05 依使用者指定，以已上傳 GitHub 並經使用者確認介面、運算及停止辦公官方查詢正常的 `Money-main/` 為基準，同步全部 15 個正式檔案。

`Money-main/` 保留為此次已驗證的來源副本；正式維護及上傳使用根目錄的檔案。過時版本、整理前備份及歷次測試資料集中於 `可刪除/`。

## 正式檔案

請保留相對路徑，`.github`、`data`、`scripts` 不要攤平。

```text
.github/workflows/update_dgpa.yml
data/dgpa_closures.json
scripts/update_dgpa.py
icon-144.png
icon-192.png
icon-512.png
icon-maskable-192.png
icon-maskable-512.png
icon.png
index.html
manifest.json
privacy.html
RemachineScript_Personal_Use.ttf
sw.js
terms.html
```

以上 15 個檔案與 `Money-main/` 的正式檔案逐一 SHA-256 相同。另保留根目錄的 `README.md` 與 `.gitignore` 作為專案說明及排除規則，共 17 個專案檔案。

## 網站與停止辦公資料

- Manifest ID 為 `/Money/`，啟動網址 `./index.html`，範圍 `./`；沿用來源版本的設定。
- Worker 快取版本沿用 v45，依部署路徑區分快取；圖示與 Manifest 的查詢參數沿用 v43。
- 停止辦公資料使用 `data/dgpa_closures.json`，來源快照的 `updatedAt` 是 `2026-10-05T00:16:38+08:00`。
- 官方查詢讀取同源資料檔；GitHub Actions 透過 `.github/workflows/update_dgpa.yml` 執行 `scripts/update_dgpa.py` 更新公開資料。兩個檔案都保留來源內容。

GitHub Actions 可能持續更新線上 JSON。日後上傳程式時，請先比對資料日期，避免用本機較舊的停止辦公資料覆蓋線上較新版本。

## 本機資料與封存

- `Money-main/`：保留的已驗證來源副本，不重複上傳。
- `可刪除/`：歷史版本、整理前備份及測試資料，不屬於目前網站執行檔。
- Python 的 `__pycache__/` 與 `.pyc` 是產生檔，來源副本中的快取已移入封存區。
- `.gitignore` 排除上述本機副本、封存區及 Python 快取。

最新整理紀錄在 `可刪除/整理封存_20261005/`，包含整理前根目錄的 16 個檔案、舊 `Money3-main` 的 14 個檔案、1 個 Python 快取，以及來源／封存／正式檔案雜湊及驗證結果。已存在的歷史封存資料保留在原位置。

本次整理沒有重新部署網站，也沒有存取或清除真實瀏覽器的排班、請假及薪給紀錄。線上功能正常是使用者已確認的狀態；本次另外檢查本機檔案完整性、語法及網站載入。

`可刪除 於2026/10/5 18:06刪除

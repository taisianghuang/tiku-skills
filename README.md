# tiku-skills：考古題題庫的 AI 技能

讓 Claude、Codex 等 AI agent 連上 [考古題題庫](https://tiku.ptsfu.org)（資工所入學考古題），**用你自己的樣式產生 PDF 題本**。安裝後只要說：

> 出 10 題台清交成、民國 105 年以後的 page replacement 題本，每題留 6 行作答空間

agent 會自己找單元、組題、下載原圖、排版成 PDF。

## 安裝

### Claude Code（推薦：一次裝好 MCP 和 skill）
```
/plugin marketplace add taisianghuang/tiku-skills
/plugin install tiku-workbook@tiku
```
plugin 內含題庫 MCP 連線（`https://tiku.ptsfu.org/mcp`），不用另外設定。

### Claude 網頁版／桌面版
1. 下載 [`dist/tiku-workbook.zip`](dist/tiku-workbook.zip)，到 Settings → Capabilities → Skills 上傳。
2. 另外加入題庫 MCP：Settings → Connectors → Add custom connector，URL 填 `https://tiku.ptsfu.org/mcp`，不需登入。
3. 網頁版的程式環境可能無法連外網下載題目圖片；這時 agent 會改給你網站題組連結，到網站按「產生 PDF 題本」即可。

### Codex
1. 把 `plugins/tiku-workbook/skills/tiku-workbook/` 整個資料夾複製到 `~/.agents/skills/tiku-workbook/`（或專案的 `.agents/skills/`），重開 Codex。
2. 加入 MCP：`codex mcp add tiku --url https://tiku.ptsfu.org/mcp`，或依 [接入教學](https://tiku.ptsfu.org/ai.html#codex) 設定。

### 其他 agent
skill 是標準的 `SKILL.md` 格式，把同一個資料夾放進你的 agent 讀 skill 的地方，再連上 `https://tiku.ptsfu.org/mcp` 即可。

## 需要什麼
- 能執行 shell、能連外網的 agent 環境。
- [uv](https://docs.astral.sh/uv/)（建議）：參考程式用 `uv run` 執行，PyMuPDF 和 Pillow 會自動安裝。沒有 uv 也可以 `pip install pymupdf pillow`。

## 內容
```
.claude-plugin/marketplace.json        Claude Code marketplace
plugins/tiku-workbook/
  .claude-plugin/plugin.json
  .mcp.json                            題庫 MCP 連線
  skills/tiku-workbook/
    SKILL.md                           流程說明（agent 讀這個）
    scripts/render_workbook.py         manifest＋樣式檔 → PDF
    styles/style.example.json          一題一頁
    styles/style.compact.json          連續排版＋作答橫線
dist/tiku-workbook.zip                 給 Claude 網頁版上傳用
```

## 自訂樣式
複製 `styles/style.example.json` 改成你要的樣子：封面、標題顏色、一題一頁或連續排版、作答橫線、頁尾都能改。也可以直接跟 agent 說「改成每題留 8 行、不要封面」，它會幫你改。

## 授權與題目來源
- 本 repo 的程式與文件以 MIT 授權。
- 題目取自各校公開發布之研究所入學考試試題，著作權屬原命題單位，僅供個人學習研究使用。題目圖片為程式自動裁切，請以各校公布之原始試題為準。
- 問題回報：contact@ptsfu.org

[English](README.md) | 繁體中文

# Claude Fleet

當你同時開著 5–7 個 Claude Code 視窗在 vibe coding，你需要一個地方一眼看完每個視窗在幹嘛——誰卡住了、誰在等你、誰做完了，以及你的用量額度還剩多少。

![](docs/screenshot-hero.png)

> 一個並行管理多個 Claude Code（與 Codex）session 的儀表板。
> 建構在 [tianyilt/claude-fleet](https://github.com/tianyilt/claude-fleet) 之上——
> 詳見 [致謝](#致謝)。本 fork 新增了即時方案用量、每張卡片的 model/token 資訊、
> PACE/Slurm GPU 佇列監控，以及更精準的 triage 判定。

## 30 秒啟動

```bash
git clone https://github.com/hungchun0201/claude-fleet
cd claude-fleet && bash run.sh
# 瀏覽器開 http://127.0.0.1:7878
```

第一次執行會自動建立 venv 並安裝相依套件，不用額外設定。用 `CLAUDE_FLEET_PORT` 可改 port。

後端只**讀取** `~/.claude/` 與 `~/.codex/`，從不修改任何 agent 狀態。每個人執行只會看到自己的 session。

## 它解決什麼

多視窗 vibe coding 的日常痛點：

- **授權提示一閃就錯過** → 頂端常駐紅條，點一下跳回那個終端機。
- **不知道每個視窗在做什麼** → 每張卡片顯示當前任務、triage 狀態、model 與背景工作。
- **不知道離用量上限還有多遠** → navbar 顯示你真實的 5 小時 session % 與每週全模型 %（含重置倒數）。
- **做完的視窗一直開著沒關** → patrol 引擎標成 `closeable`，一鍵關閉。
- **看起來在「忙」其實卡住了** → 模型被停用、或殘留的死 shell 會被獨立標示，不會偽裝成工作中。
- **找不到上週那個 session** → 全文搜尋 ~50ms 回應，附 VS Code 風格的命中片段。

## 核心功能

### Triage 分類

不是單純的 busy/idle。patrol 引擎讀每個 transcript 的 `stop_reason`、`queue-operation` 事件、API 錯誤列與背景任務狀態：

| 狀態 | 意思 | 怎麼判定 |
|------|------|----------|
| 🟢 working | 工作中 | busy，或有實際背景工作（bg Bash / Monitor / Workflow / Codex 審查 / 等 GPU）|
| 🔴 waiting | 等你處理 | 授權提示／對話框開著 |
| 🟡 stalled | 卡住、需要你 | stop_reason=tool_use 且閒置 > 5 分；Codex/Workflow 卡死、喚醒過期；**或選用的模型中途被停用**（執行 `/model` 復原）|
| 🔵 completed | 做完了 | stop_reason=end_turn 且閒置 > 5 分 |
| ⚪ closeable | 可安心關閉 | completed 且閒置 > 1 小時 |

分類是**結構化**的——配對 tool_use/tool_result 與 task 通知，而非用關鍵字比對文字，所以只是「提到」背景工作的 session 不會被誤判成 working，完成的任務也會自動清除。它追蹤：

- **背景任務** — `Bash run_in_background` / `Monitor persistent` / `Workflow`，從 spawn-ack → task-notification 的完整生命週期。turn 在它們執行中結束時維持 `working` 而非 `completed`。
- **Workflow 執行** — ⚙️ 徽章顯示即時 agent 進度（從 run 的 journal 讀 done/started）；超過 15 分鐘無輸出標為卡死。
- **模型被停用的 turn** — 當 turn 因選用模型被撤銷而中斷（合成的 API 錯誤列），卡片顯示 `stalled` 並提示 `/model`，而非誤判成「工作中」。
- **殘留的背景 shell** — turn 結束但仍有背景 shell 活著（忘了關的 server、裸 `&`）時，卡片不再強制「工作中」：它會 completed，並顯示 🐚 徽章 + **那個 shell 實際在跑什麼**（從行程樹即時讀取），讓死 shell 不會偽裝成工作。
- **等 GPU（Slurm / PACE）** — sleep 在 ScheduleWakeup 或跑 queue 輪詢的 session 會有 ⏳ 徽章與下次喚醒時間；儀表板自己透過 SSH 跑 `sacct`/`squeue`，把最新 job 狀態顯示在卡片上。需要真正的 Slurm/GPU 關鍵字（`squeue`、job id、H100/L40S…），單純 hostname 不算。
- **Codex 審查** — 🔍 徽章標示 `codex exec` 子程序與進行中的 MCP 呼叫，含卡死偵測（靜默或缺 rollout）與可選的一次性 [ntfy](https://ntfy.sh) 推播。設 `CLAUDE_FLEET_NTFY_TOPIC` 或把 topic 寫進 `~/.config/claude-fleet/ntfy-topic`；不設 = 關閉告警（topic 形同密碼，因此沒有預設值）。

### 方案與 token 用量

navbar 鏡像 **Settings → Usage**，用你帳號的真實數字——唯讀地從 `/api/oauth/usage` 取得，使用 Claude Code 存在你 keychain 的 OAuth token：

- **`5h <tokens> / <pct>%`** — 目前 5 小時 session：本機 billable token 估算 + 帳號真實使用率 %，附 `reset in 3hr Xmin` 倒數。
- **`week <pct>%`** — 每週全模型上限。

即時取用跑在慢速背景 poller（約每 5 分鐘）所以不會卡住畫面；端點不可用時退回本機 token 估算；可用 `CLAUDE_FLEET_PLAN_USAGE=0` 關閉。每張卡片也顯示該 session 的 **model** 與**目前 context window 的 token 數**。

> 該用量端點未公開且需 OAuth；這只是用**你自己的** token 對**你自己的**帳號做唯讀查詢。不想用就關掉。

### 搜尋

對所有 Claude + Codex transcript 跑 ripgrep，~50ms。不只搜標題——搜「hailuo」能找到對話中提過 Hailuo 的 session，即使標題是別的。每筆結果附最多 3 段命中片段，一眼看出為何命中。

![](docs/screenshot-search.png)

### Skill / Memory 追蹤

skill 面板回報三個維度——正式的 `/skill` 呼叫、skill 檔的讀寫、以及 Bash 對 `skills/` 的引用：

```
paper2video        333   1 invoke · ↓122 reads · ↑53 writes · 157 bash
feishu-notify       45  24 invokes · ↓7 reads · ↑7 writes · 7 bash
```

memory 面板依類型（user / feedback / project / reference）分組，每筆顯示 `↓3 ↑2`（被 3 個 session 讀、被 2 個改）。

![](docs/screenshot-skills.png)
![](docs/screenshot-memory.png)

### Timeline + plan 歷史

打開任一 session 看完整對話流——skill 呼叫紫色、memory 讀取藍色虛線、memory 寫入粉紅——以及該 session 的 plan 版本歷史（每次 Write 是完整快照，每次 Edit 是紅綠 diff）。

![](docs/screenshot-timeline.png)

### 動作

| 按鈕 | 作用 |
|------|------|
| Focus | 跳到那個終端機分頁 |
| Fork | `claude --resume <sid> --fork-session`——新 session 繼承歷史 |
| Resume | `claude --resume <sid>`——接續原 session |
| Review | 背景跑 `claude -p` 審查；結論（PASS/FAIL/PARTIAL）顯示在卡片上 |
| Close | SIGTERM |
| Export | 匯出對話文件（timeline + plan 歷史 + skill/memory 摘要）|

> **Focus 設定（macOS）。** Terminal.app 與 iTerm2 開箱即用——包含在 **tmux** 裡跑的 session（內建的 [`scripts/focus-tty.sh`](scripts/focus-tty.sh) 把行程 tty → 所屬分頁 → 提到前景）。放一個可執行的 `~/.claude/focus-tty.sh`（接 `<tty>` 參數）即可自訂。

## 隱私

設計上可以安心分享與截圖：

- **唯讀**：後端從不寫入 `~/.claude` / `~/.codex`。
- **不洩漏路徑**：UI 把家目錄路徑顯示成 `~/…`，截圖不會露出你的 username。
- **不內建密鑰**：ntfy topic 與方案用量都在 runtime 從你自己的 env/keychain 解析，不會 commit 任何個人資訊。

## 架構

單檔前端（Alpine.js + Tailwind 走 CDN，無 npm）。FastAPI 後端，每 2 秒透過 SSE 推送。

```
app.py                FastAPI + SSE；GPU 佇列與方案用量 poller
core/
  sessions.py         讀 sessions/*.json，對應 TTY
  transcripts.py      解析 JSONL；抽取 skill/memory/plan/背景任務/用量
  patrol.py           triage 分類引擎
  usage.py            本機 billable-token 聚合（5h 窗口）
  plan_usage.py       透過 /api/oauth/usage 取真實帳號上限（唯讀、快取）
  shells.py           即時背景 shell 檢視（行程樹）
  codex.py            Codex session 解析 + 審查偵測
  search.py           跨平台 ripgrep 搜尋
  actions.py          focus / fork / review / close / export
  history.py          統一索引 + 全文 rg 搜尋
  skills.py / memory.py / plans.py / perms.py / alerts.py
static/index.html     單檔 SPA
```

## 致謝

原作由 **[tianyilt](https://github.com/tianyilt)** 創建，即 [**tianyilt/claude-fleet**](https://github.com/tianyilt/claude-fleet)——triage 引擎、搜尋、skill/memory 追蹤，以及整個單檔架構都源自於此。本 repo 是在其基礎上的 fork，新增了即時方案用量、每張卡片的 model/token 資訊、PACE/Slurm GPU 佇列監控、模型錯誤 & 殘留 shell 的 triage，以及路徑遮蔽。原始設計的功勞全歸 tianyilt。

上游也致謝：

- [HarnessKit](https://github.com/RealZST/HarnessKit) — 跨平台 skill 管理的 UI 參考
- [Synergy](https://github.com/SII-Holos/synergy) — memory-engram 分類視圖的靈感來源

## 授權

[MIT](LICENSE)

# 第一次用真實錄音測試

呢份指南係俾第一次使用 Brain 嘅人。先用 **5–10 個複製出嚟嘅音頻檔**
試完整流程，確認效果後先放大量錄音。

## 開始之前

1. 開 Terminal，進入 project folder：

   ```sh
   cd /Users/harry/Projects/personal/transcript-workflow
   ```

2. 確保 oMLX 已啟動，而且 `config/config.yaml` 已填好 summary model。

3. 建立／更新資料庫 schema（**必須先做**；程式唔會自動 migrate）：

   ```sh
   uv run python src/manage.py migrate
   ```

   如果有 migration 未 apply，`brain doctor` 會 FAIL，所有 ORM 指令
   (`brain run`、`brain status`、`brain serve` 等) 會 exit 1 並提示
   以上指令。

4. 檢查環境：

   ```sh
   uv run brain doctor
   ```

   `FAIL` 要先處理；MacWhisper、oMLX 或 model 相關 `WARN` 亦應先檢查。

> 安全提示：現階段程式只會讀取音頻檔，唔會自動移動或刪除佢。
> 測試時請將錄音 **copy** 入 inbox，唔好將唯一一份原檔 move 入去。
> Step 6 嘅「處理成功 N 日後刪除音頻」功能尚未實作。

## 1. 揀一小批測試錄音

最好包括幾種情況：

- 廣東話（包括廣東話＋英文）
- 普通話
- 英文
- Finnish＋English
- 一段較長或背景較嘈嘅錄音

用 Finder 將副本放入：

```text
data/inbox/
```

支援 `.wav`、`.mp3` 同 `.m4a`，副檔名大小寫皆可。檔名可以繼續用
錄音日期／時間，毋須改名。其他格式會安全略過。

## 2. 執行一次完整流程

```sh
uv run brain run
```

新檔案要保持不變一段時間（預設 30 秒）先會處理，所以第一次可能只見到
`skipped_unstable`。等 30 秒後再執行一次：

```sh
uv run brain run
```

一次 `brain run` 會依序做：

```text
收錄檔案 → 建議語言路線 → MacWhisper transcription → oMLX summary/tag
```

高信心 routing 可以自動繼續，但仍會標示為未經人手確認；語言不明確嘅錄音
會停喺 Needs Review，唔會亂揀 model。另外，當 oMLX classifier 無效或
不能連線時，如果 deterministic 證據非常強（廣東話／普通話 marker 分數、
CJK 比例、覆蓋度全部過晒嚴格門檻），系統會用保守 heuristic gate 自動揀
profile——呢啲分數係 heuristic 證據，唔係校準過嘅概率。如果
`auto_transcribe: false`，呢啲結果同樣會停喺 Needs Review。

## 3. 開啟網頁介面

開另一個 Terminal 視窗並執行：

```sh
cd /Users/harry/Projects/personal/transcript-workflow
uv run brain serve
```

然後用瀏覽器開：

```text
http://127.0.0.1:8787
```

主要頁面：

- **Recordings**：按日期睇錄音，進入後可睇 summary、transcript 及 history。
- **Review**：集中處理語言不明、失敗或等待操作嘅錄音。
- **Tags**：查看目前可用及已 retired 嘅 tags。

停止 server：返去該 Terminal 按 `Control-C`。

## 4. Routing 要點處理

進入 **Review**，再點入一段 recording：

- 建議正確：確認目前 routing。
- 建議錯誤或未能決定：手動選 profile，再執行 transcription。

目前 profiles：

| 錄音內容 | Profile | MacWhisper model |
| --- | --- | --- |
| 廣東話、廣東話＋英文 | `cantonese` | `apple:zh-HK` |
| 普通話 | `mandarin` | `apple:zh-CN` |
| 英文、Finnish、Finnish＋English | `european` | Parakeet v3 |

Auto detector 目前只係 routing 建議，未經足夠真實錄音 evaluation，唔應視為已證明準確。

如果想用 CLI 手動處理，可先從網頁或以下命令取得 recording ID：

```sh
uv run brain review --json
```

然後：

```sh
# 確認現有建議；不會重新 transcription
uv run brain route RECORDING_ID --confirm

# 改用指定 profile，並立即 transcription
uv run brain route RECORDING_ID --profile cantonese --transcribe-now
```

## 5. 檢查結果

每段錄音至少檢查：

1. Routing profile 是否正確。
2. Transcript 有冇漏字、錯語言或明顯亂碼。
3. Summary 有冇忠於原文，長錄音有冇漏掉後半段。
4. Suggested tags 是否合理。
5. 手動新增、確認或移除 tag 後，頁面結果是否正確。

Summary 同 transcript 可以喺網頁下載，亦可用 CLI 複製：

```sh
uv run brain summary RECORDING_ID --format markdown
uv run brain summary RECORDING_ID --format text
uv run brain transcripts RECORDING_ID
```

Markdown/text 係正常可讀格式，方便直接 copy 去其他 LLM；JSON 只係內部結構及需要機器處理時使用。

## 6. 遇到失敗

先查看：

```sh
uv run brain status --json
uv run brain review --json
```

`brain run` **唔會不停自動重試失敗項目**。修正原因後，可以喺 recording
頁面按 Retry，或者執行：

```sh
uv run brain retry RECORDING_ID
```

如果只係想重新生成已有 summary：

```sh
uv run brain summarize RECORDING_ID --regenerate
```

失敗嘅 retranscription 或 re-summarization 唔會刪除之前成功嘅版本。

Transcription 失敗時，attempt history 會顯示真正嘅 MacWhisper 錯誤
（已去除檔案路徑、長度受限），而唔係「Transcribing ...」進度行。常見
情況：`apple:zh-HK`／`apple:zh-CN` 唔支援 `--speakers`（speaker
detection）。如果要保留 speaker labels，請改用支援 diarization 嘅
model；如果唔需要，可以喺 `config.yaml` 設
`macwhisper.speakers_fallback: true`（預設關閉），失敗後會自動用
`--no-speakers` 重試一次，並喺結果中明確報告「冇 speaker labels」。
MP3／M4A 會先自動轉做暫存 PCM WAV 先交俾 MacWhisper；原檔只會被讀取。

## 7. 原音頻可以幾時刪？

現階段 Brain **不會自動刪除原音頻**。你可以人手刪除已處理錄音；database、
transcript、summary 同 tags 會保留，而 recording 會顯示 audio missing。

不過第一次 evaluation 建議暫時保留測試音頻，直至你確認 transcript 及 summary
滿意，方便用另一個 model 重做或比較。正式嘅 N 日保留及安全自動清理會喺 Step 6
處理。

## 建議記錄測試結果

可為每段錄音簡單記低：

```text
檔名／日期：
實際語言：
系統建議 profile：
最後確認 profile：
Transcript：好／可接受／差
Summary：好／可接受／差
備註：
```

呢批人手結果之後可以用嚟評估 Cantonese／Mandarin auto-routing，而唔係只憑感覺調 threshold。

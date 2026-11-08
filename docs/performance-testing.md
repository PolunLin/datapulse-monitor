# 效能與可用性測試

本測試測量單一 FastAPI worker 的 REST、WebSocket、批次入庫與故障恢復。WebSocket 使用 `WS_MAX_CONNECTIONS=0`，持續接收全部三台設備的事件，不以閒置 TCP 連線數當作服務容量。

## 隔離與測試條件

| 項目 | 設定 |
| --- | --- |
| Compose | 僅使用 `compose.performance.yaml`，不合併開發設定 |
| 專案與儲存 | 每輪隨機 `datapulse-perf-*` 名稱、專用 DB／volume／network |
| 後端 | 1 worker、2 CPU、2 GiB RAM |
| MariaDB | 11.7.2、2 CPU、2 GiB RAM；不發布 DB 連接埠 |
| DB 連線池 | 原設定 5 個常駐、最多另開 5 個 |
| WebSocket | 不限制應用連線總數；單連線 queue 100、發送逾時 2 秒 |
| 資料 | 3 台設備各每秒一筆；每筆推送前查 DB 驗證帳號 |
| Token | 測試環境設 60 分鐘，避免測試期間正常過期干擾結果 |
| 日誌 | 測試環境 WARNING；正式 INFO 配置的效能需另量測 |
| 負載端 | 主機 Python asyncio，與 Docker Desktop 共用實體主機 |

負載端會記錄 CPU、事件迴圈延遲與檔案描述符限制。若負載端先飽和，只能得到受測容量的下界，不能認定為伺服器極限。這些數字也不代表同數量的完整 Streamlit 使用者，未涵蓋瀏覽器渲染、圖表與所有頁面操作。

## 執行

使用已安裝 backend 開發依賴的 Python。以下指令都從專案根目錄執行，`RUN_DIR` 每輪必須使用新目錄。

```bash
RUN_DIR=artifacts/performance/my-run
backend/.venv/bin/python scripts/run_performance.py setup --run-dir "$RUN_DIR" --port 19110
backend/.venv/bin/python scripts/run_performance.py monitor --run-dir "$RUN_DIR" --duration 2400
```

資源監測指令會持續執行；在另一個終端依序執行測試，不要讓容量與故障測試互相干擾：

```bash
backend/.venv/bin/python scripts/run_performance.py capacity --run-dir "$RUN_DIR" --stages 100,200,400,800,1200,1600,2400,3200 --hold-seconds 45
mv "$RUN_DIR/websocket-capacity.json" "$RUN_DIR/websocket-capacity-all-devices.json"
backend/.venv/bin/python scripts/run_performance.py capacity --run-dir "$RUN_DIR" --stages 250,350,500 --hold-seconds 180 --device-id device-001
mv "$RUN_DIR/websocket-capacity.json" "$RUN_DIR/websocket-capacity-device.json"
backend/.venv/bin/python scripts/run_performance.py http --run-dir "$RUN_DIR" --stages 10,25,50,100,200,400 --hold-seconds 30
backend/.venv/bin/python scripts/run_performance.py availability --run-dir "$RUN_DIR"
backend/.venv/bin/python scripts/run_performance.py inspect --run-dir "$RUN_DIR"
```

若所有階段都通過，繼續增加連線數。首次失敗後在前後區間細分，再對候選穩定值延長持續時間。重跑前先更名保存 `websocket-capacity.json`，避免覆蓋原始測量。若測到 API／資料推送嚴重失敗，工具會停止增加負載並關閉已建立的連線。

每輪記錄後端是否剛重啟，以及是否曾過載。比較負載形狀時，可在各自獨立服務的相同啟動狀態下重測；過載後的恢復測試則應保留該狀態並另外標註。短測通過後仍須做持續觀察，本次即發現單設備 1,000 條短測通過，但延長測試大量斷線。

```bash
backend/.venv/bin/python scripts/summarize_performance.py --run-dir "$RUN_DIR"
backend/.venv/bin/python scripts/run_performance.py cleanup --run-dir "$RUN_DIR"
```

清理只針對該輪專案，移除專用容器、volume、network 與產生的憑證；不清理全域 image。`artifacts/` 被 Git 忽略。`private.json`、`private.env`、`credentials.json` 為權限 `0600` 的機密，不可分享；清理後只保留公開報告。

## 容量判讀

| 指標 | 短測暫定穩定條件 |
| --- | --- |
| 連線 | 階段結束活躍連線至少達目標的 99% |
| 新鮮度 | 至少 99% 的目標連線在最近 5 秒收到事件 |
| 事件延遲 | 事件產生到收到的 p95 小於 2 秒 |
| 共存 API | live、ready、帶認證 sample-feed 各成功率至少 99%、p95 小於 1 秒 |
| 負載端 | 事件迴圈排程延遲 p95 小於 100 ms |

WebSocket 階段另外送出合計 5 RPS 的 API 請求，三個端點輪流分配。REST 專測則只對帶認證的 sample-feed 每次排程發出一個請求，記錄目標 RPS、實際成功 RPS、未能發出的排程與延遲。延遲與 HTTP 成功率門檻屬本輪暫定判準，不能直接當作每月可用性承諾。

每階段延遲使用有界抽樣，百分位為估算值；完整收件總數及連線數另外累計。時間戳延遲依賴客戶端與伺服器時鐘同步。

## 故障恢復

| 情境 | 注入方式 | 觀察 |
| --- | --- | --- |
| WebSocket 開關 | 關閉 20 秒再開啟 | REST 持續成功、WS 重連後收到新事件 |
| 正常後端重啟 | Compose restart | 正常關閉排空、REST 與 WS 恢復 |
| DB 暫停 | pause 30 秒後 unpause | ready／業務查詢失敗、解除後恢復、writer 清除積壓 |
| 後端崩潰 | 在專用容器內 SIGKILL Uvicorn | restart policy 真的生效、REST 與 WS 恢復、pending 資料是否遺失 |

觀察器以每秒一次的頻率分別探測三個 HTTP 端點，另維持 10 條退避重連的 WebSocket。HTTP 恢復以連續三次成功確認，WS 以重新收到事件確認。報告分開列出故障造成的觀測中斷時間，以及故障解除後的恢復時間；一秒探測可能漏掉極短暫的中斷。

既有已提交事件與尚未提交的 `pending` 事件分別核對。資料核對只針對抽樣 event IDs，不宣稱完整稽核或端到端零遺失。此版本記憶體佇列在非正常終止時可能遺失 pending 事件。單後端與單 DB 的測試結果屬故障恢復能力，不能解讀為無停機高可用性。

本輪實際結果見同目錄的 `performance-results-2026-11-08.md`。

# WebSocket 協定

第二階段提供 `/ws/v1/samples`，與 REST API 共用 FastAPI 服務及 JWT 認證。Compose 預設位址為 `ws://localhost:8010/ws/v1/samples`。

## 連線與認證

請先呼叫 `/api/v1/auth/login` 取得 JWT，再使用 `Authorization: Bearer <token>` header 開啟 WebSocket。JWT 不接受放在 query string，避免出現在網址、代理與存取日誌。

`admin`、`user`、`viewer` 都能接收公用設備監測事件。可用 `?device_id=device-001&category=temperature` 篩選設備及分類，省略代表不限制該條件。設備 ID 僅允許英數字、底線與連字號，最長 64 字元。無效篩選、憑證或不合法的握手會被拒絕。設備生成器不依賴訂閱者：預設 3 台設備每台每秒一筆，沒有人連線仍持續批次保存。

瀏覽器原生 WebSocket API 無法任意設定 Authorization header；Python CLI 與第三階段的 Streamlit 伺服器端接收器都使用此 header。Streamlit 為每個登入 Session 維護獨立接收器，瀏覽器不直接持有後端 WebSocket 連線。Swagger 的 HTTP 測試介面不會直接建立此 WebSocket。

## 獨立控制 WebSocket

管理員可在服務執行中獨立開啟或關閉 WebSocket，不需重啟 FastAPI：

| 操作 | 專案根目錄指令 | 管理 API |
| --- | --- | --- |
| 開啟 | `uv run --project backend python scripts/websocket_control.py start` | `PUT /api/v1/monitoring/websocket`，JSON `{"enabled": true}` |
| 關閉 | `uv run --project backend python scripts/websocket_control.py stop` | `PUT /api/v1/monitoring/websocket`，JSON `{"enabled": false}` |
| 查詢 | `uv run --project backend python scripts/websocket_control.py status` | `GET /api/v1/monitoring/websocket` |

兩個 API 均需 `Authorization: Bearer <token>` 且角色為 Admin；未登入回應 401、User／Viewer 回應 403。PUT 只接受必要的布林欄位 `enabled`，拒絕字串、數字、`null` 與未定義欄位。回應格式為 `{"enabled": true, "connections": 0}`；既有 `GET /api/v1/monitoring/status` 也提供 `websocket_enabled` 及 `websocket_connections`。

CLI 會提示輸入管理員 email 與密碼，或使用 `DATAPULSE_EMAIL`／`DATAPULSE_PASSWORD`；不讀取 `.env`，不顯示密碼或 JWT。`--api-url` 或 `DATAPULSE_API_URL` 可指定服務位址，預設 `http://localhost:8010`。

關閉時立即拒絕新連線（預設 Uvicorn 握手回應 HTTP 503，讓客戶端可稍後重試，與認證失敗的 403 區分），既有連線以 `1001`、原因 `WebSocket service disabled` 結束。不支援 ASGI 拒絕回應擴充的伺服器會退回握手前關閉。重新開啟後，客戶端需重新連線；不重播停用期間的事件，可用 REST 歷史查詢補齊已提交資料。重複開啟或關閉皆安全，並行切換依序處理。REST API、帳號登入狀態、模擬生成及資料庫批次寫入持續運作。關閉 WebSocket 不會使健康檢查失敗。

這是目前單一 worker 的執行狀態，不寫入資料庫。初始設定 `WS_ENABLED=true`，後端重啟後恢復此設定；若要預設關閉，在 `.env` 設定 `WS_ENABLED=false` 後重建後端。首次部署新功能或修改初始設定需執行 `docker compose up --build -d --no-deps backend`，這次更新會短暫重啟 REST API；之後的執行中切換不需重啟。多 worker 與多副本部署尚不支援跨程序同步此開關。

## 伺服器訊息

成功連線先收到：

```json
{"type": "ready", "category": null, "device_id": "device-001", "persistence": "pending"}
```

之後每個事件使用獨立 JSON 訊息，範例如下：

```json
{
  "type": "sample.created",
  "event_id": "e5ca5c5e-5aec-4840-b758-228caef2e773",
  "title": "設備 A 溫度",
  "device_id": "device-001",
  "device_name": "設備 A",
  "unit": "°C",
  "value": "85.250000",
  "category": "temperature",
  "timestamp": "2026-11-08T01:00:00Z",
  "threshold": "80.000000",
  "is_anomaly": true,
  "persistence": "pending"
}
```

數值以十進位字串序列化，時間使用 UTC。異常條件是嚴格大於閾值；等於閾值不標示異常。事件 ID 在生成時建立，批次寫入沿用相同 ID。資料庫自增 ID 尚未產生，因此事件不包含歷史記錄的 `id`。

`pending` 表示事件進入持久化流程；即時收到不能當作已提交確認。不另推送 commit 通知。所有登入者都可以使用 `/api/v1/monitoring/samples?event_id=<uuid>` 確認資料已可查詢；原 Admin `/monitoring/history` 入口保持原權限。

## 客戶端訊息與關閉

唯一支援的應用層命令是文字 JSON `{"type":"ping"}`，伺服器回覆 `{"type":"pong"}`。不接受客戶端建立監測事件或修改資料；其他命令、二進位訊息、無效 JSON 與超出訊息大小限制的輸入會關閉連線。

| 情境 | 行為 |
| --- | --- |
| 握手缺少、無效或過期 JWT | 接受連線前拒絕；HTTP 403 |
| 已連線 token 過期、登出撤銷或帳號停用 | 關閉碼 `1008` |
| 不支援的命令／無效內容 | 關閉碼 `1008`；傳輸層大小限制也可能先回 `1009` |
| 慢速客戶端佇列滿載、發送逾時或連線額滿 | 拒絕或關閉，已接受連線使用 `1013` |
| 持續驗證時資料庫不可用 | 關閉碼 `1011` |
| 管理員獨立關閉 WebSocket | 既有連線使用 `1001`；新連線在預設 Uvicorn 握手時回應 HTTP 503 |
| 服務正常關閉 | 模組使用 `1001`；Uvicorn 重啟時也可能先送出 `1012` |

伺服器在發送事件前重新驗證身分，無事件時也依設定間隔驗證，預設為每秒。每條連線有自己的有界佇列與發送任務，發送逾時不會阻塞其他連線。連線結束會移除訂閱並取消該連線的背景任務。

## 重連與可靠性

WebSocket 本身沒有離線重播或 resume cursor。Streamlit 接收器會對可重試的網路中斷使用退避重連；監控頁在進入時及約每 5 秒以 REST 同步最近 300 筆已提交設備樣本，與即時訊息依事件 ID 合併、去重及排序。更早資料可透過歷史模式分頁查詢。認證拒絕、憑證到期或撤銷時停止重試，清除登入狀態並要求重新登入。CLI 示範程式則在連線結束時回報原因，由使用者重新執行。

已登入使用者可透過設備歷史 API 補查已提交資料，但尚在記憶體中的事件可能在程序崩潰或關閉排空逾時時遺失。`event_id` 可協助客戶端去重，但不代表端到端 exactly-once 保證。部署固定單一 worker；跨程序廣播仍屬後續架構調整。

## 命令列示範

```bash
uv run --project backend python scripts/realtime_demo.py --count 5
uv run --project backend python scripts/realtime_demo.py --device-id device-001 --count 5 --verify-history
```

兩個命令皆接受任一登入角色。CLI 會以相同事件 ID 輪詢歷史，最多等待 10 秒；如果你將批次寫入間隔設得更長，可能需要自行延後到歷史 API 查詢。Ctrl+C 可隨時關閉連線。

## 獨立開關驗證（2026-11-08）

| 檢查 | 結果 |
| --- | --- |
| 隔離 MariaDB／後端／指令自動測試 | 335 項通過，含完整後端回歸及 CLI 測試；Ruff 通過 |
| 前端接收器 | 27 項通過，含 `1001 → 503 → 重新連線`，保留登入憑證及已接收資料 |
| 管理與切換 | 未登入 401、非管理員 403、無效輸入 422；重複與並行切換、握手期間停用、關機期間啟用皆驗證 |
| 本機真實指令 | `stop` 關閉既有連線並拒絕新連線，`status` 可查詢，`start` 後恢復事件接收 |
| REST 與處理不中斷 | 切換期間 122 次 REST 健康／登入狀態請求皆為 200；容器與啟動時間不變，生成與保存計數繼續增加 |

首次套用新版後完成上述切換驗收，最後恢復開啟；測試未修改既有帳號、一般記錄或資料庫結構，獨立測試環境已清除。開關仍依單一 worker 的執行狀態運作，重啟後使用 `WS_ENABLED` 初始化。

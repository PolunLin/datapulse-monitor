# DataPulse — 即時資料分析與監控


DataPulse 是即時資料分析與監控系統，使用 FastAPI、Streamlit 與 MariaDB 11.7.2。三台模擬設備各自每秒產生資料，透過 WebSocket 推送、SQLAlchemy ORM 批次保存，並提供資料管理、分析與 Admin 管理功能。

## 已完成功能與題目對照

已完成使用者管理、資料管理、即時監控、資料分析與系統管理五大模組，並提供 Docker Compose 部署。下表列出題目要求與實作位置，操作方式見 [功能操作展示](docs/demo.md)。

| 題目要求 | 已完成內容 | 操作位置／說明 |
| --- | --- | --- |
| FastAPI、RESTful API、CRUD | 依 HTTP 方法提供新增、讀取、修改、刪除與對應狀態碼 | `/api/v1/records`、`/api/v1/records/{id}`；Swagger `/docs` |
| JWT、註冊、登入／登出、三角色 | Admin／User／Viewer；登出與權限變更可撤銷既有 token | 登入頁、側邊欄、系統管理 |
| 創建者或 Admin 才能更新／刪除 | 後端檢查角色與資料擁有者；Viewer 唯讀 | [權限與輸入規則](docs/demo.md#權限與輸入驗證) |
| 非同步、Pydantic、Swagger、錯誤與日誌 | 非同步 API／資料庫存取、輸入驗證、API 文件、統一錯誤及 Request ID | [系統架構](docs/architecture.md)、[系統日誌](docs/admin.md) |
| MariaDB 11.7、SQLAlchemy ORM | MariaDB 11.7.2；資料讀寫使用 ORM，沒有手寫 SQL 查詢或資料變更 | [資料庫設計](docs/database.md) |
| Alembic、連線池、asyncmy | 結構遷移、非同步 Session 與 Engine 連線池 | [遷移管理](backend/migrations)、[連線設定](backend/app/db/session.py) |
| 定期或條件觸發批次寫入 | 每批滿 100 筆或等待最多 5 秒提交 | [即時推送與批次寫入](#即時推送與批次寫入) |
| Streamlit、多頁面、Session、API 整合 | 各頁串接後端；登入狀態、表單與圖表依 Session 管理 | [前端操作](docs/frontend.md) |
| 標題、數值、分類、時間戳；分頁／篩選／排序 | 資料新增、查詢、編輯、刪除；分類使用設備 A／B／C | 資料管理 |
| CSV／JSON 批量導入 | 逐列驗證，全部通過後整批寫入；介面阻擋同次登入重複匯入 | 資料管理 → 批次匯入；[示範資料](sample_data/README.md) |
| 每秒隨機資料、WebSocket、折線圖／柱狀圖、異常告警 | 三設備各每秒一筆，持續推送並更新圖表；超過閾值標示異常 | 即時監控；[設備監控說明](docs/device-monitoring.md) |
| 統計、時間查詢、分類聚合、趨勢、Excel | 總計、平均、最大／最小；個人與系統資料分開查詢並可匯出 | 資料分析；[分析與資料交換](docs/analytics.md) |
| Admin 使用者列表、權限、日誌、資料庫狀態、歷史管理 | 提供管理頁面；歷史入口整合至資料分析，讀取範圍如下 | 系統管理；[Admin 操作](docs/admin.md) |
| Docker 多階段建置、Compose、網路、Volume、環境變數 | 前後端多階段建置、多容器通訊、資料保存與環境設定 | [快速啟動](#快速啟動)、[Compose 設定](compose.yaml) |

**歷史查詢範圍：** 系統管理頁、`/admin/*` 及 `/api/v1/monitoring/history` 限 Admin；設備歷史 `/api/v1/monitoring/samples`、`sample-feed`、系統來源分析與匯出則開放所有已登入角色。

## 快速啟動

需求：Docker Engine／Docker Desktop、Docker Compose v2，以及 Python 3（僅用於產生本機設定）。

```bash
python3 scripts/init_env.py
docker compose up --build -d
docker compose ps
```

`init_env.py` 會建立權限為 `0600` 的 `.env`，產生獨立的資料庫密碼、JWT 金鑰及初始管理員密碼，不列印機密，也不覆寫已存在的設定。`.env` 不會加入版本控制。

| 入口 | 預設網址 |
| --- | --- |
| Streamlit 前端 | http://localhost:8510 |
| Swagger／API 操作 | http://localhost:8010/docs |
| OpenAPI | http://localhost:8010/openapi.json |
| API 就緒檢查 | http://localhost:8010/health/ready |

預設使用 8010／8510 避免常見連接埠衝突；可修改 `.env` 的 `BACKEND_PORT`、`FRONTEND_PORT`，並同步調整 `PUBLIC_API_URL`。開發入口只綁定 `127.0.0.1`；MariaDB 不對主機發布連接埠。

啟動順序為 **MariaDB 健康檢查 → Alembic 遷移及初始管理員 → FastAPI 就緒 → Streamlit**。後端以單一 worker 執行。遷移失敗會阻止後端啟動。

## 初始帳號與認證

- 初始管理員帳號位於 `.env` 的 `BOOTSTRAP_ADMIN_EMAIL`（預設 `admin@example.com`），密碼為 `BOOTSTRAP_ADMIN_PASSWORD`。
- 註冊 API 只能建立 `user`，無法自行指定角色或使用者 ID。
- Bootstrap 只在 email 不存在時建立初始管理員；既有帳號的角色、啟用狀態、密碼與 Token 版本都保持原樣。管理員被另一位 Admin 降權或停用後，重新執行遷移及初始化仍可成功啟動，不會自動升權或重新啟用。
- 若不需要初始管理員，將兩個 `BOOTSTRAP_ADMIN_*` 設定都留空。
- JWT 使用 HS256、有效期限、issuer、audience 與資料庫 token version 驗證；每次請求重新檢查使用者是否啟用及目前角色。
- **登出會撤銷該帳號所有已發出的 token**。目前不提供 refresh token。

Swagger 操作：先執行註冊，接著按 **Authorize**，在 `username` 填寫 email、`password` 填寫密碼。登入端點使用 OAuth2 form，其他資料端點使用 JSON。

Swagger 同步列出各端點的錯誤狀態與 `error`／`request_id` 回應格式；`X-Request-ID` 可用於對照日誌，401 另提供 `WWW-Authenticate: Bearer`。匯入錯誤保留逐列明細；Excel 成功回應仍為二進位 `.xlsx`。兩種資料來源的分析與匯出均開放所有登入角色，系統管理及管線控制仍限 Admin。

| 方法 | 路徑 | 功能 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 註冊一般使用者 |
| POST | `/api/v1/auth/login` | 登入並取得 Bearer token |
| POST | `/api/v1/auth/logout` | 撤銷帳號目前所有 token |
| GET | `/api/v1/users/me` | 查看自己的身分 |
| POST / GET | `/api/v1/records` | 新增／查詢資料 |
| GET / PATCH / DELETE | `/api/v1/records/{id}` | 讀取／修改／刪除一筆資料 |
| GET | `/health/live`、`/health/ready` | 程序存活／資料庫與即時處理就緒 |
| WebSocket | `/ws/v1/samples` | 所有已登入角色接收模擬監測事件 |
| GET | `/api/v1/monitoring/devices` | 所有已登入角色查詢模擬設備清單 |
| GET | `/api/v1/monitoring/samples` | 所有已登入角色查詢設備歷史、時間範圍與分頁 |
| GET | `/api/v1/monitoring/sample-feed` | 所有已登入角色取得設備近期快照及游標增量資料 |
| GET | `/api/v1/monitoring/history` | Admin 查詢已提交的監測歷史 |
| GET | `/api/v1/monitoring/status` | Admin 查看生成器、批次寫入與連線狀態 |
| GET / PUT | `/api/v1/monitoring/websocket` | Admin 查詢／即時切換 WebSocket，不重啟 REST API |
| GET | `/api/v1/analytics` | 筆數、總和、平均、最大／最小、分類與台灣時間分組 |
| POST | `/api/v1/record-imports` | CSV／JSON 整批驗證及匯入，限 User／Admin |
| GET | `/api/v1/record-exports` | 依篩選條件下載 Excel 明細 |
| GET | `/api/v1/admin/users` | Admin 使用者列表、角色／狀態／email 篩選 |
| GET / PATCH | `/api/v1/admin/users/{id}` | Admin 查詢使用者及修改角色／啟用狀態 |
| GET | `/api/v1/admin/logs` | Admin 查詢持久化應用日誌與管理操作紀錄 |
| GET | `/api/v1/admin/logging-status` | Admin 查看日誌寫入佇列與計數 |
| GET | `/api/v1/admin/database` | Admin 查看資料庫健康、資料量與連線池 |

| 角色 | 讀取 | 新增 | 修改／刪除 |
| --- | --- | --- | --- |
| viewer | 所有記錄 | 不允許 | 不允許 |
| user | 所有記錄 | 允許 | 自己建立的記錄 |
| admin | 所有記錄 | 允許 | 所有記錄 |

一般 CRUD 記錄採登入者共享讀取的單一工作區。模擬監測資料另存於不可變的 `realtime_samples`；所有已登入角色可接收設備即時事件，並透過 `/monitoring/samples` 查詢設備歷史。原有 `/monitoring/history` 管理入口與管線狀態維持 Admin 限定；設備歷史的聚合分析與 Excel 匯出開放所有登入角色。所有 `/admin/*` 端點與系統管理頁面均限 Admin；角色或啟用狀態實際變更會撤銷目標帳號所有既有 token。

## Streamlit 操作

開啟 http://localhost:8510 ，使用初始管理員登入，或註冊一般使用者帳號。

介面統一使用台灣標準時間（固定 UTC+08:00），包含監控、查詢、Admin、分析與 Excel。資料管理的分類限定設備 A／B／C；日期使用日曆，時間使用時／分／秒下拉框，數值欄位拒絕非數字並保留精確小數。CSV／JSON 採用相同後端驗證。舊分類資料保留可查，不自行猜測設備。

| 頁面／操作 | 行為 |
|---|---|
| 即時監控 | 選擇設備後載入既有歷史，合併 WebSocket 新事件，每秒更新圖表及異常告警 |
| 暫停／繼續 | 暫停目前畫面更新，設備持續生成及保存；繼續後重新同步最近歷史 |
| 歷史查詢 | 集中在資料分析，依來源、設備、時間與異常等條件分頁查詢 |
| 資料管理 | 新增、列表、標題／分類／時間篩選、排序、分頁、編輯與確認刪除 |
| 批次匯入 | User／Admin 上傳 CSV 或 JSON；任一列錯誤就整批拒絕並顯示明細 |
| 資料分析 | 區分我的新增／匯入資料與系統監控資料，設備 A／B／C 下拉選單，統計、圖表與歷史明細 |
| Excel 下載 | 匯出目前分析條件下的原始明細；完整保留 Decimal 與台灣時間（含 +08:00） |
| 系統管理（Admin） | 使用者／角色與啟用狀態、系統日誌、資料庫／管線狀態；歷史入口導向資料分析 |
| 權限 | Viewer 僅檢視；User 僅編輯自己的記錄；Admin 可操作所有記錄 |
| 登出 | 撤銷該帳號既有 token，停止接收器並清除登入、圖表與表單內容 |

JWT 只保存在各自的伺服器端 Session State；不放入網址、瀏覽器儲存或全域快取。重新載入瀏覽器會建立新 Session，需再次登入。監控頁每秒透過 Streamlit fragment 更新；背景執行緒只接收 WebSocket 並放入有界佇列，不呼叫 Streamlit API。每個 Session 最多緩衝 300 筆事件，圖表顯示最近 30／120／300 筆；暫停時仍每 5 秒確認登入有效性。

前端客戶端的真實服務驗收（會建立並刪除一筆測試記錄，最後登出該帳號）：

```bash
uv run --project frontend python scripts/frontend_smoke.py
```

更多細節見 [前端設計與操作](docs/frontend.md)。

## 分析與匯入／匯出

在「資料管理」匯入 [CSV 範例](sample_data/records.csv) 或 [JSON 範例](sample_data/records.json)，接著到「資料分析」查詢並下載 Excel。[錯誤範例](sample_data/records-invalid.csv) 可用來確認逐列錯誤與整批不寫入。

| 項目 | 行為與上限 |
|---|---|
| 資料來源 | 我的資料固定查登入者的 `records`；系統來源查已提交的 `realtime_samples`，所有角色可讀 |
| 統計 | 筆數、總和、平均、最大、最小；台灣時間（UTC+8）分時／分日，分類及時間桶各最多 1,000 組 |
| 匯入 | 2 MiB、5,000 筆；整批交易，擁有者為登入者；介面在同一登入 Session 阻擋已成功匯入的相同內容，API 或重新登入後重傳仍可能新增另一批 |
| 匯出 | 最多 10,000 筆，超過需縮小條件；精確數值以 Excel 文字儲存 |
| 時間 | 所有篩選採 `[start_time, end_time)`；省略匯入時間則使用目前 UTC |

詳見 [API、檔案規則與操作步驟](docs/analytics.md)。

## Admin 系統管理

以 Admin 登入後開啟「系統管理」，使用功能選單切換四個管理區域。

| 區域 | 行為 |
|---|---|
| 使用者管理 | 分頁搜尋、角色與啟用狀態修改；明確確認後提交，不能自行降級或停用 |
| 系統日誌 | 依等級、類型、Request ID、操作人與時間查詢，查看管理變更前後值 |
| 資料庫狀態 | 連線健康／延遲、MariaDB 版本、各表筆數、連線池與即時管線 |
| 即時歷史 | 導向資料分析的系統來源，設備、標題、UUID、時間與異常條件共用於歷史、統計與匯出 |

`0003_system_logs` 遷移新增持久化日誌，保留原有資料。角色／啟用狀態變更與管理操作紀錄共用交易，寫入失敗會一起回滾。一般應用日誌透過容量 1,000 筆的佇列寫入，定期保留最新 10,000 筆；省略或失敗會顯示計數。日誌不保存密碼、Token、請求本文或 SQL 參數。

完整權限、保留期限語意、故障行為與驗收步驟見 [Admin 操作文件](docs/admin.md)。

## CRUD 資料格式

```json
{
  "title": "設備 A 溫度",
  "value": "25.500000",
  "category": "device-001",
  "timestamp": "2026-11-08T09:00:00+08:00"
}
```

- `category` 新寫入只接受 `device-001`、`device-002`、`device-003`，對應設備 A／B／C。舊分類記錄保留可查；明確修改分類時必須選擇這三台設備。
- `value` 使用 `DECIMAL(18,6)`；只接受有限數值，JSON 回應為字串，避免浮點誤差。
- 提供 `timestamp` 時必須帶時區；省略時使用目前 UTC。資料庫以 UTC 儲存，API 回傳帶時區時間；介面、日期／時間選擇器與 Excel 使用台灣時間（UTC+8）。
- `created_by` 由 token 對應的登入者決定；無法透過輸入修改。
- PATCH 支援部分更新，拒絕空物件、必填欄位的 `null` 及未定義欄位。
- 列表回應為 `items`、`total`、`page`、`page_size`；每頁最多 100 筆。
- 篩選參數：`category`、`q`（標題搜尋）、`created_by`、`start_time`、`end_time`。時間範圍採 `[start_time, end_time)`。
- 排序參數：`sort_by=timestamp|value|title|id`、`sort_order=asc|desc`，搭配 ID 穩定排序。
- 無效資料為 422、未認證為 401、權限不足為 403、找不到資料為 404、email 重複為 409。

## 即時推送與批次寫入

Compose 預設啟用單一模擬生成器，為 3 台固定設備各自每秒產生一筆溫度，合計約每秒 3 筆；分類為 `temperature`、單位 `°C`、預設閾值 80。設備有固定 ID，資料包含名稱、單位與事件當下閾值；隨機變化與偶發升溫模擬運作與異常。生成器隨服務啟動，不依賴任何人登入、開頁或訂閱。事件先進入有容量上限的持久化佇列，再廣播到各 WebSocket 連線的發送佇列。批次 writer 開始收集一批後，等待最多 5 秒或累積滿 100 筆就提交；每批使用自己的 ORM Session 與交易。資料庫故障重試或佇列積壓時，實際入庫延遲可能更長。

進入監控頁會先啟動設備訂閱並讀取最近已提交的 300 筆資料，再按事件 ID 去重、時間排序。之後約每 5 秒依提交游標補入新增資料，能接續延遲提交的舊時間事件，無須反覆載入完整窗口或計算歷史總筆數。畫面另依最新樣本時間顯示停滯告警，門檻為 10 秒或三倍設備取樣間隔，取較大值；暫停與 `legacy` 歷史排除。更早的內容透過「查看歷史與分析」進入資料分析，帶入目前設備後分頁查詢。詳見 [設備監控](docs/device-monitoring.md)。

| 設定 | 預設值 | 用途 |
| --- | --- | --- |
| `SIMULATOR_ENABLED` | Compose 為 `true`；程式設定為 `false` | 是否自動生成資料，測試預設停用 |
| `SIMULATOR_INTERVAL_SECONDS` | `1` | 每台設備的模擬生成間隔 |
| `SIMULATOR_CATEGORY` | `temperature` | 模擬資料分類 |
| `SIMULATOR_THRESHOLD` | `80` | `value > threshold` 時標示異常 |
| `REALTIME_BATCH_SIZE` | `100` | 每批最多筆數 |
| `REALTIME_FLUSH_INTERVAL_SECONDS` | `5` | 未滿一批時的寫入間隔 |
| `REALTIME_QUEUE_CAPACITY` | `1000` | 待寫入佇列上限；另有最多一批進行中的資料 |
| `REALTIME_SHUTDOWN_TIMEOUT_SECONDS` | `10` | 正常關閉時排空資料的最長等待 |
| `WS_ENABLED` | `true` | 後端啟動時的 WebSocket 開關；執行中可用管理員指令切換 |
| `WS_QUEUE_CAPACITY` | `100` | 每個客戶端發送佇列上限 |
| `WS_SEND_TIMEOUT_SECONDS` | `2` | 單次網路發送逾時 |
| `WS_AUTH_CHECK_INTERVAL_SECONDS` | `1` | 閒置連線重新驗證身分的間隔 |
| `WS_MAX_CONNECTIONS` | `100` | 單一程序的 WebSocket 連線上限；`0` 表示不設應用層連線數上限 |

WebSocket 使用 `Authorization: Bearer <token>` header，支援 `?device_id=device-001&category=temperature` 篩選。JWT 不接受放入 query string。連線成功先收到 `ready`，之後收到 `sample.created`；`event_id` 可用來對照歷史資料。登出、token 過期及帳號停用也會使既有連線失效。

### 獨立啟動／關閉 WebSocket

在專案根目錄執行，使用 Admin 帳號登入：

| 操作 | 指令 |
| --- | --- |
| 開啟 WebSocket | `uv run --project backend python scripts/websocket_control.py start` |
| 關閉 WebSocket | `uv run --project backend python scripts/websocket_control.py stop` |
| 查詢開關與連線數 | `uv run --project backend python scripts/websocket_control.py status` |

指令會提示輸入管理員 email 與密碼，也接受 `DATAPULSE_EMAIL`／`DATAPULSE_PASSWORD` 環境變數；不讀取 `.env`，不列印密碼或 JWT。服務位址預設為 `http://localhost:8010`，可用 `--api-url` 或 `DATAPULSE_API_URL` 指定。

`stop` 立即拒絕新連線，並以關閉碼 `1001` 關閉既有 WebSocket；`start` 允許客戶端重新連線。重複執行相同指令不會重啟服務。這些操作不重啟後端、不停止 REST API、模擬生成器或批次寫入，歷史資料仍持續保存。執行中的開關只保存在目前單一 worker 的記憶體；後端重啟後依 `.env` 的 `WS_ENABLED` 決定初始狀態。

首次套用這項功能時需更新後端一次：`docker compose up --build -d --no-deps backend`。這次更新會短暫重啟後端；之後使用上述 `start`／`stop` 指令不需重啟。開關 API 與關閉行為詳見 [WebSocket 協定](docs/websocket-protocol.md#獨立控制-websocket)。

以任一登入帳號接收指定設備的五筆事件，並確認這些事件已入庫：

```bash
uv run --project backend python scripts/realtime_demo.py --device-id device-001 --count 5 --verify-history
```

指令會提示輸入 email 與密碼，也接受 `DATAPULSE_EMAIL`／`DATAPULSE_PASSWORD` 環境變數；不讀取 `.env`，不列印密碼或 JWT。可使用 `--api-url`、`--device-id`、`--category` 指定服務、設備與分類，Ctrl+C 結束。Swagger 可測試 HTTP 歷史與狀態 API，WebSocket 請使用此 CLI 或支援 header 的客戶端。

**收到事件不代表已提交。** 即時事件的 `persistence` 固定為 `pending`，沒有後續提交確認推送；歷史 API 只查已成功提交的資料。writer 以穩定 `event_id` 與唯一約束避免重試造成重複。資料庫暫時不可用時保留批次並退避重試，佇列滿時暫停生成；慢速 WebSocket 客戶端則單獨斷線，不阻塞其他連線。

`monitoring/status` 的生成、寫入與失敗計數只代表目前程序，重啟後歸零；`persisted_count` 不是資料庫歷史總筆數。需要累積數量時使用歷史查詢的 `total`。

正常關閉會先停止生成，再限時排空 writer、關閉連線並釋放 Engine。writer 排空逾時後，另給取消清理最多 1 秒；排空失敗會記錄未確認事件數。若程序突然崩潰，或資料庫持續不可用而超過關閉期限，尚在記憶體中的資料仍可能遺失。此版本無離線重播與跨程序廣播，部署維持單一 worker。

## 測試

獨立環境的壓力、WebSocket 無上限容量與故障恢復測試見 [效能與可用性測試](docs/performance-testing.md)。測試使用專用 Compose、獨立資料庫與隨機憑證，不使用開發環境的 volume。

專案附有自動化測試，可使用下列指令自行執行。

測試使用真正的 MariaDB 11.7.2、獨立 Compose project 與暫存資料庫；先執行遷移及 `alembic check`，再執行 Ruff 與 pytest。

```bash
docker compose -p datapulse-monitor-test -f compose.yaml -f compose.test.yaml --profile test run --build --rm tests
docker compose -p datapulse-monitor-test -f compose.yaml -f compose.test.yaml --profile test run --build --rm --no-deps frontend-tests
docker compose -p datapulse-monitor-test -f compose.yaml -f compose.test.yaml --profile test down --volumes --remove-orphans
```

清理指令僅針對 `datapulse-monitor-test`。測試 fixture 強制資料庫名稱以 `_test` 結尾才允許清理資料；正式開發 volume 不會被測試使用。

### 持續整合

專案提供 [GitHub Actions workflow](.github/workflows/ci.yml)，包含後端與 MariaDB 整合、前端測試、乾淨環境部署三個工作，並以 `DataPulse CI required` 彙總狀態。測試報告會保存在各次執行的 Artifacts；workflow 本身不會建立分支保護規則。

### 乾淨環境與完整啟動驗收

先將已提交的版本 clone 到另一個目錄，在該副本執行下列命令；副本不能已有 `.env`，也不需要先執行 `init_env.py`。

```bash
python3 scripts/clean_environment_smoke.py --project datapulse-clean-acceptance --backend-port 18010 --frontend-port 18510
```

若要保留失敗診斷，加上 `--diagnostics-dir artifacts/clean-deployment`；只會收集本次腳本擁有的測試環境，在清理前輸出服務狀態、允許的事件與錯誤類型。任意日誌文字、請求本文、SQL 參數及憑證不會保存。

腳本產生獨立憑證，驗證 MariaDB → Alembic → Bootstrap → FastAPI → Streamlit 的完整啟動鏈，以及三角色、CRUD、Swagger、WebSocket 與批次入庫。接著由第二位 Admin 將初始化管理員降為 Viewer，移除容器但保留該測試專案的 volume，再啟動整套服務，確認角色、資料及監測歷史均保留且繼續產生新事件。

驗收結束時僅清除本次建立的 Compose 專案、volume 與 `.env`。腳本會拒絕既有 `.env`、已使用的專案名稱、已占用連接埠及開發預設埠 8010／8510。未指定 `--project` 時使用隨機名稱。GitHub Actions 的獨立 `clean-deployment` job 會執行同一套驗收，其他 job 另跑 MariaDB 整合與 Streamlit 測試。

## 遷移與資料保存

```bash
docker compose run --rm migrate
docker compose exec backend alembic current
docker compose exec backend alembic check
```

結構變更使用 Alembic migration，不在應用啟動時呼叫 `create_all()`。所有業務資料讀寫使用 SQLAlchemy ORM；遷移使用 Alembic schema operations。Engine 配置連線池、連線健康檢查及參數遮蔽。

`docker compose down` 會保留 `mariadb_data` volume；重新 `up -d` 可接續資料。開發環境的 `down --volumes` 會刪除資料庫，僅在刻意重設時使用。已有 volume 時，更改 `.env` 的資料庫初始化密碼不會自動變更資料庫內既有帳號。

## 程式結構與文件

| 目錄 | 職責 |
| --- | --- |
| `backend/app/api` | HTTP 路由與依賴注入 |
| `backend/app/services` | 身分驗證、權限、業務規則與交易 |
| `backend/app/repositories` | ORM 存取；不自行提交交易 |
| `backend/app/models`、`schemas` | 持久化結構與 API 資料驗證 |
| `backend/app/core`、`db` | 設定、安全、日誌有界批次寫入與資料庫資源 |
| `backend/app/ws` | WebSocket 認證、連線、分類過濾與有界發送佇列 |
| `backend/app/realtime` | 事件結構、模擬生成、事件分派與批次寫入 |
| `backend/migrations`、`tests` | 結構遷移與整合驗證 |
| `frontend/views`、`components` | 登入、CRUD、監控、分析、資料交換、Admin 管理及共用表單／圖表 |
| `frontend/clients`、`state` | REST／WebSocket 客戶端、Session 與有界圖表緩衝 |
| `frontend/tests` | 客戶端、狀態生命週期與 Streamlit AppTest |

## 中文 Demo（7 分 20 秒）



https://github.com/user-attachments/assets/b620905c-006f-46cd-8720-9f6b0f1ee852


詳見 [架構](docs/architecture.md)、[資料庫設計](docs/database.md)、[功能操作展示](docs/demo.md) 與 [WebSocket 協定](docs/websocket-protocol.md)。

# DataPulse 驗收流程

請先依根目錄 README 建立環境設定並啟動 Compose。預設 Streamlit 位址為 `http://localhost:8510`，Swagger 為 `http://localhost:8010/docs`。若調整宿主機連接埠，請同步調整 `PUBLIC_API_URL`。

```bash
python3 scripts/init_env.py
docker compose up --build -d
docker compose ps
```

初始化腳本會在不存在時建立 `.env` 並產生隨機密碼，不會覆寫既有設定。Admin 帳號與密碼在該檔的 `BOOTSTRAP_ADMIN_EMAIL`、`BOOTSTRAP_ADMIN_PASSWORD`；不應提交或公開此檔。遷移服務成功執行 Alembic 與 seed 後，後端才啟動。

Seed 僅建立尚不存在的初始化帳號；既有帳號的角色、停用狀態、密碼與 Token 版本不變。另一位 Admin 降權初始化帳號後，重跑完整 Compose 啟動鏈仍應成功，且該帳號維持降權後的權限。

完整自動驗收請使用另一個乾淨 clone（沒有 `.env`），執行：

```bash
python3 scripts/clean_environment_smoke.py --project datapulse-clean-demo --backend-port 18010 --frontend-port 18510
```

此流程會建立並清除獨立測試專案，涵蓋首次啟動、三角色、CRUD、Swagger、WebSocket 入庫，以及初始化管理員降權後保留 volume 重建服務；不要在目前使用中的開發目錄執行。一般單元與整合測試指令見 README。

2026-11-08 已從提交 `db497be` 的乾淨 clone 成功執行此流程，使用隔離專案與 18010／18510 埠。兩次完整啟動、降權保留、資料保存與新事件入庫全部通過；結束後測試容器、volume 及 `.env` 均已清除。另有 397 項後端／指令測試與 248 項前端測試通過。

## 啟動與認證

1. 開啟 Streamlit，以已註冊帳號登入，確認進入即時監控頁面。
2. 開啟 Swagger，使用 `POST /api/v1/auth/register` 註冊帳號；JSON 提供 `email` 與 `password`。
3. 使用 Swagger 的 **Authorize** 登入。`username` 欄位填註冊的 email，密碼填自己的密碼。對應 endpoint 是 `POST /api/v1/auth/login`，接受表單格式。
4. 呼叫 `GET /api/v1/users/me`，確認角色為 `user`，且回應不包含密碼雜湊。

## CRUD

以 `POST /api/v1/records` 建立一筆資料：

```json
{
  "title": "機房溫度",
  "value": "26.500000",
  "category": "device-001",
  "timestamp": "2026-11-08T10:00:00+08:00"
}
```

| 操作 | 預期結果 |
| --- | --- |
| `POST /api/v1/records` | `201`，由伺服器填入 `created_by`；時間轉為 UTC，數值保留十進位精度 |
| `GET /api/v1/records` | 回傳 `items`、`total`、`page`、`page_size` |
| `GET /api/v1/records/{id}` | 讀取指定記錄 |
| `PATCH /api/v1/records/{id}` | 可修改 `title`、`value`、`category`、`timestamp` |
| `DELETE /api/v1/records/{id}` | `204`；再次讀取回傳 `404` |

列表可測試 `category`、`q`、`created_by`、`start_time`、`end_time` 篩選；`sort_by` 支援 `timestamp`、`value`、`title`、`id`，搭配 `sort_order=asc` 或 `desc`。時間查詢採包含起點、不包含終點的區間。頁碼從 1 開始，每頁最多 100 筆。

## 權限與輸入驗證

| 情境 | 預期 |
| --- | --- |
| 未登入存取資料 API | `401` |
| 註冊時指定 `role=admin` | `422`；角色不能由註冊請求設定 |
| 建立或修改時指定 `created_by` | `422`；不能冒用其他擁有者 |
| 第二位 User 讀取第一位 User 的記錄 | 成功；目前已登入使用者共享讀取一般資料 |
| 第二位 User 修改／刪除第一位 User 的記錄 | `403` |
| Admin 修改／刪除其他人的記錄 | 成功 |
| Viewer 讀取／寫入資料 | 讀取成功、寫入 `403` |
| 無時區的時間、空標題、空 PATCH、必填欄位指定 null | `422` |

初始 Admin 由環境設定與 seed 命令建立。第五階段可由 Admin 在系統管理頁將已註冊的測試帳號改為 Viewer 或另一位 Admin，操作細節見 [Admin 驗收](admin.md#驗收順序)。

## 登出與保存

呼叫 `POST /api/v1/auth/logout` 後，該使用者所有既有 token 均失效。重新使用舊 token 呼叫 `/users/me` 應回傳 `401`；重新登入可取得新 token。

建立資料後執行一般服務重啟，重新登入並確認資料仍在。保留 named volume；`docker compose down -v` 會刪除資料，不屬於保存驗收步驟。

## 即時推送與歷史保存

Compose 預設開啟模擬生成器，3 台設備每台每秒生成一筆溫度。不需要新增 WebSocket 連線來啟動生成器；多開客戶端也不會額外啟動生成器。

```bash
uv run --project backend python scripts/realtime_demo.py --device-id device-001 --count 5 --verify-history
```

依提示輸入任一角色的帳號與密碼。預期先顯示連線成功，再列出指定設備五筆不同 `event_id` 的事件，最後顯示已在 MariaDB 歷史中確認這五筆資料。CLI 不會列印登入密碼或 token。

| 情境 | 驗收方式與結果 |
| --- | --- |
| 即時推送 | 開兩個 CLI，確認重疊期間收到相同事件 ID，並非每連線獨立生成 |
| 一般使用者訂閱 | User／Viewer 也能接收事件並使用 `--verify-history` |
| 設備篩選 | `--device-id device-002` 僅接收設備 B；同分類的其他設備不會混入 |
| 分類篩選 | 加上 `--category temperature`；不匹配目前生成器分類時不收到資料 |
| 批次入庫 | 登入者以事件 ID 查 `/api/v1/monitoring/samples`，正常情況等待一個 flush 間隔後可看到資料 |
| 異常判斷 | 事件的 `is_anomaly` 與 `value > threshold` 一致；等待模擬器偶發超溫，或以資料分析系統來源的「僅異常」示範 |
| 歷史權限 | User／Viewer 可查 monitoring samples；管理 history 或 status 仍回 `403` |
| 即時撤銷 | 保持連線，從另一個客戶端登出同一帳號；原 WebSocket 會以 `1008` 關閉 |
| 管線狀態 | Admin 呼叫 `/api/v1/monitoring/status`，可觀察生成、待寫入、已保存、失敗與連線狀態 |

即時監測資料位於獨立的 `realtime_samples`；原本 `/api/v1/records` 的一般 CRUD 記錄不會被生成器寫入。監測歷史為不可變快照，不提供對外修改 API。

## 故障與關閉驗證

批次寫入的暫時連線失敗、重送去重、佇列滿載背壓、關閉排空，以及 WebSocket 慢客戶端隔離都有自動測試。手動示範可短暫停止本專案的 `db` 容器，觀察 ready 狀態與日誌，再啟動資料庫，確認 writer 恢復寫入。此期間新 WebSocket 驗證可能失敗，已連線的持續驗證也會關閉連線。

正常停止先結束生成器，再限時保存待寫入批次。資料庫持續不可用而超出排空期限時，會報告未保存資料；不可把程序停止當作所有事件已成功提交的證明。記憶體佇列不提供突然斷電後的重播。

## Streamlit 操作驗收

| 操作 | 預期結果 |
| --- | --- |
| 錯誤密碼登入 | 顯示登入失敗，受保護頁面不會開啟 |
| 正常登入 | 顯示目前帳號與角色，可切換即時監控及資料管理 |
| 新增記錄 | 提交成功後可在資料管理搜尋到；數值保留小數精度 |
| 修改與刪除 | User 僅能操作自己的記錄，Admin 可操作所有記錄；刪除需明確確認 |
| Viewer 操作 | 能查看資料及圖表，沒有寫入控制項 |
| 篩選與分頁 | 調整條件後由第一頁查詢，空結果也能繼續操作 |
| 即時監控 | 選設備後立即呈現已保存歷史，接著每秒更新該設備折線圖、柱狀圖及最新數值 |
| 異常告警 | 超過事件閾值的數值有紅色標記，並出現在異常清單 |
| 暫停／恢復 | 暫停畫面並保留圖表，設備仍持續產生；恢復後同步最近已保存資料 |
| 歷史分析 | 監控按「查看歷史與分析」帶入設備；在分析頁選系統／個人來源、設備、時間及異常條件，翻頁查看明細 |
| 離開後返回 | 先離開監控頁，稍後返回或重新登入，仍能載入離開期間已保存的資料 |
| 登出／憑證失效 | 清除本 Session 資料並停止接收；受保護頁面要求重新登入 |
| 瀏覽器重新載入 | Session 重建，需要重新登入，不從網址還原 token |

可使用前端客戶端煙霧測試重現 REST CRUD、WebSocket 接收與登出撤銷：

```bash
uv run --project frontend python scripts/frontend_smoke.py
```

使用 User 或 Admin 測試帳號；指令結束時會刪除其建立的 QA 記錄並登出。詳細 Session、連線清理與錯誤行為見 [Streamlit 前端](frontend.md)。

第四階段已提供 CSV／JSON 匯入、Excel 匯出與統計分析；完整驗收順序見 [分析與資料交換](analytics.md#驗收流程)。可先匯入範例、查詢分類與時間趨勢、下載 Excel，再用錯誤檔確認整批不寫入。

第五階段的管理操作請使用第二個測試帳號，依 [Admin 驗收順序](admin.md#驗收順序) 驗證角色、停用／啟用、日誌、資料庫與異常歷史。角色或狀態變更會撤銷目標帳號的既有登入；目前登入的 Admin 不能修改自己的角色或啟用狀態。

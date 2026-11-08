# 示範資料

第四階段可在「資料管理 → 批次匯入」上傳下列檔案，或使用 `POST /api/v1/record-imports` 的 multipart `file` 欄位。

| 檔案 | 用途 |
|---|---|
| `records.json` | 2 筆溫度資料，驗證 JSON 陣列匯入 |
| `records.csv` | 4 筆跨日期、跨分類資料，驗證匯入與分析 |
| `records-invalid.csv` | 第 3 列數值錯誤，驗證整批不寫入及錯誤列號 |

需要 User 或 Admin；記錄擁有者為目前登入者。再次上傳同一份檔案會新增另一批，這些檔案不會由系統自動匯入。詳見 [資料交換規則](../docs/analytics.md)。

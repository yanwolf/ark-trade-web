# 方舟運算 → 永豐自動下單 (Zeabur 版)

架構與 txf-sim 相同模式:GitHub repo → Zeabur 部署,Postgres 存歷史紀錄。

## 部署步驟

1. 把這個資料夾推上一個新的 GitHub repo(例如 `ark-trade-web`)
2. Zeabur 建立新專案,從這個 GitHub repo 部署(跟 txf-sim / pump-dump-hunter 一樣的流程)。這個專案有 `Dockerfile`,Zeabur 偵測到會直接照它建置(python:3.11-slim,跟 txf-sim 同一個基礎環境,避免 shioaji 底層函式庫裝不起來的問題)
3. 在同一個 Zeabur 專案裡加一個 **Postgres** 服務,Zeabur 會自動注入 `DATABASE_URL` 環境變數(沒有的話會退回本地 SQLite,重啟就會遺失資料,不建議正式用)
4. 在 Zeabur 環境變數設定:
   ```
   SJ_API_KEY=你的方舟證券帳戶的Key
   SJ_SECRET_KEY=你的方舟證券帳戶的Secret
   SJ_SIMULATION=true   # 先保持模擬,測試沒問題再改成 false
   ```
   (正式下單才需要再加 `SJ_CA_PATH` / `SJ_CA_PASSWORD`,但憑證檔案怎麼放到 Zeabur 上需要額外處理,等你要切正式環境時再一起弄)
5. 部署完成後會拿到一個網址(像 `xxx.zeabur.app`),打開就是 Dashboard

## 每日使用流程

1. 打開方舟運算 app,看今天的「調節庫存」(賣出)和「布局自選」(買進)建議,以及各檔的建議調節/布局股數(已經把日盤台股+夜盤水位加總算好)
2. 你自行判斷要調節/布局哪一檔(位階、升溫等條件,OCR 無法辨識這部分,前面討論過,維持人工判斷)
3. 打開 Dashboard 網址,把決定好的建議一筆一筆填入表單(代號/買賣/**股數**,單位是零股,1~999)新增
4. Dashboard 會列出「待確認清單」,同時顯示你目前的實際持股(股)方便比對
5. 確認沒問題,按「確認送出全部訂單」— 這時才會真的呼叫 Shioaji 用「盤中零股」下單(9:00~13:30,分批撮合)
6. 下方「歷史紀錄」會自動累積每次的下單結果(含失敗原因)

## 目前狀態

- ✅ 程式邏輯完成(登入、比對持股、下單、歷史紀錄),下單單位為零股(IntradayOdd)
- ✅ 已部署到 Zeabur,模擬模式運作正常(tzujen-ark-trade.zeabur.app)
- ✅ 永豐帳戶已完成電子簽署
- ⏳ 尚待完成永豐官方 API 測試審核(Dashboard 上方有測試按鈕),審核通過才能切換正式環境

## 之後可以擴充的方向

- Telegram 通知(跟 txf-sim 一樣,新增建議或下單完成時推播)
- 自動抓取台股即時股價顯示在待確認清單旁,方便你判斷

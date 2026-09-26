# 單一網站與 managed router 診斷

先確認執行位置、要觀測的 controller 與 client source IP。若在
[RPi-ImmortalWrt](https://github.com/daviddwlee84/RPi-ImmortalWrt)，使用該專案
`just diagnose-site`：它透過既有 strict SSH 取得記憶體中的 API credential，核對
confirmed proxy marker／來源 digest，再使用 private CA 驗證 HTTPS controller。
不要從 legacy 訂閱、Dashboard URL query 或聊天紀錄複製 credential。

一般 controller 可執行以下唯讀觀測；參數須放在 subcommand 之前：

```sh
python3 scripts/clash_api.py \
  --controller https://192.0.2.1:9090 \
  --ca-cert /absolute/private/ca.crt \
  --secret-file /absolute/private/api-secret \
  --read-only observe api.anthropic.com --client 192.0.2.2 --duration 20
```

Secret file 必須是 single-link、non-symlink regular file，mode 0600。
`observe` 的 JSON stdout 是私密報告；需要保存時先設定 `umask 077`，使用新的 ignored
private 路徑。此通用指令只觀察，不主動開啟網站，也不綁定裝置 transaction。

## 證據與輸出

1. 記錄 client route、DNS 來源與 fake-IP 的實際路由。`utun*` 可能仍持有
   `198.18.0.0/15`，即使用戶端介面顯示 TUN 關閉；先排除這個衝突。
2. 在同一 bounded window 讀取 `/connections` 與 `/logs?level=info`，只保留目標域名／來源
   的 rule、rule payload、core 原始 chain 次序與時間。不要把 chain 次序自行反轉成推測路徑。
3. 分開記錄 DNS、TCP/TLS、HTTP status 與登入／應用結果。403、429 或 curl 收到有效 TLS
   回應不代表帳號可用，也不等於 DNS 污染。沒有記錄時回報 insufficient。
4. 先檢查有效 profile 是否在觀測期間改變。只有 stable evidence 才能形成候選；先提
   精確 `DOMAIN`，人工審查擁有者與共享 CDN 後才考慮 suffix／IP 範圍。

回覆使用這個結構：

```text
觀測：期間／目標／client（分享前脫敏）
路由：matched rule → core chain；證據來源為 connection 或 log
可用性：DNS、client route、TLS/HTTP；應用登入未測
判定：observed-route / insufficient / configuration-changed
下一步：重現、候選規則或既有 transaction；沒有自動套用
```

## 範圍與後續知識

`--read-only` 拒絕非 GET controller request 與顯示 secrets；它仍允許 GET latency tests，
因此不等於完全被動。單次觀測不保存完整瀏覽歷史，不能回推任意過去區間或提供可靠流量總量。
Logs stream 關閉／閒置、記錄上限與採樣遺漏必須保留在報告。

通用術語、GFW／DNS 排查、官方 template 差異與長期規則迭代放在
[clash-rules 診斷知識](https://github.com/daviddwlee84/clash-rules/blob/main/docs/diagnosis.md) 與
[組合／版本流程](https://github.com/daviddwlee84/clash-rules/blob/main/docs/architecture.md)。
避免將同一套 FAQ 複製到每台硬體 repo。

AI 固定群組應直接引用一個 leaf node；失敗後由管理者切换私密政策並重新建置。
這不保證供應商固定出口 IP、不保證服務接受該 IP，也不會把既有 fail-open 變成 kill switch。

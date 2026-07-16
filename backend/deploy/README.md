# デプロイ手順（Azure VM / 単一ドメイン: venv + systemd + Caddy）

小規模公開向けの最小構成。**1台の Ubuntu VM** でバックエンド(FastAPI)を常時起動し、
**Caddy が同一ドメインでフロント(静的)と API(/api) を配信**する（CORS 不要）。
自然言語検索は 600MB のベクトルを常駐させるため **RAM 2GB 以上**が必要
（Azure なら B1ms=2GB か B2s=4GB。1GB の B1s では動かない）。

- 想定OS: Ubuntu 24.04 LTS
- 想定配置: リポジトリを `/opt/ranobe` に clone
- ホスト名: Azure が無料で払い出す **`<好きな名前>.japaneast.cloudapp.azure.com`**
  （パブリックIPの「DNS名ラベル」で設定。独自ドメイン購入は不要）を Caddyfile に使う
- Azure のネットワークセキュリティグループ(NSG)で **22 / 80 / 443** を受信許可にしておく

---

## 1. サーバ初期設定

```bash
sudo adduser --disabled-password --gecos "" ranobe        # 実行用ユーザー
sudo apt update
sudo apt install -y python3.12 python3.12-venv git caddy nodejs npm  # Caddy=自動HTTPS, node=フロントのビルド用
sudo mkdir -p /opt/ranobe && sudo chown ranobe:ranobe /opt/ranobe
```

## 2. コード配置と依存インストール

```bash
sudo -u ranobe -H bash
cd /opt/ranobe
git clone <このリポジトリのURL> .
cd backend
python3.12 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

## 3. 環境変数 `.env`（backend/.env、git 管理外）

`backend/.env.example` を元に作成し、必要なキーを埋める。最低限:

```
GEMINI_API_KEY=...
OPENAI_API_KEY=...          # 埋め込み + クエリ分割(gpt-5.5) に必須
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
XAI_API_KEY=...             # レビュー収集を回さないなら空でも可
```

> この構成はフロントとAPIが同一ドメイン（同一オリジン）なので **CORS 不要**。
> `FRONTEND_ORIGINS` は設定しなくてよい（別ドメインで配信する場合のみ指定）。

## 4. ベクトルキャッシュを生成（初回のみ / データ更新時）

`data/vector_cache/facets.npz` は 600MB で git に含めていないため、サーバ上で生成する。
DB から約600MB を取得するので数分かかる。

```bash
cd /opt/ranobe/backend
./venv/bin/python -m scripts.rebuild_vector_cache
```

> データ（6観点ベクトル）を更新したときは、この再生成 → `sudo systemctl restart ranobe-backend`
> を行うと検索に反映される。

## 5. systemd で常時起動

```bash
exit   # ranobe シェルを抜けて sudo 可能なユーザーへ
sudo cp /opt/ranobe/backend/deploy/ranobe-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ranobe-backend
sudo systemctl status ranobe-backend           # active (running) を確認
curl -s http://127.0.0.1:8000/health           # {"status":"ok"}
```

- worker は **1 固定**（unit 内で指定済み）。複数にすると各プロセスが 600MB 確保して
  メモリが枯渇する。1〜3人ならこれで十分。

## 6. フロントエンドをビルド（Caddy が配信する）

同一ドメイン配信なので API は相対パス `/api` を向ける。ビルド成果物 `dist/` を
Caddy がそのまま配信する。

```bash
cd /opt/ranobe/frontend
# 環境変数（同一オリジンなので API は /api の相対パス）
cat > .env <<'EOF'
VITE_API_URL=/api
VITE_SUPABASE_URL=...            # ログイン機能を使う場合。使わないなら空でも可
VITE_SUPABASE_ANON_KEY=...       # anon キー（service キーではない）
EOF
npm ci
npm run build                    # /opt/ranobe/frontend/dist を生成
```

## 7. Caddy で HTTPS 公開（フロント + API を同一ドメインで配信）

```bash
sudo cp /opt/ranobe/backend/deploy/Caddyfile /etc/caddy/Caddyfile
# Caddyfile 内のホスト名を、Azure のパブリックIPに設定した DNS名に置換する
sudo sed -i 's/ranobe-xxx.japaneast.cloudapp.azure.com/<自分のDNS名>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

- Caddyfile は `/api/*` を uvicorn(127.0.0.1:8000) へ、それ以外を `frontend/dist` の
  静的ファイル（React SPA）へ振り分ける（設定済み）。
- 初回アクセス時に Let's Encrypt で証明書を自動取得（数十秒）。

## 8. 動作確認

```bash
DNS=<自分のDNS名>
curl -s "https://$DNS/health"                                  # {"status":"ok"}
curl -s "https://$DNS/api/series/recommended?limit=3" | head -c 200
curl -s -G "https://$DNS/api/series/semantic" \
     --data-urlencode "q=疲れた時に読める料理が美味しそうな異世界スローライフ" \
     --data-urlencode "limit=3"
```

ブラウザで `https://<自分のDNS名>/` を開けば、フロントの検索UIが使える。

---

## 運用メモ
- ログ: `sudo journalctl -u ranobe-backend -f`
- 更新デプロイ: `git pull` → 依存が変われば `pip install -r requirements.txt` →
  `sudo systemctl restart ranobe-backend`
- **コスト注意**: 本構成は URL を知っていれば誰でもアクセスでき、**1検索ごとに
  OpenAI(gpt-5.5 分割 + 埋め込み) が課金**される。URL が広まると無制限に叩かれ得るので、
  レート制限 + 検索結果キャッシュの追加を推奨（必要なら実装する）。
- メモリ目安: 1 worker で常駐 ≈ 1GB。2GB インスタンスなら余裕。
```

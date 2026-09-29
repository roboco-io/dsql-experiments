---
layout: page
title: "Aurora DSQL利用ガイド"
question: "人とコーディングエージェントがDSQLでサービスを構築・運用するときに守るべきルール"
lang: "ja"
permalink: /ja/guide/
updated_at: "2026-09-29"
---

## 結論

このガイドは、2026-09-24–29にソウルリージョンで実行したE001–E012の実験で実際に遭遇した問題と解決方法をルールとしてまとめたものです。人は各節の説明を、コーディングエージェントはすぐ下の「コーディングエージェント向けルール」を先に読んでください。各ルールには根拠となった実験レポートを付けています。DSQLのサポート範囲は変わり続けるため、エラーメッセージがこのガイドと異なる場合は[AWS公式ドキュメント](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/)を優先してください。DSQLを使うかどうかの判断は[総合判断](../decision/)を参照してください。

## コーディングエージェント向けルール

この節は、コーディングエージェントがDSQL用のコードを作成・修正するときに守るべきルールです。**必須**は違反すると実際にエラーやデータの問題が起きたルールで、**推奨**は性能・コストの問題があったルールです。

**接続**

1. **必須:** パスワードの代わりにIAMトークンで認証します。ユーザー`admin`、データベース`postgres`、ポート5432、`sslmode=verify-full`。トークンは`boto3.client("dsql").generate_db_connect_admin_auth_token(Hostname=..., Region=..., ExpiresIn=900)`で署名します。([E003](../experiments/e003/))
2. **必須:** トークンはプロセスごとにキャッシュして再利用し(10分以内)、キャッシュが空のときに複数のスレッドが同時に署名しないようロックをかけます。同時に署名すると、インスタンス認証情報の取得が失敗しました(`NoCredentialsError`)。([E002](../experiments/e002/))
3. **必須:** 接続プールを使い、接続の寿命を1時間より短く(例: 50分)設定します。DSQLは1時間を過ぎた接続を切断します。([E002](../experiments/e002/))
4. **推奨:** リクエストごとに新しい接続を張らないでください。スループットが接続を維持した場合の約1/70に落ちました。([E003](../experiments/e003/))

**トランザクション**

5. **必須:** すべての書き込みトランザクションをSQLSTATE `40001`(コミット時点の競合)で再試行します。指数バックオフ+ジッター、最大3回、合計期限2秒で検証しました。([E004](../experiments/e004/))
6. **必須:** 書き込みには、業務ID(冪等キー)を記録するレシート行を同じトランザクションに含めます。コミット応答を受け取れなかった場合(接続切断)は、業務IDでコミットの有無を照会してからのみ再試行します。([E004](../experiments/e004/), [E006](../experiments/e006/))
7. **必須:** 1つのトランザクションで3,000行を超えて変更せず、300秒を超えないようにします。超えると`54000`(`transaction row limit exceeded`、`transaction age limit of 300s exceeded`)で拒否されます。大量処理は3,000行未満のバッチに分割します。([E008](../experiments/e008/))
8. **必須:** 分離レベルを指定しないでください。DSQLはREPEATABLE READのみをサポートし、`SET TRANSACTION ISOLATION LEVEL READ COMMITTED`などは拒否されます。([E001](../experiments/e001/))
9. **必須:** `SELECT ... FOR UPDATE`が他のトランザクションを待たせると想定しないでください。DSQLでは待たずに、一方がコミット時点で`40001`により失敗します。([E001](../experiments/e001/), [E004](../experiments/e004/))
10. **必須:** DDLとDMLを同じトランザクションに混在させないでください。トランザクション開始後に作成したテーブルは、そのトランザクションからは見えませんでした(`42P01`)。([E004](../experiments/e004/))

**スキーマ**

11. **必須:** `serial`を使わないでください。sequence・identityには`CACHE 65536`を明示します。デフォルトのCACHEは拒否されます。([E001](../experiments/e001/))
12. **必須:** インデックスは`CREATE INDEX ASYNC`で作成し、`pg_index.indisvalid`が真になるまで待ってからそのインデックスに依存します。`pg_indexes`に表示されていても使えるとは限りません。1,100万行で12分かかりました。([E002](../experiments/e002/), [E008](../experiments/e008/))
13. **必須:** 外部キーの参照する側の列(例: `ledger.order_id`)にインデックスを自分で作成します。ない場合、親行の削除が子テーブル全体を走査し、300秒の上限に達しました。([E002](../experiments/e002/))
14. **必須:** PL/pgSQL関数・トリガー、一時テーブル、パーティションを使わないでください。ロジックはアプリケーションかSQL関数に移します。([E001](../experiments/e001/))
15. **推奨:** 少数の行に書き込みが集中する設計(グローバルカウンター、人気商品の在庫1行)を避けてください。接続256では再試行後も39%が失敗しました。行を分割するか、書き込みをまとめます。([E004](../experiments/e004/))

**クエリと運用**

16. **必須:** `statement_timeout`とサーバー側のキャンセルに依存しないでください。どちらも動作しませんでした。クライアント側の期限と処理サイズの制限で代替します。([E001](../experiments/e001/))
17. **推奨:** 数百万行以上を走査する集計は、範囲を分割するか分析用ストアに回します。1,100万行の集計に36秒、約1,700 DPUかかりました。([E012](../experiments/e012/))
18. **推奨:** `SHOW max_connections`の値(20)を接続上限として使わないでください。1,000本の同時接続がすべて成功しました。([E003](../experiments/e003/), [E004](../experiments/e004/))
19. **必須:** ポイントインタイムリカバリ(PITR)があると想定しないでください。復旧はAWS Backupのフルバックアップから新しいクラスターへの復元でのみ可能です。([E007](../experiments/e007/))
20. **推奨:** データロード中の`XX000 server unavailable`は一時的なエラーとみなして再試行します。([E002](../experiments/e002/))

## 接続

DSQLにはパスワードがなく、接続のたびにIAM権限で署名したトークンをパスワードの代わりに渡します。署名はネットワーク呼び出しなしで0.2 ms程度のため負担は小さいですが、最初の署名はAWSクライアントの作成のために100 ms以上かかり、複数のスレッドが同時に初回署名するとインスタンス認証情報の取得が失敗しました。以下は今回の実験ツールで使った方法です。

```python
import threading, time, boto3, psycopg

SYSTEM_CA = "/etc/pki/tls/certs/ca-bundle.crt"      # Amazon Linux 2023のシステムCAバンドル(今回のランナー)

_LOCK, _CACHE, _TTL = threading.Lock(), {}, 600      # トークン有効期間(15分)より短く再利用

def dsql_token(host, region):
    now = time.monotonic()
    with _LOCK:                                        # 同時の初回接続でも署名は1回だけ
        hit = _CACHE.get(host)
        if hit and now - hit[1] < _TTL:
            return hit[0]
        client = boto3.client("dsql", region_name=region)
        token = client.generate_db_connect_admin_auth_token(Hostname=host, Region=region, ExpiresIn=900)
        _CACHE[host] = (token, now)
        return token

def connect(host, region):
    return psycopg.connect(host=host, port=5432, dbname="postgres", user="admin",
                           password=dsql_token(host, region), sslmode="verify-full",
                           sslrootcert=SYSTEM_CA, connect_timeout=15)
```

- ランナー(アプリケーション)のIAMロールには、対象クラスターに対する`dsql:DbConnectAdmin`(管理者)または`dsql:DbConnect`(ユーザーロール)の権限が必要です。
- 新規接続1回は、TLSと認証を含めてp50 15.5 ms、p99 120 msでした。接続プールの最小接続をあらかじめ開いておいてください。
- 接続の寿命は1時間以内で入れ替えてください。今回のツールは50分ごとに再接続しました。

## トランザクションと再試行

DSQLはロックで待たせる代わりに、コミット時に競合を検査します(楽観的同時実行制御)。同じ行を同時に変更したトランザクションのうち1つはコミットで`40001`により失敗するため、再試行がなければそのリクエストは失敗で終わります。この方式のおかげで、PostgreSQLのREPEATABLE READが許容するwrite skewもDSQLでは`40001`で防がれました(E004)。

```python
import random, time, psycopg

# 例: reconnect()は上のconnect()で新しい接続を返す関数と仮定する。
def run_with_retry(conn, work, op_id, max_attempts=3, deadline_s=2.0, base_s=0.01):
    """work(conn)は1つのトランザクション内で業務の書き込みとレシート(op_id)の書き込みを行う。"""
    t0 = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            with conn.transaction():
                if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                    return "duplicate"                 # 反映済みの業務: 再実行しない
                work(conn)
            return "committed"
        except psycopg.errors.SerializationFailure:     # 40001: コミット時点の競合
            delay = random.uniform(0, base_s * 2 ** (attempt - 1))
            if attempt == max_attempts or time.monotonic() - t0 + delay >= deadline_s:
                raise
            time.sleep(delay)
        except psycopg.OperationalError:                # コミット中に切断されると結果が不明
            conn = reconnect()                          # 新しい接続でレシートを確認してから再試行
            if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                return "committed"
```

- レシートテーブルは`op_id text PRIMARY KEY`とし、業務の書き込みと同じトランザクションで挿入します。この方式により、E004の競合66セルとE006の30秒間の接続遮断で、コミットの消失・重複は0件でした。
- 大量変更は、対象IDを一度照会したあと3,000行未満に分割してそれぞれコミットします。バッチごとに対象を照会し直すと非常に遅くなります(E002)。

```python
ids = [r[0] for r in conn.execute("SELECT id FROM orders WHERE created_at < %s", (cutoff,)).fetchall()]
for i in range(0, len(ids), 2000):
    with conn.transaction():
        conn.execute("DELETE FROM orders WHERE id = ANY(%s)", (ids[i:i + 2000],))
```

## スキーマとインデックス

- **ID生成:** `serial`の代わりにidentityまたはsequenceを`CACHE 65536`で宣言するか、アプリケーションでUUIDなどを生成します。
- **インデックス作成:** `CREATE INDEX ASYNC idx ON t (col)`はすぐに戻り、ビルドはバックグラウンドで進みます。デプロイスクリプトは次のように完了を待つ必要があります。

```sql
SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'idx';
```

- **ビルド中の影響:** 1,100万行のインデックスビルド(12分)の間、毎秒1,000件の負荷における書き込みp99が30.7 msから79.8 msに増えましたが、失敗はありませんでした(E008)。
- **外部キー:** 外部キー制約は動作しますが、参照する側の列のインデックスは自動では作成されません。親行を削除したり結合したりする列には、インデックスを自分で作成してください。
- **列の変更:** `ALTER TABLE ... ADD COLUMN`/`DROP COLUMN`は負荷中でも0.1秒以内に終わりました。
- **サポートされず設計変更が必要なもの:** PL/pgSQL関数、トリガー、一時テーブル、パーティション。GIN・式インデックスの代替方法は検証していません(E001)。

## クエリと性能

- **遅延:** クエリ1回の遅延がAuroraより長いです(書き込みp95 約28–41 ms、読み取り約5–10 ms、ソウル、E002)。1つのリクエストで複数のクエリを順番に実行せず、結合や1つのトランザクションにまとめて減らしてください。
- **拡張:** 接続と同時実行数を増やすと、スループットがほぼ比例して増えました(接続64で6,680 TPS、256で24,541 TPS、遅延はほぼ同じ)。スループットが必要なら同時実行数を増やしてください。
- **最新性:** コミットしたデータはどの接続からもすぐに見えました(E005)。書き込み直後の読み取りを特定のノードに送るルーティングは不要です。
- **大規模集計:** 処理した行数に比例して遅くなり、DPUで課金されます。300秒の上限を超えるクエリは拒否され、サーバー側のキャンセルもないため、範囲を分割して実行してください(E012)。

## 運用と復旧

- **作成・削除:** クラスターの作成は32秒、削除は約2分でした(E011)。容量・インスタンスサイズ・vacuumの設定はありません。
- **バックアップ:** バックアップはAWS Backupでのみ行います。バックアップボールトとサービスロール(`AWSBackupServiceRolePolicyForBackup`、`AWSBackupServiceRolePolicyForRestores`)を先に作成する必要があり、アカウントのAWS Backup設定でDSQLの利用が有効になっている必要があります。バックアップは毎回フルバックアップで、約100 MBに481秒かかりました(E007)。
- **復元:** `start-restore-job`は常に新しいクラスターを作成します。デフォルトで削除保護が有効になるため、必要に応じてメタデータ`regionalConfig`の`isDeletionProtectionEnabled`を指定してください。新しいクラスターはエンドポイントが異なるため、アプリケーションの設定とIAM権限を変更する必要があります。復元は129秒でした。
- **ポイントインタイムリカバリなし:** 復旧できる最新時点は最後のバックアップ時刻です。許容できるデータ損失時間に合わせてバックアップ周期を決めてください。
- **自動化の注意点:** 存在しないバックアップボールトを照会すると`AccessDeniedException`が返りました。ボールトが存在しないという意味として扱う必要があります。
- **監視:** コストと使用量はCloudWatch `AWS/AuroraDSQL`の`TotalDPU`で確認しました。分単位の合計です。

## コスト見積もり

- **料金体系(ソウル、2026-09-11掲載版):** 100万DPUあたり$10、ストレージ1 GB-月あたり$0.40、毎月10万DPUと1 GB-月が無料。
- **リクエストあたりのDPU:** 今回の注文業務では、リクエスト1回あたり0.029–0.032 DPU(100万件あたり約$0.31)でした。クエリの形によって大きく異なるため、実際の業務で測定してください。
- **簡易計算:** 1時間あたりのコスト ≈ 平均TPS × 3,600 × リクエストあたりのDPU × $10 / 100万。今回の業務では、RDS Multi-AZ(`db.r6g.xlarge`、1時間あたり約$1.22)との損益分岐点は平均約1,100 TPSでした(E010)。
- **ロード:** 約5 GiBのロードに約52万DPU(約$5.2)かかりました(E002)。

## 導入前チェックリスト

- [ ] すべての書き込み経路に`40001`の再試行と業務IDレシートがある。
- [ ] コミット応答を受け取れなかったリクエストは、レシートで確認してからのみ再試行する。
- [ ] 3,000行・300秒を超える処理がない(バッチ、移行、クリーンアップ処理を含む)。
- [ ] 分離レベルの指定、`SELECT FOR UPDATE`での待機、`statement_timeout`に依存するコードがない。
- [ ] `serial`、PL/pgSQL、トリガー、一時テーブル、パーティションを使っていない。
- [ ] インデックスは`CREATE INDEX ASYNC`の後に`indisvalid`を確認しており、外部キー側のインデックスを作成した。
- [ ] 接続プールがあり、接続の寿命が1時間より短く、トークンをキャッシュし、同時署名を防いでいる。
- [ ] 少数の行に書き込みが集中する箇所がないか、分散設計になっている。
- [ ] バックアップ周期と、新しいクラスターに切り替える復旧手順が文書化されている。
- [ ] 実際の業務でリクエストあたりのDPUを測定し、固定インスタンスのコストと比較した。

---
layout: page
title: "Aurora DSQL 사용 가이드"
question: "사람과 코딩 에이전트가 DSQL로 서비스를 만들고 운영할 때 지켜야 할 규칙"
permalink: /guide/
updated_at: "2026-09-29"
---

## 결론

이 가이드는 2026-09-24–29에 서울 리전에서 실행한 E001–E012 실험에서 실제로 겪은 문제와 해결 방법을 규칙으로 정리한 것입니다. 사람은 각 절의 설명을, 코딩 에이전트는 바로 아래 "에이전트용 규칙"을 먼저 읽으면 됩니다. 규칙마다 근거가 된 실험 보고서를 달았습니다. DSQL의 지원 범위는 계속 바뀌므로, 오류 메시지가 이 가이드와 다르면 [AWS 공식 문서](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/)를 우선합니다. DSQL을 쓸지 말지의 판단은 [종합 판단](../decision/)을 보세요.

## 에이전트용 규칙

이 절은 코딩 에이전트가 DSQL용 코드를 만들거나 고칠 때 지켜야 할 규칙입니다. "반드시"는 어기면 오류나 데이터 문제가 실제로 났던 규칙이고, "권장"은 성능·비용 문제가 있었던 규칙입니다.

**연결**

1. 반드시: 비밀번호 대신 IAM 토큰으로 인증한다. 사용자 `admin`, 데이터베이스 `postgres`, 포트 5432, `sslmode=verify-full`. 토큰은 `boto3.client("dsql").generate_db_connect_admin_auth_token(Hostname=..., Region=..., ExpiresIn=900)`으로 서명한다. ([E003](../experiments/e003/))
2. 반드시: 토큰은 프로세스마다 캐시해 재사용하고(10분 이내), 캐시가 비었을 때 여러 스레드가 동시에 서명하지 않도록 잠금을 건다. 동시에 서명하면 인스턴스 자격 증명 조회가 실패(`NoCredentialsError`)했다. ([E002](../experiments/e002/))
3. 반드시: 연결 풀을 쓰고, 연결 수명을 1시간보다 짧게(예: 50분) 설정한다. DSQL은 1시간이 지난 연결을 끊는다. ([E002](../experiments/e002/))
4. 권장: 요청마다 새 연결을 맺지 않는다. 처리량이 연결 유지 대비 약 1/70로 떨어졌다. ([E003](../experiments/e003/))

**트랜잭션**

5. 반드시: 모든 쓰기 트랜잭션을 SQLSTATE `40001`(커밋 시점 충돌)에서 다시 시도한다. 지수 백오프+지터, 최대 3회, 총 기한 2초로 검증했다. ([E004](../experiments/e004/))
6. 반드시: 쓰기에는 업무 ID(멱등 키)를 기록하는 영수증 행을 같은 트랜잭션에 넣는다. 커밋 응답을 받지 못하면(연결 끊김) 업무 ID로 커밋 여부를 조회한 뒤에만 다시 시도한다. ([E004](../experiments/e004/), [E006](../experiments/e006/))
7. 반드시: 한 트랜잭션에서 3,000행을 넘게 바꾸지 않고, 300초를 넘기지 않는다. 넘으면 `54000`(`transaction row limit exceeded`, `transaction age limit of 300s exceeded`)으로 거절된다. 대량 작업은 3,000행 미만 배치로 나눈다. ([E008](../experiments/e008/))
8. 반드시: 격리 수준을 지정하지 않는다. DSQL은 REPEATABLE READ만 지원하고 `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` 등은 거절된다. ([E001](../experiments/e001/))
9. 반드시: `SELECT ... FOR UPDATE`가 다른 트랜잭션을 기다리게 한다고 가정하지 않는다. DSQL에서는 기다리지 않고 한쪽이 커밋 시점에 `40001`로 실패한다. ([E001](../experiments/e001/), [E004](../experiments/e004/))
10. 반드시: DDL과 DML을 같은 트랜잭션에 섞지 않는다. 트랜잭션을 시작한 뒤 만든 테이블은 그 트랜잭션에서 보이지 않았다(`42P01`). ([E004](../experiments/e004/))

**스키마**

11. 반드시: `serial`을 쓰지 않는다. sequence·identity는 `CACHE 65536`을 명시한다. 기본 CACHE는 거절된다. ([E001](../experiments/e001/))
12. 반드시: 인덱스는 `CREATE INDEX ASYNC`로 만들고, `pg_index.indisvalid`가 참이 될 때까지 기다린 뒤 그 인덱스에 의존한다. `pg_indexes`에 보인다고 쓸 수 있는 것은 아니다. 1,100만 행에 12분이 걸렸다. ([E002](../experiments/e002/), [E008](../experiments/e008/))
13. 반드시: 외래 키의 참조하는 쪽 열(예: `ledger.order_id`)에 인덱스를 직접 만든다. 없으면 부모 행 삭제가 자식 테이블 전체를 훑다 300초 한도에 걸렸다. ([E002](../experiments/e002/))
14. 반드시: PL/pgSQL 함수·트리거, 임시 테이블, 파티션을 쓰지 않는다. 로직은 애플리케이션이나 SQL 함수로 옮긴다. ([E001](../experiments/e001/))
15. 권장: 소수 행에 쓰기가 몰리는 설계(전역 카운터, 인기 상품 재고 한 행)를 피한다. 연결 256에서 재시도 후에도 39%가 실패했다. 행을 나누거나 쓰기를 모은다. ([E004](../experiments/e004/))

**쿼리와 운영**

16. 반드시: `statement_timeout`과 서버 측 취소에 의존하지 않는다. 둘 다 동작하지 않았다. 클라이언트 기한과 작업 크기 제한으로 대신한다. ([E001](../experiments/e001/))
17. 권장: 수백만 행 이상을 훑는 집계는 범위를 나누거나 분석 저장소로 보낸다. 1,100만 행 집계가 36초, 약 1,700 DPU였다. ([E012](../experiments/e012/))
18. 권장: `SHOW max_connections` 값(20)을 연결 한도로 쓰지 않는다. 1,000개 동시 연결이 모두 성공했다. ([E003](../experiments/e003/), [E004](../experiments/e004/))
19. 반드시: 시점 복원(PITR)이 있다고 가정하지 않는다. 복구는 AWS Backup 전체 백업에서 새 클러스터로만 가능하다. ([E007](../experiments/e007/))
20. 권장: 적재 중 `XX000 server unavailable`은 일시 오류로 보고 다시 시도한다. ([E002](../experiments/e002/))

## 연결

DSQL은 비밀번호가 없고, 연결할 때마다 IAM 권한으로 서명한 토큰을 비밀번호 자리에 넣습니다. 서명은 네트워크 호출 없이 0.2 ms 정도라 부담이 작지만, 첫 서명은 AWS 클라이언트를 만드느라 100 ms 이상 걸렸고, 여러 스레드가 동시에 처음 서명하면 인스턴스 자격 증명 조회가 실패했습니다. 아래는 이번 실험 도구에서 쓴 방식입니다.

```python
import threading, time, boto3, psycopg

SYSTEM_CA = "/etc/pki/tls/certs/ca-bundle.crt"      # Amazon Linux 2023의 시스템 CA 묶음(이번 실험의 러너)

_LOCK, _CACHE, _TTL = threading.Lock(), {}, 600      # 토큰 유효기간(15분)보다 짧게 재사용

def dsql_token(host, region):
    now = time.monotonic()
    with _LOCK:                                        # 동시에 처음 연결해도 서명은 한 번만
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

- 러너(애플리케이션) IAM 역할에는 해당 클러스터에 대한 `dsql:DbConnectAdmin`(관리자) 또는 `dsql:DbConnect`(사용자 역할) 권한이 필요합니다.
- 새 연결 한 번은 TLS와 인증을 포함해 p50 15.5 ms, p99 120 ms였습니다. 연결 풀의 최소 연결을 미리 열어 두세요.
- 연결 수명은 1시간 안에서 교체하세요. 이번 도구는 50분마다 다시 연결했습니다.

## 트랜잭션과 재시도

DSQL은 잠금으로 기다리게 하는 대신 커밋할 때 충돌을 검사합니다(낙관적 동시성 제어). 같은 행을 동시에 바꾼 트랜잭션 가운데 하나는 커밋에서 `40001`로 실패하므로, 재시도가 없으면 그 요청은 실패로 끝납니다. 이 방식 덕분에 PostgreSQL의 REPEATABLE READ가 허용하는 write skew도 DSQL에서는 `40001`로 막혔습니다(E004).

```python
import random, time, psycopg

# 예시: reconnect()는 위 connect()로 새 연결을 돌려주는 함수라고 가정한다.
def run_with_retry(conn, work, op_id, max_attempts=3, deadline_s=2.0, base_s=0.01):
    """work(conn)은 한 트랜잭션 안에서 업무 쓰기와 영수증(op_id) 쓰기를 함께 한다."""
    t0 = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            with conn.transaction():
                if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                    return "duplicate"                 # 이미 반영된 업무: 다시 하지 않는다
                work(conn)
            return "committed"
        except psycopg.errors.SerializationFailure:     # 40001: 커밋 시점 충돌
            delay = random.uniform(0, base_s * 2 ** (attempt - 1))
            if attempt == max_attempts or time.monotonic() - t0 + delay >= deadline_s:
                raise
            time.sleep(delay)
        except psycopg.OperationalError:                # 커밋 중 연결이 끊기면 결과가 불명확하다
            conn = reconnect()                          # 새 연결로 영수증을 조회한 뒤에만 재시도
            if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                return "committed"
```

- 영수증 테이블은 `op_id text PRIMARY KEY`로 두고, 업무 쓰기와 같은 트랜잭션에서 넣습니다. 이 방식으로 E004의 경합 66셀과 E006의 30초 연결 차단에서 커밋 유실·중복이 0건이었습니다.
- 대량 변경은 대상 ID를 한 번 조회한 뒤 3,000행 미만으로 나눠 각각 커밋합니다. 배치마다 대상 조회를 다시 하면 매우 느려집니다(E002).

```python
ids = [r[0] for r in conn.execute("SELECT id FROM orders WHERE created_at < %s", (cutoff,)).fetchall()]
for i in range(0, len(ids), 2000):
    with conn.transaction():
        conn.execute("DELETE FROM orders WHERE id = ANY(%s)", (ids[i:i + 2000],))
```

## 스키마와 인덱스

- **ID 생성:** `serial` 대신 identity 또는 sequence를 `CACHE 65536`으로 선언하거나, 애플리케이션에서 UUID 등을 만듭니다.
- **인덱스 생성:** `CREATE INDEX ASYNC idx ON t (col)`은 곧바로 돌아오고 빌드는 뒤에서 진행됩니다. 배포 스크립트는 다음처럼 완료를 기다려야 합니다.

```sql
SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'idx';
```

- **빌드 중 영향:** 1,100만 행 인덱스 빌드(12분) 동안 초당 1,000건 부하의 쓰기 p99가 30.7 ms에서 79.8 ms로 늘었지만 실패는 없었습니다(E008).
- **외래 키:** 외래 키 제약은 동작하지만 참조하는 쪽 열의 인덱스는 자동으로 생기지 않습니다. 부모 행을 지우거나 조인하는 열에는 인덱스를 직접 만드세요.
- **열 변경:** `ALTER TABLE ... ADD COLUMN`/`DROP COLUMN`은 부하 중에도 0.1초 안에 끝났습니다.
- **지원되지 않아 설계를 바꿔야 하는 것:** PL/pgSQL 함수, 트리거, 임시 테이블, 파티션. GIN·표현식 인덱스의 대체 방법은 검증하지 않았습니다(E001).

## 쿼리와 성능

- **지연:** 쿼리 한 번의 지연이 Aurora보다 깁니다(쓰기 p95 약 28–41 ms, 읽기 약 5–10 ms, 서울, E002). 한 요청에서 쿼리를 여러 번 순서대로 실행하지 말고, 조인이나 한 트랜잭션으로 줄이세요.
- **확장:** 연결과 동시성을 늘리면 처리량이 거의 비례해 늘었습니다(연결 64에서 6,680 TPS, 256에서 24,541 TPS, 지연 거의 동일). 처리량이 필요하면 동시성을 늘리세요.
- **최신성:** 커밋한 데이터는 어느 연결에서나 바로 보였습니다(E005). 쓰기 직후 읽기를 특정 노드로 보내는 라우팅은 필요 없습니다.
- **큰 집계:** 처리한 행 수에 비례해 느려지고 DPU로 과금됩니다. 300초 한도를 넘는 쿼리는 거절되고 서버 측 취소가 없으므로, 범위를 나눠 실행하세요(E012).

## 운영과 복구

- **생성·삭제:** 클러스터 생성은 32초, 삭제는 약 2분이었습니다(E011). 용량·인스턴스 크기·vacuum 설정은 없습니다.
- **백업:** AWS Backup으로만 백업합니다. 백업 볼트와 서비스 역할(`AWSBackupServiceRolePolicyForBackup`, `AWSBackupServiceRolePolicyForRestores`)을 먼저 만들어야 하고, 계정의 AWS Backup 설정에서 DSQL 사용이 켜져 있어야 합니다. 백업은 매번 전체 백업이며 약 100 MB에 481초가 걸렸습니다(E007).
- **복원:** `start-restore-job`은 항상 새 클러스터를 만듭니다. 기본으로 삭제 보호가 켜지므로 필요하면 메타데이터 `regionalConfig`의 `isDeletionProtectionEnabled`를 지정하세요. 새 클러스터는 엔드포인트가 달라 애플리케이션 설정과 IAM 권한을 바꿔야 합니다. 복원은 129초였습니다.
- **시점 복원 없음:** 복구 가능한 최신 시점은 마지막 백업 시각입니다. 허용 가능한 데이터 손실 시간에 맞춰 백업 주기를 정하세요.
- **자동화 주의:** 존재하지 않는 백업 볼트를 조회하면 `AccessDeniedException`이 돌아왔습니다. 볼트가 없다는 뜻으로 처리해야 합니다.
- **관측:** 비용과 사용량은 CloudWatch `AWS/AuroraDSQL`의 `TotalDPU`로 확인했습니다. 분 단위 합계입니다.

## 비용 추정

- **요금 구조(서울, 2026-09-11 게시본):** 백만 DPU당 $10, 스토리지 GB-월당 $0.40, 매월 10만 DPU와 1 GB-월 무료.
- **요청당 DPU:** 이번 주문 업무에서 요청 한 번에 0.029–0.032 DPU(백만 건당 약 $0.31)였습니다. 쿼리 모양에 따라 크게 다르므로 실제 업무로 측정하세요.
- **간단 계산:** 시간당 비용 ≈ 평균 TPS × 3,600 × 요청당 DPU × $10 / 100만. 이번 업무에서 RDS Multi-AZ(`db.r6g.xlarge`, 시간당 약 $1.22)와의 손익분기는 평균 약 1,100 TPS였습니다(E010).
- **적재:** 약 5 GiB 적재에 약 52만 DPU(약 $5.2)가 들었습니다(E002).

## 도입 전 점검표

- [ ] 모든 쓰기 경로에 `40001` 재시도와 업무 ID 영수증이 있다.
- [ ] 커밋 응답을 못 받은 요청을 영수증으로 확인한 뒤에만 재시도한다.
- [ ] 3,000행·300초를 넘는 작업이 없다(배치, 이관, 정리 작업 포함).
- [ ] 격리 수준 지정, `SELECT FOR UPDATE` 대기, `statement_timeout`에 의존하는 코드가 없다.
- [ ] `serial`, PL/pgSQL, 트리거, 임시 테이블, 파티션을 쓰지 않는다.
- [ ] 인덱스는 `CREATE INDEX ASYNC` 후 `indisvalid`를 확인하고, 외래 키 쪽 인덱스를 만들었다.
- [ ] 연결 풀이 있고, 연결 수명이 1시간보다 짧고, 토큰을 캐시하며 동시 서명을 막는다.
- [ ] 소수 행에 쓰기가 몰리는 곳이 없거나 분산 설계가 되어 있다.
- [ ] 백업 주기와 새 클러스터로 전환하는 복구 절차가 문서화되어 있다.
- [ ] 실제 업무로 요청당 DPU를 측정하고 고정 인스턴스 비용과 비교했다.

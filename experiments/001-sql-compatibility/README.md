# 001 — SQL 호환성과 실행 가능성

> 상태: completed (2026-09-24 실측 · 잔여 리소스 0 확인)

## 질문과 가설

- 확인할 질문: 기존 PostgreSQL 업무 SQL을 D1(DSQL)에서 R1/A1/A2와 같은 결과·정합성으로 실행할 수 있는가? 무엇을 바꿔야 하는가?
- 가설: 일부 DDL·트랜잭션·잠금 동작은 수정이 필요하다. DSQL 지원 범위는 계속 바뀌므로 코드에 지원 여부를 가정하지 않고 실제 실행 결과로 판정한다.
- 판단 기준: 항목별 `pass`(문법 수용 + 의미 검증 통과) / `semantic_mismatch`(수용됐지만 결과·불변식 불일치) / `unsupported`(SQLSTATE 0A000) / `rejected_needs_review`(42601·42883·42704·42809, 사람이 확인) / `inconclusive_infra`·`inconclusive_auth`(접속·인증 문제, 호환성 결과 아님) / `harness_error`(도구 버그) / `error`(기타 SQL 오류).

## 비교 조건 (계획 대비 축소 — 명시적 편차)

SQL 기능 검사는 처리량·HA와 무관하므로 소형 구성을 사용한다. **이 결과로 운영 HA·성능이 계획 구성과 같다고 주장하지 않는다.**

| ID | 이번 구성 | 계획 구성과의 차이 |
| --- | --- | --- |
| D1 | DSQL 단일 리전 클러스터, `admin` IAM 토큰, `verify-full` TLS | 없음(기능 검사 전용) |
| R1-lite | RDS PostgreSQL 16, `db.t4g.micro`→`small`→`m7g.large` 중 주문 가능한 첫 클래스, gp3 20 GiB, 기본 Single-AZ(`--multi-az`로 standby) | 클래스·스토리지·(기본) standby 없음 |
| A1-lite | Aurora PostgreSQL 16 provisioned writer 1대, `db.t4g.medium`→`r7g.large`→`r6g.large` | reader 없음, 소형 클래스 |
| A2-lite | Aurora Serverless v2 writer 1대, 0.5–2 ACU | reader 없음, ACU 범위 축소 |

- 엔진 버전: `discover`가 `describe_db_engine_versions`로 16.x를 조회해 RDS·Aurora 공통 최신 minor를 우선 선택하고 `describe_orderable_db_instance_options`로 클래스 조합을 확인한다. 결과는 `artifacts/<prefix>/discovery.json`.
- 접속: 로컬 PC → 공개 엔드포인트. **기능 검사 전용이며 지연·TPS 근거로 쓰지 않는다.**
- 격리 수준: 각 서비스 기본값을 기록한다. `isolation_begin_*`은 `BEGIN ISOLATION LEVEL X` 수용·실행(단순 SELECT)을 판정하고, `SHOW transaction_isolation`은 관측 전용으로 값이 있으면 `observed_isolation`에 기록, 미지원이면 `isolation_observation=unavailable:<SQLSTATE>`로 남긴다(수준을 단정하지 않음). 기존 `isolation_*`(`BEGIN` 후 `SET TRANSACTION`)은 "SET TRANSACTION isolation syntax" 문법 경로 항목으로, 그 실패(예: DSQL `0A000`)는 격리 수준 자체의 미지원을 뜻하지 않는다. `rr_lost_update`는 `BEGIN ISOLATION LEVEL REPEATABLE READ`를 사용한다.
- ORM 스키마 변경·논리 복제/CDC: 선정된 요구가 없어 **미측정**으로 결과에 명시한다.

## 검사 항목 (`sqlcases.py`)

각 항목은 전용 테이블·새 연결·autocommit + 명시적 `BEGIN/COMMIT`으로 격리되어, 한 항목의 미지원 문장이 다른 항목에 영향을 주지 않는다. 시작 전·후 `DROP ... IF EXISTS`로 재실행 가능하다. `variant_of`는 원본이 실패할 때 쓸 수 있는 수정안, `depends_on`은 선행 기능 의존을 뜻한다.

PK/UNIQUE/CHECK, FK(위반 거부까지 검사), sequence(+CACHE 65536 변형), identity(+CACHE 변형), serial, UPSERT, JOIN/CTE/window, recursive CTE, JSONB 런타임/저장 컬럼(+text 캐스트 변형), GIN·B-tree·표현식 인덱스(+`CREATE INDEX ASYNC` 변형), 임시 테이블(세션 간 비가시성), 범위 파티션(라우팅·범위 밖 거부), SQL/PL/pgSQL 함수, 트리거, `SELECT FOR UPDATE`(두 연결의 차단/낙관적 충돌 동작 관측 + lost update 불변식), 격리 수준 3종, REPEATABLE READ lost update, `statement_timeout`(pg_sleep·CPU 변형), 클라이언트 취소, 공통 주문 트랜잭션(FK 원본 / FK 없는 이식 변형: 조건부 재고 차감, 품절·중복 업무 ID 거절, 재고 음수 0·재고 변동=판매량·영수증=주문·주문 합계 검증).

모든 문장에는 클라이언트 측 30초 기한(초과 시 cancel)이 있다. 충돌(40001)은 주문 트랜잭션에서만 최대 3회 재시도한다.

## 재현 절차

전제: Python 3.10+, AWS 프로필 `roboco`, 리전 `ap-northeast-2`. 아래 `<ACCOUNT>`는 사람이 확인한 12자리 계정 ID이며 모든 AWS 명령이 STS 신원과 대조한다.

```bash
cd experiments/001-sql-compatibility
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests              # 오프라인 테스트(AWS 호출 없음)
curl -fsSo artifacts-rds-ca.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem  # RDS CA(공개)
MYIP=$(curl -fsS https://checkip.amazonaws.com)/32  # 본인 공인 IPv4 /32 확인 후 사용

PREFIX=$(python3 e001.py init --account-id <ACCOUNT> --max-lifetime-minutes 180 | tail -1)
python3 e001.py discover --account-id <ACCOUNT> --prefix $PREFIX   # 읽기 전용

# 한 구성씩: 생성 → 실행 → 즉시 삭제 → 검증 (cycle은 오류 시에도 finally로 삭제·검증)
python3 e001.py cycle --account-id <ACCOUNT> --prefix $PREFIX --config D1
python3 e001.py cycle --account-id <ACCOUNT> --prefix $PREFIX --config R1 --client-cidr $MYIP --rds-ca-bundle artifacts-rds-ca.pem
python3 e001.py cycle --account-id <ACCOUNT> --prefix $PREFIX --config A1 --client-cidr $MYIP --rds-ca-bundle artifacts-rds-ca.pem
python3 e001.py cycle --account-id <ACCOUNT> --prefix $PREFIX --config A2 --client-cidr $MYIP --rds-ca-bundle artifacts-rds-ca.pem
python3 e001.py summarize --prefix $PREFIX
python3 e001.py verify --account-id <ACCOUNT> --prefix $PREFIX    # 종료 코드 0 = 잔여 0
```

단계별 실행은 `provision` / `run` / `cleanup` / `verify`를 같은 인자로 호출한다. `--case NAME`(반복 가능)으로 일부 항목만 실행한다. `artifacts-rds-ca.pem`은 공개 CA이며 Git에 넣지 않는다(`*.pem` 무시). D1 TLS는 `sslrootcert=system`(libpq 16+)을 사용하며 필요 시 `--dsql-sslrootcert PATH`로 바꾼다.

### 안전장치

- `--account-id` 필수, STS `GetCallerIdentity` 불일치 시 중단. 리전은 `ap-northeast-2` 고정.
- 실행 prefix `e001-<UTC>-<4hex>`와 태그 `e001:run-prefix`·`e001:config`·`e001:expires-at`·`e001:managed-by`를 모든 리소스에 부여. manifest(`artifacts/<prefix>/manifest.json`, 0600)에 **생성 요청 전(RDS) 또는 응답 직후(DSQL/EC2), 대기 전에** ID를 기록.
- 동시에 한 구성만 활성, 순서 D1→R1→A1→A2(`--allow-order-override`로만 변경, 편차로 기록).
- 절대 수명(기본 180분, 최대 240분): 만료 후 `provision`/`run` 거부. 자동 삭제 스케줄러는 만들지 않으므로 **운영자가 cleanup을 반드시 실행**한다.
- 삭제 대상은 manifest에 있고 이 run·config 태그가 정확히 일치하는 리소스뿐이다. 계정 전체 목록 삭제는 하지 않는다. 충돌 등으로 manifest에 빠진 EC2 리소스는 정확한 태그 필터로만 찾아 추가한다.
- 보안 그룹 인바운드는 `--client-cidr`의 공인 IPv4 /32, TCP 5432만 허용(0.0.0.0/0·사설 대역·IPv6 거부). NAT 게이트웨이 없음.
- RDS 비밀번호는 `artifacts/<prefix>/secrets/<cfg>.json`(0600)에만 저장하고 출력하지 않는다. Secrets Manager·액세스 키·약정·구독을 만들지 않는다. 오류 메시지의 호스트·비밀번호는 `<redacted>`로 치환한다.
- 백업: R1 `BackupRetentionPeriod=0`, Aurora는 최소값 1. 삭제는 `SkipFinalSnapshot=True`·`DeleteAutomatedBackups=True`, deletion protection 없음.

## 리소스 목록과 삭제 순서

| 구성 | 생성 리소스 |
| --- | --- |
| D1 | DSQL 클러스터 1 |
| R1 | VPC 1, 서브넷 2(서로 다른 AZ), IGW 1(main route table에 0.0.0.0/0 경로), 보안 그룹 1, DB 서브넷 그룹 1, DB 인스턴스 1 |
| A1/A2 | 위 네트워크 동일 + Aurora DB 클러스터 1 + writer 인스턴스 1 |

삭제 순서: DSQL 클러스터 → DB 인스턴스 → DB 클러스터 → DB 서브넷 그룹 → 보안 그룹 → 서브넷 → IGW(분리 후 삭제) → VPC. 각 단계는 실제 사라짐(NotFound)까지 대기하며, 재실행해도 안전하다(idempotent).

`verify`는 manifest의 모든 ID, 인스턴스·클러스터별 수동/자동 스냅샷과 보존된 자동 백업, 이 prefix 태그의 EC2 리소스를 조회해 `remaining_count`를 계산하고 `artifacts/<prefix>/verify-*.json`에 남긴다. Resource Groups Tagging API 결과는 삭제 직후 지연될 수 있어 검토용으로 별도 표시한다. 수동 교차 확인 예:

```bash
aws --profile roboco --region ap-northeast-2 dsql list-clusters
aws --profile roboco --region ap-northeast-2 rds describe-db-instances --query "DBInstances[?starts_with(DBInstanceIdentifier,'$PREFIX')]"
aws --profile roboco --region ap-northeast-2 rds describe-db-clusters --query "DBClusters[?starts_with(DBClusterIdentifier,'$PREFIX')]"
aws --profile roboco --region ap-northeast-2 ec2 describe-vpcs --filters Name=tag:e001:run-prefix,Values=$PREFIX
```

## 산출물

`artifacts/<prefix>/`(Git 제외): `manifest.json`(리소스·이벤트 시각·엔드포인트), `discovery.json`, `results/<run_id>.json`(항목별 결과·SQLSTATE·단계별 ms·관측값·환경·클라이언트 버전), `summary.md|json`(엔드포인트 없는 결과표), `verify-*.json`, `secrets/`. 공개 보고서에는 검토한 `summary.md`만 옮기고 계정 ID·ARN·엔드포인트·비밀번호는 넣지 않는다. run ID 형식: `YYYYMMDDTHHMMSSZ-E001-<cfg>-r01`.

## 비용 (추정 방법 — 금액 미확정)

전체 12개 실험 공유 예산 USD 50. 단가는 실행 당일 공식 가격 페이지에서 확인해 날짜와 함께 기록한다(여기서 금액을 가정하지 않음).

- R1/A1: `(manifest의 생성 요청~삭제 완료 시간) × 인스턴스 시간당 On-Demand 단가` + 스토리지(GB-월 비례) + Aurora I/O. RDS 최소 과금 단위(10분)를 확인해 반영.
- A2: CloudWatch `ServerlessDatabaseCapacity`로 ACU-시간 적분 × ACU 단가(최소 0.5 ACU 상시 과금).
- D1: DPU 사용량(CloudWatch의 DSQL DPU 지표 — 실행 시 지표명 확인) × DPU 단가 + 저장량. 소량 검사라 저장 비용은 미미할 것으로 보이나 추정 대신 실측으로 기록.
- VPC·서브넷·IGW·보안 그룹은 무료, 공인 IPv4 주소(RDS 퍼블릭 엔드포인트)는 시간당 과금 대상이므로 포함.
- 실제 청구액(2026-09-28 Cost Explorer 확인): RDS·Aurora 합계 약 USD 0.08, DSQL $0(월 무료 사용량 안). 사전 추정 범위(약 $0.06–0.13) 안이다.
- 예상 가동 시간은 구성당 생성 5–20분 + 실행 수 분 + 삭제 5–15분이며, 실제 값은 manifest의 이벤트 시각으로 계산한다.

## 한계

- 로컬 클라이언트 기능 검사로 지연·처리량·HA를 평가하지 않는다. 축소 구성은 계획 구성과 다르다.
- `rejected_needs_review`는 미지원일 가능성이 높지만 SQL 오타 가능성도 있어 사람이 판정한다.
- `SELECT FOR UPDATE`·격리 수준은 두 연결의 단일 시나리오 관측이며, 경합 하 정합성은 E004에서 검증한다.
- 클라이언트 취소가 서버에서 지원되지 않으면 기한 초과 문장은 서버 완료까지 기다릴 수 있다(작업량을 작게 제한함).
- 절대 수명은 도구 수준의 거부·태그이며 AWS가 자동 삭제하지 않는다. 독립 워치독이 없으므로 프로세스·머신이 죽으면 운영자가 `cleanup`/`verify`를 직접 실행해야 한다. 만료는 각 SQL 케이스 시작 전에만 재확인하며, 실행 중인 케이스를 중단하지 않는다.
- 태그 기반 고아 채택(EC2, D1의 DSQL `list_clusters`)은 정확한 run/config/managed-by 태그가 붙은 리소스만 대상으로 한다. 태그가 붙기 전에 실패한 리소스는 찾지 못한다. 태그 인덱스(Resource Groups Tagging API) 항목은 삭제 후에도 남을 수 있으므로 검토용으로만 기록하고, 남은 리소스 수에는 포함하지 않는다.

## 실행 기록

| run ID | 구성 | 실행 시각(UTC) | 코드 커밋 |
| --- | --- | --- | --- |
| `20260924T071705Z-E001-D1-r01` | D1 | 07:17:05–07:17:22 | `dfbbe69` |
| `20260924T073213Z-E001-R1-r01` | R1-lite(db.t4g.micro, Multi-AZ, 16.15) | 07:32:13–07:32:26 | `dfbbe69` |
| `20260924T074125Z-E001-A1-r01` | A1-lite(db.t4g.medium, 16.15) | 07:41:25–07:41:38 | `dfbbe69` |
| `20260924T075455Z-E001-A2-r01` | A2-lite(db.serverless 0.5–2 ACU, 16.15) | 07:54:55–07:55:09 | `dfbbe69` |

prefix `e001-20260924t071645z-d551`. 파일럿 prefix `e001-20260924t071247z-cf9e`(D1 1회, 커밋 `48f0d65`)는 도구 점검용이며 결과 비교에 쓰지 않는다.

편차: R1 프로비저닝 중 오케스트레이터 세션이 중단되어, 인스턴스 `available`(07:31:46Z) 후 manifest에 연결 정보를 보완하고 `run`/`cleanup`/`verify`를 수동으로 이어 실행했다(manifest 이벤트 `provision_done`의 `note`). R1은 `--multi-az`로 생성되었다(CloudTrail 확인). A1·A2는 `cycle`로 실행했다.

## 결과·제약·편의성·결론

검토한 결과표와 해석은 [공개 보고서](../../docs/_experiments/e001.md)에 있다. 요약: D1 35개 중 pass 17 / unsupported 16 / rejected_needs_review 1(`serial`) / semantic_mismatch 1(클라이언트 취소). R1·A1·A2는 DSQL 전용 `CREATE INDEX ASYNC`만 거부(42601)하고 34개 pass. 주문 트랜잭션(FK 포함)은 D1에서도 통과했다. 원본 결과는 `artifacts/<prefix>/results/`, 행렬은 `summary.md`.

## 정리 기록

계정 `roboco` 프로필, 리전 `ap-northeast-2`. 정리 완료(UTC): D1 07:18:46, R1 07:35:56, A1 07:48:12, A2 08:01:18, 삭제 실패 없음. `verify` 08:01:37Z(`d551`)·08:01:38Z(`cf9e`) 모두 `remaining_count=0`, 계정 전체 `e001` 접두어 RDS 인스턴스·클러스터·클러스터 스냅샷, DSQL 클러스터, `e001:run-prefix` 태그 VPC 0개 확인.

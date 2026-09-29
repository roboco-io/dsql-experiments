# 002 — OLTP 처리량과 지연의 한계

> 상태: completed (2026-09-28 네 구성 1차 측정, 2026-09-29 DSQL 단독 2차 측정, 두 실행 모두 잔여 리소스 0 확인). 공통 도착률 본 측정·경계 탐색·반복은 하지 않았다. 결과는 [공개 보고서](https://roboco.io/dsql-experiments/experiments/e002/).

## 질문과 가설

- 확인할 질문: 같은 기본 OLTP 업무와 S 데이터에서 DSQL(D1)의 SLO 용량과 같은 요청률의 p99 지연은 R1·A1·A2의 몇 배인가?
- 가설: DSQL은 분산 커밋 때문에 낮은 요청률에서 고정 지연이 대조군보다 크다. 반면 서비스가 용량을 관리하므로 고정 용량 대조군이 포화되는 요청률에서도 SLO를 더 오래 유지한다.
- 가설의 판단 기준: 0.5 × `Qref`에서 D1의 p99가 대조군보다 크고, D1의 Q(또는 관측 하한)가 R1·A1의 Q보다 크면 가설을 지지한다. 둘 중 하나라도 반대로 관측되면 기각한다.

## 이번 실행의 범위와 계획 대비 편차

계획([EXPERIMENT_PLAN.md#e002](../../EXPERIMENT_PLAN.md#e002))의 구성과 S·기본 OLTP 조건을 따른다. 비용 상한은 **USD 50**(2026-09-26 사용자 지정)이며 비용 가드는 USD 45에서 실행을 멈춘다. 아래 편차는 설계 단계에서 확정한 것이다(2026-09-27 사용자 결정).

| 항목 | 계획 | 이번 실행 | 이유 |
| --- | --- | --- | --- |
| 측정 시간·반복 | 예비 2+5분, 본 10+20분 × 3회 | 파일럿 후 결정. 기본안 3회 × (3분 워밍업 + 10분 수집) | D1 DPU와 A2 ACU 비용을 파일럿 실측 단가로 계산한 뒤 상한 안의 최대 시간을 사용자가 정한다 |
| 경계 탐색 | 경계가 보이지 않으면 25%씩 상향 | 25%씩 상향하되 러너 용량 또는 비용 가드에 닿으면 중단 | 비용. 중단한 서비스의 Q는 `관측 하한`으로 표시한다 |
| 실행 순서 | 시간대·실행 순서를 바꾼다 | 네 구성을 **병렬**로 같은 시간대에 측정하고, 반복마다 도착률 순서만 섞는다 | `Qref`가 네 구성 예비 결과의 최솟값이라 순차 실행은 재적재 또는 유휴 비용이 든다 |
| 부하 발생기 | 비버스터블 8 vCPU EC2 | 구성당 `c7g.4xlarge` **Spot** 1대(발생기 포화 시 추가) | 사용자 지정: Spot 우선. E004에서 `c7g.2xlarge`는 동시성 256에서 포화됐다 |

## 비교 조건

| 항목 | D1 | R1 | A1 | A2 |
| --- | --- | --- | --- | --- |
| 서비스 / 엔진 | Aurora DSQL 단일 리전 | RDS for PostgreSQL 16 | Aurora PostgreSQL 16 | Aurora PostgreSQL 16 Serverless v2 |
| 배포 | 서울, 서비스 관리 | Multi-AZ DB 인스턴스(standby 1) | writer 1 + 다른 AZ reader 1 | writer 1 + 다른 AZ reader 1(promotion tier 1) |
| 용량 | 서비스 관리(DPU 과금) | `db.r6g.xlarge` | `db.r6g.xlarge` × 2 | 각 4–32 ACU |
| 스토리지 | 서비스 관리 | gp3 400 GiB, 12,000 IOPS, 500 MiB/s | Aurora Standard | Aurora Standard |
| 요청 대상 | 클러스터 엔드포인트 | writer | writer 엔드포인트 | writer 엔드포인트 |
| 인증 | IAM 토큰 | 비밀번호(RDS 관리 secret) | 같음 | 같음 |

- 엔진 버전은 R1·A1·A2가 모두 제공하는 PostgreSQL 16의 최신 공통 minor를 실행 직전에 조회해 고정한다.
- 공통: 같은 데이터와 시드, 기본 OLTP 혼합, 명시적 `REPEATABLE READ`, 재시도 정책, 연결 풀 총 256, 같은 러너 사양·이미지·클라이언트 버전을 쓴다.
- 통제하지 못한 차이: DSQL은 퍼블릭 엔드포인트이고 대조군은 VPC 내부 경로다(RTT를 셀마다 기록한다). 인증 방식이 다르다. A1·A2의 reader는 요청을 받지 않지만 HA 비용 비교를 위해 띄운다.

## 설계

### 구성 요소

E004 하네스(`experiments/004-transaction-contention/`)를 복사해 확장한다. 실험 디렉터리마다 독립적으로 재현하기 위해서이며, 이미 실측한 E004 코드는 바꾸지 않는다.

| 모듈 | 출처 | 역할 |
| --- | --- | --- |
| `safety.py` | E004 복사, E002 태그·구성 | manifest, 태그, 최대 수명, 삭제 순서, 잔여 검증 |
| `cost.py` | E004 복사, 확장 | 병렬 누적 비용 가드(A2 ACU·D1 DPU·A1/A2 I/O는 CloudWatch 실측) |
| `infra.py`, `remote.py` | E004 복사 | DB·Spot 러너 생성, SSM 실행과 결과 회수 |
| `retry.py`, `conn.py`, `hist.py` | E004 복사 | 재시도, 서비스별 연결·인증, 지연 히스토그램 |
| `schema.py`, `datagen.py` | 신규 | S 데이터 스키마와 고정 시드 생성·병렬 적재(DSQL은 트랜잭션당 3,000행 미만) |
| `workload.py` | 신규 | 기본 OLTP 4개 업무와 혼합 비율 |
| `openloop.py` | 신규 | 포아송 도착 스케줄, 다중 프로세스 발행, skipped 판정, 지연 분해 |
| `slo.py` | 신규 | 셀 SLO 판정, Q와 `Qref` 선택 |
| `invariants.py` | E004 복사, 조정 | 셀 종료 후 불변식 검사 |
| `e002.py` | 신규 | CLI(`init`, `batch-up`, `load`, `pilot`, `explore`, `measure`, `summarize`, `batch-down`, `verify`) |

### 데이터와 업무

- 스키마는 계획의 8개 테이블이다. E001에서 검증한 공통 SQL로 PK, FK, 고유 제약, 재고 CHECK, 고객별 주문 검색용 복합 인덱스를 만든다. 금액은 정수 최소 단위로 저장한다.
- 행 수는 로컬 PostgreSQL 16에서 고정 시드로 생성해 테이블과 인덱스가 약 5 GiB가 되는 값으로 정하고, 모든 서비스에 같은 행 수를 적재한다. 서비스별 물리 크기는 측정값으로만 기록한다.
  - 보정 결과(2026-09-28, 로컬 PostgreSQL 16, `calibrate.py`로 2% 적재 후 외삽): 테넌트 100, 고객 1,000,000, 상품·재고 200,000, 주문 11,000,000(고객당 11), 주문 항목 약 27,500,000(주문당 1–4줄), 원장 11,000,000, 합계 약 5.10 GiB. 영수증은 측정 중에만 생긴다.
- 업무 혼합(요청 수 기준): 상품 조회 40%, 주문 이력(고객별 범위 조회·JOIN·정렬, 페이지 20건) 30%, 주문 생성(재고 조건부 차감 + 주문·항목·영수증) 20%, 취소(상태 검증 후 변경·재고 반환) 10%. 접근은 균등 분포다.
- 재고와 취소 대상은 측정 중 고갈되지 않게 생성한다.
- 반복 사이의 초기화: 측정 중 생성된 주문·항목·영수증을 삭제하고 변경된 재고만 원래 값으로 되돌린다. 전체 재적재는 하지 않는다.
- 재시도: 최대 3회 시도, 총 마감 2초, 지수 backoff와 full jitter(E004와 같음). 커밋 여부가 불명확하면 영수증을 조회해 판정한다.

### open-loop 부하 발생기

- 요청 예정 시각을 목표 도착률의 포아송 과정(지수 간격)으로 미리 생성한다. DB가 느려져도 예정 요청 수를 줄이지 않는다.
- 러너는 여러 프로세스에 부하를 나누고, 각 프로세스는 asyncio와 psycopg3 연결 풀을 쓴다. 연결 풀 총 크기는 모든 서비스에서 256으로 고정한다.
- 지연은 예정 시각부터 응답까지다. 풀 대기, 실행, 재시도 대기를 나눠 기록한다.
- 예정 시각에서 2초 안에 발행하지 못한 요청은 `skipped`로 기록하고 실패로 센다. 2초 마감을 넘긴 요청도 실패다.
- 발생기 포화: 러너 CPU 85% 초과 또는 발행 지연(schedule lag) p99 10 ms 초과면 셀을 무효로 표시하고 러너를 늘려 다시 측정한다.

### 측정 절차

1. **예비 탐색:** 고정 동시성 16/64/256으로 각 서비스의 SLO를 만족한 최대 성공 TPS를 구한다. 네 값 중 최솟값을 `Qref`로 고정한다. 예비값으로 우열을 판단하지 않는다.
2. **본 측정:** 모든 서비스에 같은 0.5/1.0/1.2 × `Qref` 도착률을 넣는다. 필요하면 0.8배를 추가한다.
3. **경계 탐색:** 1.2 × `Qref`에서도 SLO를 만족하는 서비스는 도착률을 25%씩 올리고 경계 주변을 좁힌다. 러너 용량 또는 비용 가드에 닿으면 멈추고 그 서비스의 Q를 `관측 하한`으로 표시한다.
4. **셀별 SLO 판정:** 상품 조회·주문 이력은 각각 p95 50 ms 이하·p99 100 ms 이하, 쓰기 업무는 p95 100 ms 이하·p99 200 ms 이하, 최종 기술 실패율(skipped·timeout 포함)은 0.1% 이하. 업무 거절은 따로 보고한다.
5. **Q 채택:** 모든 반복에서 SLO를 통과한 최대 도착률이다. 반복이 3회 미만이면 `잠정`으로 표시한다. 반복 간 편차가 10%를 넘으면 원인을 점검하고, 예산이 허락하면 2회를 추가한다.
6. **정합성:** 셀마다 불변식(재고 음수 0건, 확정 주문 수량과 재고 변동 일치, 업무 ID별 효과 1회, 중단 거래의 부분 반영 0건)을 검사한다. 위반이 있는 셀은 성능 판정에 쓰지 않는다.

### 측정 지표

요청 유형별 지연 히스토그램과 p50/p95/p99/최소/최대, 표본 수, 성공 TPS·시도 TPS, SQLSTATE별 오류, skipped 수, 셀별 RTT, 러너 CPU와 schedule lag. CloudWatch의 CPU, 연결 수, IOPS, A1·A2 I/O 횟수, A2 ACU(writer·reader 각각), D1 DPU. 대조군은 `pg_stat_activity` 대기 이벤트를 샘플링한다. 경계 셀마다 CPU·I/O·잠금·풀 대기·발생기 중 먼저 한계에 닿은 것을 포화 원인으로 기록한다.

## 실행 구조

네 구성과 구성당 Spot 러너 1대를 함께 띄우고 병렬로 진행한다.

1. `batch-up`: 네 구성과 러너를 생성하고 manifest에 기록한다.
2. `load`: 구성별로 같은 S 데이터를 적재하고 물리 크기와 적재 시간(D1은 DPU)을 기록한다.
3. `pilot`: D1·A2에서 짧은 셀로 시도당 DPU와 도착률별 ACU를 잰다. 비용 추정을 출력하고 사용자 결정을 받는다.
4. `explore`: 네 구성의 예비 탐색을 병렬로 실행하고 `Qref`를 확정한다.
5. `measure`: 본 측정과 경계 탐색을 병렬로 실행한다.
6. `summarize`, `batch-down`, `verify`: 결과를 저장한 뒤 모든 리소스를 삭제하고 잔여 0개를 확인한다.

실패나 중단이 나면 즉시 `batch-down`과 `verify`를 실행한다.

## 비용 가드

- 상한 USD 50, 가드 USD 45. 네 구성의 누적 비용을 셀마다 다시 계산하고 가드를 넘으면 새 셀을 시작하지 않는다.
- 2026-09-28 사용자 결정으로 E002 상한을 USD 60(가드 USD 54, `--budget-cap 54`)으로 올렸다. 파일럿과 두 차례 Spot 회수·인덱스 보강으로 누적 추정이 USD 28.3에 이르러, 본 측정 1회 × (워밍업 120 s + 측정 300 s)와 경계 탐색을 USD 45 안에서 마칠 수 없었기 때문이다.
- 이미 전체 적재 시간을 잰 구성을 다시 적재할 때는 가드 예약을 그 시간의 3배로 잡는다(`LOAD_REPEAT_FACTOR`). 파일럿 후 다시 만든 A2는 처음 적재에 552 s가 걸렸다.
- 단가(2026-09-27 AWS Price List API, 서울, On-Demand, USD): RDS PostgreSQL `db.r6g.xlarge` Multi-AZ $1.079/h; Aurora PostgreSQL `db.r6g.xlarge` Standard $0.627/h; Aurora Serverless v2 Standard $0.20/ACU-h; Aurora Standard I/O $0.24/백만 I/O; Aurora 스토리지 $0.12/GB-월. DSQL은 백만 DPU당 $10(E004에서 확인). gp3 스토리지·IOPS와 EC2 Spot 단가는 실행 직전에 조회한다.
- 사전 추정(가동 5시간 가정, D1 DPU·I/O 제외): R1 약 $5.4, A1 약 $6.3, A2 $8–64(ACU에 따름). 이 추정은 파일럿 전 값이며, 파일럿 결과로 측정 시간과 반복을 다시 정한다.

## 리소스 목록과 삭제 순서

E004와 같은 방식이다. 모든 리소스에 `Experiment=E002` 태그를 달고 manifest에 기록한다. 최대 수명을 넘으면 새 작업을 거부한다. 삭제 순서는 러너 → DB 인스턴스 → 클러스터 → DSQL 클러스터 → 서브넷 그룹·파라미터 그룹·보안 그룹 → IAM 인스턴스 프로파일이다. 태그 조회와 manifest 양쪽으로 잔여 0개를 확인한다.

## 테스트

- 오프라인 단위 테스트: 도착 간격 분포, skipped와 마감 판정, 업무 혼합 비율, SLO 판정, Q·`Qref` 선택, 비용 계산, manifest 상태 전이.
- 통합 테스트: Docker의 PostgreSQL 16에서 소규모 적재와 open-loop 실행.

## 재현 절차

작업 디렉터리는 `experiments/002-oltp-throughput/`이다. `ACCT`는 실행 직전에 사용자에게 확인받은 계정 ID이고, `PFX`는 `init`이 출력하는 실행 접두사다. 둘 다 저장소에 기록하지 않는다.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
A="--account-id $ACCT"
.venv/bin/python e002.py init $A --max-lifetime-minutes 900          # PFX 출력
.venv/bin/python e002.py discover $A --prefix $PFX
.venv/bin/python e002.py batch-up $A --prefix $PFX --configs D1,A2
.venv/bin/python e002.py pilot $A --prefix $PFX                      # 결정 지점: 비용 추정을 보고 반복·시간 결정
.venv/bin/python e002.py batch-up $A --prefix $PFX --configs D1,R1,A1,A2
.venv/bin/python e002.py load $A --prefix $PFX --configs R1,A1,A2
.venv/bin/python e002.py explore $A --prefix $PFX                    # Qref 확정(qref.json)
.venv/bin/python e002.py measure $A --prefix $PFX --reps N --warmup-s W --measure-s M
.venv/bin/python e002.py metrics $A --prefix $PFX                    # 셀별 CloudWatch 지표 첨부(삭제 후에도 15일 동안 가능)
.venv/bin/python e002.py summarize --prefix $PFX
.venv/bin/python e002.py batch-down $A --prefix $PFX                 # 삭제 후 잔여 검증까지 수행
.venv/bin/python e002.py verify $A --prefix $PFX                     # remaining_count=0 확인
```

- `pilot`은 D1에 S 데이터의 2%를 먼저 적재해 전체 적재 DPU를 추정하고, 가드를 넘을 것으로 추정되면 전체 적재를 거부한다. 파일럿이 끝나면 A2를 삭제하고 D1 러너를 종료한다. D1 클러스터는 데이터를 유지한 채 결정을 기다린다.
- 각 단계는 끝난 작업을 건너뛰므로, 실패한 단계는 원인을 고친 뒤 같은 명령으로 다시 실행한다. Spot 러너를 잃은 구성은 `replace-runner --config C`로 교체한다.
- 성공, 실패, 중단과 관계없이 실험을 멈추면 즉시 `batch-down`과 `verify`를 실행한다. `status`는 현재 추정 비용과 남은 수명을 보여 준다.

### 검증

```bash
.venv/bin/python -m unittest discover -s tests -v                                  # 오프라인
docker run -d --rm --name e002-pg -e POSTGRES_PASSWORD=e002 -p 55433:5432 postgres:16 -c max_connections=600
E002_PG_DSN=postgresql://postgres:e002@localhost:55433/postgres .venv/bin/python -m unittest tests.test_pg_integration -v
.venv/bin/python rehearse.py --dsn postgresql://postgres:e002@localhost:55433/postgres --slo-factor 20
```

`rehearse.py`는 AWS 없이 스키마 생성, 적재, 예비 탐색, `Qref`, 본 측정 셀, Q 선택까지 같은 러너 코드로 실행한다. 노트북에서는 부하 발생기와 DB가 CPU를 나눠 쓰므로 SLO와 발행 지연 한도를 `--slo-factor`배로 완화한다. 리허설 수치는 결과로 쓰지 않는다.

## 실행 기록

측정 전이다.

## 성능 결과

미측정.

## 제약과 동작

미측정.

## 개발·운영 편의성

미측정.

## 비용

미측정. 실제 청구액은 실행 후 Cost Explorer로 확인한다.

## 결론과 한계

미측정.

## 정리 기록

실행 전이다.

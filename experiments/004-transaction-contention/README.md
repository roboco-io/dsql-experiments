# 004 — 트랜잭션 경합과 데이터 정합성

> 상태: completed (2026-09-26 실측 · 잔여 리소스 0 확인)

## 질문과 가설

- 확인할 질문: 인기 상품과 동시 갱신 경합에서 D1(DSQL)의 OCC 충돌·재시도는 R1/A1/A2의 잠금 대기·오류와 비교해 정합성, 재시도 포함 지연·TPS, 구현량에 어떤 차이를 만드는가? 업무 불변식은 지켜지는가?
- 가설: D1은 잠금 대기 대신 커밋 시점 충돌(SQLSTATE 40001)로 경합을 드러내므로 집중 부하·고동시성에서 원시 충돌률이 대조군보다 높고, 재시도 없이는 최종 실패율이 크게 오른다. 최대 3회 재시도를 적용하면 최종 실패율은 낮아지지만 재시도 지연이 p99에 반영된다. 대조군은 잠금 대기로 지연이 늘고 교착(40P01)이 일부 발생한다. 올바르게 구현하면 모든 구성에서 불변식 위반은 0건이다.
- 가설의 판단 기준: 셀별 원시 충돌률·최종 실패율·p50/p95/p99·성공 TPS를 D1과 각 대조군에 대해 같은 분포·동시성·재시도 조건으로 비교한다. 불변식 위반이 1건이라도 있는 조건에서는 성능 우위를 주장하지 않는다.

## 이번 실행의 범위와 계획 대비 편차

계획([EXPERIMENT_PLAN.md#e004](../../EXPERIMENT_PLAN.md#e004))의 **조건 전체**(barrier 시나리오 × 격리 수준 3종, 균등/집중 × 동시성 16/64/256 × 재시도 없음/최대 3회)를 실행한다. 설계 당시 비용 상한은 **USD 5**(2026-09-25 사용자 지정)였고, 이 상한에 맞추려고 구성과 시간을 축소했다. D1 파일럿(2026-09-25 22:17–22:31 UTC)에서 DSQL은 시도 1회당 약 0.095 DPU를 소모했고, 서울 리전 DPU 단가는 백만 DPU당 USD 10이었다. 이 값으로 계산하면 계획 행렬 전체의 D1 비용만 약 USD 16.5가 된다. 그래서 사용자 결정에 따라 반복 횟수와 셀 시간을 추가로 줄였다. 이후 실험별 상한은 **USD 50**으로 올랐지만(2026-09-26), 사용자는 축소한 실행 조건을 그대로 유지하기로 했다. 아래 표의 항목은 모두 계획 대비 명시적 편차다.

| 항목 | 계획 | 이번 실행 | 이유·영향 |
| --- | --- | --- | --- |
| R1 | `db.r6g.xlarge` Multi-AZ, gp3 400 GiB | `db.t4g.medium` Multi-AZ, gp3 20 GiB | 비용. 버스터블 CPU라 절대 TPS·지연은 계획 구성과 다르다 |
| A1 | `db.r6g.xlarge` writer + reader, Standard | `db.t4g.medium` writer 1, **I/O-Optimized** | 비용. 요청량 비례 I/O 과금을 없애 비용을 가동 시간에 묶는다 |
| A2 | 4–32 ACU writer + reader, Standard | 0.5–4 ACU writer 1, **I/O-Optimized** | 위와 같음. 최대 ACU가 작아 고부하에서 용량 한계가 먼저 나타날 수 있다 |
| 부하 발생기 | 비버스터블 8 vCPU EC2 | `c7g.4xlarge` **Spot**(16 vCPU, 용량 부족 시 On-Demand 대체·편차 기록). 파일럿은 `c7g.2xlarge`로 실행 | 비용(사용자 지정: Spot 우선). 파일럿에서 `c7g.2xlarge`는 동시성 256일 때 CPU 사용률이 89–99%까지 올라 포화됐다 |
| 측정 시간 | 예비 2+5분, 본 10+20분 | 셀당 워밍업 5초 + 측정 20초, **반복 1회** | D1 DPU 비용 때문이다(사용자 결정, 2026-09-26). 반복이 1회뿐이라 편차를 보고할 수 없다. 측정 구간이 짧아서 캐시와 ACU가 안정되기 전 구간이 결과에 섞일 수 있다 |
| 데이터 | S(약 5 GiB) | E004 전용 소형 데이터(상품 10,000·계정 1,000) | 경합 관측이 목적이며 데이터 크기 효과는 E002/E008 범위 |
| 부하 방식 | open-loop(E002) | 고정 동시성 closed-loop | 계획의 "고정 동시성 탐색" 단계에 해당 |

**이 결과로 계획 구성의 절대 TPS·지연을 주장하지 않는다.** 정합성, 충돌률·재시도 효과의 방향, 서비스별 경합 동작을 비교하는 근거로 쓰며, 계획 규모의 성능 비교는 E002에서 한다.

## 비교 조건

| 항목 | D1 | R1 / A1 / A2 (대조군) |
| --- | --- | --- |
| 서비스 / 엔진 / 버전 | Aurora DSQL 단일 리전 | RDS PostgreSQL 16 / Aurora PostgreSQL 16(공통 최신 minor를 `discover`로 선택·기록) |
| 리전 / AZ / 배포 | `ap-northeast-2`, 서비스 관리 | R1 Multi-AZ 인스턴스, A1/A2 writer 1(단일 AZ) |
| 용량 | 서비스 관리(DPU 과금) | R1/A1 `db.t4g.medium`, A2 0.5–4 ACU |
| 스토리지 | 서비스 관리 | R1 gp3 20 GiB, A1/A2 Aurora I/O-Optimized |
| 네트워크 | 퍼블릭 엔드포인트(IAM 인증) | **비공개** 엔드포인트, EC2 보안 그룹에서 5432만 허용 |
| 인증 | IAM 토큰(`dsql:DbConnectAdmin`, EC2 인스턴스 프로파일) | 마스터 비밀번호(RDS 관리 Secrets Manager 시크릿) |
| 부하 도구 / 발생기 | Python 3 + psycopg 3(async), 같은 EC2·같은 코드 | 같음 |
| 업무 / 비율 | 주문 생성 70% / 계정 이체 30% | 같음 |
| 동시성 / 분포 | 16/64/256 × 균등/집중(상위 1% 키에 80%) | 같음 |
| 격리 수준 | REPEATABLE READ(공통·기본값) | 공통 REPEATABLE READ + 기본값 READ COMMITTED |
| 재시도 | 없음 / 최대 3회 시도·총 마감 2초·지수 backoff(10 ms, ×2)+full jitter | 같음 |

통제하지 못한 차이: DSQL은 퍼블릭 엔드포인트, 대조군은 VPC 내부 경로다(RTT를 셀마다 기록). 인증 방식이 다르다. `t4g` 버스터블 CPU 크레딧 상태, A2의 ACU 확장 속도는 통제하지 않고 기록만 한다.

## 설계

### 구성 요소

| 파일 | 역할 |
| --- | --- |
| `e004.py` | CLI: `init`/`discover`/`provision`/`run`/`pilot`/`cleanup`/`verify`/`cycle`/`summarize` |
| `safety.py` | E001에서 복사·조정. 계정 확인, prefix·태그(`e004:*`), manifest, 수명, 정리 계획 |
| `infra.py` | 네트워크·IAM·Spot EC2·DB 생성/삭제, SSM 명령 실행·청크 전송/회수 |
| `schema.py` | 스키마·결정적 시드 데이터·초기 상태 복원 SQL |
| `scenarios.py` | barrier로 순서를 고정한 두 연결 시나리오 |
| `load.py` | 고정 동시성 경합 부하(다중 프로세스 × asyncio 연결) |
| `retry.py` | 재시도 정책·커밋 불명확 처리 |
| `invariants.py` | 업무 불변식 검사 |
| `cost.py` | 시간 비용 + 요청량 비용 추정·가드 |
| `tests/` | 오프라인 단위 테스트(AWS 호출 없음) |

### 실행 구조

- 로컬 PC가 AWS 리소스 생성·삭제와 SSM 명령을 조율한다. 시나리오·부하는 같은 VPC의 Spot EC2 1대에서 실행하며, 배치 전체(D1→R1→A1→A2)에 같은 EC2를 쓴다.
- EC2 보안 그룹에는 **인바운드 규칙이 없다.** 키 페어를 만들지 않고 SSM `SendCommand`(`AWS-RunShellScript`)로 실행하며, 디버깅 시에만 `start-session`을 쓴다. EC2는 공인 IP로 IGW를 거쳐 SSM·pip에 접속한다. NAT·VPC 엔드포인트는 만들지 않는다.
- 코드 번들은 gzip+base64로 `SendCommand` 인자에 넣어 보내며, 인자 한도를 넘으면 청크로 나눈다. 결과는 EC2에서 gzip+base64로 출력하고, 호출당 출력 한도(약 24KB)에 맞춰 청크로 나눠 로컬 `artifacts/`로 회수한다.
- EC2 역할 권한: `AmazonSSMManagedInstanceCore`, 이번 DSQL 클러스터 ARN 한정 `dsql:DbConnectAdmin`, 이번 RDS 관리 시크릿 ARN 한정 `secretsmanager:GetSecretValue`(구성마다 인라인 정책 교체).

### 데이터와 업무

- 테이블: `products`, `inventory`, `orders`, `order_items`, `operation_receipts`, `accounts`. FK 없이 E001의 이식 가능한 주문 트랜잭션과 같은 SQL을 모든 구성에 쓴다. 금액은 정수 최소 단위.
- 주문 생성: 업무 ID 영수증 삽입(고유 제약) → 조건부 재고 차감(`stock >= qty`) → 주문·항목 삽입. 품절은 업무 거절로 따로 집계.
- 계정 이체: 두 계정 잔액을 읽고 출금 계정 잔액 ≥ 금액이면 이동, 영수증 삽입.
- 재고는 측정 중 고갈되지 않게 생성한다. 셀마다 초기 상태로 복원한다(주문·영수증 삭제, 재고·잔액 재설정; DSQL 트랜잭션당 행 수 한도에 맞춰 나눠 실행).

### ① barrier 시나리오 (격리 수준 RC/RR/SER 각각, 미지원은 `N/A`)

| 시나리오 | 순서 | 허용되는 결과 |
| --- | --- | --- |
| lost update | T1·T2가 같은 재고를 읽고 각자 읽은 값에서 차감해 쓴다 | RC: 발생 가능 / RR·SER: 한쪽 오류(40001) 또는 대기 후 오류 |
| write skew | "두 계정 잔액 합 ≥ 0"을 각자 확인 후 서로 다른 계정에서 출금 | RC·RR: 발생 가능(PostgreSQL RR은 스냅샷 격리) / SER: 한쪽 40001 |
| 이중 주문 | 같은 업무 ID로 동시에 주문 | 모든 수준: 효과 1회(고유 제약 23505 또는 충돌) |
| 교착 | T1 A→B, T2 B→A 갱신 | 대조군: 40P01 가능 / D1: 커밋 시 40001 |
| `SELECT FOR UPDATE` 조건부 차감 | 두 트랜잭션이 같은 행을 잠그고 차감 | 대조군: 대기 후 순차 처리 / D1: 대기 대신 커밋 충돌 여부 관측 |

결과를 "현상 발생 / 오류로 차단(SQLSTATE) / 대기로 차단(대기 ms)"으로 기록하고, 위 기준표와 대조해 **허용된 현상**과 **구현 오류**를 구분한다.

### ② 경합 부하 행렬

- 공통 행렬(REPEATABLE READ): 균등/집중 × 16/64/256 × 재시도 없음/최대 3회 × 3회 = **구성당 36셀**.
- 기본 격리 수준 표(대조군만, READ COMMITTED): 균등/집중 × 16/64/256 × 최대 3회 × 3회 = **18셀**. D1의 기본값은 RR이므로 공통 행렬 결과를 재사용한다.
- 셀: 워밍업 30초 → 측정 60초 → 불변식 검사 → 초기 상태 복원. 반복 차수마다 셀 순서를 섞는다(시드 기록).
- 256 셀 실행 전 `SHOW max_connections`를 조회해 부족하면 **적용 불가(연결 한도)**로 기록하고 건너뛴다.
- 재시도 대상: 40001, 40P01. 연결 단절 등으로 커밋 여부가 불명확하면 업무 ID 영수증을 조회한 뒤 재시도 여부를 정한다.

### ③ 불변식 (셀마다)

재고 음수 0건 · 초기 재고 − 현재 재고 = 확정 주문 수량 합 · 계정 잔액 총합 보존 · 업무 ID별 효과 정확히 1회 · 부분 반영(영수증 없는 주문, 주문 없는 항목) 0건.

### ④ 측정 지표 (셀별)

시도 수, 커밋 수, SQLSTATE별 원시 충돌률(충돌/시도), 최종 실패율(업무 거절 별도), 재시도 포함 p50/p95/p99·최소·최대(ms, 히스토그램 저장), 성공 TPS, 셀별 RTT, 대조군 lock wait(`pg_stat_activity` 샘플링; D1은 `N/A`), A2 ACU·D1 DPU(CloudWatch), EC2 CPU(포화 시 셀 무효 표시).

## 비용 가드

- 단가(2026-09-25 AWS Price List API, 서울, On-Demand, USD): RDS PostgreSQL `db.t4g.medium` Multi-AZ $0.203/h; Aurora `db.t4g.medium` I/O-Optimized $0.147/h; Aurora Serverless v2 I/O-Optimized $0.26/ACU-h; Aurora Standard I/O $0.24/백만 I/O(이번 미사용). EC2 Spot `c7g.2xlarge` $0.074–0.125/h(같은 날 가격 이력, AZ별 상이). DSQL DPU 단가는 **미확인**이며 실행 당일 공식 가격 페이지에서 확인한다.
- 시간 비용 = manifest 이벤트 시각 × 단가. 요청량 비용 = D1 DPU 추정(파일럿에서 측정한 트랜잭션당 DPU × 계획 트랜잭션 수).
- **D1 파일럿**: 본 실행 전 D1에서 동시성별 20초 셀 1개씩 실행 후 CloudWatch DPU로 전체 행렬 비용을 추정한다. 상한을 넘으면 측정 시간 축소안(최소 30초)을 사용자에게 제시해 결정을 받은 뒤 진행한다.
- 셀 시작 전마다 `누적 추정 + 정리 여유`를 확인하고, 가드 임계값을 넘으면 실행을 멈추고 정리한다. 가드 임계값은 처음에 **USD 4.5**(상한 USD 5의 90%)였다가, 상한이 올라간 2026-09-26부터 **USD 45**(상한 USD 50의 90%)로 바뀌었다. 리소스별 절대 수명 태그(`e004:expires-at`)를 둔다.
- 시간 기반 사전 추정(DPU 제외): 구성당 약 2시간 가동 가정 시 R1 약 $0.41, A1 약 $0.29, A2 $0.26–2.08(ACU 사용량에 따름), EC2 약 $1. 실제 비용은 manifest·CloudWatch·Cost Explorer로 확인하며 그전까지 `미확인`.

## 리소스 목록과 삭제 순서

| 범위 | 리소스 |
| --- | --- |
| 공통(배치) | VPC 1, 서브넷 2(다른 AZ), IGW 1, 보안 그룹 2(EC2·DB), IAM 역할 1 + 인스턴스 프로파일 1, Spot EC2 1(+Spot 요청) |
| D1 | DSQL 클러스터 1 |
| R1 | DB 서브넷 그룹 1, DB 인스턴스 1, RDS 관리 시크릿 1 |
| A1 / A2 | DB 서브넷 그룹 1, DB 클러스터 1, writer 인스턴스 1, RDS 관리 시크릿 1 |

- 구성별 DB는 해당 구성 실행 직후 삭제(`SkipFinalSnapshot=True`, `DeleteAutomatedBackups=True`)하고 NotFound까지 대기한다. 공통 리소스는 배치 종료 시 EC2 → 인스턴스 프로파일·역할 → 보안 그룹 → 서브넷 → IGW → VPC 순으로 삭제한다.
- 모든 리소스에 `e004:run-prefix`·`e004:config`·`e004:expires-at`·`e004:managed-by` 태그를 붙이고, manifest에 있고 태그가 일치하는 리소스만 삭제한다.
- `verify`는 manifest의 모든 ID, 스냅샷·보존된 자동 백업, RDS 관리 시크릿, IAM 역할·프로파일, Spot 요청, 이 prefix 태그의 EC2 리소스(인스턴스·볼륨·ENI 포함)를 조회해 `remaining_count`를 계산한다. 0이어야 `completed`.
- RDS 관리 시크릿은 RDS가 DB를 삭제할 때 함께 삭제하거나 삭제를 예약한다. 도구는 RDS가 소유한 시크릿을 직접 복원하거나 삭제하지 않는다. 삭제가 예약된 시크릿은 `secrets_pending_deletion`에 따로 기록하고 `remaining_count`에는 넣지 않는다. 다만 DB가 삭제된 뒤에도 시크릿이 삭제 예약 없이 남아 있으면 도구가 강제 삭제한다. 이 판단이 정말 맞는지는 실제 실행에서 확인해 정리 기록에 남긴다.
- run prefix의 절대 수명 기본값은 900분(최대 960분)이다. `cycle`은 구성을 생성하기 전에, 해당 구성의 예상 소요 시간에 정리 여유 30분을 더한 시간과 남은 수명을 비교한다. 남은 수명이 부족하면 생성을 거부한다.

## 테스트

- 오프라인 단위 테스트(TDD): 불변식 검사기(위반 데이터 주입 시 탐지), 재시도 정책(마감·backoff·커밋 불명확), 시나리오 판정표, 비용 추정·가드, 정리 계획, SSM 청크 분할·조립.
- 로컬 PostgreSQL 16(Docker 사용 가능 시)으로 시나리오·부하 실행기 소규모 통합 검증. 불가하면 D1 파일럿을 첫 통합 검증으로 삼고 편차로 기록한다.

## 재현 절차

전제: Python 3.10+, AWS 프로필 `roboco`, 리전 `ap-northeast-2`, session-manager-plugin(디버깅용). `<ACCOUNT>`는 사람이 확인한 12자리 계정 ID이며 모든 AWS 명령이 STS 신원과 대조한다. `<DPU_PRICE>`는 실행 당일 [Aurora DSQL 가격 페이지](https://aws.amazon.com/rds/aurora/dsql/pricing/)에서 확인한 서울 리전 백만 DPU당 USD 단가다.

```bash
cd experiments/004-transaction-contention
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python3 -m unittest discover -s tests -v                    # 오프라인 테스트(AWS 호출 없음)

PREFIX=$(python3 e004.py init --account-id <ACCOUNT> | tail -1)
A="--account-id <ACCOUNT> --prefix $PREFIX --dsql-usd-per-million-dpu <DPU_PRICE>"
python3 e004.py discover $A                                  # 읽기 전용: 버전·클래스·AZ·Spot 가격
python3 e004.py batch-up $A                                  # VPC·SG·IAM·Spot 러너 생성과 부트스트랩(실패 시 자동 삭제)
python3 e004.py cycle $A --config D1                         # 생성→스키마→시나리오→파일럿 후 정지(D1 유지)
python3 e004.py estimate $A                                  # 파일럿 기반 전체 비용 추정 확인(사용자 결정)
python3 e004.py cycle $A --config D1                         # 행렬 실행 → D1 삭제
python3 e004.py cycle $A --config R1                         # 이하 대조군도 실행 직후 삭제
python3 e004.py cycle $A --config A1
python3 e004.py cycle $A --config A2
python3 e004.py batch-down $A                                # 러너·네트워크·IAM 삭제 + verify(종료 코드 0 = 잔여 0)
python3 e004.py summarize --prefix $PREFIX
```

- `cycle`은 성공·실패·예산 중단과 관계없이 해당 구성을 삭제한다. 예외는 두 가지다. D1 파일럿 직후에는 비용 결정을 위해 D1을 유지한다. Spot 중단(종료 코드 3)이 발생하면 재개를 위해 DB를 유지한다. Spot 중단 시에는 `replace-runner` 후 같은 `cycle`을 다시 실행하면 완료된 셀을 건너뛰고 이어서 진행한다. 재개하지 않으면 `cleanup --config <cfg>`를 즉시 실행한다.
- 셀 시간을 줄여야 하면 모든 `cycle`에 같은 `--measure-s`/`--warmup-s`를 준다(편차로 기록).
- 수동 교차 확인: `aws --profile roboco --region ap-northeast-2 dsql list-clusters`, `rds describe-db-instances`/`describe-db-clusters`(prefix 필터), `ec2 describe-instances --filters Name=tag:e004:run-prefix,Values=$PREFIX`, `iam get-role --role-name $PREFIX-runner`(NoSuchEntity 기대).

### 검증

- 오프라인 단위 테스트 47개(AWS·DB 호출 없음): `python3 -m unittest discover -s tests -v`.
- 로컬 PostgreSQL 16 통합 테스트(2026-09-26, Docker `postgres:16`): 시나리오 15개 조합이 모두 기대표와 일치했고, 소규모 셀(동시성 8)에서 불변식 위반 0건이었다. 실행 방법은 `tests/test_pg_integration.py` 머리말에 있다.
- 시나리오의 `waited_ms`는 도구가 트랜잭션 진행 순서를 조율하는 간격(0.5초 관찰 후 진행)에 따라 달라진다. 따라서 대기가 있었는지 여부만 해석하고, 수치를 DB 대기 시간으로 해석하지 않는다.

## 실행 기록

prefix `e004-20260925t221414z-e5fd`, 계정 `roboco` 프로필, 리전 `ap-northeast-2`. 셀별 결과 파일(`artifacts/<prefix>/results/<cfg>/<cell_id>.json`)의 `cell_id`가 셀 단위 실행 ID다. 셀 ID 형식은 `<cfg>-<RR|RC>-<분포>-c<동시성>-<재시도>-r<반복>`이다.

| run ID | 구성 | 측정 구간(UTC) | 코드 커밋 | 셀 수 |
| --- | --- | --- | --- | ---: |
| `20260925T225056Z-E004-D1-r01` | D1 | 22:50:56–23:19:33, 23:54:52–00:24:38 | `e3b71f9`(셀 5개), `9474ed6`(셀 7개) | 12 |
| `20260926T003913Z-E004-R1-r01` | R1 `db.t4g.medium` Multi-AZ 16.15 | 00:39:13–00:55:00 | `9474ed6` | 18 |
| `20260926T010357Z-E004-A1-r01` | A1 `db.t4g.medium` I/O-Optimized 16.15 | 01:03:57–01:19:56 | `9474ed6` | 18 |
| `20260926T014033Z-E004-A2-r01` | A2 0.5–4 ACU I/O-Optimized 16.15 | 01:40:33–01:56:30 | `9474ed6` | 18 |

- **파일럿:** D1에서 22:17–22:31 UTC에 셀 6개를 실행했다. 조건은 워밍업 5초 + 측정 20초, 러너 `c7g.2xlarge`, 커밋 `809f4f8`이다. 이 결과는 비용 추정에만 쓰고 비교 결과에는 넣지 않았다. 같은 시점의 D1 시나리오 결과는 도구 버그(`42P01`을 차단으로 판정)로 무효 처리했고, 22:50에 다시 실행했다.
- **실행 중 편차:**
  - 러너를 `c7g.4xlarge`로 교체했다.
  - 반복 1회, 셀 워밍업 5초 + 측정 20초로 실행했다(사용자 결정).
  - 23:19 UTC에 Spot 러너가 회수되어(용량 부족) 러너를 교체하고 재개했다.
  - D1 셀 1개가 DSQL `InternalError_: server unavailable`로 실패해 다시 실행했다. 첫 시도 기록은 `results/D1-errors/`에 보관했다.
- **러너 환경:** Python 3.11.16, psycopg 3.3.6, libpq 18.0.6, boto3 1.43.103, aarch64 16 vCPU.

## 성능 결과

검토한 결과표와 해석은 [공개 보고서](../../docs/_experiments/e004.md)에 있다. 원본 행렬은 `artifacts/<prefix>/summary.md|json`에 있다. 요약:

- 66개 셀 전부에서 불변식 위반이 0건이었다.
- 집중 분포에서 D1의 원시 충돌률은 동시성 256일 때 67–71%였다. 커밋에 성공한 요청의 p99는 110 ms 이하였다.
- 대조군은 집중 분포 고동시성에서 잠금 대기가 2초 마감을 넘겨 성공 TPS가 24–512로 떨어졌다.
- A2의 동시성 256 셀 두 개는 연결 타임아웃으로 성공이 0건이었고, 해석에서 제외했다.

## 제약과 동작

| 항목 | 공식 문서·확인 날짜 | 관측 동작 | 영향 |
| --- | --- | --- | --- |
| DSQL 격리 수준 | E001(2026-09-24) | `BEGIN ISOLATION LEVEL REPEATABLE READ`만 수용 | RC/SER 시나리오는 적용 불가 |
| DSQL 트랜잭션당 수정 행 한도 | [쿼터](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/CHAP_quotas.html) 3,000행(2026-09-26) | 셀 초기화를 1,000행 단위로 나눠 실행해야 했고, D1 셀당 3–8분이 걸렸다 | 대량 삭제·정리 작업의 운영 부담 |
| DSQL `SHOW max_connections` | 쿼터상 클러스터당 1만 연결(2026-09-26) | 20을 반환했다 | 이 값으로 동시성을 제한하면 안 된다(도구에 반영) |
| DSQL DDL 가시성 | 미확인 | 트랜잭션을 시작한 뒤에 만든 테이블이 보이지 않았다(`42P01`) | 테이블을 먼저 만들어야 한다(도구에 반영) |
| DSQL 잠금 동작 | [동시성 제어](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-concurrency-control.html) | `FOR UPDATE`를 걸어도 다른 트랜잭션이 대기하지 않고, 커밋 시점에 `40001`로 실패했다 | 재시도 구현이 필수다 |

## 개발·운영 편의성

준비 시간은 D1 42초, A1 5분 30초, A2 6분 37초, R1 12분 8초였다. 삭제 시간은 D1 약 2분, R1 약 3분, A1·A2 약 11분이었다. 모두 1회 측정값이다. 재시도 구현량은 공개 보고서를 참고한다. 작업 시간은 측정하지 않았다.

## 비용

추정 합계는 약 USD 3.4다. 내역은 D1 DPU 222,363(약 $2.22), R1 약 $0.11, A1 약 $0.07, A2 1.91 ACU-시간(약 $0.50), 러너·네트워크 약 $0.45다. 실제 청구액은 미확인이다(Cost Explorer에 반영된 뒤 대조). 단가 출처와 계산 방식은 공개 보고서에 있다.

## 결론과 한계

공개 보고서의 "결론과 한계" 절을 따른다. 핵심 한계는 반복 1회, 25초 셀, 소형 대조군, closed-loop의 coordinated omission이다.

## 정리 기록

- **삭제 완료 시각(UTC):** D1 00:26:37, R1 00:58:04, A1 01:31:21, A2 02:07:38, BATCH 02:08:14. 삭제 실패는 없었다.
- **manifest에 없던 러너:** 러너 교체 코드 버그로 `c7g.4xlarge` 러너 1대가 manifest에 기록되지 않은 채 생성되었다(22:45:53 UTC). 태그로 소유를 확인한 뒤 수동으로 종료했고, 22:48 UTC에 종료를 확인했다. 코드는 커밋 `e3b71f9`에서 고쳤다.
- **검증:** `python3 e004.py verify --account-id <ACCOUNT> --prefix e004-20260925t221414z-e5fd`를 02:09:45 UTC에 실행했고 `remaining_count=0`이었다. 02:08:19의 첫 검증에서는 종료된 러너의 Spot 요청이 아직 `active` 상태여서 1이 나왔다.
- **수동 교차 확인:** DSQL `list-clusters` 0개, `e004` 접두어 RDS 인스턴스·클러스터·클러스터 스냅샷 0개, IAM 역할 `NoSuchEntity`, 실험 태그의 EC2 인스턴스·볼륨·ENI·Spot 요청(open/active) 0개.

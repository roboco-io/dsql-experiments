# 실험 목록

[전체 실험 계획](../EXPERIMENT_PLAN.md)은 **DSQL의 성능·개발/운영 편의성을 RDS PostgreSQL Multi-AZ 인스턴스, Aurora Provisioned, Aurora Serverless v2와 비교**합니다. DSQL을 첫 실험부터 포함합니다. E001은 실측을 마치고 결과를 공개 보고서에 반영했습니다. E004도 실측을 마쳤습니다. E002는 부분 결과(DSQL 최대 용량 미측정)를 공개했고 재측정 과제를 이슈에 남겼습니다. 나머지 항목은 계획 상태입니다.

| 실험 | 확인할 질문 | 우선순위 | 상태 | 결과 | 이슈 | 공개 보고서 |
| --- | --- | --- | --- | --- | --- | --- |
| [001 — SQL 호환성과 이관](../EXPERIMENT_PLAN.md#e001) | DSQL에서 같은 업무를 구현하려면 무엇을 바꿔야 하는가? | P0 | completed | DSQL 17/35 무수정 통과, 16개 미지원(0A000); 대조군 34/35 | [#1](https://github.com/roboco-io/rds-experiments/issues/1) | [보고서](https://roboco.io/rds-experiments/experiments/e001/) |
| [002 — OLTP 처리량과 지연](002-oltp-throughput/) | DSQL의 SLO 용량·지연은 각 대조군의 몇 배인가? | P0 | completed | 부분 결과: 1600 TPS까지 DSQL SLO 통과(쓰기 p95 약 41 ms, Serverless v2 약 7 ms), 연결 16개 기준 DSQL 1,006 TPS vs 대조군 3,065–5,657 TPS, DSQL 최대 용량 미측정 | [#2](https://github.com/roboco-io/rds-experiments/issues/2) | [보고서](https://roboco.io/rds-experiments/experiments/e002/) |
| [003 — 연결과 풀링](../EXPERIMENT_PLAN.md#e003) | DSQL의 인증·연결 갱신·급증 대응에 어떤 부담이 있는가? | P0 | planned | 미측정 | [#3](https://github.com/roboco-io/rds-experiments/issues/3) | [보고서](https://roboco.io/rds-experiments/experiments/e003/) |
| [004 — 트랜잭션 정합성](004-transaction-contention/) | DSQL의 경합·재시도는 정합성·지연·구현량에 어떤 영향을 주는가? | P0 | completed | 불변식 위반 0/66셀. 집중 경합에서 DSQL은 대기 없이 40001로 실패(동시성 256 충돌률 67–71%, 성공 p99 ≤110 ms), 소형 대조군은 잠금 대기로 처리량 급감 | [#4](https://github.com/roboco-io/rds-experiments/issues/4) | [보고서](https://roboco.io/rds-experiments/experiments/e004/) |
| [005 — 읽기 확장과 최신성](../EXPERIMENT_PLAN.md#e005) | DSQL은 reader 분산 대비 성능·최신성·라우팅 작업이 어떻게 다른가? | P1 | planned | 미측정 | [#5](https://github.com/roboco-io/rds-experiments/issues/5) | [보고서](https://roboco.io/rds-experiments/experiments/e005/) |
| [006 — 연결 장애와 복구](../EXPERIMENT_PLAN.md#e006) | 같은 연결 장애 뒤 업무 복구·커밋 보존·재연결 부담은? | P0 공통 / P1 서비스별 장애 | planned | 미측정 | [#6](https://github.com/roboco-io/rds-experiments/issues/6) | [보고서](https://roboco.io/rds-experiments/experiments/e006/) |
| [007 — 백업과 복원](../EXPERIMENT_PLAN.md#e007) | DSQL의 복구 기능·시간·작업량은 기존 서비스와 어떻게 다른가? | P1 | planned | 미측정 | [#7](https://github.com/roboco-io/rds-experiments/issues/7) | [보고서](https://roboco.io/rds-experiments/experiments/e007/) |
| [008 — 운영 작업과 성장](../EXPERIMENT_PLAN.md#e008) | DSQL의 DDL·진단·성장 대응은 얼마나 간편하며 제약은 무엇인가? | P0 기본 작업 / P1 장기 운영 | planned | 미측정 | [#8](https://github.com/roboco-io/rds-experiments/issues/8) | [보고서](https://roboco.io/rds-experiments/experiments/e008/) |
| [009 — 급증·유휴 후 재개](../EXPERIMENT_PLAN.md#e009) | DSQL 대 기존 서버리스·고정 용량의 지연·수동 개입·비용은? | P0 | planned | 미측정 | [#9](https://github.com/roboco-io/rds-experiments/issues/9) | [보고서](https://roboco.io/rds-experiments/experiments/e009/) |
| [010 — 비용과 선택 기준](../EXPERIMENT_PLAN.md#e010) | DSQL의 성능·편의성 이점은 어떤 비용·제약을 수반하는가? | P0 | planned | 미측정 | [#10](https://github.com/roboco-io/rds-experiments/issues/10) | [보고서](https://roboco.io/rds-experiments/experiments/e010/) |
| [011 — 개발·운영 편의성](../EXPERIMENT_PLAN.md#e011) | DSQL은 초기 도입·반복 운영의 작업 시간과 수정량을 얼마나 줄이거나 늘리는가? | P0 | planned | 미측정 | [#11](https://github.com/roboco-io/rds-experiments/issues/11) | [보고서](https://roboco.io/rds-experiments/experiments/e011/) |
| [012 — 대용량 조회·집계](../EXPERIMENT_PLAN.md#e012) | DSQL의 큰 쿼리 성능·OLTP 간섭·튜닝 부담은? | P1 | planned | 미측정 | [#12](https://github.com/roboco-io/rds-experiments/issues/12) | [보고서](https://roboco.io/rds-experiments/experiments/e012/) |

P0는 DSQL 도입 판단을 위한 최소 비교와 필수 검증, P1은 운영·조회 특성 확대 검증입니다. R2·NVMe·스토리지 변형은 필요한 경우에만 추가합니다. 편의성과 비용은 최초 생성부터 삭제까지 수집합니다.

업무 우선순위는 **OLTP → 급증·유휴 → 대용량 조회·집계**입니다. E011은 마지막에 시작하는 별도 DSQL 실험이 아니라 전 과정의 편의성 기록입니다. 번호는 식별자이며 실행 순서는 [계획의 단계표](../EXPERIMENT_PLAN.md)에 따릅니다.

## 실험 추가

`NNN-주제` 형식의 디렉터리를 만들고 [실험 템플릿](../templates/experiment.md)을 `README.md`로 복사합니다. 디렉터리 이름에는 소문자 영문, 숫자, 하이픈을 사용합니다.

각 실험은 독립적으로 재현할 수 있도록 필요한 코드와 설정, 실행 방법, 리소스 정리 방법을 포함합니다. 언어와 도구는 실험에 맞게 선택하고 버전을 기록합니다.

실험을 추가하면 이 문서에 경로, 질문, 상태, 결과 요약을 연결합니다. 상태는 `planned`, `running`, `completed` 중 하나로 표시합니다.

## 결과 기록

- 실행마다 고유한 실행 ID와 UTC 시작·종료 시각, 코드 커밋을 기록합니다.
- 검토할 결과 요약과 비식별화한 측정 자료는 실험 디렉터리에 보관합니다.
- 대용량 원본·로그는 실험 내 `artifacts/`에 보관합니다. 이 경로는 Git에서 제외됩니다. 결과 요약에 원본의 보관 위치와 식별 정보를 기록합니다.
- 연결 문자열의 비밀번호, 자격 증명, 개인정보가 결과나 로그에 포함되지 않도록 합니다.
- DSQL과 각 대조군의 성능 비율·작업 시간 차이·이관 수정량을 같은 업무·정합성·SLO 기준으로 보고합니다.
- 실험 종료 시 성공·실패·중단 여부와 관계없이 모든 실험용 리소스를 삭제하고 잔여 0개를 확인해야 `completed`로 처리합니다.

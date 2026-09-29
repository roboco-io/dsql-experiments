# DSQL 중심 RDS·Aurora 비교 실험

**Aurora DSQL이 기존 인스턴스 기반·서버리스 서비스 대비 제공하는 성능과 개발·운영 편의성**을 실험하는 저장소입니다. 호환성·정합성 제약과 비용을 함께 비교해 DSQL이 유리하거나 불리한 조건을 파악합니다.

최초 비교부터 **DSQL, RDS PostgreSQL Multi-AZ 인스턴스, Aurora PostgreSQL Provisioned, Aurora PostgreSQL Serverless v2**를 포함합니다. DSQL의 지연·처리량뿐 아니라 초기 연결, 이관, 용량 대응, 진단·복구·삭제에 드는 작업 시간과 수정량을 비교합니다. [실험 계획](EXPERIMENT_PLAN.md)에 대상 구성, 업무 부하, 측정 기준과 실행 순서를 정리했습니다. 12개 실험을 모두 마쳤습니다(9개는 최소 범위 MVP). 결론은 [DSQL 도입 판단 종합 보고서](https://roboco.io/rds-experiments/decision/)에, 사람과 코딩 에이전트가 따를 규칙과 코드 예시는 [Aurora DSQL 사용 가이드](https://roboco.io/rds-experiments/guide/)(원문 [docs/guide.md](docs/guide.md))에 있습니다.

업무 우선순위는 **일반 OLTP → 트래픽 급증·유휴 → 대용량 조회·집계**입니다. 편의성과 비용은 첫 생성부터 삭제까지 기록하며, 세부 인스턴스·스토리지 튜닝은 DSQL 채택 판단에 필요할 때 확대합니다. 공식 문서와 대상 선정 근거는 [서비스 선정 기록](SERVICE_SELECTION.md)에 정리했습니다.

**[공개 실험 노트](https://roboco.io/rds-experiments/)**에서 계획과 검토한 결과를 확인할 수 있습니다. [GitHub 이슈](https://github.com/roboco-io/rds-experiments/issues?q=is%3Aissue+label%3Aexperiment)에서 각 실험의 진행 상황을 관리합니다. 결과 작성·검증·자동 배포 방법은 [공개 절차](PUBLISHING.md)에 정리했습니다.

## 확인할 질문

| 관점 | 실험으로 확인할 내용 |
| --- | --- |
| 성능 | 같은 부하에서 DSQL의 지연·오류율과 SLO 충족 처리량이 각 대조군 대비 얼마나 다른가? |
| 편의성 | 첫 사용·이관·확장·진단·복구·삭제의 작업 시간, 수동 개입과 수정량이 얼마나 줄거나 늘어나는가? |
| 제약 | SQL·인증·트랜잭션·연결·복원 차이 때문에 적용할 수 없거나 추가 구현이 필요한 업무는 무엇인가? |
| 비용 | 동일 요구사항을 만족하는 성공 업무당 비용과 실행·유휴·정리를 포함한 전체 비용은 얼마인가? |

## 구조

```text
rds-experiments/
├── README.md
├── INTENT.md                 # 실험 목적과 학습 기록
├── EXPERIMENT_PLAN.md        # 비교 대상, 공통 방법, 개별 실험 계획
├── SERVICE_SELECTION.md      # Exa 조사와 서비스 포함·보류 근거
├── PUBLISHING.md             # 결과 공개·검증·배포 절차
├── docs/                     # GitHub Pages 공개 보고서와 레이아웃
├── scripts/                  # 공개 보고서·사이트 검증
├── experiments/              # 실험별 설명, 설정, 코드, 결과 요약
│   └── README.md
└── templates/
    └── experiment.md         # 실험 계획과 결과 기록 양식
```

## 새 실험 시작하기

저장소 루트에서 실행합니다. 아래 이름은 예시입니다.

```bash
mkdir -p experiments/001-connection-scaling
cp templates/experiment.md experiments/001-connection-scaling/README.md
```

1. 확인할 질문과 비교 조건을 먼저 적습니다.
2. 해당 실험에 필요한 설정·실행·정리 방법을 같은 디렉터리에 작성합니다.
3. 실행 조건, 측정 결과, 비용, 해석의 한계를 기록합니다.

실험 목록과 기록 규칙은 [experiments/README.md](experiments/README.md), 목적은 [INTENT.md](INTENT.md)를 참고하세요.

## 비교 원칙

- 리전, 엔진 버전, 배포 방식, 인스턴스 또는 용량 설정, 스토리지, 파라미터를 기록합니다.
- 데이터 크기, 쿼리, 동시성, 부하 발생기 위치와 사양을 명시하고, 비교군 간 차이를 설명합니다.
- 워밍업과 측정을 구분하고, 반복 실행별 결과와 변동을 남깁니다.
- 문서에 명시된 제약과 실험에서 관측한 동작을 구분합니다.
- DSQL의 우위를 전제하지 않으며, 같은 업무·정합성·SLO를 충족한 결과를 대조군별 비율과 작업 시간 차이로 제시합니다. 기능 미지원·미측정은 0이나 성능 우위로 해석하지 않습니다.
- 비용은 단가 출처·확인 날짜·통화·사용량을 함께 남기며, 예상 비용과 실제 청구액을 구분합니다.
- 실험 종료 시 성공·실패·중단 여부와 관계없이 해당 실험을 위해 프로비저닝한 리소스를 모두 즉시 삭제합니다. 스냅샷·백업 등 부속 리소스까지 삭제하고, 잔여 리소스가 0개임을 검증·기록해야 실험을 완료 처리합니다. 상세 절차는 [프로젝트 규칙](AGENTS.md#cost-control--mandatory-cleanup)을 따릅니다.
- AWS 가격과 지원 기능은 각 실험 시점의 공식 문서를 확인합니다.

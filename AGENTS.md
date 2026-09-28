# Repository Guidelines

## Project Structure & Module Organization

This repository records AWS RDS and Aurora experiments on performance, constraints, and cost. Experiment workloads are not implemented yet. A Jekyll site publishes reviewed reports through GitHub Pages.

The primary question is how Aurora DSQL compares with existing instance-based and serverless services in performance and developer/operator convenience, including compatibility tradeoffs and cost. Include DSQL in the core comparisons and record task time and manual effort alongside performance.

- `README.md`: overview and comparison principles.
- `INTENT.md`: purpose and learning record.
- `templates/experiment.md`: experiment planning and results template.
- `experiments/README.md`: experiment index; add each experiment's path, question, status, and summary.
- `experiments/NNN-topic/`: keep each experiment's code, configuration, instructions, and reviewed results together. Store large raw measurements in its ignored `artifacts/` directory.
- `docs/_experiments/eNNN.md`: public report, status, and linked GitHub issue for each experiment.
- `PUBLISHING.md`: result publication and deployment procedures; `scripts/` validates public reports and generated links.

## Build, Test, and Development Commands

Start an experiment from the repository root:

```bash
mkdir -p experiments/001-connection-scaling
cp templates/experiment.md experiments/001-connection-scaling/README.md
```

These commands create an experiment directory and copy the recording template. Run `git diff --check` to check tracked edits for whitespace errors. Document workload setup, execution, validation, and cleanup within each experiment. For the public site, run `bundle exec ruby scripts/validate_reports.rb`, `bundle exec jekyll build --source docs --destination _site`, and `python3 scripts/check_site.py`; see `PUBLISHING.md` for setup.

## Coding Style & Naming Conventions

Use `NNN-topic` directory names with lowercase English letters, digits, and hyphens. Preserve the template's headings, tables, and units. Existing documentation is in Korean; keep terminology consistent when extending it.

No language-specific indentation rules, formatter, or linter is configured. Follow the chosen language's conventions, use consistent indentation, and record tool versions per experiment. Use `planned`, `running`, or `completed` for experiment status.

## Testing & Experiment Validation

No automated testing framework or coverage threshold exists. Document validation commands when adding executable code. Separate warmup from measurement, repeat runs, and report variation. Record run IDs, UTC start/end times, code commits, engine versions, workload settings, and uncontrolled differences. Verify cleanup and record any remaining resources.

## Cost Control & Mandatory Cleanup

- 비용 관리를 위해 실험이 종료되면 성공·실패·중단 여부와 관계없이 해당 실험을 위해 프로비저닝한 리소스를 모두 즉시 삭제합니다. 삭제 범위는 해당 실험에서 생성한 리소스로 한정합니다.
- 프로비저닝 전에 생성할 리소스 목록과 삭제·검증 명령을 실험 문서에 작성하고, 생성 후 계정·리전·리소스 ID를 기록합니다.
- 삭제 대상에는 DB 인스턴스·클러스터·복제본, 부하 발생기, 스토리지, 네트워크, 모니터링 등 실험용 부속 리소스와 스냅샷·보존된 백업을 포함합니다. 리소스 중지나 용량 축소만으로 정리를 완료한 것으로 간주하지 않습니다. 필요한 측정 결과와 로그는 삭제 전에 로컬 `artifacts/`에 저장하고, 민감 정보를 제거한 요약만 Git에 남깁니다.
- 삭제 요청 후 실제 삭제 완료까지 확인하고, 사용한 계정·리전의 잔여 리소스를 조회합니다. 실험 문서에 UTC 정리 시각, 삭제 대상·결과, 검증 명령·결과를 기록하며, 잔여 리소스가 0개임을 확인해야 상태를 `completed`로 변경할 수 있습니다.
- 삭제 실패 시 잔여 리소스, 실패 원인과 후속 조치를 기록하고 정리 미완료로 표시합니다. 실패를 해결하고 잔여 리소스를 모두 삭제한 뒤 다시 검증합니다.

## Commit & Pull Request Guidelines

History contains one commit, `Initialize RDS experiments repository`; no broader convention is established. Use short imperative summaries and split unrelated changes into logical commits. Before pushing, run applicable checks, push to all configured remotes, and verify success.

In pull requests, describe the question, changed files, reproduction commands, validation performed, and relevant results or limitations. Link related issues when available and update the experiment index.

## Security & Configuration

Keep credentials, passwords, personal data, and infrastructure state out of Git. Commit sanitized summaries and reference raw artifact locations. Record official pricing sources, verification dates, currencies, and usage; distinguish estimates from actual charges.

## Public Results

Track each experiment in its GitHub issue and publish reviewed Korean reports in `docs/_experiments/`. Keep the issue, experiment index, and report status consistent. When an experiment finishes, write a narrative `## 결과 요약` (a few paragraphs, no tables) at the top of the report body; end it with `### 프로덕션 사용 관점`, at most 500 characters on whether the results support using DSQL in production; the report page folds every other section. Also set the report's one-line `result` so the public experiment list shows it, and keep it in sync with the result column of `experiments/README.md`. Mark unmeasured fields explicitly; never invent benchmark results. A completed report must include run IDs, the execution commit, measurement date, conclusions and limitations, and verified cleanup with zero remaining resources. Pushes affecting the site on `main` trigger the Pages workflow. Verify the deployment and public report before closing the issue. Never copy raw `artifacts/` or infrastructure state into `docs/`.

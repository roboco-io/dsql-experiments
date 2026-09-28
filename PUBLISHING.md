# 실험 결과 공개 절차

공개 사이트: <https://roboco.io/rds-experiments/>

실험별 이슈는 [실험 목록](experiments/README.md)에 연결되어 있습니다. 실험 코드는 `experiments/NNN-topic/`, 검토한 공개 보고서는 `docs/_experiments/eNNN.md`에서 관리합니다. 이슈를 닫는 것만으로 사이트 상태가 변경되지는 않습니다.

## 보고서 갱신

1. 해당 이슈와 보고서의 상태를 `planned` → `running` → `completed` 순으로 갱신합니다. 완료 전 중간 기록은 `running`으로 두고 잠정 결과임을 밝힙니다.
2. 기존 보고서의 `experiment_id`, `slug`, `title`, `question`, `priority`, `issue`, `issue_url`을 유지합니다. 계획이 바뀌면 계획 문서·실험 목록·이슈·보고서의 질문도 함께 맞춥니다.
3. 원본 자료를 로컬 `artifacts/`에 보관하고, 비식별화한 요약·표·그래프만 공개 보고서에 넣습니다. 이미지는 `docs/assets/results/eNNN/`에 저장하고 `{{ '/assets/results/eNNN/figure.png' | relative_url }}`로 연결할 수 있습니다.
4. Markdown 본문에 다음 제목을 사용합니다: `## 결과 요약`, `## 실행 조건`, `## 성능 결과`, `## 개발·운영 편의성`, `## 비용`, `## 결론과 한계`, `## 정리 기록`. `## 결과 요약`은 본문 맨 앞에 두고, 실험의 질문·방법·핵심 관측·해석의 한계를 표나 목록 없이 서너 문단의 서술형으로 씁니다. 공개 페이지는 `결과 요약`과 `확인할 질문`만 펼쳐 보여 주고 나머지 절은 접어 두며, 독자가 절 제목을 눌러 펼칩니다. 미측정 항목은 미측정으로 표시하고 근거 없는 수치를 채우지 않습니다.
5. 모든 실험용 리소스를 삭제하고 잔여 0개를 확인한 뒤, 아래 완료 메타데이터를 실제 값으로 채웁니다. 미측정 안내 문구를 제거하고 실험 목록도 갱신합니다. 실험이 끝날 때마다 `result`에 한 줄 결과를 적습니다. 이 값은 공개 사이트 실험 목록의 해당 실험 행과 `experiments/README.md`의 결과 열에 같은 내용으로 표시됩니다.
6. 로컬 검증 후 `main`에 반영합니다. GitHub Actions의 **Publish experiment results**가 보고서 검증·사이트 빌드·링크 검사를 수행하고 GitHub Pages로 배포합니다. 공개 URL의 내용까지 확인한 뒤 이슈에 결과 링크를 남기고 종료합니다.

완료 보고서에 추가할 메타데이터의 형식입니다. 값은 실제 측정 기록에서 가져오며 아래 설명 문자열을 그대로 사용하지 않습니다.

```yaml
status: completed
summary: "핵심 관측 결과와 적용 범위"
result: "실험 목록에 표시할 한 줄 결과"
measured_at: "YYYY-MM-DD"
code_commit: "실행 코드의 40자리 Git 커밋 SHA"
run_ids:
  - "실제 UTC 실행 ID"
cleanup_verified: true
remaining_resources: 0
```

완료 상태의 보고서는 이 메타데이터와 필수 본문 제목이 없으면 배포 검증에서 거부합니다. 이 검증은 실제 측정·삭제 증거 검토를 대신하지 않습니다. 정리 기록에 UTC 시각, 삭제·조회 명령, 비식별 검증 결과를 남깁니다. 기술 실패로 목적을 달성하지 못한 실험은 실패 결과·한계를 본문에 명시하고 정리가 끝나기 전에는 `completed`로 표시하지 않습니다.

## 로컬 빌드와 확인

Ruby 3.4, Bundler 2.6, Python 3.10 이상을 사용합니다. 정확한 gem 버전은 `Gemfile.lock`에 고정합니다.

```bash
bundle install
bundle exec ruby scripts/validate_reports.rb
bundle exec jekyll build --source docs --destination _site
python3 scripts/check_site.py
git diff --check
bundle exec jekyll serve --source docs --destination _site --baseurl /rds-experiments
```

미리보기는 <http://127.0.0.1:4000/rds-experiments/>에서 확인합니다. `_site/`는 생성물이며 Git에 넣지 않습니다. 저장소의 `docs/`만 빌드하고 내부 작업 계획인 `docs/superpowers/`는 제외합니다.

## 배포 확인과 수정

```bash
gh run list --workflow pages.yml --limit 5
gh run view RUN_ID --log-failed
gh api repos/roboco-io/rds-experiments/pages --jq .html_url
```

실패한 빌드는 이전 공개 결과를 덮어쓰지 않습니다. 원인을 수정한 커밋을 `main`에 반영하면 다시 배포합니다. 실험 원본·자격 증명·개인정보·인프라 상태를 `docs/`에 복사하지 않습니다.

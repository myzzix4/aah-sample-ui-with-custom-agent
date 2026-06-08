# Sample #1 — UI + Custom Agent

AAH Code Deploy 시나리오 **#1** 샘플 repo.

| 차원 | 선택 |
|---|---|
| **UI** | ✅ 있음 (직접 개발 Flask + 삼성생명 mockup) |
| **Agent** | ✅ 직접 개발 (boto3 + Anthropic native tool_use) |

## 동작

1. `services/ui` → Flask Web (App Runner, x86_64) — 삼성생명 데모 홈페이지 + 우하단 floating 채팅
2. `services/agent` → AgentCore Runtime (ARM64) — ReAct loop, KB 2종 (Bedrock + Databricks)
3. `aah.yaml` 의 `env_from_services.AGENT_RUNTIME_ARN: agent.runtime_arn` — agent 먼저 배포되면 그 ARN 이 UI 환경변수로 자동 주입

## 배포

`/develop/code-deploy` 페이지에서 **샘플 #1 카드** 클릭 → 자동 prefill + 매니페스트 분석 → 배포.

- 소요: ~6~8분 (agent build → AgentCore Runtime + Memory provision → ui build → App Runner)
- KB는 선택 — `BEDROCK_KB_ID` 또는 `DBX_*` 박으면 활성, 비우면 일반 LLM 응답

## 시연

배포 완료 후 App Runner URL 클릭 → 삼성생명 mock 홈페이지 → 우하단 💬 → 채팅.

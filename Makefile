.PHONY: install test run verify-phase-a verify-phase-b verify-phase-c verify-phase-d verify-phase-e verify-phase-e-stability verify-agn verify-agn-governance verify-agn-mvp soak-sse-2h agn2-start agn2-stop agn2-status agn2-refresh agn2-emergency-stop agn2-release-stop agn-up agn-down agn-smoke agn-smoke-real agn-sync-preflight agn-sync-once agn-sync-loop memory-merge provider-show provider-probe prepare-codex-agn-home collab-status collab-route collab-run collab-review validate-qwen-local-provider validate-low-token-tools validate-project-memory validate-execution-protocol validate-high-risk-safety validate-model-router validate-provider-contracts validate-protocol-drift audit-memory-surfaces incident-triage dispatch-real review-file telegram-listen-stdin-once telegram-listen-polling-once telegram-send-once run-agn-task-stdin kirara-heartbeat-once kirara-message kirara-web-search kirara-mail-unread kirara-calendar-upcoming kirara-task-list kirara-task-add pointer-info pointer-read-tail pointer-search kirara-state-init kirara-state-status kirara-state-sync-in kirara-state-sync-out run-event-driven-regression run-evo6-console-regression research-day research-validate research-autonomy-once clean-phase-a clean-phase-c clean-phase-d clean-phase-e tidy-workspace-preview tidy-workspace tidy-workspace-full check-portability

INTERVAL_SECONDS ?= 300

install:
	python3 -m pip install -r requirements.txt

test:
	pytest -q

run:
	python3 -m uvicorn agn_api.main:app --host 127.0.0.1 --port 8000

verify-phase-a:
	python3 scripts/validation/verify_phase_a.py

verify-phase-b:
	python3 scripts/validation/verify_phase_b.py

verify-phase-c:
	python3 scripts/validation/verify_phase_c.py

verify-phase-d:
	python3 scripts/validation/verify_phase_d.py

verify-phase-e:
	python3 scripts/validation/verify_phase_e.py

verify-phase-e-stability:
	python3 scripts/validation/verify_phase_e.py --mode stability

verify-role-config:
	python3 scripts/validation/verify_role_config.py

verify-agn:
	python3 -m py_compile scripts/*.py scripts/validation/*.py
	pytest -q
	$(MAKE) agn-smoke
	$(MAKE) agn-smoke-real

verify-agn-governance:
	python3 -m py_compile scripts/*.py scripts/validation/*.py
	pytest -q
	$(MAKE) agn-smoke
	@TASK_ID="verify-governance-neg-$$(date +%s)"; \
	if python3 scripts/coordinator_ingest.py --task-id "$$TASK_ID" --task-kind repo --request-text "negative gate check" >/dev/null 2>&1; then \
		echo "expected coordinator_ingest repo-missing to fail"; \
		exit 1; \
	fi

verify-agn-mvp:
	python3 -m py_compile scripts/*.py scripts/validation/*.py agn_api/*.py
	pytest -q
	$(MAKE) verify-agn
	$(MAKE) verify-agn-governance
	python3 scripts/validation/verify_agn_mvp.py

soak-sse-2h:
	python3 scripts/validation/soak_sse.py --clients 50 --duration-seconds 7200 --base-url http://127.0.0.1:8000

# ── AGN2.0 unified lifecycle ──
agn2-start:
	python3 scripts/agn2_system.py start

agn2-stop:
	bash scripts/agn_down.sh

agn2-status:
	python3 scripts/agn2_system.py status

agn2-refresh:
	python3 scripts/agn2_system.py refresh

agn2-emergency-stop:
	python3 scripts/agn2_system.py emergency-stop --reason "$(REASON)"

agn2-release-stop:
	python3 scripts/agn2_system.py release-stop --reason "$(REASON)"

# Legacy aliases (delegate to AGN2.0 unified targets)
agn-up: agn2-start
	bash scripts/agn_up.sh

agn-down: agn2-stop

agn-smoke:
	python3 scripts/validation/agn_smoke.py

agn-smoke-real:
	python3 scripts/validation/agn_smoke_real.py

run-event-driven-regression:
	python3 scripts/validation/run_event_driven_regression.py

run-evo6-console-regression:
	python3 scripts/validation/run_evo6_console_regression.py

research-day:
	python3 scripts/research_flow.py

research-validate:
	python3 scripts/validation/run_research_permissive_validation.py

research-autonomy-once:
	python3 scripts/research_autonomy.py --once

agn-sync-preflight:
	python3 scripts/agn_git_sync.py preflight

agn-sync-once:
	python3 scripts/agn_git_sync.py once

agn-sync-loop:
	python3 scripts/agn_git_sync.py loop --interval-seconds "$(INTERVAL_SECONDS)"

memory-merge:
	python3 scripts/memory_sync.py merge --output runtime/kirara_memory_merged.json

provider-show:
	python3 scripts/provider_registry.py show

provider-probe:
	python3 scripts/provider_registry.py probe --output runtime/provider_capabilities.json

prepare-codex-agn-home:
	python3 scripts/maintenance/prepare_codex_agn_home.py

collab-status:
	python3 scripts/agent_collaboration.py status

collab-route:
	python3 scripts/agent_collaboration.py route --from-json-file "$(FILE)"

collab-run:
	python3 scripts/agent_collaboration.py run --from-json-file "$(FILE)" --output "$(OUT)"

collab-review:
	python3 scripts/agent_collaboration.py review --file "$(FILE)" --goal "$(GOAL)" --include-dir "$(DIR)"

validate-qwen-local-provider:
	uv run --with httpx python scripts/validation/run_qwen_local_provider_validation.py

validate-low-token-tools:
	uv run --with httpx python scripts/validation/run_low_token_tools_validation.py

validate-project-memory:
	python3 scripts/validation/run_project_memory_validation.py

validate-execution-protocol:
	python3 scripts/validation/run_execution_protocol_validation.py

validate-high-risk-safety:
	python3 scripts/validation/run_high_risk_safety_validation.py

validate-model-router:
	uv run --with httpx python scripts/validation/run_model_router_validation.py

validate-provider-contracts:
	python3 scripts/validation/run_provider_contract_validation.py

validate-protocol-drift:
	python3 scripts/validation/run_protocol_drift_validation.py

audit-memory-surfaces:
	python3 scripts/maintenance/audit_memory_surfaces.py

review-file:
	python3 scripts/agn2_execution_workflow.py review --file "$(FILE)" --goal "$(GOAL)" --include-dir "$(DIR)"

incident-triage:
	python3 scripts/safety/incident_triage.py

dispatch-real:
	python3 scripts/coordinator_ingest.py --task-id "$(TASK_ID)" --source manual --task-kind repo --request-text "$(TEXT)" --repo-path "$(REPO_PATH)" --work-branch "$(BRANCH)" --executor-provider "$(EXECUTOR_PROVIDER)" --reviewer-provider "$(REVIEWER_PROVIDER)"

telegram-listen-stdin-once:
	python3 scripts/telegram_listener.py --stdin

telegram-listen-polling-once:
	python3 scripts/telegram_listener.py --once

telegram-send-once:
	python3 scripts/telegram_sender.py --once --dry-run

run-agn-task-stdin:
	python3 scripts/run_agn_task.py --from-stdin

kirara-heartbeat-once:
	python3 scripts/kirara_heartbeat.py --once

kirara-message:
	python3 scripts/kirara_message_tool.py --text "$(TEXT)" --chat-id "$(CHAT_ID)" --task-id "$(TASK_ID)" --correlation-id "$(CORRELATION_ID)" --kind "$(KIND)"

kirara-web-search:
	python3 scripts/kirara_sense.py web-search --query "$(Q)"

kirara-mail-unread:
	python3 scripts/kirara_sense.py mail-unread

kirara-calendar-upcoming:
	python3 scripts/kirara_sense.py calendar-upcoming

kirara-task-list:
	python3 scripts/kirara_tasks.py list

kirara-task-add:
	python3 scripts/kirara_tasks.py add --task-id "$(TASK_ID)" --title "$(TITLE)" --due-in-minutes "$(DUE_IN_MINUTES)" --chat-id "$(CHAT_ID)" --correlation-id "$(CORRELATION_ID)"

pointer-info:
	python3 scripts/agn_pointer_tool.py info --ref "$(REF)"

pointer-read-tail:
	python3 scripts/agn_pointer_tool.py read --ref "$(REF)" --mode tail --tail-lines "$(TAIL_LINES)"

pointer-search:
	python3 scripts/agn_pointer_tool.py search --ref "$(REF)" --pattern "$(PATTERN)" --max-matches "$(MAX_MATCHES)"

kirara-state-init:
	python3 scripts/kirara_state_sync.py init --repo-url "$(REPO_URL)"

kirara-state-status:
	python3 scripts/kirara_state_sync.py status

kirara-state-sync-in:
	python3 scripts/kirara_state_sync.py sync-in

kirara-state-sync-out:
	python3 scripts/kirara_state_sync.py sync-out

clean-phase-a:
	rm -f ssot/*.json
	rm -f audit/events.jsonl
	rm -f reports/phase_A_acceptance.json reports/phase_A_acceptance.md

clean-phase-c:
	rm -f ssot/*.json
	rm -f audit/events.jsonl
	rm -f reports/phase_C_acceptance.json reports/phase_C_acceptance.md reports/phase_C_verify.log

clean-phase-d:
	rm -f dispatch/*.json dispatch/acks/*.json
	rm -f results/*.json
	rm -f verdicts/*.json
	rm -f audit/events.jsonl
	rm -f reports/phase_D_acceptance.json reports/phase_D_acceptance.md reports/phase_D_verify.log

clean-phase-e:
	rm -f reports/phase_E_*.json reports/phase_E_*.md reports/phase_E_*.log

tidy-workspace-preview:
	python3 scripts/maintenance/tidy_workspace.py

tidy-workspace:
	python3 scripts/maintenance/tidy_workspace.py --apply

tidy-workspace-full:
	python3 scripts/maintenance/tidy_workspace.py --apply --include-workspace

check-portability:
	python3 scripts/maintenance/check_portability.py

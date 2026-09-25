.PHONY: setup host up down logs ps test cli reset demo-nomodels loadtest-up loadtest watch

setup:            ## install host tools and models (once)
	./scripts/setup-host.sh

host:             ## run whisper.cpp + Ollama on the host (keep it open)
	./scripts/run-host.sh

up:               ## build and start everything: http://localhost:8080
	docker compose up --build -d
	@echo "open http://localhost:8080"

down:
	docker compose down

logs:
	docker compose logs -f --tail=50 worker api

ps:
	docker compose ps

test:
	uv run pytest

cli:              ## terminal captions: make cli FILE=samples/jfk.wav
	uv run python -m conffy.worker.cli $(FILE) --glossary nerdearla

reset:            ## wipe all sessions and captions (Valkey volume)
	docker compose down -v

demo-nomodels:    ## the stack with fake models: try conffy without a GPU or model downloads
	ASR_PROVIDER=replay MT_PROVIDER=replay docker compose up --build -d
	@echo "open http://localhost:8080 (captions are simulated)"

loadtest-up:      ## 15 talks with fake models, 15 workers, 3 api replicas (wipes Valkey!)
	docker compose down -v
	SESSIONS_FILE=config/sessions-loadtest.yml ASR_PROVIDER=replay MT_PROVIDER=replay \
		docker compose up --build -d --scale worker=15 --scale api=3

loadtest:         ## 1,500 viewers for 2 minutes against localhost:8080
	uv run python loadtest/sse_clients.py --viewers 1500 --duration 120 --json loadtest/results.json

watch:            ## per-talk latencies every 5 s (capacity measurement)
	uv run python loadtest/watch_sessions.py --csv loadtest/capacity.csv
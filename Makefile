.PHONY: setup host up down logs ps test cli reset

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

cli:              ## terminal captions: make cli FILE=samples/charla.mp3
	uv run python -m conffy.worker.cli $(FILE) --glossary nerdearla

reset:            ## wipe all sessions and captions (Valkey volume)
	docker compose down -v

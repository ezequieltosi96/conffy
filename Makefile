.PHONY: setup host up up-obs down logs ps test cli reset demo-nomodels loadtest-up loadtest watch \
	room start stop mic-devices mic

# Defaults for the talk-related targets (override on the command line: make room ID=sala-yt FROM=en TO=es)
ID       ?= sala-yt
TITLE    ?= $(ID)
FROM     ?= en
TO       ?= es
GLOSSARY ?= nerdearla
DEVICE   ?= 0
API      ?= http://localhost:8080

setup:            ## install host tools and models (once)
	./scripts/setup-host.sh

host:             ## run whisper.cpp + Ollama on the host (keep it open)
	./scripts/run-host.sh

up:               ## build and start everything: http://localhost:8080
	docker compose up --build -d
	@echo "open http://localhost:8080"

up-obs:           ## same as up, plus MediaMTX to receive OBS/vMix/ffmpeg streams on rtmp://localhost:1935
	docker compose --profile rtmp up --build -d
	@echo "open http://localhost:8080"
	@echo "OBS > Settings > Stream: Service Custom, Server rtmp://localhost:1935, Stream key = talk id"

down:             ## stop everything (including MediaMTX)
	docker compose --profile rtmp down

logs:
	docker compose logs -f --tail=50 worker api

ps:
	docker compose --profile rtmp ps

test:
	uv run pytest

cli:              ## terminal captions: make cli FILE=samples/jfk.wav
	uv run python -m conffy.worker.cli $(FILE) --glossary $(GLOSSARY)

reset:            ## wipe all talks and captions (Valkey volume); sessions.yml is seeded again on next up
	docker compose --profile rtmp down -v

room:             ## create a talk fed by RTMP: make room ID=sala-yt FROM=en TO=es TITLE="Charla desde YouTube"
	curl -sS -X POST $(API)/api/sessions -H 'Content-Type: application/json' \
		-d '{"id":"$(ID)","title":"$(TITLE)","source":{"kind":"url","uri":"rtmp://mediamtx:1935/$(ID)"},"source_lang":"$(FROM)","target_langs":["$(TO)"],"glossary":"$(GLOSSARY)"}'
	@echo
	@echo "viewer: $(API)/?s=$(ID)&lang=$(TO)   overlay: $(API)/overlay.html?s=$(ID)&lang=$(TO)"

start:            ## (re)start a talk: make start ID=sala-a
	curl -sS -X POST $(API)/api/sessions/$(ID)/start; echo

stop:             ## stop a talk: make stop ID=sala-a
	curl -sS -X POST $(API)/api/sessions/$(ID)/stop; echo

mic-devices:      ## list this Mac's audio inputs (for make mic DEVICE=n)
	-ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | sed -n '/audio devices/,$$p'

mic:              ## send a Mac audio input to MediaMTX as talk ID: make mic DEVICE=1 ID=sala-mic (needs up-obs)
	ffmpeg -hide_banner -loglevel warning -f avfoundation -i ":$(DEVICE)" \
		-ac 1 -ar 48000 -c:a aac -b:a 96k -f flv rtmp://localhost:1935/$(ID)

demo-nomodels:    ## the stack with fake models: try conffy without a GPU or model downloads
	ASR_PROVIDER=replay MT_PROVIDER=replay docker compose up --build -d
	@echo "open http://localhost:8080 (captions are simulated)"

loadtest-up:      ## 15 talks with fake models, 15 workers, 3 api replicas (wipes Valkey!)
	docker compose --profile rtmp down -v
	SESSIONS_FILE=config/sessions-loadtest.yml ASR_PROVIDER=replay MT_PROVIDER=replay \
		docker compose up --build -d --scale worker=15 --scale api=3

loadtest:         ## 1,500 viewers for 2 minutes against localhost:8080
	uv run python loadtest/sse_clients.py --viewers 1500 --duration 120 --json loadtest/results.json

watch:            ## per-talk latencies every 5 s (capacity measurement)
	uv run python loadtest/watch_sessions.py --csv loadtest/capacity.csv
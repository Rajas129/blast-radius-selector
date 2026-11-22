IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check maps report

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Build, run once on the sample under the scorer's limits, score, and show
# where the seconds went (stderr of the run is kept in run.log).
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT) 2> run.log
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)
	python3 tools/report.py $(SAMPLE) $(OUTPUT)

check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT) 2> run.log
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Rebuild maps/ inside the image (real duckdb, amd64 timings), then commit maps/.
# Takes about ten minutes.
maps: build
	docker run --rm --platform linux/amd64 --volume $(CURDIR):/work --workdir /work \
		--env PYTHONDONTWRITEBYTECODE=1 $(IMAGE) sh tools/build_maps.sh /opt/sqlglot sample/tests.json

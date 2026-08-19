# sql-db-0819 — build / test entry points
#
# The pinned sqllogictest runner lives in third_party/sqllogictest
# (see README.md and third_party/sqllogictest/PIN.txt for the pin).
#
# Targets:
#   make build         build the official sqllogictest runner binary
#   make unit          run engine unit tests (stdlib unittest)
#   make test          run official runner vs this engine on select1/select2
#                      (per-file pass/fail, exit 0 only if both pass, 0 skips)
#   make negative-test verify the runner really fails on wrong expectations
#   make clean         remove build artifacts

SHELL := /bin/bash
SQLLOGICTEST_DIR := third_party/sqllogictest
PIN := c67f97bf3ca7e590d12e073408bcacaf2ff0f3a0

.PHONY: build unit test negative-test clean pin-check

build: pin-check
	$(MAKE) -C $(SQLLOGICTEST_DIR)/src -f Makefile.no-odbc

pin-check:
	@actual="$$(sed -n '1s/.*: //p' $(SQLLOGICTEST_DIR)/PIN.txt)"; \
	if [ "$$actual" != "$(PIN)" ]; then \
	  echo "ERROR: sqllogictest pin mismatch: expected $(PIN), found $$actual" >&2; \
	  exit 1; \
	fi; \
	echo "sqllogictest pin OK: $(PIN)"

unit:
	python3 -m unittest discover -s tests -v

test: build
	cd $(SQLLOGICTEST_DIR) && bash $(CURDIR)/tools/run_sqllogictest.sh test/select1.test test/select2.test

negative-test: build
	bash $(CURDIR)/tools/run_negative_test.sh $(CURDIR)/test/negative.test

clean:
	$(MAKE) -C $(SQLLOGICTEST_DIR)/src -f Makefile.no-odbc clean
	rm -f $(SQLLOGICTEST_DIR)/src/sqllogictest
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

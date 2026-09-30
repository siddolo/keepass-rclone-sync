PYTHON ?= python3
CRON_SCHEDULE ?= */15 * * * *
UI_LANGUAGE ?=
# POSIX quoting keeps apostrophes, backticks, and $ in paths as data.
shell_quote = '$(subst ','"'"',$(1))'
MAKEFILE_DIR := $(shell dirname -- $(call shell_quote,$(MAKEFILE_LIST)))
LANGUAGE_OPTION = $(if $(UI_LANGUAGE),--language $(call shell_quote,$(UI_LANGUAGE)))

.DEFAULT_GOAL := help
.PHONY: help config auth sync cron-setup check status test i18n-update i18n-compile

help:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/manage.py) $(LANGUAGE_OPTION) --help

config:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/manage.py) $(LANGUAGE_OPTION) config

auth:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/manage.py) $(LANGUAGE_OPTION) auth

sync:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/sync.py) $(LANGUAGE_OPTION)

cron-setup:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/manage.py) $(LANGUAGE_OPTION) cron-setup --schedule $(call shell_quote,$(CRON_SCHEDULE))

check:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/manage.py) $(LANGUAGE_OPTION) check

status:
	@$(PYTHON) $(call shell_quote,$(MAKEFILE_DIR)/sync.py) $(LANGUAGE_OPTION) --status

test:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s $(call shell_quote,$(MAKEFILE_DIR)/tests) -v

i18n-update:
	xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=N_ --sort-output --no-location --package-name=keepass-rclone-sync --directory=$(call shell_quote,$(MAKEFILE_DIR)) --output=$(call shell_quote,$(MAKEFILE_DIR)/locales/keepass-sync.pot) sync.py manage.py i18n.py
	msgmerge --update --backup=none --no-fuzzy-matching $(call shell_quote,$(MAKEFILE_DIR)/locales/it/LC_MESSAGES/keepass-sync.po) $(call shell_quote,$(MAKEFILE_DIR)/locales/keepass-sync.pot)

i18n-compile:
	msgfmt --check --check-format --output-file=$(call shell_quote,$(MAKEFILE_DIR)/locales/it/LC_MESSAGES/keepass-sync.mo) $(call shell_quote,$(MAKEFILE_DIR)/locales/it/LC_MESSAGES/keepass-sync.po)

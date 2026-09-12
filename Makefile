.PHONY: run test check install

run:
	python3 localcode.py

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q localcode tests
	desktop-file-validate data/io.localcode.LocalCode.desktop

install:
	./scripts/install.sh

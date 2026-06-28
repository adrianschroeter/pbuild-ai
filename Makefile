.PHONY: test

test:
	PYTHONPATH=src python3 -m unittest discover -s src/pbuild_ai/tests -v

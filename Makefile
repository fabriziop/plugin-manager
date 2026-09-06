.PHONY: dist deb clean test

dist:
	./build.sh

deb:
	./build_deb.sh

test:
	pytest -q

clean:
	rm -rf dist build build-deb debpkg debian .pytest_cache .eggs *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.egg' \) -delete
	rm -f ./*.deb

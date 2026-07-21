.PHONY: dist deb clean test

dist:
	./build.sh

deb:
	./build_deb.sh

test:
	pytest -q

clean:
	rm -rf dist build-deb debpkg .pytest_cache *.egg-info

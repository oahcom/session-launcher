.PHONY: all test lint check doccheck clean

all: test lint check doccheck

test:
	python3 -m pytest tests/ -x -q --tb=short

lint:
	@failed=0; for f in $$(find . -name '*.py' -not -path './.git/*' -not -path './__pycache__/*' -not -path './venv/*'); do python3 -m py_compile "$$f" 2>&1 || failed=1; done; if [ "$$failed" = 1 ]; then exit 1; fi

check:
	PYTHONPATH=src python3 -c "from routing.roles import load_roles; print('import OK')"

# 文档行数与 xxx.py:NNN 引用随代码腐烂。检查器只有一份（session-pipeline 侧），
# 三个仓共用 --root 复用；单独 checkout 没有兄弟仓时明说 SKIP，不静默通过。
doccheck:
	@if [ -f ../session-pipeline/scripts/doccheck.py ]; then \
		python3 ../session-pipeline/scripts/doccheck.py --root . ; \
	else echo 'SKIP doccheck: 缺 ../session-pipeline/scripts/doccheck.py'; fi

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete

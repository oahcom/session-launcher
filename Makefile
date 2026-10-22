.PHONY: all test lint check doccheck clean

all: test lint check doccheck

test:
	python3 -m pytest tests/ -x -q --tb=short

lint:
	@failed=0; for f in $$(find . -name '*.py' -not -path './.git/*' -not -path './__pycache__/*' -not -path './venv/*'); do python3 -m py_compile "$$f" 2>&1 || failed=1; done; if [ "$$failed" = 1 ]; then exit 1; fi

check:
	PYTHONPATH=src python3 -c "from routing.roles import load_roles; print('import OK')"

# 文档行数与 xxx.py:NNN 引用随代码腐烂。检查器只有一份（session-pipeline 侧），
# 三个仓共用 --root 复用。缺检查器时**失败**而不是 SKIP 通过：
# 一道永远不跑的检查等于没有检查（本仓刚从"看起来有闸门"变成真闸门）。
doccheck:
	@if [ -f ../session-pipeline/scripts/doccheck.py ]; then \
		python3 ../session-pipeline/scripts/doccheck.py --root . ; \
	else echo 'FAIL doccheck: 缺 ../session-pipeline/scripts/doccheck.py（单独 clone 时把检查器一并带上）'; exit 1; fi

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete

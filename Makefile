.PHONY: install backend frontend test test-frontend

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 业务测试只依赖标准库，未安装 FastAPI 也能跑
test:
	cd backend && python3 -m unittest discover -s tests -v

test-frontend:
	cd frontend && npm run typecheck

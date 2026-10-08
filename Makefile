# vibe-agent 常用操作。
#
# 本地开发用 `make` 系列；容器相关用 `make docker-*`。
# 所有目标都假设你在这个目录下执行。

SHELL := /bin/bash
PYTHON ?= python3
VENV ?= .venv
VENV_PY := $(VENV)/bin/python
WORKSPACE ?= $(CURDIR)
IMAGE ?= vibe-agent:latest

.DEFAULT_GOAL := help
.PHONY: help venv install install-browser install-dev test test-full doctor run clean package \
        docker-build docker-build-slim docker-run docker-shell docker-doctor \
        docker-test compose-up compose-run compose-doctor

help:  ## 显示所有可用目标
	@echo ""
	@echo "  vibe-agent"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

.PHONY: install-dev

# ------------------------------------------------------------------ 本地

venv:  ## 创建虚拟环境
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install -q --upgrade pip

install: venv  ## 安装核心依赖
	$(VENV_PY) -m pip install -r requirements.txt

install-browser: install  ## 安装可选依赖（渲染 / 像素对比）
	$(VENV_PY) -m pip install -r requirements-browser.txt

install-dev: venv  ## 安装全部依赖，测试零跳过
	$(VENV_PY) -m pip install -r requirements-dev.txt

test:  ## 跑测试（用系统 python，缺可选依赖的用例会跳过）
	$(PYTHON) -m unittest discover -s tests

test-full: install-dev  ## 跑全部测试（含真渲染，零跳过）
	$(VENV_PY) -m unittest discover -s tests

doctor:  ## 环境自检
	$(PYTHON) run.py doctor -C "$(WORKSPACE)"

run:  ## 进入交互模式
	$(PYTHON) run.py -C "$(WORKSPACE)"

package:  ## 打一个可以拷到别的机器的包（不含密钥，约 190KB）
	./scripts/package.sh

package-from-worktree:  ## 打包当前工作区（含未提交改动）
	./scripts/package.sh --working-tree

clean:  ## 清理缓存与临时产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist

# ------------------------------------------------------------------ 容器

docker-build:  ## 构建全功能镜像（含 Node + Chromium，约 2GB）
	docker build -t $(IMAGE) .

docker-build-slim:  ## 构建精简镜像（约 300MB，不含浏览器）
	docker build -t $(IMAGE) --build-arg WITH_BROWSER=0 --build-arg WITH_NODE=0 .

docker-run:  ## 在容器里跑交互模式
	docker run --rm -it \
		-e VA_API_KEY -e VA_BASE_URL -e VA_MODEL \
		-e VA_VISION_MODEL -e VA_VISION_BASE_URL -e VA_VISION_API_KEY \
		-e VA_UID=$$(id -u) -e VA_GID=$$(id -g) \
		-e GIT_USER_NAME="$$(git config --global user.name)" \
		-e GIT_USER_EMAIL="$$(git config --global user.email)" \
		-v "$(WORKSPACE):/workspace" \
		-v vibe-agent-home:/home/va \
		$(IMAGE)

docker-shell:  ## 进容器开个 bash（排查用）
	docker run --rm -it --entrypoint /bin/bash \
		-e VA_UID=$$(id -u) -e VA_GID=$$(id -g) \
		-v "$(WORKSPACE):/workspace" \
		$(IMAGE)

docker-doctor:  ## 在容器里做环境自检
	docker run --rm -t \
		-e VA_UID=$$(id -u) -e VA_GID=$$(id -g) \
		-v "$(WORKSPACE):/workspace" \
		$(IMAGE) python3 -m va doctor -C /workspace

docker-test:  ## 在容器里跑测试
	docker run --rm -t --entrypoint python3 \
		$(IMAGE) -m unittest discover -s /app/tests

compose-run:  ## 用 compose 跑交互模式
	WORKSPACE="$(WORKSPACE)" docker compose run --rm va

compose-doctor:  ## 用 compose 做自检
	WORKSPACE="$(WORKSPACE)" docker compose run --rm va doctor

compose-up:  ## 构建镜像
	WORKSPACE="$(WORKSPACE)" docker compose build

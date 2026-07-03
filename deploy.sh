#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_NAME="tg-message-monitor"
ACTION="${1:-deploy}"

usage() {
  cat <<'EOF'
用法:
  ./deploy.sh          构建并后台启动（默认）
  ./deploy.sh deploy   同上
  ./deploy.sh build    仅构建镜像
  ./deploy.sh login    交互式登录（首次需输入 Telegram 验证码）
  ./deploy.sh logs     查看日志
  ./deploy.sh stop     停止服务
  ./deploy.sh restart  重新构建并重启
EOF
}

require_config() {
  if [[ ! -f config.yaml ]]; then
    echo "[ERROR] config.yaml 不存在，请先复制 config.example.yaml 并填写配置"
    exit 1
  fi
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[ERROR] 未找到 docker，请先安装 Docker"
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] 未找到 docker compose，请先安装 Docker Compose 插件"
    exit 1
  fi
}

prepare_dirs() {
  mkdir -p sessions
}

build_image() {
  echo "==> 构建镜像 ${SERVICE_NAME}"
  docker compose build
}

start_service() {
  echo "==> 启动服务 ${SERVICE_NAME}"
  docker compose up -d --remove-orphans
  docker compose ps
}

case "$ACTION" in
  deploy)
    require_docker
    require_config
    prepare_dirs
    build_image
    start_service
    echo
    echo "部署完成。"
    echo "查看日志: ./deploy.sh logs"
    echo "首次登录: ./deploy.sh login"
    ;;
  build)
    require_docker
    build_image
    ;;
  login)
    require_docker
    require_config
    prepare_dirs
    echo "==> 交互式登录（输入验证码后会话保存到 ./sessions）"
    docker compose run --rm -it "$SERVICE_NAME"
    ;;
  logs)
    require_docker
    docker compose logs -f "$SERVICE_NAME"
    ;;
  stop)
    require_docker
    docker compose down
    ;;
  restart)
    require_docker
    require_config
    prepare_dirs
    build_image
    docker compose down || true
    start_service
    ;;
  -h | --help | help)
    usage
    ;;
  *)
    echo "[ERROR] 未知命令: $ACTION"
    usage
    exit 1
    ;;
esac

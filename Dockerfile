FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    LIBRARY_ROOT=/library \
    DATA_DIR=/data

WORKDIR /app

# 国内网络环境：pip 与 apt 走清华镜像（海外部署可删掉这两段换行）
COPY requirements.txt .
RUN pip install --timeout 180 --retries 5 -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && printf 'deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-non-free-firmware\ndeb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-non-free-firmware\n' > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

COPY app/ app/
COPY static/ static/
COPY run.py .

# 数据卷：/library 书籍原文件；/data 数据库+封面+日志（换设备时整体拷走即可迁移）
VOLUME ["/library", "/data"]

EXPOSE 5000

CMD ["python", "app/main.py"]

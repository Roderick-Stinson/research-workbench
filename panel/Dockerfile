FROM python:3.10-slim
# 国内环境可通过构建参数换源（默认清华源、直连不走代理）；海外环境传 APT_MIRROR=deb.debian.org PIP_INDEX=https://pypi.org/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
# git：读代码快照（只读）
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends git tzdata && rm -rf /var/lib/apt/lists/*
COPY panel/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r /tmp/requirements.txt
WORKDIR /app
CMD ["python", "-m", "uvicorn", "panel.app:app", "--host", "0.0.0.0", "--port", "8765"]

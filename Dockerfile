# 使用轻量 Python 官方镜像
FROM python:3.11-slim-bookworm

WORKDIR /app

# 运行时环境变量优化
ENV MALLOC_ARENA_MAX=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装浏览器所需最小系统依赖集
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 ca-certificates \
    fonts-liberation libasound2 libpangocairo-1.0-0 libpango-1.0-0 libu2f-udev xvfb \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 camoufox 浏览器
RUN camoufox fetch

# 复制项目文件
COPY . .

EXPOSE 7860

CMD ["python", "main.py"]

FROM python:3.11.14-slim
RUN sed -i s@/deb.debian.org/@/mirrors.tuna.tsinghua.edu.cn/@g /etc/apt/sources.list.d/debian.sources

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV WEB_PORT 8000
WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN apt-get update && apt-get install -y gosu dos2unix build-essential python3-dev && apt-get clean \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && useradd app

COPY ptbrush /app
ADD docker-entrypoint.sh /docker-entrypoint.sh
RUN dos2unix /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

ENV PYTHONPATH /app

WORKDIR /app

VOLUME ["/app/data"]

# Expose web interface port
EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
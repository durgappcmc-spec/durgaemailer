FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.deploy.txt .
RUN pip install --no-cache-dir -r requirements.deploy.txt

COPY . .

ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV APP_REQUIRE_LOGIN=false
ENV CHROMA_DIR=/tmp/chroma
ENV RELAY_LIGHT_MEMORY=true
ENV AUTO_SYNC_GMAIL_DAYS=7
ENV AUTO_SYNC_MAX_PER=25
ENV AUTO_SYNC_INTERVAL_MINUTES=60
ENV STREAMLIT_SERVER_MAX_UPLOAD_SIZE=15
ENV STREAMLIT_SERVER_ENABLE_STATIC_SERVING=false
ENV MALLOC_ARENA_MAX=2

EXPOSE 8501

# Render sets $PORT; default to 8501 locally
CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-8501} --server.headless=true"]

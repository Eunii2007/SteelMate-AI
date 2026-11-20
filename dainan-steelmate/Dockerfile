FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DAINAN_QUOTE_DB=/var/lib/dainan-steel-ai/quotes.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app.py quote_store.py workbench.css ./
COPY core/ ./core/
COPY data/ ./data/
COPY .streamlit/config.toml ./.streamlit/config.toml
RUN mkdir -p /var/lib/dainan-steel-ai

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]

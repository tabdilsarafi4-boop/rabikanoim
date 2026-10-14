FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY panel.py bot.py login_worker.py ./
ENV DATA_DIR=/data PANEL_HOST=0.0.0.0 PANEL_PORT=8080 COOKIE_SECURE=1 BEHIND_PROXY=1
CMD ["python", "-u", "panel.py"]

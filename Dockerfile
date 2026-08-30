FROM python:3.12-slim

# Optional: only needed if you host somewhere that wants a container.
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user

ENV PORT=7860
EXPOSE 7860
# One worker so the in-memory embed-user cache is shared across requests;
# long timeout because an Omni AI job can take 15-20s+ to execute.
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:${PORT} app:app"]

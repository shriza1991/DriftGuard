FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator orchestrator
COPY mock_api mock_api
COPY evaluation evaluation
COPY contracts contracts
COPY reports reports
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]

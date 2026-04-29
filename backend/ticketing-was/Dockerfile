FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

# 엔트리포인트는 K8s Deployment에서 CMD override로 결정:
#   read-api:  uvicorn read_app:app --host 0.0.0.0 --port 5000
#   write-api: uvicorn write_app:app --host 0.0.0.0 --port 5001
EXPOSE 5000 5001
CMD ["uvicorn", "read_app:app", "--host", "0.0.0.0", "--port", "5000"]


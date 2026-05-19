FROM python:3.9-slim
WORKDIR /app
RUN pip install flask docker
COPY app.py .
CMD ["python3", "-u", "app.py"]

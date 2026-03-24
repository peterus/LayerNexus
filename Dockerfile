FROM python:3.14-slim

# Prevent bytecode and buffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy project files
COPY . .

# Collect static files (DJANGO_SECRET_KEY must be set for manage.py)
RUN DJANGO_SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

# Create directories for persistent data
RUN mkdir -p /app/media /app/data

# Entrypoint runs migrations before starting the application
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]

EXPOSE 8000

CMD ["gunicorn", "layernexus.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

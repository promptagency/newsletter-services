FROM python:3.11-slim

# Install curl for health checks
RUN apt-get update && \
    apt-get install -y curl && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY scraper.py .

# Expose port
EXPOSE 5001

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=scraper.py

# Run the application with a production WSGI server
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${SCRAPER_PORT:-5001} --workers 1 --threads 4 --timeout 60 --access-logfile - scraper:app"]
